"""
Textove vyhledavani v klinickem textu (dekurzy, anamnezy) - napric VICE databazemi.

POZOR na vykon: hleda se BEZ LIMITU - projde se cela tabulka (pripadne jen zaznamy
vybranych pacientu, pokud je predem znamy filtr z SQL) a vrati se VSECHNY shody, ne
jen prvnich N. U velmi velkych tabulek (statisice a vice zaznamu) proto muze textove
hledani trvat déle - to je vedomy kompromis: presnost odpovedi ma prednost pred rychlosti.

Jadrem je hledej_v_tabulce(), ktera misto pevneho vyrazu bere funkci over_text(text) -> shoda
nebo None (viz shoda_kterykoliv_vyraz a shoda_cislo_u_klicoveho_slova nize). Funkce
hledej_v_*_jedna (pevny vyraz, pouziva stara aplikace asistent.py) a hledej_v_*_pokrocile
(pouziva filtry.py) jsou jen tenke obaly kolem ni.
"""
import datetime
import re

from firebird.driver import DatabaseError

from db import pripravit_text, rozdel_na_davky

# zdroj -> (tabulka, sloupec s datem, sloupec s textem, razeni)
_TABULKY = {
    "dekurz": ("M_DEKURS", "DATUM", "DEKURS", " ORDER BY DATUM DESC"),
    "anamneza": ("M_ANAMNESA", "DATCAS", "TXTANAM", ""),
}


def precti_blob(hodnota):
    """Prevede hodnotu z databaze (muze byt str, bytes, nebo 'lazy' BlobReader) na cisty retezec."""
    if hodnota is None or isinstance(hodnota, str):
        return hodnota
    if not isinstance(hodnota, bytes):
        hodnota = hodnota.read()
        if isinstance(hodnota, str):
            return hodnota
    return hodnota.decode("cp1250", errors="replace")


def _nacti_pripravene_radky(cur, dotazy, on_progress):
    """
    Postupne provede vsechny dotazy (seznam trojic (sql, sql_pocet, params), jedna na davku
    pacientu) a pro kazdy nacteny zaznam vraci (idosb, datum, pripraveny_text). Prubezne hlasi
    postup pres on_progress(zpracovano, celkem), pokud je zadany - kvuli tomu se nejdriv
    spocita SELECT COUNT(*), coz je levne (necte se pri nem obsah textovych poli).

    ODOLNOST VUCI ZIVE DATABAZI: hledani BEZ LIMITU muze u velkych tabulek bezet i nekolik
    minut - mezitim muze personal ordinace zaznam bezne prepsat/smazat. Pak uz jeho puvodni
    BLOB neexistuje a ovladac pri jeho cteni vyhodi DatabaseError ("BLOB not found").
    Takovy zaznam jen PRESKOCIME (nepocita se jako shoda) a pokracujeme dal.
    """
    celkem = None
    if on_progress:
        celkem = 0
        for _, sql_pocet, params in dotazy:
            cur.execute(sql_pocet, params)
            celkem += cur.fetchone()[0]

    zpracovano = 0
    preskozeno = 0
    posledni_procento = -1

    for sql, _, params in dotazy:
        cur.execute(sql, params)
        while True:
            try:
                radek = cur.fetchone()
                if radek is None:
                    break
                idosb, datum, blob = radek
                # Velky BLOB se cte az tady (ne pri fetchone) - i tady muze prijit "BLOB not found".
                text = precti_blob(blob)
            except DatabaseError:
                text = None
                preskozeno += 1
            else:
                text = pripravit_text(text)
            zpracovano += 1
            if celkem:
                procento = 100 * zpracovano // celkem
                if procento != posledni_procento:
                    posledni_procento = procento
                    on_progress(zpracovano, celkem)
            if text:
                yield idosb, datum, text

    if celkem:
        on_progress(celkem, celkem)
    if preskozeno:
        print(f"[text_search] Preskoceno {preskozeno} z {zpracovano} zaznamu behem hledani "
              f"(zaznam byl mezitim v zive databazi zmenen/smazan - BLOB not found).")


