"""
Zarazeni pacienta do ordinace.

Pravidla (dohodnuta podle skutecnych dat - registrace v S_OSOBA.IDOSBPRA chybi u ~80-98 %
pacientu a pacienti casto chodi do vic ordinaci):

- ORDINACE PACIENTA = ordinace jeho POSLEDNIHO DEKURZU. Dekurz je s ordinaci spojeny pres
  M_DEKURS.IDPRAZDR -> G_PRAZDR.ID (pracoviste + zdravotnik) -> G_PRAZDR.IDPRA -> G_PRA.ID.
  Pri shode data rozhoduje vyssi M_DEKURS.ID (pozdeji zalozeny zaznam).
- Zaznamy "Systemu" (polozka G_PRA s ID 0 / nazvem "System" - automaticke zapisy programu)
  se ignoruji, jako by neexistovaly.
- Je-li zadane obdobi (od-do), rozhoduje posledni dekurz V TOMTO OBDOBI. Pacient bez dekurzu
  v obdobi pak nema v obdobi zadnou ordinaci.
- Bez obdobi: pacient, ktery nema zadny dekurz od lekare, dostane ordinaci podle registrace
  (S_OSOBA.IDOSBPRA), pokud ji ma vyplnenou.
- Ordinace se STEJNYM ICP jsou JEDNA ordinace - i napric databazemi (napr. Kraluv Dvur + Beroun).
  Ordinace bez ICP se rozlisuji podle databaze a ID.

Vse se pocita v Pythonu z jednoho souhrnneho dotazu na databazi (GROUP BY) - zadne SQL od AI,
hodnoty obdobi jdou do SQL jako parametry "?".
"""
import datetime

from db import get_connection, seznam_databazi
import schema_pravidla

_NAZVY_SYSTEMU = {"systém", "system"}

SQL_ORDINACE = "SELECT ID, NAZEV, ICP FROM G_PRA"
SQL_REGISTRACE = "SELECT ID, IDOSBPRA FROM S_OSOBA WHERE IDOSBPRA IS NOT NULL"


def _sql_posledni_dekurzy(datum_od, datum_do):
    """Pro kazdou dvojici (pacient, ordinace) datum a ID posledniho dekurzu - z toho se v Pythonu
    vybere ordinace s celkove poslednim dekurzem. Konec obdobi se porovnava jako "< den po do",
    aby se zapocital cely posledni den i u sloupcu s casem."""
    sql = ("SELECT d.IDOSB, z.IDPRA, MAX(d.DATUM), MAX(d.ID) FROM M_DEKURS d "
           "JOIN G_PRAZDR z ON z.ID = d.IDPRAZDR WHERE z.IDPRA IS NOT NULL")
    params = []
    if datum_od is not None:
        sql += " AND d.DATUM >= ?"
        params.append(datum_od.isoformat())
    if datum_do is not None:
        sql += " AND d.DATUM < ?"
        params.append((datum_do + datetime.timedelta(days=1)).isoformat())
    return sql + " GROUP BY d.IDOSB, z.IDPRA", params


def _je_system(id_pra, nazev):
    return id_pra == 0 or (nazev or "").strip().casefold() in _NAZVY_SYSTEMU


def _ordinace_databaze(cur, nazev_databaze):
    """Vrati {G_PRA.ID: (klic, nazev, icp)} pro skutecne ordinace jedne databaze (bez Systemu)."""
    cur.execute(SQL_ORDINACE)
    vysledek = {}
    for id_pra, nazev, icp in cur.fetchall():
        if _je_system(id_pra, nazev):
            continue
        nazev = (nazev or "").strip() or f"Ordinace {id_pra}"
        icp = (icp or "").strip()
        klic = f"ICP {icp}" if icp else f"{nazev_databaze} #{id_pra}"
        vysledek[id_pra] = (klic, nazev, icp)
    return vysledek


