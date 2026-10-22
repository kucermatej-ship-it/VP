"""
Deterministicke (BEZ AI) filtrovani pacientu podle strukturovanych kriterii - vek, pohlavi,
ordinace, telesne hodnoty, diagnoza, medikace, pojistovna, statni prislusnost, identifikace osoby.

Na rozdil od asistent.py tady SQL nepise AI - skladame ho rucne v Pythonu z pevne dane sady
filtru, takze tu neni prostor pro halucinace (spatny nazev tabulky/sloupce, spatna Firebird
syntaxe, spatne pouziti JOINu apod.) - presne ty chyby, ktere delal jazykovy model u volnych
otazek. Hodnoty od uzivatele se do SQL vkladaji VZDY jako parametry ("?"), nikdy primym vlozenim
do textu retezce - to je standardni ochrana proti SQL injection.

Vysledny seznam pacientu lze pak (volitelne) dale zuzit textovym hledanim v dekurzech/anamnezach
(text_search.py) - to uz AI nepotrebuje vubec, staci presny hledany vyraz od uzivatele.
"""
from db import seznam_databazi, get_connection, rozdel_na_davky
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


def _rozdel_hodnoty(text):
    """Rozdeli textovy vstup oddeleny carkami na seznam ocistenych, neprazdnych hodnot -
    pro filtry, kde uzivatel muze zadat vic hodnot najednou (napr. diagnoza: "J069, I10")."""
    if not text:
        return []
    return [h.strip() for h in text.split(",") if h.strip()]


