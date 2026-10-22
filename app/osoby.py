"""
Deduplikace pacientu napric databazemi podle rodneho cisla.

Ruzne databaze maji nezavisle cislovana ID pacientu (S_OSOBA.ID), takze
stejny clovek muze mit v kazde databazi jine cislo. Jedine spolehlive
univerzalni spojeni je RODCIS - pokud je stejne, jde o stejnou osobu.
"""
from db import nacti_podle_id, pripojeni_podle_databaze


def rodna_cisla_pro_pacienty(idosb_podle_databaze, spojeni=None):
    """
    idosb_podle_databaze: dict {nazev_databaze: [idosb, ...]}
    spojeni: volitelny dict {nazev_databaze: jiz_otevrene_pripojeni} - viz db.pripojeni_podle_databaze().
    Vrati dict {(nazev_databaze, idosb): rodne_cislo}.
    """
    neprazdne = [n for n, ids in idosb_podle_databaze.items() if ids]
    vysledek = {}
    for nazev, con in pripojeni_podle_databaze(neprazdne, spojeni):
        for id_pacienta, rodcis in nacti_podle_id(con, "SELECT ID, RODCIS FROM S_OSOBA WHERE ID IN",
                                                  idosb_podle_databaze[nazev]):
            vysledek[(nazev, id_pacienta)] = rodcis
    return vysledek


def pocet_unikatnich_osob(idosb_podle_databaze, spojeni=None):
    """
    Vrati (skutecny_pocet_osob, pocet_shod_mezi_databazemi).
    skutecny_pocet_osob = pocet ruznych lidi podle rodneho cisla (napric vsemi databazemi).
    pocet_shod = kolikrat se stejne rodne cislo objevilo ve vice nez jedne databazi.
    """
    rodna_cisla = list(rodna_cisla_pro_pacienty(idosb_podle_databaze, spojeni=spojeni).values())
    platna = [rc for rc in rodna_cisla if rc]
    unikatni = set(platna)
    pocet_bez_rc = len(rodna_cisla) - len(platna)
    return len(unikatni) + pocet_bez_rc, len(platna) - len(unikatni)
