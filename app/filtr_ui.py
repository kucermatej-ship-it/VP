"""
SmartMedix - filtrovani pacientu BEZ AI.

Alternativni rozhrani k ui.py/asistent.py: misto aby otazku formulovanou volnym textem
prevadela na SQL umela inteligence (a obcas se spletla), si tu uzivatel filtry vybira rovnou
v ovladacich prvcich. SQL se sklada deterministicky v kodu (filtry.py) - zadny prostor pro
halucinace.

AI se pouziva jen volitelne v bocnim panelu pro navrh synonym k hledanemu vyrazu (synonyma.py) -
posila se tam jen vyraz, ktery uzivatel napise, nikdy data pacientu.
"""
import os
import datetime
import streamlit as st

import filtry
import ordinace
import osoby
import synonyma
import text_search
from db import EXPORT_SLOZKA
from export import export_pacienty_filtr, export_zaznamy_po_krocich


@st.cache_data(show_spinner="Nacitam seznam ordinaci ze vsech databazi...")
def nacti_ordinace_cache():
    return ordinace.nacti_ordinace()


@st.cache_data(show_spinner="Zjistuji ordinaci pacientu podle posledniho dekurzu "
                            "(poprve to u velke databaze muze trvat i desitky sekund)...")
def prirad_ordinace_cache(datum_od, datum_do):
    return ordinace.prirad_ordinace(datum_od, datum_do)


def ordinace_pro_export():
    """{(databaze, idosb): popis ordinace} pro sloupec "Ordinace" v exportu - podle posledniho
    dekurzu v obdobi kroku 0 (je-li zadane), jinak podle posledniho dekurzu vubec."""
    prirazeni = prirad_ordinace_cache(None, None)
    od, do = st.session_state.casove_omezeni_od, st.session_state.casove_omezeni_do
    if od or do:
        v_obdobi = prirad_ordinace_cache(od, do)
        prirazeni = {nazev: {**pacienti, **v_obdobi.get(nazev, {})} for nazev, pacienti in prirazeni.items()}
    return ordinace.nazvy_ordinaci_pacientu(prirazeni, nacti_ordinace_cache())


@st.cache_data(show_spinner="Nacitam ciselnik vykonu ze vsech databazi...")
def nacti_vykony_cache():
    return filtry.nacti_vykony()


def ukazatel_postupu():
    """Vytvori ukazatel postupu a vrati (funkce on_progress(zlomek, zprava), funkce na jeho skryti)."""
    progress_bar = st.progress(0)
    stav_text = st.empty()

    def aktualizuj(zlomek, zprava):
        progress_bar.progress(min(max(zlomek, 0.0), 1.0))
        stav_text.text(zprava)

    def skryj():
        progress_bar.empty()
        stav_text.empty()

    return aktualizuj, skryj


def nova_cesta_exportu(predpona):
    os.makedirs(EXPORT_SLOZKA, exist_ok=True)
    znacka_casu = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    return os.path.join(EXPORT_SLOZKA, f"{predpona}_{znacka_casu}.csv")


def rozsah(filtry_dict, popisek, klic, minimum, maximum, vychozi, krok=None, popisek_posuvniku=None):
    """Zaskrtavatko + posuvnik "od-do". Kdyz je zaskrtnute, zapise do filtry_dict klice
    "<klic>_od" a "<klic>_do"."""
    if st.checkbox(popisek):
        argumenty = {"step": krok} if krok is not None else {}
        od, do = st.slider(popisek_posuvniku or popisek, minimum, maximum, vychozi, **argumenty)
        filtry_dict[f"{klic}_od"], filtry_dict[f"{klic}_do"] = od, do


def text_filtr(filtry_dict, popisek, klic, popisek_pole, placeholder=None):
    """Zaskrtavatko + textove pole. Kdyz je zaskrtnute a vyplnene, zapise hodnotu do filtry_dict[klic]."""
    if st.checkbox(popisek):
        hodnota = st.text_input(popisek_pole, placeholder=placeholder)
        if hodnota.strip():
            filtry_dict[klic] = hodnota.strip()


st.set_page_config(page_title="SmartMedix - filtrovani pacientu", page_icon="🔎")
st.title("🔎 SmartMedix - filtrovani pacientu")
st.caption("Vyberte filtry a stisknete tlacitko Filtrovat. Zadne SQL se negeneruje pomoci AI - "
           "vysledek je vzdy presny a opakovatelny. Vse bezi lokalne, zadna data nikam neodchazeji.")