def sestav_sql(filtry):
    """
    filtry: dict - jen klice s vyplnenou hodnotou se do dotazu promitnou (jednotliva kriteria
    se kombinuji pres AND). Podporovane klice:
        vek_od, vek_do (int)
        pohlavi ("M" nebo "F")
        ordinace (seznam str - presne nazvy ordinaci, vybrane ze seznamu - viz nacti_ordinace())
        bmi_od, bmi_do / vaha_od, vaha_do / vyska_od, vyska_do (float)
        tlaksys_od, tlaksys_do / tlakdia_od, tlakdia_do / puls_od, puls_do (int)
        datum_od, datum_do (datetime.date) - jedno spolecne casove omezeni pro VSECHNY nize
            uvedene filtry, ktere maji nejake datum:
              - telesne hodnoty (BMI/vaha/vyska/tlak/puls): mereni (M_PRISTAV.DATUM) v tomto obdobi
              - medikace: predepsano (M_MEDIREC.DATUM) v tomto obdobi
              - vykony: proveden (P_DOKLADPOL.DATUM) v tomto obdobi
              - diagnoza: VYJIMKA - jde o TRVALOU diagnozu, takze se posuzuje jen podle datum_do
                (diagnoza zapsana nejpozdeji do konce obdobi = "od DATOD dale" plati, bez ohledu
                na DATDO) - datum_od se u diagnozy NEPOUZIVA
        diagnoza (str - jedna nebo vic hodnot oddelenych carkou, napr. "J069, I10" - hleda se
            v KOD i NAZEV, mezi hodnotami je NEBO)
        medikace (str - jedna nebo vic hodnot oddelenych carkou - nazev leku nebo jeho cast,
            case-insensitive, mezi hodnotami je NEBO)
        vykony (seznam str - presne kody vykonu, vybrane ze seznamu - viz nacti_vykony())
        poj (str - 3-mistny kod pojistovny)
        statni_prislusnost (str - cast nazvu)
        rodne_cislo (str - s lomitkem i bez)
        prijmeni, jmeno, adresa (str)

    Vraci (sql, params) - sql pouziva "?" jako zastupny znak, params je seznam hodnot ve
    stejnem poradi, v jakem se "?" v sql vyskytuji.
    """
    podminky = []
    params = []

    if filtry.get("vek_od") is not None or filtry.get("vek_do") is not None:
        vek_od = filtry.get("vek_od") if filtry.get("vek_od") is not None else 0
        vek_do = filtry.get("vek_do") if filtry.get("vek_do") is not None else 130
        podminky.append("EXTRACT(YEAR FROM CURRENT_DATE) - EXTRACT(YEAR FROM DATNAR) BETWEEN ? AND ?")
        params += [vek_od, vek_do]

    if filtry.get("pohlavi"):
        podminky.append("POHLAVI = ?")
        params.append(filtry["pohlavi"])

    if filtry.get("ordinace"):
        zastupne = ", ".join("?" for _ in filtry["ordinace"])
        podminky.append(f"IDOSBPRA IN (SELECT ID FROM G_PRA WHERE NAZEV IN ({zastupne}))")
        params += list(filtry["ordinace"])

    # Casove omezeni (datum_od/datum_do) - jedno spolecne obdobi pro VSECHNY filtry nize, ktere
    # maji nejake datum: telesne hodnoty (M_PRISTAV.DATUM), medikace (M_MEDIREC.DATUM - datum
    # vystaveni receptu) a vykony (P_DOKLADPOL.DATUM) se posuzuji jako "zaznam s datem v tomto
    # rozmezi". Diagnoza (M_STALADGN) je vyjimka - jde o TRVALOU diagnozu, takze se neposuzuje
    # podle rozmezi, ale jen podle toho, jestli uz v dobe konce obdobi byla zapsana (DATOD <= do) -
    # jednou zapsana trvala diagnoza plati "od DATOD dale", bez ohledu na DATDO.
    datum_od = filtry.get("datum_od")
    datum_do = filtry.get("datum_do")
    ma_casove_omezeni = datum_od is not None or datum_do is not None
    _datum_od_iso = datum_od.isoformat() if datum_od else "1900-01-01"
    _datum_do_iso = datum_do.isoformat() if datum_do else "2100-01-01"

    for sloupec, klic_od, klic_do in [
        ("BMI", "bmi_od", "bmi_do"),
        ("VAHA", "vaha_od", "vaha_do"),
        ("VYSKA", "vyska_od", "vyska_do"),
        ("TLAKSYS", "tlaksys_od", "tlaksys_do"),
        ("TLAKDIA", "tlakdia_od", "tlakdia_do"),
        ("PULS", "puls_od", "puls_do"),
    ]:
        od = filtry.get(klic_od)
        do = filtry.get(klic_do)
        if od is not None or do is not None:
            od = od if od is not None else 0
            do = do if do is not None else 999999
            if ma_casove_omezeni:
                podminky.append(
                    f"ID IN (SELECT IDOSB FROM M_PRISTAV WHERE {sloupec} BETWEEN ? AND ? AND DATUM BETWEEN ? AND ?)"
                )
                params += [od, do, _datum_od_iso, _datum_do_iso]
            else:
                podminky.append(f"ID IN (SELECT IDOSB FROM M_PRISTAV WHERE {sloupec} BETWEEN ? AND ?)")
                params += [od, do]

    if filtry.get("diagnoza"):
        hodnoty = _rozdel_hodnoty(filtry["diagnoza"])
        if hodnoty:
            dilci = []
            for h in hodnoty:
                dilci.append("(UPPER(KOD) LIKE UPPER(?) OR UPPER(NAZEV) LIKE UPPER(?))")
                params += [f"%{h}%", f"%{h}%"]
            dilci_sql = "(" + " OR ".join(dilci) + ")"
            if datum_do is not None:
                # Trvala diagnoza "od DATOD dale" - staci, aby byla zapsana nejpozdeji do konce obdobi.
                podminky.append(
                    f"ID IN (SELECT IDOSB FROM M_STALADGN WHERE {dilci_sql} AND DATOD <= ?)"
                )
                params.append(_datum_do_iso)
            else:
                podminky.append(f"ID IN (SELECT IDOSB FROM M_STALADGN WHERE {dilci_sql})")

    if filtry.get("medikace"):
        hodnoty = _rozdel_hodnoty(filtry["medikace"])
        if hodnoty:
            dilci = []
            for h in hodnoty:
                dilci.append("UPPER(NAZEV) LIKE UPPER(?)")
                params.append(f"%{h}%")
            dilci_sql = " OR ".join(dilci)
            if ma_casove_omezeni:
                podminky.append(
                    f"ID IN (SELECT IDOSB FROM M_MEDIREC WHERE ({dilci_sql}) AND DATUM BETWEEN ? AND ?)"
                )
                params += [_datum_od_iso, _datum_do_iso]
            else:
                podminky.append(f"ID IN (SELECT IDOSB FROM M_MEDIREC WHERE {dilci_sql})")

    if filtry.get("vykony"):
        zastupne = ", ".join("?" for _ in filtry["vykony"])
        if ma_casove_omezeni:
            podminky.append(
                f"ID IN (SELECT IDOSB FROM P_DOKLADPOL WHERE KODVYK IN ({zastupne}) AND DATUM BETWEEN ? AND ?)"
            )
            params += list(filtry["vykony"]) + [_datum_od_iso, _datum_do_iso]
        else:
            podminky.append(f"ID IN (SELECT IDOSB FROM P_DOKLADPOL WHERE KODVYK IN ({zastupne}))")
            params += list(filtry["vykony"])

    if filtry.get("poj"):
        podminky.append("POJ = ?")
        params.append(filtry["poj"])

    if filtry.get("statni_prislusnost"):
        podminky.append("STATPRIS IN (SELECT KOD FROM C_STATPRIS WHERE UPPER(NAZEV) LIKE UPPER(?))")
        params.append(f"%{filtry['statni_prislusnost']}%")

    if filtry.get("rodne_cislo"):
        rc_cislice = filtry["rodne_cislo"].replace("/", "").replace(" ", "").strip()
        podminky.append("REPLACE(RODCIS, '/', '') = ?")
        params.append(rc_cislice)

    if filtry.get("prijmeni"):
        podminky.append("UPPER(PRIJMENI) LIKE UPPER(?)")
        params.append(f"%{filtry['prijmeni']}%")

    if filtry.get("jmeno"):
        podminky.append("UPPER(JMENO) LIKE UPPER(?)")
        params.append(f"%{filtry['jmeno']}%")

    if filtry.get("adresa"):
        podminky.append("UPPER(ADRESA) LIKE UPPER(?)")
        params.append(f"%{filtry['adresa']}%")

    sql = "SELECT DISTINCT ID FROM S_OSOBA"
    if podminky:
        sql += " WHERE " + " AND ".join(podminky)
    return sql, params