def _sestav_dotazy(zdroj, idosb_seznam, datum_od, datum_do):
    """
    Pripravi seznam (sql, sql_pocet, params) - jeden dotaz na kazdou davku pacientu (nebo jeden
    pro celou tabulku, kdyz idosb_seznam je None). ID pacientu se vkladaji primo do SQL az po
    prevodu na int, data obdobi jako parametry "?".

    Konec obdobi se porovnava jako "< den po datum_do" - sloupec s datem muze obsahovat i cas
    (napr. M_ANAMNESA.DATCAS), a "<= datum_do" by pak vynechal zaznamy z posledniho dne obdobi
    zapsane po pulnoci.
    """
    tabulka, sloupec_data, sloupec_textu, razeni = _TABULKY[zdroj]
    podminky = f"{sloupec_textu} IS NOT NULL"
    params = []
    if datum_od is not None:
        podminky += f" AND {sloupec_data} >= ?"
        params.append(datum_od.isoformat())
    if datum_do is not None:
        podminky += f" AND {sloupec_data} < ?"
        params.append((datum_do + datetime.timedelta(days=1)).isoformat())

    davky = [None] if idosb_seznam is None else rozdel_na_davky(sorted(set(int(i) for i in idosb_seznam)))
    dotazy = []
    for davka in davky:
        podminky_davky = podminky
        if davka is not None:
            podminky_davky = f"IDOSB IN ({','.join(str(i) for i in davka)}) AND {podminky}"
        dotazy.append((
            f"SELECT IDOSB, {sloupec_data}, CAST({sloupec_textu} AS BLOB SUB_TYPE 0) "
            f"FROM {tabulka} WHERE {podminky_davky}{razeni}",
            f"SELECT COUNT(*) FROM {tabulka} WHERE {podminky_davky}",
            params,
        ))
    return dotazy


def hledej_v_tabulce(con, zdroj, over_text, idosb_seznam=None, on_progress=None, datum_od=None, datum_do=None):
    """
    Prohleda texty v tabulce dane zdrojem ("dekurz" = M_DEKURS.DEKURS, "anamneza" =
    M_ANAMNESA.TXTANAM) na JIZ OTEVRENEM pripojeni con - pripojeni otevira a zavira volajici.

    over_text: funkce over_text(text) -> shoda (dict) nebo None - viz shoda_kterykoliv_vyraz /
    shoda_cislo_u_klicoveho_slova nize.
    idosb_seznam: None = cela tabulka, jinak jen zaznamy techto pacientu (prazdny seznam = nic).
    datum_od, datum_do: volitelne datetime.date - jen zaznamy s datem v tomto rozmezi.
    on_progress: volitelna funkce on_progress(zpracovano, celkem) pro ukazatel postupu.

    Vraci seznam [{"idosb", "datum", "ukazka", ...dalsi klice ze shody}, ...].
    """
    dotazy = _sestav_dotazy(zdroj, idosb_seznam, datum_od, datum_do)
    vysledky = []
    for idosb, datum, text in _nacti_pripravene_radky(con.cursor(), dotazy, on_progress):
        shoda = over_text(text)
        if shoda:
            vysledky.append({"idosb": idosb, "datum": datum, **shoda})
    return vysledky


# --- Funkce over_text pro ruzne typy hledani ------------------------------------------------

def _sestav_vzor_hledani(vyraz):
    """
    Pripravi (zobrazovany_vyraz, regex) pro hledani JEDNOHO vyrazu v textu (necitlive na
    velikost pismen).

    Je-li vyraz obaleny v kulatych zavorkach, napr. "(EF)", hleda se jako CELE SAMOSTATNE SLOVO
    (hranice slova pred i za nim) - "EF" tak NENAJDE "efekt" ani "telefonat". Bez zavorek se
    hleda jako PODRETEZEC - "EF" tak najde i "efekt".
    """
    vyraz = vyraz.strip()
    if vyraz.startswith("(") and vyraz.endswith(")") and len(vyraz) > 2:
        jadro = vyraz[1:-1].strip()
        if jadro:
            return jadro, re.compile(r"(?<!\w)" + re.escape(jadro) + r"(?!\w)", re.IGNORECASE)
    return vyraz, re.compile(re.escape(vyraz), re.IGNORECASE)


