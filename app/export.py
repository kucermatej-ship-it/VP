"""
Export seznamu pacientu (Databaze, jmeno, prijmeni, rodne cislo) do CSV - napric vice databazemi.

POZOR: vystupni soubor obsahuje citlive osobni udaje (rodne cislo).
Ukladejte ho jen lokalne a nesdilejte nezabezpecenym zpusobem.
"""
import csv
from db import get_connection, pripravit_text, seznam_databazi, rozdel_na_davky


def export_pacienty(idosb_podle_databaze, cesta_souboru, spojeni=None):
    """
    idosb_podle_databaze: dict {nazev_databaze: [idosb, ...]}
    Ulozi CSV se sloupci ID pacienta, Prijmeni, Jmeno, Rodne cislo, Datum narozeni.
    Nazev databaze (pracoviste) se v exportu neuvadi - pouziva se jen interne pro spojeni
    ID pacienta se spravnou databazi, viz db.py.

    spojeni: volitelny dict {nazev_databaze: jiz_otevrene_pripojeni} - kdyz je predano
    (typicky z asistent.py, ktery uz pripojeni ma otevrene z prubehu stejne otazky), pouzije
    se rovnou beze zbytecneho dalsiho pripojovani. Kdyz neni predano, funkce si pripojeni
    sama otevre a na konci zavre (puvodni chovani, napr. pri exportu z tlacitka v UI).
    """
    if not idosb_podle_databaze:
        return 0

    cesty = None
    if spojeni is None:
        cesty = {o["nazev"]: o["cesta"] for o in seznam_databazi()}
    celkem = 0

    with open(cesta_souboru, "w", newline="", encoding="utf-8-sig") as f:
        zapisovac = csv.writer(f, delimiter=";")
        zapisovac.writerow(["ID pacienta", "Prijmeni", "Jmeno", "Rodne cislo", "Datum narozeni"])

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
                    cur.execute(
                        f"SELECT ID, PRIJMENI, JMENO, RODCIS, DATNAR FROM S_OSOBA WHERE ID IN ({idcka}) ORDER BY PRIJMENI"
                    )
                    for id_pacienta, prijmeni, jmeno, rodcis, datnar in cur.fetchall():
                        zapisovac.writerow([
                            id_pacienta,
                            pripravit_text(prijmeni),
                            pripravit_text(jmeno),
                            rodcis,
                            datnar,
                        ])
                        celkem += 1
            finally:
                if sam_zaviram:
                    con.close()

    return celkem


def _nacti_udaje_pacientu(idosb_podle_databaze, spojeni=None):
    """
    Spolecny pomocnik pro export_* funkce nize - vrati dict
    {(databaze, idosb): (prijmeni, jmeno, rodcis, datnar, ordinace)} - ordinace je NAZEV
    z G_PRA (LEFT JOIN, takze pacient bez prirazene ordinace bude mit "").
    spojeni: volitelny dict {nazev_databaze: jiz_otevrene_pripojeni} - viz export_pacienty().
    """
    if not idosb_podle_databaze:
        return {}
    cesty = None
    if spojeni is None:
        cesty = {o["nazev"]: o["cesta"] for o in seznam_databazi()}

    udaje_pacientu = {}
    for nazev_databaze, idosb_mnozina in idosb_podle_databaze.items():
        if not idosb_mnozina:
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
            idosb_seznam = sorted(int(i) for i in idosb_mnozina)
            cur = con.cursor()
            for davka in rozdel_na_davky(idosb_seznam, 1000):
                idcka = ",".join(str(i) for i in davka)
                cur.execute(
                    f"SELECT s.ID, s.PRIJMENI, s.JMENO, s.RODCIS, s.DATNAR, g.NAZEV "
                    f"FROM S_OSOBA s LEFT JOIN G_PRA g ON s.IDOSBPRA = g.ID WHERE s.ID IN ({idcka})"
                )
                for id_pacienta, prijmeni, jmeno, rodcis, datnar, ordinace in cur.fetchall():
                    udaje_pacientu[(nazev_databaze, id_pacienta)] = (
                        pripravit_text(prijmeni), pripravit_text(jmeno), rodcis, datnar, ordinace or ""
                    )
        finally:
            if sam_zaviram:
                con.close()
    return udaje_pacientu


