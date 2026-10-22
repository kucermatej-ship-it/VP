"""
Export seznamu pacientu do CSV (oddelovac ";", kodovani utf-8-sig) - napric vice databazemi.

POZOR: vystupni soubor obsahuje citlive osobni udaje (rodne cislo).
Ukladejte ho jen lokalne a nesdilejte nezabezpecenym zpusobem.
"""
import csv
from db import nacti_podle_id, pripojeni_podle_databaze, pripravit_text

_PRAZDNE_UDAJE = ("", "", "", "", "", "", "")
_ZAKLADNI_HLAVICKA = ["Databaze", "ID pacienta", "Prijmeni", "Jmeno", "Rodne cislo", "Datum narozeni",
                      "Telefon", "E-mail", "Ordinace"]


def _nacti_udaje_pacientu(idosb_podle_databaze, spojeni=None):
    """
    Vrati dict {(databaze, idosb): (prijmeni, jmeno, rodcis, datnar, telefon, email, ordinace)} -
    ordinace je NAZEV z G_PRA (LEFT JOIN, takze pacient bez prirazene ordinace bude mit "").
    Chybejici telefon/e-mail je "".
    spojeni: volitelny dict {nazev_databaze: jiz_otevrene_pripojeni} - viz db.pripojeni_podle_databaze().
    """
    neprazdne = [n for n, ids in idosb_podle_databaze.items() if ids]
    udaje = {}
    for nazev, con in pripojeni_podle_databaze(neprazdne, spojeni):
        radky = nacti_podle_id(
            con,
            "SELECT s.ID, s.PRIJMENI, s.JMENO, s.RODCIS, s.DATNAR, s.TEL, s.MAIL, g.NAZEV "
            "FROM S_OSOBA s LEFT JOIN G_PRA g ON s.IDOSBPRA = g.ID WHERE s.ID IN",
            idosb_podle_databaze[nazev],
        )
        for id_pacienta, prijmeni, jmeno, rodcis, datnar, telefon, email, ordinace in radky:
            udaje[(nazev, id_pacienta)] = (
                pripravit_text(prijmeni), pripravit_text(jmeno), rodcis, datnar,
                (telefon or "").strip(), (email or "").strip(), ordinace or "",
            )
    return udaje


def export_pacienty(idosb_podle_databaze, cesta_souboru, spojeni=None):
    """
    Export pro starou AI aplikaci (asistent.py, ui.py): sloupce ID pacienta, Prijmeni, Jmeno,
    Rodne cislo, Datum narozeni - v ramci kazde databaze serazeno podle prijmeni.
    Vraci pocet zapsanych pacientu.
    """
    if not idosb_podle_databaze:
        return 0
    udaje = _nacti_udaje_pacientu(idosb_podle_databaze, spojeni=spojeni)
    with open(cesta_souboru, "w", newline="", encoding="utf-8-sig") as f:
        zapisovac = csv.writer(f, delimiter=";")
        zapisovac.writerow(["ID pacienta", "Prijmeni", "Jmeno", "Rodne cislo", "Datum narozeni"])
        # Poradi databazi zachovame, uvnitr databaze radime podle prijmeni.
        for nazev in idosb_podle_databaze:
            radky = [(idosb, u) for (n, idosb), u in udaje.items() if n == nazev]
            for idosb, (prijmeni, jmeno, rodcis, datnar, *_) in sorted(radky, key=lambda r: r[1][0] or ""):
                zapisovac.writerow([idosb, prijmeni, jmeno, rodcis, datnar])
    return len(udaje)


def export_pacienty_filtr(idosb_podle_databaze, cesta_souboru, spojeni=None):
    """
    Export ze samotneho SQL filtru (filtr_ui.py, "Krok 0") - zakladni udaje vcetne telefonu,
    e-mailu a ordinace.
    Vraci pocet pacientu.
    """
    if not idosb_podle_databaze:
        return 0
    udaje = _nacti_udaje_pacientu(idosb_podle_databaze, spojeni=spojeni)
    pocet = 0
    with open(cesta_souboru, "w", newline="", encoding="utf-8-sig") as f:
        zapisovac = csv.writer(f, delimiter=";")
        zapisovac.writerow(_ZAKLADNI_HLAVICKA)
        for nazev, idosb_seznam in idosb_podle_databaze.items():
            for idosb in sorted(set(int(i) for i in idosb_seznam)):
                zapisovac.writerow([nazev, idosb, *udaje.get((nazev, idosb), _PRAZDNE_UDAJE)])
                pocet += 1
    return pocet


def _format_citace(z):
    """Jeden radek citace v ramci bunky - viz export_zaznamy_po_krocich."""
    nalezeno = z.get("nalezeny_vyraz", z.get("nalezene_cislo", ""))
    return f"[{z['zdroj']}, {z['datum']}] nalezeno: {nalezeno} — {z['ukazka']}"


def export_zaznamy_po_krocich(kroky, cesta_souboru, spojeni=None):
    """
    Ulozi CSV z VICEKROKOVEHO textoveho hledani (kazdy dalsi krok = A ZAROVEN, viz filtr_ui.py) -
    JEDEN RADEK = JEDEN PACIENT (jen ti, co prosli VSEMI kroky, tedy jsou ve vysledku posledniho
    kroku), a KAZDY KROK MA VLASTNI SLOUPEC "Citace N (popis kroku)". Ma-li pacient v jednom
    kroku vic nalezu, jsou pod sebou v jedne bunce.

    kroky: seznam dict [{"popis": str, "zaznamy": [...]}, ...] v poradi Krok 1, Krok 2, ... -
    odpovida st.session_state.kroky_hledani z filtr_ui.py.
    """
    if not kroky or not kroky[-1]["zaznamy"]:
        return 0

    finalni_pacienti = sorted({(z["databaze"], z["idosb"]) for z in kroky[-1]["zaznamy"]})
    idosb_podle_databaze = {}
    for databaze, idosb in finalni_pacienti:
        idosb_podle_databaze.setdefault(databaze, set()).add(idosb)
    udaje = _nacti_udaje_pacientu(idosb_podle_databaze, spojeni=spojeni)

    # Pro kazdy krok zvlast: {(databaze, idosb): [citace z TOHOTO kroku]}.
    citace_podle_kroku = []
    for krok in kroky:
        mapa = {}
        for z in krok["zaznamy"]:
            mapa.setdefault((z["databaze"], z["idosb"]), []).append(_format_citace(z))
        citace_podle_kroku.append(mapa)

    with open(cesta_souboru, "w", newline="", encoding="utf-8-sig") as f:
        zapisovac = csv.writer(f, delimiter=";")
        zapisovac.writerow(_ZAKLADNI_HLAVICKA + [
            f"Citace {cislo} ({krok['popis']})" for cislo, krok in enumerate(kroky, start=1)
        ])
        for klic in finalni_pacienti:
            zapisovac.writerow([
                *klic, *udaje.get(klic, _PRAZDNE_UDAJE),
                *("\n".join(mapa.get(klic, [])) for mapa in citace_podle_kroku),
            ])

    return len(finalni_pacienti)
