"""
Textove vyhledavani v klinickem textu (dekurzy, anamnezy) - napric VICE databazemi.

POZOR na vykon: hleda se BEZ LIMITU - projde se cela tabulka (pripadne jen zaznamy
vybranych pacientu, pokud je predem znamy filtr z SQL) a vrati se VSECHNY shody, ne
jen prvnich N. U velmi velkych tabulek (statisice a vice zaznamu) proto muze textove
hledani trvat déle - to je vedomy kompromis: presnost odpovedi ma prednost pred rychlosti.

Krome jednoduche funkce "obsahuje tento podretezec" (hledej_v_dekurzech_jedna / _anamnezach_jedna,
pouziva je asistent.py pro AI-rizene dotazy) jsou tu i "pokrocile" varianty (hledej_..._pokrocile),
ktere misto pevneho vyrazu berou libovolnou funkci over_text() - viz shoda_kterykoliv_vyraz a
shoda_cislo_u_klicoveho_slova nize. Pouziva je filtry.py pro rucne skladane, vicekrokove hledani
(napr. "NYHA II NEBO NYHA III" v jednom kroku, "NT-proBNP pod hranici 300" v druhem).
"""
import re

from firebird.driver import DatabaseError

from db import pripravit_text, rozdel_na_davky


def precti_blob(hodnota):
    """Prevede hodnotu z databaze (muze byt str, bytes, nebo 'lazy' BlobReader) na cisty retezec."""
    if hodnota is None:
        return None
    if isinstance(hodnota, str):
        return hodnota
    if isinstance(hodnota, bytes):
        return hodnota.decode("cp1250", errors="replace")
    obsah = hodnota.read()
    if isinstance(obsah, bytes):
        return obsah.decode("cp1250", errors="replace")
    return obsah


def _nacti_pripravene_radky(cur, sestav_sql, sestav_pocet_sql, davky, on_progress):
    """
    Spolecne jadro pro _hledej a _hledej_s_vyhodnocenim - postupne provede vsechny davky a
    pro kazdy nalezeny zaznam vraci (idosb, datum, pripraveny_text). Prubezne hlasi postup
    pres on_progress(zpracovano, celkem), pokud je zadany (viz _hledej nize pro vysvetleni,
    proc se kvuli tomu nejdriv spocita SELECT COUNT(*)).

    ODOLNOST VUCI ZIVE DATABAZI: jde o hledani BEZ LIMITU, ktere muze u velkych tabulek bezet
    i nekolik minut - mezitim muze personal ordinace zaznam bezne prepsat/smazat. Pak uz jeho
    puvodni BLOB neexistuje a ovladac pri jeho cteni vyhodi DatabaseError ("BLOB not found").
    Takovy jeden zaznam proto jen PRESKOCIME (nepocita se jako shoda) a pokracujeme dal -
    nejde o chybu v datech ani v kodu, jen o soubeh s bezicim provozem, a rozhodne by kvuli
    nemu nemelo prijit vniveč cele (casto mnohaminutove) hledani.
    """
    davky = list(davky)
    celkem_zaznamu = None
    if on_progress:
        celkem_zaznamu = 0
        for davka in davky:
            cur.execute(sestav_pocet_sql(davka))
            celkem_zaznamu += cur.fetchone()[0]

    zpracovano = 0
    preskozeno = 0
    posledni_ohlasene_procento = -1

    def ohlas_pokrok():
        nonlocal posledni_ohlasene_procento
        if on_progress and celkem_zaznamu:
            aktualni_procento = int(100 * zpracovano / celkem_zaznamu)
            if aktualni_procento != posledni_ohlasene_procento:
                posledni_ohlasene_procento = aktualni_procento
                on_progress(zpracovano, celkem_zaznamu)

    for davka in davky:
        cur.execute(sestav_sql(davka))
        while True:
            try:
                radek = cur.fetchone()
            except DatabaseError:
                zpracovano += 1
                preskozeno += 1
                ohlas_pokrok()
                continue
            if radek is None:
                break
            idosb, datum, blob_text = radek
            zpracovano += 1
            ohlas_pokrok()
            text = pripravit_text(precti_blob(blob_text))
            if text:
                yield idosb, datum, text

    if on_progress and celkem_zaznamu:
        on_progress(celkem_zaznamu, celkem_zaznamu)
    if preskozeno:
        print(f"[text_search] Preskoceno {preskozeno} z {zpracovano} zaznamu behem hledani "
              f"(zaznam byl mezitim v zive databazi zmenen/smazan - BLOB not found).")


