import io
import os
import datetime
import contextlib
import streamlit as st

# asistent.py je ve stejne slozce jako tento soubor, takze zadne dalsi nastaveni
# cesty neni potreba - funguje to bez ohledu na to, odkud program spustite.
import asistent
from db import EXPORT_SLOZKA

st.set_page_config(page_title="SmartMedix AI asistent", page_icon="🩺")
st.title("🩺 SmartMedix AI asistent")
st.caption("Zeptejte se česky na cokoliv o pacientech. Vše běží lokálně na vašem Macu, žádná data nikam neodcházejí.")

if "kontext" not in st.session_state:
    st.session_state.kontext = None
if "historie" not in st.session_state:
    st.session_state.historie = []

with st.sidebar:
    st.subheader("Nastavení")
    export_zaskrtnuto = st.checkbox("Vždy exportovat automaticky do CSV", value=False)
    st.caption("Necháte-li nezaškrtnuté, u každé odpovědi s pacienty se objeví tlačítko na export – "
               "můžete se tak rozhodnout až podle toho, kolik pacientů vyšlo.")
    if st.session_state.historie:
        st.divider()
        st.caption(f"Konverzace: {len(st.session_state.historie)} otázek")
        if st.button("🆕 Začít novou konverzaci"):
            st.session_state.kontext = None
            st.session_state.historie = []
            st.rerun()


def zobraz_odpoved_bubliny(polozka, klic):
    """Vykresli obsah jedne asistentske bubliny (pouziva se pro historii i pro novou odpoved)."""
    if polozka.get("navazujici"):
        st.caption("🔗 navazující dotaz")
    st.write(polozka["odpoved"])

    pocet_pacientu = sum(len(v) for v in (polozka.get("pacienti") or {}).values())
    cesta_exportu = polozka.get("export_soubor")

    if cesta_exportu and os.path.exists(cesta_exportu):
        with open(cesta_exportu, "rb") as soubor:
            st.download_button(
                "⬇️ Stáhnout CSV se seznamem pacientů",
                data=soubor.read(),
                file_name=os.path.basename(cesta_exportu),
                mime="text/csv",
                key=f"stahnout_{klic}",
            )
    elif pocet_pacientu > 0:
        # Zadny export jeste neprobehl - nabidnout ho az TED, kdyz uzivatel uz vidi pocet pacientu.
        if st.button(f"📥 Exportovat těchto {pocet_pacientu} pacientů do CSV", key=f"export_{klic}"):
            os.makedirs(EXPORT_SLOZKA, exist_ok=True)
            znacka_casu = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
            pripona = "_navazujici" if polozka.get("navazujici") else ""
            nova_cesta = os.path.join(EXPORT_SLOZKA, f"pacienti_{znacka_casu}{pripona}.csv")
            asistent.export_pacienty(polozka["pacienti"], nova_cesta)
            polozka["export_soubor"] = nova_cesta
            st.rerun()

    if polozka.get("technicke"):
        with st.expander("Technické detaily (plán, SQL, nalezené záznamy…)"):
            st.code(polozka["technicke"], language="text")


# Cela dosavadni konverzace jako chat - otazky a odpovedi pod sebou
for i, polozka in enumerate(st.session_state.historie):
    with st.chat_message("user"):
        st.write(polozka["otazka"])
    with st.chat_message("assistant"):
        zobraz_odpoved_bubliny(polozka, klic=f"historie_{i}")

otazka = st.chat_input("Zeptejte se na cokoliv o pacientech…")

if otazka:
    with st.chat_message("user"):
        st.write(otazka)

    with st.chat_message("assistant"):
        progress_bar = st.progress(0)
        stav_text = st.empty()

        def aktualizuj_progress(zlomek, zprava):
            progress_bar.progress(zlomek)
            stav_text.text(zprava)

        zachyceny_vystup = io.StringIO()
        vysledek = None
        try:
            with contextlib.redirect_stdout(zachyceny_vystup):
                # Automaticky exportujeme jen pri zaskrtnutem prepinaci nebo vyslovnem "exportuj..." v otazce.
                # Jinak necháme rozhodnuti az na uzivateli - viz tlacitko v zobraz_odpoved_bubliny().
                exportovat = export_zaskrtnuto or asistent.detekuj_export(otazka)
                vysledek = asistent.zeptej_se(
                    otazka,
                    exportovat=exportovat,
                    on_progress=aktualizuj_progress,
                    kontext=st.session_state.kontext,
                )
            chyba = None
        except Exception as e:
            chyba = str(e)
        cely_text = zachyceny_vystup.getvalue()

        progress_bar.empty()
        stav_text.empty()

        if chyba:
            st.error(f"Nastala chyba: {chyba}")
            nova_polozka = {"otazka": otazka, "odpoved": f"⚠️ Nastala chyba: {chyba}"}
            st.write(nova_polozka["odpoved"])
        else:
            if vysledek and vysledek.get("pacienti"):
                st.session_state.kontext = {"otazka": vysledek["otazka"], "pacienti": vysledek["pacienti"]}

            if "--- Ceska odpoved ---" in cely_text:
                technicke, zbytek = cely_text.split("--- Ceska odpoved ---", 1)
            else:
                technicke, zbytek = cely_text, ""

            if "--- Export:" in zbytek:
                odpoved_text, _ = zbytek.split("--- Export:", 1)
            else:
                odpoved_text = zbytek

            nova_polozka = {
                "otazka": otazka,
                "odpoved": odpoved_text.strip(),
                "technicke": technicke.strip(),
                "pacienti": vysledek.get("pacienti") if vysledek else None,
                "export_soubor": vysledek.get("export_soubor") if vysledek else None,
                "navazujici": bool(vysledek.get("navazujici")) if vysledek else False,
            }
            zobraz_odpoved_bubliny(nova_polozka, klic="nova")

        st.session_state.historie.append(nova_polozka)