def nacti_ordinace():
    """
    Vrati serazeny seznam vsech odlisnych nazvu ordinaci (G_PRA.NAZEV) napric VSEMI databazemi -
    pro vyber v rozhrani (misto rucniho psani nazvu). Volejte jen obcas (napr. jednou pri
    nacteni stranky, ulozene v st.cache_data v filtr_ui.py) - prochazi vsechny databaze.
    """
    sql = "SELECT DISTINCT NAZEV FROM G_PRA WHERE NAZEV IS NOT NULL"
    dotaz.over_sql(sql)
    nazvy = set()
    for o in seznam_databazi():
        con = get_connection(o["cesta"])
        try:
            _, radky = dotaz.spust_sql_s_pripojenim(sql, con)
            nazvy.update(row[0].strip() for row in radky if row[0] and row[0].strip())
        finally:
            con.close()
    return sorted(nazvy)


def nacti_vykony():
    """
    Vrati serazeny seznam dvojic (kod, nazev) vykonu napric VSEMI databazemi - JEDEN RADEK NA
    KOD, i kdyz se stejny kod vyskytuje v OBOU ciselnicich (C_VYKONY - obecny, CP_VYKONY -
    pracovistni) s jinak zapsanym nazvem. Kdyz se nazev lisi, prednost dostava C_VYKONY (obecny
    ciselnik) - filtrovani podle KODVYK samotneho na vybranem nazvu vubec nezavisi.
    """
    sql_c_vykony = "SELECT DISTINCT KOD, NAZ FROM C_VYKONY WHERE KOD IS NOT NULL"
    sql_cp_vykony = "SELECT DISTINCT KOD, NAZ FROM CP_VYKONY WHERE KOD IS NOT NULL"
    dotaz.over_sql(sql_c_vykony)
    dotaz.over_sql(sql_cp_vykony)

    # Zpracujeme nejdriv CP_VYKONY (pracovistni), pak C_VYKONY (obecny) - kdyz ma stejny kod
    # v obou ciselnicich vyplneny nazev, ten zpracovany POZDEJI (C_VYKONY) prepise ten drivejsi,
    # takze obecny ciselnik ma vysledne prednost. Prazdny nazev nikdy neprepise uz zjisteny
    # nazev - kod tak v seznamu nikdy "neztrati" svuj nazev kvuli druhemu zaznamu bez nazvu.
    nazev_podle_kodu = {}
    for o in seznam_databazi():
        con = get_connection(o["cesta"])
        try:
            for sql in (sql_cp_vykony, sql_c_vykony):
                _, radky = dotaz.spust_sql_s_pripojenim(sql, con)
                for kod, naz in radky:
                    if not kod or not kod.strip():
                        continue
                    kod_cisty = kod.strip()
                    naz_cisty = (naz or "").strip()
                    if naz_cisty or kod_cisty not in nazev_podle_kodu:
                        nazev_podle_kodu[kod_cisty] = naz_cisty
        finally:
            con.close()
    return sorted(nazev_podle_kodu.items())


