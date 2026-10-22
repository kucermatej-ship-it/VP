"""
Deterministicke (BEZ AI) filtrovani pacientu podle strukturovanych kriterii - vek, pohlavi,
ordinace, telesne hodnoty, diagnoza, medikace, vykony, pojistovna, statni prislusnost,
identifikace osoby.

SQL tu nepise AI - skladame ho rucne v Pythonu z pevne dane sady filtru, takze tu neni prostor
pro halucinace. Hodnoty od uzivatele se do SQL vkladaji VZDY jako parametry ("?"), nikdy primym
vlozenim do textu retezce - to je standardni ochrana proti SQL injection.

Vysledny seznam pacientu lze pak dale zuzit textovym hledanim v dekurzech/anamnezach
(hledej_pokrocile_v_textu_mezi_pacienty nize).
"""
import datetime

from db import get_connection, seznam_databazi
import dotaz
import text_search


# Pojistovny - stejny seznam kodu jako v asistent.py, pro vyber v rozhrani.
POJISTOVNY = [
    ("", "(nerozhoduje)"),
    ("111", "111 - Vseobecna zdravotni pojistovna CR (VZP)"),
    ("201", "201 - Vojenska zdravotni pojistovna CR (VoZP)"),
    ("205", "205 - Ceska prumyslova zdravotni pojistovna (CPZP)"),
    ("207", "207 - Oborova zdravotni pojistovna zamestnancu bank, pojistoven a stavebnictvi (OZP)"),
    ("209", "209 - Zamestnanecka pojistovna SKODA (ZPS)"),
    ("211", "211 - Zdravotni pojistovna ministerstva vnitra CR (ZPMV)"),
    ("213", "213 - RBP, zdravotni pojistovna (drive Revirni bratrska pokladna)"),
]

# Telesne hodnoty z M_PRISTAV: (sloupec, klic "od", klic "do")
_TELESNE_HODNOTY = [
    ("BMI", "bmi_od", "bmi_do"),
    ("VAHA", "vaha_od", "vaha_do"),
    ("VYSKA", "vyska_od", "vyska_do"),
    ("TLAKSYS", "tlaksys_od", "tlaksys_do"),
    ("TLAKDIA", "tlakdia_od", "tlakdia_do"),
    ("PULS", "puls_od", "puls_do"),
]


def _rozdel_hodnoty(text):
    """Rozdeli textovy vstup oddeleny carkami na seznam ocistenych, neprazdnych hodnot -
    pro filtry, kde uzivatel muze zadat vic hodnot najednou (napr. diagnoza: "J069, I10")."""
    return [h.strip() for h in (text or "").split(",") if h.strip()]


def _zastupne(hodnoty):
    """"?, ?, ?" pro IN (...) se stejnym poctem hodnot."""
    return ", ".join("?" for _ in hodnoty)