def export_pacienty_filtr(idosb_podle_databaze, cesta_souboru, spojeni=None):
    """
    Stejne jako export_pacienty(), ale navic obsahuje sloupec "Ordinace" (G_PRA.NAZEV) -
    pouziva se pro export ze samotneho SQL filtru (filtr_ui.py, "Krok 0"), kde uz o pacientovi
    zadne textove nalezy nejsou, jen jeho zakladni udaje vcetne pracoviste.
    """
    if not idosb_podle_databaze:
        return 0
    udaje_pacientu = _nacti_udaje_pacientu(idosb_podle_databaze, spojeni=spojeni)

    with open(cesta_souboru, "w", newline="", encoding="utf-8-sig") as f:
        zapisovac = csv.writer(f, delimiter=";")
        zapisovac.writerow(["Databaze", "ID pacienta", "Prijmeni", "Jmeno", "Rodne cislo", "Datum narozeni", "Ordinace"])
        for nazev_databaze, idosb_seznam in idosb_podle_databaze.items():
            for idosb in sorted(set(int(i) for i in idosb_seznam)):
                prijmeni, jmeno, rodcis, datnar, ordinace = udaje_pacientu.get(
                    (nazev_databaze, idosb), ("", "", "", "", "")
                )
                zapisovac.writerow([nazev_databaze, idosb, prijmeni, jmeno, rodcis, datnar, ordinace])

    return sum(len(set(int(i) for i in v)) for v in idosb_podle_databaze.values())


def export_zaznamy_s_citaci(zaznamy, cesta_souboru, spojeni=None):
    """
    Ulozi CSV se ZAZNAMY z textoveho hledani (filtry.py) - JEDEN RADEK = JEDEN PACIENT (na rozdil
    od export_pacienty() navic obsahuje i citace z textoveho hledani a ordinaci pacienta). Kdyz ma
    pacient vic nalezenych citaci (napr. NYHA II zmineno ve trech ruznych dekurzech), vsechny se
    ulozi DO JEDNE BUNKY sloupce "Nalezy", kazda citace na svem radku uvnitr te bunky (CSV/Excel/
    Numbers si s bunkou obsahujici zalomeni radku poradi bez problemu - staci pri otevirani nechat
    radky bunky "wrap").

    zaznamy: seznam dict ve tvaru, jaky vraci filtry.hledej_pokrocile_v_textu_mezi_pacienty() /
    filtry.hledej_v_textu_mezi_pacienty(): povinne klice "idosb", "datum", "ukazka", "zdroj",
    "databaze", volitelne "nalezeny_vyraz" nebo "nalezene_cislo".
    spojeni: volitelny dict {nazev_databaze: jiz_otevrene_pripojeni} - viz export_pacienty().
    """
    if not zaznamy:
        return 0

    idosb_podle_databaze = {}
    for z in zaznamy:
        idosb_podle_databaze.setdefault(z["databaze"], set()).add(z["idosb"])

    udaje_pacientu = _nacti_udaje_pacientu(idosb_podle_databaze, spojeni=spojeni)

    # Seskupime zaznamy podle pacienta (databaze, idosb) - poradi zachovame stejne, v jakem
    # prisly (typicky serazene podle data sestupne, viz text_search.py).
    citace_podle_pacienta = {}
    for z in zaznamy:
        citace_podle_pacienta.setdefault((z["databaze"], z["idosb"]), []).append(_format_citace(z))

    with open(cesta_souboru, "w", newline="", encoding="utf-8-sig") as f:
        zapisovac = csv.writer(f, delimiter=";")
        zapisovac.writerow([
            "Databaze", "ID pacienta", "Prijmeni", "Jmeno", "Rodne cislo", "Datum narozeni",
            "Ordinace", "Pocet nalezu", "Nalezy",
        ])
        for (nazev_databaze, idosb), citace in citace_podle_pacienta.items():
            prijmeni, jmeno, rodcis, datnar, ordinace = udaje_pacientu.get(
                (nazev_databaze, idosb), ("", "", "", "", "")
            )
            zapisovac.writerow([
                nazev_databaze, idosb, prijmeni, jmeno, rodcis, datnar,
                ordinace, len(citace), "\n".join(citace),
            ])

    return len(citace_podle_pacienta)