def shoda_kterykoliv_vyraz(vyrazy):
    """
    Vraci funkci over_text(text) -> shoda (dict) nebo None, ktera oznaci shodu, pokud text
    obsahuje ALESPON JEDEN z danych vyrazu (logicke NEBO), bez ohledu na velikost pismen.
    Priklad: shoda_kterykoliv_vyraz(["NYHA II", "NYHA III"]).
    """
    vzory = [_sestav_vzor_hledani(vyraz) for vyraz in vyrazy]

    def over_text(text):
        for zobrazovany_vyraz, vzor in vzory:
            shoda = vzor.search(text)
            if shoda:
                zacatek = max(0, shoda.start() - 60)
                return {"ukazka": text[zacatek:shoda.end() + 60], "nalezeny_vyraz": zobrazovany_vyraz}
        return None
    return over_text


_OPERATORY_CISLA = {
    "<": lambda x, h: x < h,
    "<=": lambda x, h: x <= h,
    ">": lambda x, h: x > h,
    ">=": lambda x, h: x >= h,
    "=": lambda x, h: x == h,
}

# Za klicovym slovem povolime az 20 nepocetnich znaku (mezera, dvojtecka, "je", jednotka...)
# nez ocekavame cislo - napr. "NT-proBNP: 180 pg/ml" nebo "NT-proBNP je 95".
_VZOR_CISLA_ZA_SLOVEM = re.compile(r"\D{0,20}?(-?\d+[.,]?\d*)")


def shoda_cislo_u_klicoveho_slova(klicove_slovo, operator, hranice):
    """
    Vraci funkci over_text(text) -> shoda (dict) nebo None, ktera hleda VSECHNY vyskyty
    klicoveho slova (napr. "NT-proBNP") a u kazdeho zkusi najit cislo nedaleko za nim - pokud
    ho najde, porovna ho s hranici pres operator ("<", "<=", ">", ">=", "="). Vraci i nalezene
    cislo (pole "nalezene_cislo"), aby sel vysledek rucne zkontrolovat.

    POZOR: jde o odhad z VOLNEHO textu - u neobvykleho zapisu muze shoda chybet.
    Klicove slovo v zavorkach, napr. "(EF)", se hleda jako cele samostatne slovo.
    """
    if operator not in _OPERATORY_CISLA:
        raise ValueError(f"Neznamy operator: {operator} (povolene: {sorted(_OPERATORY_CISLA)})")
    porovnej = _OPERATORY_CISLA[operator]
    _, vzor_slova = _sestav_vzor_hledani(klicove_slovo)

    def over_text(text):
        for shoda_slova in vzor_slova.finditer(text):
            konec_slova = shoda_slova.end()
            shoda_cisla = _VZOR_CISLA_ZA_SLOVEM.match(text, konec_slova, konec_slova + 30)
            if not shoda_cisla:
                continue
            try:
                cislo = float(shoda_cisla.group(1).replace(",", "."))
            except ValueError:
                continue
            if porovnej(cislo, hranice):
                zacatek = max(0, shoda_slova.start() - 40)
                return {"ukazka": text[zacatek:konec_slova + 40], "nalezene_cislo": cislo}
        return None
    return over_text


# --- Obaly pro volajici ---------------------------------------------------------------------

def hledej_v_dekurzech_pokrocile(con, over_text, idosb_seznam=None, on_progress=None, datum_od=None, datum_do=None):
    return hledej_v_tabulce(con, "dekurz", over_text, idosb_seznam, on_progress, datum_od, datum_do)


def hledej_v_anamnezach_pokrocile(con, over_text, idosb_seznam=None, on_progress=None, datum_od=None, datum_do=None):
    return hledej_v_tabulce(con, "anamneza", over_text, idosb_seznam, on_progress, datum_od, datum_do)


def hledej_v_dekurzech_jedna(con, vyraz, idosb_seznam=None, on_progress=None):
    """Hledani jednoho vyrazu (podretezec, bez ohledu na velikost pismen) - pro asistent.py."""
    return hledej_v_tabulce(con, "dekurz", shoda_kterykoliv_vyraz([vyraz]), idosb_seznam, on_progress)


def hledej_v_anamnezach_jedna(con, vyraz, idosb_seznam=None, on_progress=None):
    """Hledani jednoho vyrazu (podretezec, bez ohledu na velikost pismen) - pro asistent.py."""
    return hledej_v_tabulce(con, "anamneza", shoda_kterykoliv_vyraz([vyraz]), idosb_seznam, on_progress)