def nacti_ordinace_pacientu(idosb_podle_databaze, spojeni=None):
    """
    Vrati dict {(databaze, idosb): nazev_ordinace} - pro doplneni sloupce "Ordinace" do exportu.
    Pacient bez prirazene ordinace (IDOSBPRA NULL, nebo ordinace bez NAZEV) bude chybet -
    volajici by mel pouzit .get(..., "") pro chybejici hodnotu.
    """
    if not idosb_podle_databaze:
        return {}
    cesty = None
    if spojeni is None:
        cesty = {o["nazev"]: o["cesta"] for o in seznam_databazi()}

    vysledek = {}
    for nazev_databaze, idosb_seznam in idosb_podle_databaze.items():
        if not idosb_seznam:
            continue
        if spojeni is not None:
            con = spojeni.get(nazev_databaze)
            if con is None:
                continue
            sam_zaviram = False
        else:
            if nazev_databaze not in cesty:
                continue
            con = get_connection(cesty[nazev_databaze])
            sam_zaviram = True
        try:
            idcka_cisla = sorted(set(int(i) for i in idosb_seznam))
            cur = con.cursor()
            for davka in rozdel_na_davky(idcka_cisla, 1000):
                idcka = ",".join(str(i) for i in davka)
                cur.execute(
                    f"SELECT s.ID, g.NAZEV FROM S_OSOBA s LEFT JOIN G_PRA g ON s.IDOSBPRA = g.ID "
                    f"WHERE s.ID IN ({idcka})"
                )
                for id_pacienta, nazev_ordinace in cur.fetchall():
                    vysledek[(nazev_databaze, id_pacienta)] = nazev_ordinace or ""
        finally:
            if sam_zaviram:
                con.close()
    return vysledek


def filtruj_pacienty(filtry, on_progress=None):
    """
    Spusti sestaveny SQL dotaz napric vsemi databazemi ve slozce data/.
    Vraci dict {nazev_databaze: [id, ...]}.
    """
    sql, params = sestav_sql(filtry)
    # Staticka kontrola tabulek/sloupcu (schema_pravidla.py) jako pojistka i pro nase vlastni,
    # rucne sestavene SQL - kdyby v teto funkci pribyla chyba, projevi se hned, ne az na databazi.
    dotaz.over_sql(sql)

    databaze = seznam_databazi()
    vysledek = {}
    for i, o in enumerate(databaze):
        if on_progress:
            on_progress(i / len(databaze) if databaze else 0, f"Filtruji: databaze {o['nazev']} ({i + 1}/{len(databaze)})")
        con = get_connection(o["cesta"])
        try:
            sloupce, radky = dotaz.spust_sql_s_pripojenim(sql, con, params=params)
            vysledek[o["nazev"]] = [row[0] for row in radky]
        finally:
            con.close()
    if on_progress:
        on_progress(1.0, "Filtrovani dokonceno.")
    return vysledek