def _hledej(con, sestav_sql, sestav_pocet_sql, vyraz, idosb_seznam, on_progress=None):
    """
    Hleda na JIZ OTEVRENEM pripojeni (con) - pripojeni si otevira a zavira volajici (asistent.py),
    aby se pro jednu otazku k jedne databazi nepripojovalo opakovane.

    on_progress: volitelna funkce on_progress(zpracovano, celkem), kterou zavolame prubezne behem
    prochazeni (pri kazde zmene celeho procenta) - aby volajici (UI) mohl ukazat postup. Kvuli tomu
    se pred samotnym ctenim textu nejdriv rychle spocita celkovy pocet zaznamu (SELECT COUNT(*)) -
    to je levna operace (necte se pri ni obsah textovych poli), takze navic vykonu je zanedbatelne
    ve srovnani se samotnym cistenim a prohledavanim textu.
    """
    cur = con.cursor()
    vysledky = []
    vyraz_lower = vyraz.lower()

    davky = rozdel_na_davky(idosb_seznam, 1000) if idosb_seznam else [None]

    for idosb, datum, text in _nacti_pripravene_radky(cur, sestav_sql, sestav_pocet_sql, davky, on_progress):
        if vyraz_lower in text.lower():
            pozice = text.lower().find(vyraz_lower)
            zacatek = max(0, pozice - 60)
            konec = min(len(text), pozice + len(vyraz) + 60)
            ukazka = text[zacatek:konec]
            vysledky.append({"idosb": idosb, "datum": datum, "ukazka": ukazka})
    return vysledky


def hledej_v_dekurzech_jedna(con, vyraz, idosb_seznam=None, on_progress=None):
    """Prohleda VSECHNY zaznamy v M_DEKURS.DEKURS v JEDNE konkretni databazi (na jiz otevrenem pripojeni con)."""
    def sestav_sql(davka):
        if davka:
            idcka = ",".join(str(int(i)) for i in davka)
            return f"SELECT IDOSB, DATUM, CAST(DEKURS AS BLOB SUB_TYPE 0) FROM M_DEKURS WHERE IDOSB IN ({idcka}) AND DEKURS IS NOT NULL ORDER BY DATUM DESC"
        return "SELECT IDOSB, DATUM, CAST(DEKURS AS BLOB SUB_TYPE 0) FROM M_DEKURS WHERE DEKURS IS NOT NULL ORDER BY DATUM DESC"

    def sestav_pocet_sql(davka):
        if davka:
            idcka = ",".join(str(int(i)) for i in davka)
            return f"SELECT COUNT(*) FROM M_DEKURS WHERE IDOSB IN ({idcka}) AND DEKURS IS NOT NULL"
        return "SELECT COUNT(*) FROM M_DEKURS WHERE DEKURS IS NOT NULL"

    return _hledej(con, sestav_sql, sestav_pocet_sql, vyraz, idosb_seznam, on_progress=on_progress)


# --- Pokrocile hledani (funkce over_text misto pevneho vyrazu) -------------------------------