# Kazdy dalsi krok textoveho hledani hleda JEN mezi vysledkem kroku predchoziho - mezi kroky
# tedy plati "A ZAROVEN" (vyrazy v JEDNOM kroku znamenaji "NEBO"). Krok 0 = vysledek SQL filtru.
for klic, vychozi in {
    "vysledek_filtru": None,
    "unikatnich_osob": None,      # (skutecny_pocet, pocet_shod) - spocita se jednou po filtrovani
    "kroky_hledani": [],
    "casove_omezeni_od": None,
    "casove_omezeni_do": None,
    "synonyma_navrhy": [],
    "synonyma_kolo": 0,
}.items():
    if klic not in st.session_state:
        st.session_state[klic] = vychozi


# --- Bocni panel: AI navrh synonym/variant k hledanym vyrazum ------------------------------
# Sem se posila JEN vyraz, ktery si uzivatel sam napise - zadna data pacientu se nikam neodesilaji.
with st.sidebar:
    st.header("💡 Navrh klicovych slov (AI)")
    st.caption(
        "Sem se posila JEN vyraz, ktery napisete - zadna data pacientu se nikam neodesilaji. "
        "Lokalni AI navrhne mozna synonyma/zkratky/varianty zapisu, vy si vyberete, ktere se "
        "maji pouzit v textovem hledani."
    )
    synonyma_dotaz = st.text_input("Vyraz, ke kteremu chcete navrhnout varianty", placeholder="napr. NYHA II")
    if st.button("🔮 Navrhni synonyma"):
        if not synonyma_dotaz.strip():
            st.warning("Napiste nejdriv vyraz.")
        else:
            with st.spinner("AI premysli nad variantami..."):
                try:
                    st.session_state.synonyma_navrhy = synonyma.navrhni_synonyma(synonyma_dotaz)
                    st.session_state.synonyma_kolo += 1
                    if not st.session_state.synonyma_navrhy:
                        st.info("AI nenabidla zadne dalsi varianty.")
                except RuntimeError as e:
                    st.error(str(e))

    if st.session_state.synonyma_navrhy:
        st.write("Vyberte, ktere chcete pouzit:")
        vybrane = [
            navrh for navrh in st.session_state.synonyma_navrhy
            if st.checkbox(navrh, key=f"syn_chk_{st.session_state.synonyma_kolo}_{navrh}")
        ]
        if vybrane:
            # Cil vlozeni: pole "Hledane vyrazy..." u prave rozpracovaneho (jeste neulozeneho) kroku.
            if st.session_state.kroky_hledani:
                cilovy_pocet = st.session_state.kroky_hledani[-1]["pocet"]
            elif st.session_state.vysledek_filtru is not None:
                cilovy_pocet = sum(len(v) for v in st.session_state.vysledek_filtru.values())
            else:
                cilovy_pocet = None

            if cilovy_pocet is None:
                st.caption("Nejdriv spustte Krok 0 (tlacitko \"Filtrovat pacienty\") - pak pujde "
                           "vybrane vyrazy vlozit primo do pole hledani.")
            elif cilovy_pocet == 0:
                st.caption("Aktualni vysledek ma 0 pacientu, dalsi krok hledani neni k dispozici.")
            else:
                n = len(st.session_state.kroky_hledani)
                if st.button(f"➕ Vlozit vybrane do pole hledani (Krok {n + 1})"):
                    st.session_state[f"typ_kroku_{n}"] = "Obsahuje libovolny z vyrazu (NEBO)"
                    vyrazy = [v.strip() for v in st.session_state.get(f"vyrazy_{n}", "").split(",") if v.strip()]
                    videne = {v.casefold() for v in vyrazy}
                    for v in vybrane:
                        if v.casefold() not in videne:
                            vyrazy.append(v)
                            videne.add(v.casefold())
                    st.session_state[f"vyrazy_{n}"] = ", ".join(vyrazy)
                    st.rerun()


# --- Krok 0: filtry ---------------------------------------------------------------------------
# filtry_dict se plni rovnou pri vykreslovani ovladacich prvku - obsahuje jen pouzite filtry.
filtry_dict = {}