def _pro_kazdou_databazi(funkce):
    """Zavola funkce(nazev_databaze, cur) pro kazdou databazi ve slozce data/ (pripojeni zavre)."""
    for o in seznam_databazi():
        con = get_connection(o["cesta"])
        try:
            funkce(o["nazev"], con.cursor())
        finally:
            con.close()


def nacti_ordinace():
    """
    Vrati dict {klic_ordinace: popis} vsech skutecnych ordinaci napric databazemi, serazeny podle
    popisu - pro vyber v rozhrani. Popis: "Kraluv Dvur + Beroun (ICP 21161206)" pro ordinace s ICP,
    "Ambulance prakt. lekare (SMARTMEDIX)" pro ordinace bez ICP.
    """
    schema_pravidla.zkontroluj_sql(SQL_ORDINACE)
    nazvy_podle_klice = {}

    def zpracuj(nazev_databaze, cur):
        for klic, nazev, icp in _ordinace_databaze(cur, nazev_databaze).values():
            nazvy = nazvy_podle_klice.setdefault(klic, [])
            if nazev not in nazvy:
                nazvy.append(nazev)

    _pro_kazdou_databazi(zpracuj)

    popisy = {}
    for klic, nazvy in nazvy_podle_klice.items():
        if klic.startswith("ICP "):
            popisy[klic] = f"{' + '.join(sorted(nazvy, key=str.casefold))} (IČP {klic[4:]})"
        else:
            popisy[klic] = f"{nazvy[0]} ({klic.rsplit(' #', 1)[0]})"
    return dict(sorted(popisy.items(), key=lambda polozka: polozka[1].casefold()))


def prirad_ordinace(datum_od=None, datum_do=None):
    """
    Vrati {nazev_databaze: {idosb: klic_ordinace}} - ordinace kazdeho pacienta podle pravidel
    nahore. Pacient bez ordinace ve vysledku chybi. Prochazi vsechny dekurzy (u velke databaze
    muze trvat desitky sekund) - filtr_ui.py si vysledek pamatuje (st.cache_data).
    """
    sql_dekurzy, params = _sql_posledni_dekurzy(datum_od, datum_do)
    for sql in (SQL_ORDINACE, SQL_REGISTRACE, sql_dekurzy):
        schema_pravidla.zkontroluj_sql(sql)
    bez_obdobi = datum_od is None and datum_do is None
    vysledek = {}

    def zpracuj(nazev_databaze, cur):
        ordinace = _ordinace_databaze(cur, nazev_databaze)
        cur.execute(sql_dekurzy, params)
        posledni = {}  # idosb -> (poradi, klic)
        for idosb, id_pra, datum, id_dekurzu in cur.fetchall():
            if id_pra not in ordinace:
                continue  # System nebo neznama ordinace
            poradi = (datum is not None, datum or 0, id_dekurzu or 0)
            if idosb not in posledni or poradi > posledni[idosb][0]:
                posledni[idosb] = (poradi, ordinace[id_pra][0])
        prirazeni = {idosb: klic for idosb, (_, klic) in posledni.items()}

        if bez_obdobi:
            cur.execute(SQL_REGISTRACE)
            for idosb, id_pra in cur.fetchall():
                if idosb not in prirazeni and id_pra in ordinace:
                    prirazeni[idosb] = ordinace[id_pra][0]
        vysledek[nazev_databaze] = prirazeni

    _pro_kazdou_databazi(zpracuj)
    return vysledek


def nazvy_ordinaci_pacientu(prirazeni, popisy):
    """Z vysledku prirad_ordinace() a nacti_ordinace() sestavi {(databaze, idosb): popis ordinace}
    - pro sloupec "Ordinace" v exportu."""
    return {
        (nazev_databaze, idosb): popisy.get(klic, "")
        for nazev_databaze, pacienti in prirazeni.items()
        for idosb, klic in pacienti.items()
    }