def sestav_sql(filtry):
    """
    filtry: dict - jen klice s vyplnenou hodnotou se do dotazu promitnou (jednotliva kriteria
    se kombinuji pres AND). Podporovane klice:
        vek_od, vek_do (int)
        pohlavi ("M" nebo "F")
        ordinace (seznam str - presne nazvy ordinaci, viz nacti_ordinace())
        bmi_od, bmi_do / vaha_od, vaha_do / vyska_od, vyska_do (float)
        tlaksys_od, tlaksys_do / tlakdia_od, tlakdia_do / puls_od, puls_do (int)
        datum_od, datum_do (datetime.date) - jedno spolecne obdobi pro VSECHNY filtry s datem:
              - telesne hodnoty: mereni (M_PRISTAV.DATUM) v tomto obdobi
              - medikace: predepsano (M_MEDIREC.DATUM) v tomto obdobi
              - vykony: proveden (P_DOKLADPOL.DATUM) v tomto obdobi
              - diagnoza: VYJIMKA - trvala diagnoza plati "od DATOD dale", posuzuje se jen,
                jestli byla zapsana nejpozdeji do konce obdobi (datum_od se NEPOUZIVA)
        diagnoza (str - hodnoty oddelene carkou, hleda se v KOD i NAZEV, mezi hodnotami NEBO)
        medikace (str - hodnoty oddelene carkou, cast nazvu leku, mezi hodnotami NEBO)
        vykony (seznam str - presne kody vykonu, viz nacti_vykony())
        poj (str - 3-mistny kod pojistovny)
        statni_prislusnost (str - cast nazvu)
        rodne_cislo (str - s lomitkem i bez)
        prijmeni, jmeno, adresa (str - cast textu)

    Vraci (sql, params) - sql pouziva "?" jako zastupny znak, params jsou hodnoty ve stejnem poradi.
    """
    podminky = []
    params = []

    def pridej(podminka, *hodnoty):
        podminky.append(podminka)
        params.extend(hodnoty)

    if filtry.get("vek_od") is not None or filtry.get("vek_do") is not None:
        pridej("EXTRACT(YEAR FROM CURRENT_DATE) - EXTRACT(YEAR FROM DATNAR) BETWEEN ? AND ?",
               filtry.get("vek_od", 0) or 0, filtry.get("vek_do") if filtry.get("vek_do") is not None else 130)

    if filtry.get("pohlavi"):
        pridej("POHLAVI = ?", filtry["pohlavi"])

    if filtry.get("ordinace"):
        pridej(f"IDOSBPRA IN (SELECT ID FROM G_PRA WHERE NAZEV IN ({_zastupne(filtry['ordinace'])}))",
               *filtry["ordinace"])

    # Spolecne obdobi pro vsechny filtry s datem. Konec obdobi se porovnava jako "< den po datum_do",
    # aby se zapocital cely posledni den i u sloupcu, ktere krome data obsahuji i cas.
    datum_od = filtry.get("datum_od")
    datum_do = filtry.get("datum_do")
    do_exkluzivne = (datum_do + datetime.timedelta(days=1)).isoformat() if datum_do else None
    obdobi_sql, obdobi_params = "", []
    if datum_od is not None:
        obdobi_sql += " AND DATUM >= ?"
        obdobi_params.append(datum_od.isoformat())
    if datum_do is not None:
        obdobi_sql += " AND DATUM < ?"
        obdobi_params.append(do_exkluzivne)

    for sloupec, klic_od, klic_do in _TELESNE_HODNOTY:
        od, do = filtry.get(klic_od), filtry.get(klic_do)
        if od is not None or do is not None:
            pridej(f"ID IN (SELECT IDOSB FROM M_PRISTAV WHERE {sloupec} BETWEEN ? AND ?{obdobi_sql})",
                   od if od is not None else 0, do if do is not None else 999999, *obdobi_params)

    diagnozy = _rozdel_hodnoty(filtry.get("diagnoza"))
    if diagnozy:
        dilci = " OR ".join("UPPER(KOD) LIKE UPPER(?) OR UPPER(NAZEV) LIKE UPPER(?)" for _ in diagnozy)
        hodnoty = [f"%{h}%" for h in diagnozy for _ in range(2)]
        if datum_do is not None:
            # Trvala diagnoza "od DATOD dale" - staci, aby byla zapsana nejpozdeji do konce obdobi.
            pridej(f"ID IN (SELECT IDOSB FROM M_STALADGN WHERE ({dilci}) AND DATOD < ?)", *hodnoty, do_exkluzivne)
        else:
            pridej(f"ID IN (SELECT IDOSB FROM M_STALADGN WHERE {dilci})", *hodnoty)

    leky = _rozdel_hodnoty(filtry.get("medikace"))
    if leky:
        dilci = " OR ".join("UPPER(NAZEV) LIKE UPPER(?)" for _ in leky)
        pridej(f"ID IN (SELECT IDOSB FROM M_MEDIREC WHERE ({dilci}){obdobi_sql})",
               *(f"%{h}%" for h in leky), *obdobi_params)

    if filtry.get("vykony"):
        pridej(f"ID IN (SELECT IDOSB FROM P_DOKLADPOL WHERE KODVYK IN ({_zastupne(filtry['vykony'])}){obdobi_sql})",
               *filtry["vykony"], *obdobi_params)

    if filtry.get("poj"):
        pridej("POJ = ?", filtry["poj"])

    if filtry.get("statni_prislusnost"):
        pridej("STATPRIS IN (SELECT KOD FROM C_STATPRIS WHERE UPPER(NAZEV) LIKE UPPER(?))",
               f"%{filtry['statni_prislusnost']}%")

    if filtry.get("rodne_cislo"):
        pridej("REPLACE(RODCIS, '/', '') = ?", filtry["rodne_cislo"].replace("/", "").replace(" ", "").strip())

    for sloupec, klic in (("PRIJMENI", "prijmeni"), ("JMENO", "jmeno"), ("ADRESA", "adresa")):
        if filtry.get(klic):
            pridej(f"UPPER({sloupec}) LIKE UPPER(?)", f"%{filtry[klic]}%")

    # ID je v S_OSOBA jednoznacne, DISTINCT (a s nim zbytecne razeni) proto neni potreba.
    sql = "SELECT ID FROM S_OSOBA"
    if podminky:
        sql += " WHERE " + " AND ".join(podminky)
    return sql, params