st.subheader("Zakladni udaje")
col1, col2 = st.columns(2)
with col1:
    rozsah(filtry_dict, "Filtrovat podle veku", "vek", 0, 130, (40, 84), popisek_posuvniku="Vek (roky)")
with col2:
    if st.checkbox("Filtrovat podle pohlavi"):
        volba = st.radio("Pohlavi", ["Muz", "Zena"], horizontal=True)
        filtry_dict["pohlavi"] = "M" if volba == "Muz" else "F"

if st.checkbox("Filtrovat podle ordinace/pracoviste"):
    popisy_ordinaci = nacti_ordinace_cache()
    if popisy_ordinaci:
        vybrane_ordinace = st.multiselect("Ordinace/pracoviste (lze vybrat vic)",
                                          options=list(popisy_ordinaci), format_func=popisy_ordinaci.get)
        if vybrane_ordinace:
            filtry_dict["ordinace"] = vybrane_ordinace
        st.caption("Ordinace pacienta = ordinace jeho POSLEDNIHO DEKURZU (zapisy \"Systemu\" se nepocitaji). "
                   "Pri zadanem obdobi rozhoduje posledni dekurz v obdobi. Pacient bez dekurzu se bez obdobi "
                   "zaradi podle registrace v karte. Ordinace se stejnym ICP jsou jedna ordinace.")
    else:
        st.info("V databazich se nenasla zadna ordinace.")

casove_od, casove_do = None, None
if st.checkbox("Omezit obdobim (plati napric vsemi filtry s datem)"):
    col1, col2 = st.columns(2)
    with col1:
        casove_od = st.date_input("Od", value=None, format="DD.MM.YYYY")
    with col2:
        casove_do = st.date_input("Do", value=None, format="DD.MM.YYYY")
    st.caption(
        "Plati automaticky pro VSECHNY nize uvedene filtry, ktere maji nejake datum, a taky pro "
        "VSECHNY nasledujici kroky textoveho hledani (nemusite ho zadavat znovu u kazdeho zvlast):\n"
        "- telesne hodnoty (BMI, vaha, vyska, tlak, puls) - dle data mereni\n"
        "- medikace - dle data vystaveni receptu\n"
        "- vykony - dle data provedeni\n"
        "- diagnoza - VYJIMKA: diagnoza je trvala, takze se pocita jako platna \"od data zapisu dale\" "
        "(stejne jako u Vas v databazi) - zapocita se, pokud byla zapsana nejpozdeji do konce obdobi "
        "(pole \"Od\" u diagnozy tedy nerozhoduje)\n"
        "- textove hledani v dekurzech/anamnezach - dle data zaznamu"
    )
    if casove_od and casove_do and casove_od > casove_do:
        st.warning("Datum \"Od\" je pozdeji nez \"Do\" - vysledek by byl prazdny.")
    if casove_od:
        filtry_dict["datum_od"] = casove_od
    if casove_do:
        filtry_dict["datum_do"] = casove_do

with st.expander("Telesne hodnoty (z preventivnich prohlidek)"):
    col1, col2, col3 = st.columns(3)
    with col1:
        rozsah(filtry_dict, "BMI", "bmi", 0.0, 60.0, (18.5, 40.0), krok=0.5)
        rozsah(filtry_dict, "Vaha (kg)", "vaha", 0.0, 250.0, (50.0, 100.0), krok=1.0)
    with col2:
        rozsah(filtry_dict, "Vyska (cm)", "vyska", 50.0, 220.0, (150.0, 200.0), krok=1.0)
        rozsah(filtry_dict, "Puls", "puls", 0, 220, (50, 100), popisek_posuvniku="Puls (tepu/min)")
    with col3:
        rozsah(filtry_dict, "Tlak systolicky (horni)", "tlaksys", 50, 260, (90, 140), popisek_posuvniku="Tlak systolicky")
        rozsah(filtry_dict, "Tlak diastolicky (dolni)", "tlakdia", 30, 160, (60, 90), popisek_posuvniku="Tlak diastolicky")

