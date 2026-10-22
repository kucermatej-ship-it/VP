"""
Deduplikace pacientu napric databazemi podle rodneho cisla.

Ruzne databaze maji nezavisle cislovana ID pacientu (S_OSOBA.ID), takze
stejny clovek muze mit v kazde databazi jine cislo. Jedine spolehlive
univerzalni spojeni je RODCIS - pokud je stejne, jde o stejnou osobu.
"""
from db import get_connection, seznam_databazi, rozdel_na_davky


def rodna_cisla_pro_pacienty(idosb_podle_databaze, spojeni=None):
    """
    idosb_podle_databaze: dict {nazev_databaze: [idosb, ...]}
    spojeni: volitelny dict {nazev_databaze: jiz_otevrene_pripojeni} - viz export.export_pacienty().
    Vrati dict {(nazev_databaze, idosb): rodne_cislo}.
    """
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
            idosb_seznam_cisla = sorted(set(int(i) for i in idosb_seznam))
            cur = con.cursor()
            for davka in rozdel_na_davky(idosb_seznam_cisla, 1000):
                idcka = ",".join(str(i) for i in davka)
                cur.execute(f"SELECT ID, RODCIS FROM S_OSOBA WHERE ID IN ({idcka})")
                for id_pacienta, rodcis in cur.fetchall():
                    vysledek[(nazev_databaze, id_pacienta)] = rodcis
        finally:
            if sam_zaviram:
                con.close()
    return vysledek


def pocet_unikatnich_osob(idosb_podle_databaze, spojeni=None):
    """
    Vrati (skutecny_pocet_osob, pocet_shod_mezi_databazemi).
    skutecny_pocet_osob = pocet ruznych lidi podle rodneho cisla (napric vsemi databazemi).
    pocet_shod = kolikrat se stejne rodne cislo objevilo ve vice nez jedne databazi.
    """
    mapovani = rodna_cisla_pro_pacienty(idosb_podle_databaze, spojeni=spojeni)
    rodna_cisla_platna = [rc for rc in mapovani.values() if rc]
    pocet_bez_rc = sum(1 for rc in mapovani.values() if not rc)

    unikatni_rc = set(rodna_cisla_platna)
    pocet_shod = len(rodna_cisla_platna) - len(unikatni_rc)

    skutecny_pocet_osob = len(unikatni_rc) + pocet_bez_rc
    return skutecny_pocet_osob, pocet_shod