def _sestav_vzor_hledani(vyraz):
    """
    Pripravi (zobrazovany_vyraz, regex) pro hledani JEDNOHO vyrazu v textu (necitlive na
    velikost pismen).

    Je-li vyraz obaleny v kulatych zavorkach, napr. "(EF)", hleda se jako CELE SAMOSTATNE SLOVO
    (hranice slova pred i za nim) - "EF" tak NENAJDE "efekt" ani "telefonat". Bez zavorek se
    hleda jako PODRETEZEC (puvodni chovani) - "EF" tak najde i "efekt".
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
    obsahuje ALESPON JEDEN z dane seznamu vyrazu (logicke NEBO), bez ohledu na velikost pismen.
    Priklad pouziti: shoda_kterykoliv_vyraz(["NYHA II", "NYHA III"]).

    Vyraz obaleny v zavorkach, napr. "(EF)", se hleda jako cele samostatne slovo - viz
    _sestav_vzor_hledani vyse.
    """
    vzory = [_sestav_vzor_hledani(vyraz) for vyraz in vyrazy]

    def over_text(text):
        for zobrazovany_vyraz, vzor in vzory:
            shoda = vzor.search(text)
            if shoda:
                zacatek = max(0, shoda.start() - 60)
                konec = min(len(text), shoda.end() + 60)
                return {"ukazka": text[zacatek:konec], "nalezeny_vyraz": zobrazovany_vyraz}
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
    klicoveho slova (napr. "NT-proBNP") v textu a u kazdeho zkusi najit cislo nedaleko za nim
    (viz _VZOR_CISLA_ZA_SLOVEM) - pokud cislo najde, porovna ho s hranici pres operator
    ("<", "<=", ">", ">=", "="). Vraci i to nalezene cislo (pole "nalezene_cislo"), aby sel
    vysledek rucne zkontrolovat.

    POZOR: jde o odhad z VOLNEHO textu, ne o presna strukturovana data - u neobvykleho zapisu
    (jina jednotka pred cislem, format desetinne carky, prekleply nazev) muze shoda chybet.
    Doporucujeme u dulezitych vysledku zkontrolovat nekolik ukazek rucne.

    Klicove slovo obalene v zavorkach, napr. "(EF)", se hleda jako cele samostatne slovo - viz
    _sestav_vzor_hledani vyse (jinak by "EF" naslo i cislo za slovem "efekt" apod.).
    """
    if operator not in _OPERATORY_CISLA:
        raise ValueError(f"Neznamy operator: {operator} (povolene: {sorted(_OPERATORY_CISLA)})")
    porovnej = _OPERATORY_CISLA[operator]
    _, vzor_slova = _sestav_vzor_hledani(klicove_slovo)

    def over_text(text):
        for shoda_slova in vzor_slova.finditer(text):
            konec_slova = shoda_slova.end()
            shoda_cisla = _VZOR_CISLA_ZA_SLOVEM.match(text[konec_slova:konec_slova + 30])
            if shoda_cisla:
                try:
                    cislo = float(shoda_cisla.group(1).replace(",", "."))
                except ValueError:
                    cislo = None
                if cislo is not None and porovnej(cislo, hranice):
                    zacatek = max(0, shoda_slova.start() - 40)
                    konec = min(len(text), konec_slova + 40)
                    return {"ukazka": text[zacatek:konec], "nalezene_cislo": cislo}
        return None
    return over_text


def _hledej_s_vyhodnocenim(con, sestav_sql, sestav_pocet_sql, over_text, idosb_seznam, on_progress=None):
    """Stejne jako _hledej, ale misto pevneho podretezce pouziva funkci over_text(text) ->
    shoda (dict) nebo None - viz shoda_kterykoliv_vyraz / shoda_cislo_u_klicoveho_slova vyse."""
    cur = con.cursor()
    vysledky = []
    davky = rozdel_na_davky(idosb_seznam, 1000) if idosb_seznam else [None]

    for idosb, datum, text in _nacti_pripravene_radky(cur, sestav_sql, sestav_pocet_sql, davky, on_progress):
        shoda = over_text(text)
        if shoda:
            vysledky.append({"idosb": idosb, "datum": datum, **shoda})
    return vysledky


def _datova_podminka(sloupec, datum_od, datum_do):
    """
    Sestavi SQL fragment " AND sloupec >= 'YYYY-MM-DD' AND sloupec <= 'YYYY-MM-DD'" pro volitelne
    omezeni datem (nebo "" kdyz ani jedno neni zadano). datum_od/datum_do MUSI byt datetime.date
    (nebo None) - .isoformat() vzdy vyrobi jen cislice a pomlcky, takze je to bezpecne vlozit
    primo do SQL textu (stejny pristup jako u IDOSB IN (...) vyse, kde se cisla taky vkladaji
    primo po overeni pres int()).
    """
    fragment = ""
    if datum_od is not None:
        fragment += f" AND {sloupec} >= '{datum_od.isoformat()}'"
    if datum_do is not None:
        fragment += f" AND {sloupec} <= '{datum_do.isoformat()}'"
    return fragment


def hledej_v_dekurzech_pokrocile(con, over_text, idosb_seznam=None, on_progress=None, datum_od=None, datum_do=None):
    """
    Prohleda VSECHNY zaznamy v M_DEKURS.DEKURS pomoci funkce over_text (viz vyse).
    datum_od, datum_do: volitelne datetime.date - omezi hledani jen na zaznamy s DATUM v tomto rozmezi.
    """
    datova_podminka = _datova_podminka("DATUM", datum_od, datum_do)

    def sestav_sql(davka):
        if davka:
            idcka = ",".join(str(int(i)) for i in davka)
            return (f"SELECT IDOSB, DATUM, CAST(DEKURS AS BLOB SUB_TYPE 0) FROM M_DEKURS WHERE IDOSB IN ({idcka}) "
                    f"AND DEKURS IS NOT NULL{datova_podminka} ORDER BY DATUM DESC")
        return f"SELECT IDOSB, DATUM, CAST(DEKURS AS BLOB SUB_TYPE 0) FROM M_DEKURS WHERE DEKURS IS NOT NULL{datova_podminka} ORDER BY DATUM DESC"

    def sestav_pocet_sql(davka):
        if davka:
            idcka = ",".join(str(int(i)) for i in davka)
            return f"SELECT COUNT(*) FROM M_DEKURS WHERE IDOSB IN ({idcka}) AND DEKURS IS NOT NULL{datova_podminka}"
        return f"SELECT COUNT(*) FROM M_DEKURS WHERE DEKURS IS NOT NULL{datova_podminka}"

    return _hledej_s_vyhodnocenim(con, sestav_sql, sestav_pocet_sql, over_text, idosb_seznam, on_progress=on_progress)


def hledej_v_anamnezach_pokrocile(con, over_text, idosb_seznam=None, on_progress=None, datum_od=None, datum_do=None):
    """
    Prohleda VSECHNY zaznamy v M_ANAMNESA.TXTANAM pomoci funkce over_text (viz vyse).
    datum_od, datum_do: volitelne datetime.date - omezi hledani jen na zaznamy s DATCAS v tomto rozmezi.
    """
    datova_podminka = _datova_podminka("DATCAS", datum_od, datum_do)

    def sestav_sql(davka):
        if davka:
            idcka = ",".join(str(int(i)) for i in davka)
            return (f"SELECT IDOSB, DATCAS, CAST(TXTANAM AS BLOB SUB_TYPE 0) FROM M_ANAMNESA WHERE IDOSB IN ({idcka}) "
                    f"AND TXTANAM IS NOT NULL{datova_podminka}")
        return f"SELECT IDOSB, DATCAS, CAST(TXTANAM AS BLOB SUB_TYPE 0) FROM M_ANAMNESA WHERE TXTANAM IS NOT NULL{datova_podminka}"

    def sestav_pocet_sql(davka):
        if davka:
            idcka = ",".join(str(int(i)) for i in davka)
            return f"SELECT COUNT(*) FROM M_ANAMNESA WHERE IDOSB IN ({idcka}) AND TXTANAM IS NOT NULL{datova_podminka}"
        return f"SELECT COUNT(*) FROM M_ANAMNESA WHERE TXTANAM IS NOT NULL{datova_podminka}"

    return _hledej_s_vyhodnocenim(con, sestav_sql, sestav_pocet_sql, over_text, idosb_seznam, on_progress=on_progress)


def hledej_v_anamnezach_jedna(con, vyraz, idosb_seznam=None, on_progress=None):
    """Prohleda VSECHNY zaznamy v M_ANAMNESA.TXTANAM v JEDNE konkretni databazi (na jiz otevrenem pripojeni con)."""
    def sestav_sql(davka):
        if davka:
            idcka = ",".join(str(int(i)) for i in davka)
            return f"SELECT IDOSB, DATCAS, CAST(TXTANAM AS BLOB SUB_TYPE 0) FROM M_ANAMNESA WHERE IDOSB IN ({idcka}) AND TXTANAM IS NOT NULL"
        return "SELECT IDOSB, DATCAS, CAST(TXTANAM AS BLOB SUB_TYPE 0) FROM M_ANAMNESA WHERE TXTANAM IS NOT NULL"

    def sestav_pocet_sql(davka):
        if davka:
            idcka = ",".join(str(int(i)) for i in davka)
            return f"SELECT COUNT(*) FROM M_ANAMNESA WHERE IDOSB IN ({idcka}) AND TXTANAM IS NOT NULL"
        return "SELECT COUNT(*) FROM M_ANAMNESA WHERE TXTANAM IS NOT NULL"

    return _hledej(con, sestav_sql, sestav_pocet_sql, vyraz, idosb_seznam, on_progress=on_progress)