with st.expander("Diagnoza a medikace"):
    text_filtr(filtry_dict, "Filtrovat podle diagnozy", "diagnoza",
               "Diagnoza - kod MKN-10 nebo cast nazvu, lze zadat i vic hodnot oddelenych carkou (NEBO)",
               placeholder="napr. J069, I10, diabetes")
    text_filtr(filtry_dict, "Filtrovat podle medikace", "medikace",
               "Nazev leku - lze zadat i vic hodnot oddelenych carkou (NEBO), velikost pismen nevadi",
               placeholder="napr. Atoris, warfarin, IBUPROFEN")
    st.caption("POZOR: tyto dva filtry hledaji jen ve strukturovanych datech (M_STALADGN / M_MEDIREC), "
               "ne ve volnem textu poznamek - to lze udelat az v druhem kroku dole, po vyfiltrovani. "
               "Vyhledavani nazvu je vzdy necitlive na velikost pismen (Atoris = ATORIS = atoris).")

with st.expander("Vykony"):
    if st.checkbox("Filtrovat podle vykazanych vykonu"):
        seznam_vykonu = nacti_vykony_cache()
        if seznam_vykonu:
            popis_vykonu = {kod: f"{kod} - {naz}" if naz else kod for kod, naz in seznam_vykonu}
            vykony = st.multiselect(
                "Vykon (napiste kod nebo nazev, lze vybrat vic)",
                options=list(popis_vykonu),
                format_func=lambda k: popis_vykonu.get(k, k),
            )
            if vykony:
                filtry_dict["vykony"] = vykony
        else:
            st.info("V databazich se nenasel zadny vykon s vyplnenym kodem.")

with st.expander("Administrativni udaje"):
    if st.checkbox("Filtrovat podle zdravotni pojistovny"):
        nazvy_pojistoven = dict(filtry.POJISTOVNY)
        poj = st.selectbox("Pojistovna", options=list(nazvy_pojistoven), format_func=nazvy_pojistoven.get)
        if poj:
            filtry_dict["poj"] = poj
    text_filtr(filtry_dict, "Filtrovat podle statni prislusnosti", "statni_prislusnost",
               "Statni prislusnost (nebo jeji cast)", placeholder="napr. cesk")

with st.expander("Identifikace konkretni osoby"):
    col1, col2 = st.columns(2)
    with col1:
        text_filtr(filtry_dict, "Rodne cislo", "rodne_cislo", "Rodne cislo (s lomitkem i bez)", placeholder="960406/1622")
        text_filtr(filtry_dict, "Prijmeni", "prijmeni", "Prijmeni (nebo jeho cast)")
    with col2:
        text_filtr(filtry_dict, "Jmeno", "jmeno", "Jmeno (nebo jeho cast)")
        text_filtr(filtry_dict, "Adresa", "adresa", "Adresa (nebo jeji cast)", placeholder="napr. Praha 2")

st.divider()

if st.button("🔎 Filtrovat pacienty", type="primary"):
    if not filtry_dict:
        st.warning("Nezvolili jste zadny filtr - vysledkem by byli VSICHNI pacienti ve vsech databazich. "
                   "Pokud to opravdu chcete, zaskrtnete alespon jeden filtr se sirokym rozsahem (napr. vek 0-130).")
    else:
        aktualizuj_progress, skryj_progress = ukazatel_postupu()
        prirazeni = None
        if filtry_dict.get("ordinace"):
            prirazeni = prirad_ordinace_cache(filtry_dict.get("datum_od"), filtry_dict.get("datum_do"))
        vysledek = filtry.filtruj_pacienty(filtry_dict, on_progress=aktualizuj_progress,
                                           prirazeni_ordinaci=prirazeni)
        # Deduplikace podle RC se spocita JEDNOU tady - drive bezela pri kazdem prekresleni
        # stranky (kazde kliknuti na libovolny prvek) a pokazde znovu otevirala vsechny databaze.
        if sum(1 for ids in vysledek.values() if ids) > 1:
            aktualizuj_progress(1.0, "Overuji rodna cisla napric databazemi (deduplikace)...")
            st.session_state.unikatnich_osob = osoby.pocet_unikatnich_osob(vysledek)
        else:
            st.session_state.unikatnich_osob = None
        skryj_progress()

        st.session_state.vysledek_filtru = vysledek
        st.session_state.kroky_hledani = []
        # Casove obdobi plati automaticky i pro VSECHNY nasledujici kroky textoveho hledani.
        st.session_state.casove_omezeni_od = casove_od
        st.session_state.casove_omezeni_do = casove_do
        st.rerun()