def hledej_v_textu_mezi_pacienty(idosb_podle_databaze, vyraz, tabulka="OBOJI", on_progress=None):
    """
    Textove hledani (bez AI - vyraz zadava uzivatel primo) OMEZENE jen na jiz vyfiltrovane
    pacienty (idosb_podle_databaze). Vraci seznam zaznamu se stejnym tvarem jako text_search.py:
    [{"idosb", "datum", "ukazka", "zdroj", "databaze"}, ...]
    """
    if tabulka not in ("DEKURS", "ANAMNEZA", "OBOJI"):
        tabulka = "OBOJI"

    tabulky_k_prohledani = []
    if tabulka in ("DEKURS", "OBOJI"):
        tabulky_k_prohledani.append((text_search.hledej_v_dekurzech_jedna, "dekurz"))
    if tabulka in ("ANAMNEZA", "OBOJI"):
        tabulky_k_prohledani.append((text_search.hledej_v_anamnezach_jedna, "anamneza"))

    databaze = seznam_databazi()
    vysledky = []
    for i, o in enumerate(databaze):
        nazev = o["nazev"]
        idosb_filtr = idosb_podle_databaze.get(nazev, [])
        if not idosb_filtr:
            continue
        con = get_connection(o["cesta"])
        try:
            for hledejici_funkce, zdroj in tabulky_k_prohledani:
                def progres(zpracovano, celkem, nazev=nazev, zdroj=zdroj):
                    if on_progress:
                        on_progress(
                            (i + zpracovano / celkem) / len(databaze),
                            f"Hledam v textu: databaze {nazev}, {zdroj} - {int(100 * zpracovano / celkem)} %"
                        )
                for v in hledejici_funkce(con, vyraz, idosb_seznam=idosb_filtr, on_progress=progres):
                    vysledky.append(dict(v, zdroj=zdroj, databaze=nazev))
        finally:
            con.close()
    if on_progress:
        on_progress(1.0, "Textove hledani dokonceno.")
    return vysledky


def hledej_pokrocile_v_textu_mezi_pacienty(idosb_podle_databaze, over_text, tabulka="OBOJI",
                                            on_progress=None, datum_od=None, datum_do=None):
    """
    Stejne jako hledej_v_textu_mezi_pacienty, ale misto pevneho vyrazu bere libovolnou funkci
    over_text (viz text_search.shoda_kterykoliv_vyraz / shoda_cislo_u_klicoveho_slova) - pro
    slozitejsi kriteria: "kterykoliv z techto vyrazu" nebo "klicove slovo + cislo pod/nad hranici".
    Pouziva se pro vicekrokove hledani ve filtr_ui.py - kazdy dalsi krok muze hledat mezi vysledky
    kroku predchoziho (coz v efektu znamena "A ZAROVEN B", zatimco vyrazy v JEDNOM kroku pres
    shoda_kterykoliv_vyraz znamenaji "NEBO").

    datum_od, datum_do: volitelne datetime.date - kdyz jsou zadane, hleda se JEN v zaznamech
    s datem v tomto rozmezi (M_DEKURS.DATUM / M_ANAMNESA.DATCAS) - stejne obdobi, jake se
    pouzilo pro telesne hodnoty ve filtry.sestav_sql (pristav_datum_od/do), aby vysledky
    zustaly casove konzistentni napric celym vicekrokovym hledanim.
    """
    if tabulka not in ("DEKURS", "ANAMNEZA", "OBOJI"):
        tabulka = "OBOJI"

    tabulky_k_prohledani = []
    if tabulka in ("DEKURS", "OBOJI"):
        tabulky_k_prohledani.append((text_search.hledej_v_dekurzech_pokrocile, "dekurz"))
    if tabulka in ("ANAMNEZA", "OBOJI"):
        tabulky_k_prohledani.append((text_search.hledej_v_anamnezach_pokrocile, "anamneza"))

    databaze = seznam_databazi()
    vysledky = []
    for i, o in enumerate(databaze):
        nazev = o["nazev"]
        idosb_filtr = idosb_podle_databaze.get(nazev, [])
        if not idosb_filtr:
            continue
        con = get_connection(o["cesta"])
        try:
            for hledejici_funkce, zdroj in tabulky_k_prohledani:
                def progres(zpracovano, celkem, nazev=nazev, zdroj=zdroj):
                    if on_progress:
                        on_progress(
                            (i + zpracovano / celkem) / len(databaze),
                            f"Hledam v textu: databaze {nazev}, {zdroj} - {int(100 * zpracovano / celkem)} %"
                        )
                for v in hledejici_funkce(con, over_text, idosb_seznam=idosb_filtr, on_progress=progres,
                                           datum_od=datum_od, datum_do=datum_do):
                    vysledky.append(dict(v, zdroj=zdroj, databaze=nazev))
        finally:
            con.close()
    if on_progress:
        on_progress(1.0, "Textove hledani dokonceno.")
    return vysledky