def _format_citace(z):
    """Jeden radek citace v ramci bunky - viz export_zaznamy_s_citaci / export_zaznamy_po_krocich."""
    if "nalezeny_vyraz" in z:
        nalezeno = z["nalezeny_vyraz"]
    elif "nalezene_cislo" in z:
        nalezeno = z["nalezene_cislo"]
    else:
        nalezeno = ""
    return f"[{z['zdroj']}, {z['datum']}] nalezeno: {nalezeno} — {z['ukazka']}"


def export_zaznamy_po_krocich(kroky, cesta_souboru, spojeni=None):
    """
    Ulozi CSV z VICEKROKOVEHO textoveho hledani (kazdy dalsi krok = A ZAROVEN, viz filtr_ui.py) -
    JEDEN RADEK = JEDEN PACIENT (jen ti, co prosli VSEMI kroky, tedy jsou ve vysledku posledniho
    kroku), a KAZDY KROK MA VLASTNI SLOUPEC "Citace N (popis kroku)" - takze jde hned videt,
    diky ktere konkretni citaci pacient prosel kterym krokem, misto aby byly vsechny nalezy
    smichane dohromady.

    kroky: seznam dict [{"popis": str, "zaznamy": [...]}, ...] presne v poradi, v jakem byly
    kroky pridavany (prvni polozka = Krok 1, druha = Krok 2, atd.) - odpovida
    st.session_state.kroky_hledani z filtr_ui.py. "zaznamy" u kazdeho kroku je seznam dict
    ve tvaru z filtry.hledej_pokrocile_v_textu_mezi_pacienty().
    spojeni: volitelny dict {nazev_databaze: jiz_otevrene_pripojeni} - viz export_pacienty().
    """
    if not kroky or not kroky[-1]["zaznamy"]:
        return 0

    finalni_pacienti = sorted(set((z["databaze"], z["idosb"]) for z in kroky[-1]["zaznamy"]))

    idosb_podle_databaze = {}
    for databaze, idosb in finalni_pacienti:
        idosb_podle_databaze.setdefault(databaze, set()).add(idosb)

    udaje_pacientu = _nacti_udaje_pacientu(idosb_podle_databaze, spojeni=spojeni)

    # Pro kazdy krok zvlast si pripravime {(databaze, idosb): [radky_citaci z TOHOTO kroku]}.
    citace_podle_kroku = []
    for krok in kroky:
        mapa = {}
        for z in krok["zaznamy"]:
            mapa.setdefault((z["databaze"], z["idosb"]), []).append(_format_citace(z))
        citace_podle_kroku.append(mapa)

    with open(cesta_souboru, "w", newline="", encoding="utf-8-sig") as f:
        zapisovac = csv.writer(f, delimiter=";")
        hlavicka = ["Databaze", "ID pacienta", "Prijmeni", "Jmeno", "Rodne cislo", "Datum narozeni", "Ordinace"]
        for cislo, krok in enumerate(kroky, start=1):
            hlavicka.append(f"Citace {cislo} ({krok['popis']})")
        zapisovac.writerow(hlavicka)

        for databaze, idosb in finalni_pacienti:
            prijmeni, jmeno, rodcis, datnar, ordinace = udaje_pacientu.get(
                (databaze, idosb), ("", "", "", "", "")
            )
            radek = [databaze, idosb, prijmeni, jmeno, rodcis, datnar, ordinace]
            for mapa in citace_podle_kroku:
                radek.append("\n".join(mapa.get((databaze, idosb), [])))
            zapisovac.writerow(radek)

    return len(finalni_pacienti)