if st.session_state.vysledek_filtru is not None:
    vysledek = st.session_state.vysledek_filtru
    naivni_soucet = sum(len(v) for v in vysledek.values())

    st.subheader("Vysledek filtrovani")
    if naivni_soucet == 0:
        st.info("Zadny pacient nevyhovel zadanym filtrum.")
    else:
        for nazev, ids in vysledek.items():
            st.write(f"**{nazev}**: {len(ids)} pacientu")

        if st.session_state.unikatnich_osob:
            skutecny_pocet, pocet_shod = st.session_state.unikatnich_osob
            st.success(f"**Celkem {skutecny_pocet} unikatnich pacientu** (po deduplikaci podle rodneho "
                       f"cisla - naivni soucet by byl {naivni_soucet}, ale {pocet_shod} zaznamu patrilo "
                       "stejne osobe ve vice databazich).")
        else:
            st.success(f"**Celkem {naivni_soucet} pacientu.**")

        if st.button("📥 Exportovat tento vysledek do CSV"):
            cesta = nova_cesta_exportu("pacienti_filtr")
            export_pacienty_filtr(vysledek, cesta, ordinace_pacientu=ordinace_pro_export())
            st.success(f"Ulozeno do: {cesta}")

        st.divider()
        st.subheader("Volitelne: postupne hledani v textu (kazdy dalsi krok = A ZAROVEN)")
        st.caption(
            "Kazdy krok hleda JEN mezi vysledkem kroku predchoziho (Krok 0 = vyse vyfiltrovani "
            "pacienti) - tim jde skladat slozitejsi podminky, napr. (\"NYHA II\" NEBO \"NYHA III\") "
            "A ZAROVEN (\"NT-proBNP\" pod hranici 300)."
        )

        if st.session_state.casove_omezeni_od or st.session_state.casove_omezeni_do:
            st.caption(
                f"📅 Aktivni casove omezeni: od {st.session_state.casove_omezeni_od or '(nezadano)'} "
                f"do {st.session_state.casove_omezeni_do or '(nezadano)'} - plati i pro kroky nize."
            )

        # --- Prehled dosavadnich kroku -----------------------------------------------------
        kroky = st.session_state.kroky_hledani
        st.markdown(f"**Krok 0 (SQL filtr):** {naivni_soucet} pacientu")
        for cislo, krok in enumerate(kroky, start=1):
            st.markdown(f"**Krok {cislo}:** {krok['popis']} → {krok['pocet']} pacientu")

        aktivni_vysledek = kroky[-1]["vysledek"] if kroky else vysledek
        aktivni_pocet = kroky[-1]["pocet"] if kroky else naivni_soucet

        if kroky and st.button("↩️ Odebrat posledni krok"):
            kroky.pop()
            st.rerun()

        if aktivni_pocet == 0:
            st.info("Aktualni vysledek je prazdny (0 pacientu) - dalsi krok by uz nic nenasel.")
        else:
            st.markdown(f"**Pridat dalsi krok** (bude hledat mezi aktualnimi {aktivni_pocet} pacienty):")
            n = len(kroky)  # klice ovladacich prvku jsou cislovane podle kroku, aby se nove pole zacinalo prazdne

            typ_kroku = st.radio("Typ hledani",
                                 ["Obsahuje libovolny z vyrazu (NEBO)", "Klicove slovo + cislo pod/nad hranici"],
                                 key=f"typ_kroku_{n}")
            tabulka = st.radio("Kde hledat", ["Obojí", "Jen dekurzy", "Jen anamnézy"], horizontal=True,
                               key=f"tabulka_kroku_{n}")
            tabulka_kod = {"Obojí": "OBOJI", "Jen dekurzy": "DEKURS", "Jen anamnézy": "ANAMNEZA"}[tabulka]

            over_text = None
            popis_kroku = None

            if typ_kroku == "Obsahuje libovolny z vyrazu (NEBO)":
                vyrazy_text = st.text_input("Hledane vyrazy oddelene carkou (shoda staci u JEDNOHO z nich)",
                                            placeholder="napr. NYHA II, NYHA III", key=f"vyrazy_{n}")
                vyrazy = [v.strip() for v in (vyrazy_text or "").split(",") if v.strip()]
                if vyrazy:
                    over_text = text_search.shoda_kterykoliv_vyraz(vyrazy)
                    popis_kroku = "obsahuje " + " NEBO ".join(f"„{v}“" for v in vyrazy)
                st.caption(
                    "Vyraz v kulatych zavorkach, napr. „(EF)“, se hleda jako CELE SAMOSTATNE "
                    "SLOVO - nenajde tak „efekt“ ani „telefonat“. Bez zavorek se hleda jako "
                    "podretezec - „EF“ tak najde i „efekt“."
                )
            else:
                col1, col2, col3 = st.columns(3)
                with col1:
                    klicove_slovo = st.text_input("Klicove slovo", placeholder="napr. NT-proBNP", key=f"klicslovo_{n}")
                with col2:
                    operator = st.selectbox("Porovnani", ["<", "<=", ">", ">=", "="], key=f"operator_{n}")
                with col3:
                    hranice = st.number_input("Hranicni hodnota", value=0.0, step=1.0, key=f"hranice_{n}")
                st.caption("Hleda cislo do ~20 znaku ZA klicovym slovem (napr. \"NT-proBNP: 180 pg/ml\" "
                           "-> 180). Jde o odhad z volneho textu - u nezvykleho zapisu muze shoda chybet, "
                           "doporucujeme namatkou zkontrolovat par ukazek nize.")
                if klicove_slovo.strip():
                    over_text = text_search.shoda_cislo_u_klicoveho_slova(klicove_slovo.strip(), operator, hranice)
                    popis_kroku = f"„{klicove_slovo.strip()}“ {operator} {hranice}"

            if st.button("➕ Pridat tento krok", disabled=over_text is None):
                aktualizuj_progress, skryj_progress = ukazatel_postupu()
                zaznamy = filtry.hledej_pokrocile_v_textu_mezi_pacienty(
                    aktivni_vysledek, over_text, tabulka=tabulka_kod, on_progress=aktualizuj_progress,
                    datum_od=st.session_state.casove_omezeni_od, datum_do=st.session_state.casove_omezeni_do,
                )
                skryj_progress()

                novy_vysledek = {}
                for z in zaznamy:
                    novy_vysledek.setdefault(z["databaze"], set()).add(z["idosb"])
                novy_vysledek = {nazev: sorted(ids) for nazev, ids in novy_vysledek.items()}

                kroky.append({
                    "popis": popis_kroku,
                    "vysledek": novy_vysledek,
                    "pocet": sum(len(v) for v in novy_vysledek.values()),
                    "zaznamy": zaznamy,
                })
                st.rerun()

        # --- Vysledek posledniho kroku (pokud nejaky je) ------------------------------------
        if kroky:
            posledni_krok = kroky[-1]
            st.divider()
            st.subheader(f"Vysledek po posledním kroku: {posledni_krok['pocet']} pacientu")
            if posledni_krok["pocet"] == 0:
                st.info("Zadny pacient nevyhovel vsem krokum dohromady.")
            else:
                for v in posledni_krok["zaznamy"][:15]:
                    if "nalezene_cislo" in v:
                        popisek_shody = f" [nalezeno cislo: {v['nalezene_cislo']}]"
                    elif "nalezeny_vyraz" in v:
                        popisek_shody = f" [nalezen vyraz: {v['nalezeny_vyraz']}]"
                    else:
                        popisek_shody = ""
                    st.write(f"**[{v['databaze']}]** pacient {v['idosb']} ({v['zdroj']}, {v['datum']}){popisek_shody}: "
                             f"...{v['ukazka']}...")

                st.caption("Export: jeden radek na pacienta. Kazdy krok hledani ma VLASTNI sloupec "
                           "\"Citace N (popis kroku)\" - tak jde hned videt, diky ktere citaci pacient "
                           "prosel kterym krokem. Ma-li pacient v jednom kroku vic nalezu, jsou pod sebou "
                           "v jedne bunce.")
                if st.button("📥 Exportovat vysledek posledniho kroku do CSV (s citacemi)"):
                    cesta = nova_cesta_exportu("citace_filtr_text")
                    export_zaznamy_po_krocich(kroky, cesta, ordinace_pacientu=ordinace_pro_export())
                    st.success(f"Ulozeno do: {cesta}")