def _spust_ve_vsech_databazich(sql, params=None, on_progress=None):
    """
    Overi SQL (schema_pravidla.py - pojistka i pro nase rucne sestavene SQL) a spusti ho
    postupne ve vsech databazich ve slozce data/. Vraci dict {nazev_databaze: radky}.
    """
    dotaz.over_sql(sql)
    databaze = seznam_databazi()
    vysledek = {}
    for i, o in enumerate(databaze):
        if on_progress:
            on_progress(i / len(databaze), f"Filtruji: databaze {o['nazev']} ({i + 1}/{len(databaze)})")
        con = get_connection(o["cesta"])
        try:
            _, vysledek[o["nazev"]] = dotaz.spust_sql_s_pripojenim(sql, con, params=params)
        finally:
            con.close()
    if on_progress:
        on_progress(1.0, "Filtrovani dokonceno.")
    return vysledek


def nacti_ordinace():
    """
    Vrati serazeny seznam vsech odlisnych nazvu ordinaci (G_PRA.NAZEV) napric VSEMI databazemi -
    pro vyber v rozhrani. Prochazi vsechny databaze, filtr_ui.py vysledek uklada do cache.
    """
    vysledek = _spust_ve_vsech_databazich("SELECT DISTINCT NAZEV FROM G_PRA WHERE NAZEV IS NOT NULL")
    return sorted({nazev.strip() for radky in vysledek.values() for (nazev,) in radky if nazev and nazev.strip()})


def nacti_vykony():
    """
    Vrati serazeny seznam dvojic (kod, nazev) vykonu napric VSEMI databazemi - JEDEN RADEK NA KOD.
    Kdyz ma stejny kod nazev v obou ciselnicich, prednost ma C_VYKONY (obecny) pred CP_VYKONY
    (pracovistni). Prazdny nazev nikdy neprepise uz zjisteny nazev.
    """
    nazev_podle_kodu = {}
    # Nejdriv CP_VYKONY, pak C_VYKONY - pozdeji zpracovany ciselnik ma prednost.
    for tabulka in ("CP_VYKONY", "C_VYKONY"):
        vysledek = _spust_ve_vsech_databazich(f"SELECT DISTINCT KOD, NAZ FROM {tabulka} WHERE KOD IS NOT NULL")
        for radky in vysledek.values():
            for kod, naz in radky:
                kod = (kod or "").strip()
                naz = (naz or "").strip()
                if kod and (naz or kod not in nazev_podle_kodu):
                    nazev_podle_kodu[kod] = naz
    return sorted(nazev_podle_kodu.items())


def filtruj_pacienty(filtry, on_progress=None):
    """
    Spusti sestaveny SQL dotaz napric vsemi databazemi ve slozce data/.
    Vraci dict {nazev_databaze: [id, ...]}.
    """
    sql, params = sestav_sql(filtry)
    vysledek = _spust_ve_vsech_databazich(sql, params, on_progress)
    return {nazev: [radek[0] for radek in radky] for nazev, radky in vysledek.items()}


def hledej_pokrocile_v_textu_mezi_pacienty(idosb_podle_databaze, over_text, tabulka="OBOJI",
                                            on_progress=None, datum_od=None, datum_do=None):
    """
    Textove hledani OMEZENE jen na jiz vyfiltrovane pacienty (idosb_podle_databaze).
    over_text: funkce z text_search (shoda_kterykoliv_vyraz / shoda_cislo_u_klicoveho_slova).
    tabulka: "DEKURS", "ANAMNEZA" nebo "OBOJI".
    datum_od, datum_do: volitelne datetime.date - jen zaznamy s datem v tomto rozmezi
    (stejne obdobi jako v kroku 0, aby vysledky zustaly casove konzistentni).

    Vraci seznam [{"idosb", "datum", "ukazka", "zdroj", "databaze", ...}, ...].
    """
    zdroje = {"DEKURS": ["dekurz"], "ANAMNEZA": ["anamneza"]}.get(tabulka, ["dekurz", "anamneza"])

    databaze = seznam_databazi()
    vysledky = []
    for i, o in enumerate(databaze):
        nazev = o["nazev"]
        idosb_filtr = idosb_podle_databaze.get(nazev)
        if not idosb_filtr:
            continue
        con = get_connection(o["cesta"])
        try:
            for zdroj in zdroje:
                def progres(zpracovano, celkem, nazev=nazev, zdroj=zdroj, i=i):
                    if on_progress:
                        on_progress((i + zpracovano / celkem) / len(databaze),
                                    f"Hledam v textu: databaze {nazev}, {zdroj} - {int(100 * zpracovano / celkem)} %")
                for v in text_search.hledej_v_tabulce(con, zdroj, over_text, idosb_filtr,
                                                      progres if on_progress else None, datum_od, datum_do):
                    vysledky.append(dict(v, zdroj=zdroj, databaze=nazev))
        finally:
            con.close()
    if on_progress:
        on_progress(1.0, "Textove hledani dokonceno.")
    return vysledky
