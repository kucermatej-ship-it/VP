"""
SmartMedix - filtrovani pacientu BEZ AI.

Alternativni rozhrani k ui.py/asistent.py: misto aby otazku formulovanou volnym textem
prevadela na SQL umela inteligence (a obcas se u slozitejsich dotazu spletla - spatny nazev
sloupce, spatna Firebird syntaxe, spatne propojeni tabulek...), tady si uzivatel filtry
vybira rovnou v ovladacich prvcich (posuvniky, zaskrtavatka, vyber ze seznamu). SQL se sklada
deterministicky v kodu (filtry.py) - zadny prostor pro halucinace.

AI se pouziva nanejvys volitelne az v DRUHEM kroku - pro textove hledani v dekurzech/anamnezach
mezi jiz vyfiltrovanymi pacienty (a i tam jen jako presny hledany vyraz od uzivatele, ne jako
generator SQL).
"""
import os
import datetime
import streamlit as st

import filtry
import osoby
import synonyma
import text_search
from db import EXPORT_SLOZKA
from export import export_pacienty_filtr, export_zaznamy_po_krocich


@st.cache_data(show_spinner="Nacitam seznam ordinaci ze vsech databazi...")
def nacti_ordinace_cache():
    return filtry.nacti_ordinace()


@st.cache_data(show_spinner="Nacitam ciselnik vykonu ze vsech databazi...")
def nacti_vykony_cache():
    return filtry.nacti_vykony()

st.set_page_config(page_title="SmartMedix - filtrovani pacientu", page_icon="🔎")
st.title("🔎 SmartMedix - filtrovani pacientu")
st.caption("Vyberte filtry a stisknete tlacitko Filtrovat. Zadne SQL se negeneruje pomoci AI - "
           "vysledek je vzdy presny a opakovatelny. Vse bezi lokalne, zadna data nikam neodchazeji.")

if "vysledek_filtru" not in st.session_state:
    st.session_state.vysledek_filtru = None
if "popis_filtru" not in st.session_state:
    st.session_state.popis_filtru = None
# Kazdy dalsi krok textoveho hledani hleda JEN mezi vysledkem kroku predchoziho - v efektu to
# znamena "A ZAROVEN" mezi kroky (zatimco vyrazy zadane v JEDNOM kroku pres "kterykoliv z vyrazu"
# znamenaji "NEBO"). Krok 0 je vzdy vysledek SQL filtru (vysledek_filtru), dalsi kroky jsou tady.
if "kroky_hledani" not in st.session_state:
    st.session_state.kroky_hledani = []
if "casove_omezeni_od" not in st.session_state:
    st.session_state.casove_omezeni_od = None
if "casove_omezeni_do" not in st.session_state:
    st.session_state.casove_omezeni_do = None
if "synonyma_navrhy" not in st.session_state:
    st.session_state.synonyma_navrhy = []
if "synonyma_vybrane" not in st.session_state:
    st.session_state.synonyma_vybrane = set()
if "synonyma_kolo" not in st.session_state:
    st.session_state.synonyma_kolo = 0


# --- Bocni panel: AI navrh synonym/variant k hledanym vyrazum ------------------------------
# Sem se posila JEN vyraz, ktery si uzivatel sam napise - zadna data pacientu se nikam
# neodesilaji (viz synonyma.py). Nezavisi na tom, jestli uz Krok 0 probehl - pripravovat si
# vyrazy jde kdykoliv, vlozit vybrane do pole hledani ale jde az kdyz existuje aktualni
# vysledek (Krok 0 nebo pozdejsi), do ktereho se dalsi krok hledani prida.
with st.sidebar:
    st.header("💡 Navrh klicovych slov (AI)")
    st.caption(
        "Sem se posila JEN vyraz, ktery napisete - zadna data pacientu se nikam neodesilaji. "
        "Lokalni AI navrhne mozna synonyma/zkratky/varianty zapisu, vy si vyberete, ktere se "
        "maji pouzit v textovem hledani."
    )
    synonyma_dotaz = st.text_input(
        "Vyraz, ke kteremu chcete navrhnout varianty",
        placeholder="napr. NYHA II",
        key="synonyma_dotaz",
    )
    if st.button("🔮 Navrhni synonyma"):
        if not synonyma_dotaz.strip():
            st.warning("Napiste nejdriv vyraz.")
        else:
            with st.spinner("AI premysli nad variantami..."):
                try:
                    st.session_state.synonyma_navrhy = synonyma.navrhni_synonyma(synonyma_dotaz)
                    st.session_state.synonyma_vybrane = set()
                    st.session_state.synonyma_kolo += 1
                    if not st.session_state.synonyma_navrhy:
                        st.info("AI nenabidla zadne dalsi varianty.")
                except RuntimeError as e:
                    st.error(str(e))

    if st.session_state.synonyma_navrhy:
        st.write("Vyberte, ktere chcete pouzit:")
        for navrh in st.session_state.synonyma_navrhy:
            klic_checkboxu = f"syn_chk_{st.session_state.synonyma_kolo}_{navrh}"
            zaskrtnuto = st.checkbox(navrh, key=klic_checkboxu)
            if zaskrtnuto:
                st.session_state.synonyma_vybrane.add(navrh)
            else:
                st.session_state.synonyma_vybrane.discard(navrh)

        vybrane = sorted(st.session_state.synonyma_vybrane)
        if vybrane:
            # Cil vlozeni: pole "Hledane vyrazy..." u prave rozpracovaneho (jeste neulozeneho)
            # kroku - stejne dynamicke klice ("vyrazy_N", "typ_kroku_N") jako v hlavnim tele.
            if st.session_state.kroky_hledani:
                cilovy_pocet = st.session_state.kroky_hledani[-1]["pocet"]
            elif st.session_state.vysledek_filtru is not None:
                cilovy_pocet = sum(len(v) for v in st.session_state.vysledek_filtru.values())
            else:
                cilovy_pocet = None

            if cilovy_pocet is None:
                st.caption(
                    "Nejdriv spustte Krok 0 (tlacitko \"Filtrovat pacienty\") - pak pujde "
                    "vybrane vyrazy vlozit primo do pole hledani."
                )
            elif cilovy_pocet == 0:
                st.caption("Aktualni vysledek ma 0 pacientu, dalsi krok hledani neni k dispozici.")
            else:
                cislo_kroku = len(st.session_state.kroky_hledani)
                if st.button(f"➕ Vlozit vybrane do pole hledani (Krok {cislo_kroku + 1})"):
                    klic_typu = f"typ_kroku_{cislo_kroku}"
                    klic_pole = f"vyrazy_{cislo_kroku}"
                    st.session_state[klic_typu] = "Obsahuje libovolny z vyrazu (NEBO)"
                    stavajici = st.session_state.get(klic_pole, "")
                    puvodni_vyrazy = [v.strip() for v in stavajici.split(",") if v.strip()]
                    videne = {v.casefold() for v in puvodni_vyrazy}
                    for v in vybrane:
                        if v.casefold() not in videne:
                            puvodni_vyrazy.append(v)
                            videne.add(v.casefold())
                    st.session_state[klic_pole] = ", ".join(puvodni_vyrazy)
                    st.rerun()


def cislo_nebo_none(hodnota):
    """Streamlit number_input vraci 0 i kdyz je pole 'prazdne' (min_value=0) - pouzivame
    zvlast checkbox 'pouzit tento filtr', takze tady staci hodnotu vratit tak, jak je."""
    return hodnota


st.subheader("Zakladni udaje")
col1, col2 = st.columns(2)
with col1:
    pouzit_vek = st.checkbox("Filtrovat podle veku")
    vek_od, vek_do = (0, 130)
    if pouzit_vek:
        vek_od, vek_do = st.slider("Vek (roky)", 0, 130, (40, 84))
with col2:
    pouzit_pohlavi = st.checkbox("Filtrovat podle pohlavi")
    pohlavi = None
    if pouzit_pohlavi:
        volba = st.radio("Pohlavi", ["Muz", "Zena"], horizontal=True)
        pohlavi = "M" if volba == "Muz" else "F"

pouzit_ordinace = st.checkbox("Filtrovat podle ordinace/pracoviste")
ordinace = None
if pouzit_ordinace:
    seznam_ordinaci = nacti_ordinace_cache()
    if seznam_ordinaci:
        ordinace = st.multiselect("Ordinace/pracoviste (lze vybrat vic)", options=seznam_ordinaci)
    else:
        st.info("V databazich se nenasla zadna ordinace s vyplnenym nazvem.")

pouzit_casove_obdobi = st.checkbox("Omezit obdobim (plati napric vsemi filtry s datem)")
casove_od, casove_do = None, None
if pouzit_casove_obdobi:
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

with st.expander("Telesne hodnoty (z preventivnich prohlidek)"):
    col1, col2, col3 = st.columns(3)
    with col1:
        pouzit_bmi = st.checkbox("BMI")
        bmi_od, bmi_do = (0.0, 100.0)
        if pouzit_bmi:
            bmi_od, bmi_do = st.slider("BMI", 0.0, 60.0, (18.5, 40.0), step=0.5)
        pouzit_vaha = st.checkbox("Vaha (kg)")
        vaha_od, vaha_do = (0.0, 500.0)
        if pouzit_vaha:
            vaha_od, vaha_do = st.slider("Vaha (kg)", 0.0, 250.0, (50.0, 100.0), step=1.0)
    with col2:
        pouzit_vyska = st.checkbox("Vyska (cm)")
        vyska_od, vyska_do = (0.0, 999.0)
        if pouzit_vyska:
            vyska_od, vyska_do = st.slider("Vyska (cm)", 50.0, 220.0, (150.0, 200.0), step=1.0)
        pouzit_puls = st.checkbox("Puls")
        puls_od, puls_do = (0, 999)
        if pouzit_puls:
            puls_od, puls_do = st.slider("Puls (tepu/min)", 0, 220, (50, 100))
    with col3:
        pouzit_tlaksys = st.checkbox("Tlak systolicky (horni)")
        tlaksys_od, tlaksys_do = (0, 999)
        if pouzit_tlaksys:
            tlaksys_od, tlaksys_do = st.slider("Tlak systolicky", 50, 260, (90, 140))
        pouzit_tlakdia = st.checkbox("Tlak diastolicky (dolni)")
        tlakdia_od, tlakdia_do = (0, 999)
        if pouzit_tlakdia:
            tlakdia_od, tlakdia_do = st.slider("Tlak diastolicky", 30, 160, (60, 90))

with st.expander("Diagnoza a medikace"):
    pouzit_diagnoza = st.checkbox("Filtrovat podle diagnozy")
    diagnoza = None
    if pouzit_diagnoza:
        diagnoza = st.text_input(
            "Diagnoza - kod MKN-10 nebo cast nazvu, lze zadat i vic hodnot oddelenych carkou (NEBO)",
            placeholder="napr. J069, I10, diabetes",
        )
    pouzit_medikace = st.checkbox("Filtrovat podle medikace")
    medikace = None
    if pouzit_medikace:
        medikace = st.text_input(
            "Nazev leku - lze zadat i vic hodnot oddelenych carkou (NEBO), velikost pismen nevadi",
            placeholder="napr. Atoris, warfarin, IBUPROFEN",
        )
    st.caption("POZOR: tyto dva filtry hledaji jen ve strukturovanych datech (M_STALADGN / M_MEDIREC), "
               "ne ve volnem textu poznamek - to lze udelat az v druhem kroku dole, po vyfiltrovani. "
               "Vyhledavani nazvu je vzdy necitlive na velikost pismen (Atoris = ATORIS = atoris).")

with st.expander("Vykony"):
    pouzit_vykony = st.checkbox("Filtrovat podle vykazanych vykonu")
    vykony = None
    if pouzit_vykony:
        seznam_vykonu = nacti_vykony_cache()
        if seznam_vykonu:
            vykony_moznosti = [kod for kod, _ in seznam_vykonu]
            popis_vykonu = {kod: f"{kod} - {naz}" if naz else kod for kod, naz in seznam_vykonu}
            vykony_vybrane = st.multiselect(
                "Vykon (napiste kod nebo nazev, lze vybrat vic)",
                options=vykony_moznosti,
                format_func=lambda k: popis_vykonu.get(k, k),
            )
            vykony = vykony_vybrane
        else:
            st.info("V databazich se nenasel zadny vykon s vyplnenym kodem.")

with st.expander("Administrativni udaje"):
    pouzit_poj = st.checkbox("Filtrovat podle zdravotni pojistovny")
    poj = None
    if pouzit_poj:
        poj = st.selectbox("Pojistovna", options=[k for k, _ in filtry.POJISTOVNY],
                            format_func=lambda k: dict(filtry.POJISTOVNY)[k])
    pouzit_statpris = st.checkbox("Filtrovat podle statni prislusnosti")
    statni_prislusnost = None
    if pouzit_statpris:
        statni_prislusnost = st.text_input("Statni prislusnost (nebo jeji cast)", placeholder="napr. cesk")

with st.expander("Identifikace konkretni osoby"):
    col1, col2 = st.columns(2)
    with col1:
        pouzit_rc = st.checkbox("Rodne cislo")
        rodne_cislo = None
        if pouzit_rc:
            rodne_cislo = st.text_input("Rodne cislo (s lomitkem i bez)", placeholder="960406/1622")
        pouzit_prijmeni = st.checkbox("Prijmeni")
        prijmeni = None
        if pouzit_prijmeni:
            prijmeni = st.text_input("Prijmeni (nebo jeho cast)")
    with col2:
        pouzit_jmeno = st.checkbox("Jmeno")
        jmeno = None
        if pouzit_jmeno:
            jmeno = st.text_input("Jmeno (nebo jeho cast)")
        pouzit_adresa = st.checkbox("Adresa")
        adresa = None
        if pouzit_adresa:
            adresa = st.text_input("Adresa (nebo jeji cast)", placeholder="napr. Praha 2")

st.divider()

if st.button("🔎 Filtrovat pacienty", type="primary"):
    filtry_dict = {}
    if pouzit_vek:
        filtry_dict["vek_od"], filtry_dict["vek_do"] = vek_od, vek_do
    if pouzit_pohlavi:
        filtry_dict["pohlavi"] = pohlavi
    if pouzit_ordinace and ordinace:
        filtry_dict["ordinace"] = ordinace
    if pouzit_bmi:
        filtry_dict["bmi_od"], filtry_dict["bmi_do"] = bmi_od, bmi_do
    if pouzit_vaha:
        filtry_dict["vaha_od"], filtry_dict["vaha_do"] = vaha_od, vaha_do
    if pouzit_vyska:
        filtry_dict["vyska_od"], filtry_dict["vyska_do"] = vyska_od, vyska_do
    if pouzit_tlaksys:
        filtry_dict["tlaksys_od"], filtry_dict["tlaksys_do"] = tlaksys_od, tlaksys_do
    if pouzit_tlakdia:
        filtry_dict["tlakdia_od"], filtry_dict["tlakdia_do"] = tlakdia_od, tlakdia_do
    if pouzit_puls:
        filtry_dict["puls_od"], filtry_dict["puls_do"] = puls_od, puls_do
    if pouzit_casove_obdobi:
        if casove_od:
            filtry_dict["datum_od"] = casove_od
        if casove_do:
            filtry_dict["datum_do"] = casove_do
    if pouzit_diagnoza and diagnoza:
        filtry_dict["diagnoza"] = diagnoza
    if pouzit_medikace and medikace:
        filtry_dict["medikace"] = medikace
    if pouzit_vykony and vykony:
        filtry_dict["vykony"] = vykony
    if pouzit_poj and poj:
        filtry_dict["poj"] = poj
    if pouzit_statpris and statni_prislusnost:
        filtry_dict["statni_prislusnost"] = statni_prislusnost
    if pouzit_rc and rodne_cislo:
        filtry_dict["rodne_cislo"] = rodne_cislo
    if pouzit_prijmeni and prijmeni:
        filtry_dict["prijmeni"] = prijmeni
    if pouzit_jmeno and jmeno:
        filtry_dict["jmeno"] = jmeno
    if pouzit_adresa and adresa:
        filtry_dict["adresa"] = adresa

    if not filtry_dict:
        st.warning("Nezvolili jste zadny filtr - vysledkem by byli VSICHNI pacienti ve vsech databazich. "
                   "Pokud to opravdu chcete, zaskrtnete alespon jeden filtr se sirokym rozsahem (napr. vek 0-130).")
    else:
        progress_bar = st.progress(0)
        stav_text = st.empty()

        def aktualizuj_progress(zlomek, zprava):
            progress_bar.progress(min(max(zlomek, 0.0), 1.0))
            stav_text.text(zprava)

        vysledek = filtry.filtruj_pacienty(filtry_dict, on_progress=aktualizuj_progress)
        progress_bar.empty()
        stav_text.empty()

        st.session_state.vysledek_filtru = vysledek
        st.session_state.popis_filtru = filtry_dict
        st.session_state.kroky_hledani = []
        # Casove obdobi si ulozime zvlast, aby se automaticky pouzilo i pro VSECHNY nasledujici
        # kroky textoveho hledani (dekurzy/anamnezy) - uzivatel ho pak nemusi zadavat znovu.
        st.session_state.casove_omezeni_od = casove_od if pouzit_casove_obdobi else None
        st.session_state.casove_omezeni_do = casove_do if pouzit_casove_obdobi else None
        st.rerun()

if st.session_state.vysledek_filtru is not None:
    vysledek = st.session_state.vysledek_filtru
    naivni_soucet = sum(len(v) for v in vysledek.values())
    pocet_databazi_s_vysledky = sum(1 for v in vysledek.values() if v)

    st.subheader("Vysledek filtrovani")
    if naivni_soucet == 0:
        st.info("Zadny pacient nevyhovel zadanym filtrum.")
    else:
        for nazev, ids in vysledek.items():
            st.write(f"**{nazev}**: {len(ids)} pacientu")

        if pocet_databazi_s_vysledky > 1:
            with st.spinner("Overuji rodna cisla napric databazemi (deduplikace)..."):
                skutecny_pocet, pocet_shod = osoby.pocet_unikatnich_osob(vysledek)
            st.success(f"**Celkem {skutecny_pocet} unikatnich pacientu** (po deduplikaci podle rodneho "
                       f"cisla - naivni soucet by byl {naivni_soucet}, ale {pocet_shod} zaznamu patrilo "
                       "stejne osobe ve vice databazich).")
        else:
            st.success(f"**Celkem {naivni_soucet} pacientu.**")

        if st.button("📥 Exportovat tento vysledek do CSV"):
            os.makedirs(EXPORT_SLOZKA, exist_ok=True)
            znacka_casu = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
            cesta = os.path.join(EXPORT_SLOZKA, f"pacienti_filtr_{znacka_casu}.csv")
            export_pacienty_filtr(vysledek, cesta)
            st.success(f"Ulozeno do: {cesta}")

        st.divider()
        st.subheader("Volitelne: postupne hledani v textu (kazdy dalsi krok = A ZAROVEN)")
        st.caption(
            "Kazdy krok hleda JEN mezi vysledkem kroku predchoziho (Krok 0 = vyse vyfiltrovani "
            "pacienti) - tim jde skladat slozitejsi podminky, napr. (\"NYHA II\" NEBO \"NYHA III\") "
            "A ZAROVEN (\"NT-proBNP\" pod hranici 300). Zadny AI planovac tu neni potreba."
        )

        if st.session_state.casove_omezeni_od or st.session_state.casove_omezeni_do:
            st.caption(
                f"📅 Aktivni casove omezeni: od {st.session_state.casove_omezeni_od or '(nezadano)'} "
                f"do {st.session_state.casove_omezeni_do or '(nezadano)'} - plati i pro kroky nize."
            )

        # --- Prehled dosavadnich kroku -----------------------------------------------------
        st.markdown(f"**Krok 0 (SQL filtr):** {naivni_soucet} pacientu")
        for cislo, krok in enumerate(st.session_state.kroky_hledani, start=1):
            st.markdown(f"**Krok {cislo}:** {krok['popis']} → {krok['pocet']} pacientu")

        if st.session_state.kroky_hledani:
            aktivni_vysledek = st.session_state.kroky_hledani[-1]["vysledek"]
            aktivni_pocet = st.session_state.kroky_hledani[-1]["pocet"]
        else:
            aktivni_vysledek = vysledek
            aktivni_pocet = naivni_soucet

        if st.session_state.kroky_hledani and st.button("↩️ Odebrat posledni krok"):
            st.session_state.kroky_hledani.pop()
            st.rerun()

        if aktivni_pocet == 0:
            st.info("Aktualni vysledek je prazdny (0 pacientu) - dalsi krok by uz nic nenasel.")
        else:
            st.markdown(f"**Pridat dalsi krok** (bude hledat mezi aktualnimi {aktivni_pocet} pacienty):")

            typ_kroku = st.radio(
                "Typ hledani",
                ["Obsahuje libovolny z vyrazu (NEBO)", "Klicove slovo + cislo pod/nad hranici"],
                key=f"typ_kroku_{len(st.session_state.kroky_hledani)}",
            )
            tabulka = st.radio("Kde hledat", ["Obojí", "Jen dekurzy", "Jen anamnézy"],
                                horizontal=True, key=f"tabulka_kroku_{len(st.session_state.kroky_hledani)}")
            tabulka_kod = {"Obojí": "OBOJI", "Jen dekurzy": "DEKURS", "Jen anamnézy": "ANAMNEZA"}[tabulka]

            over_text = None
            popis_kroku = None

            if typ_kroku == "Obsahuje libovolny z vyrazu (NEBO)":
                vyrazy_text = st.text_input(
                    "Hledane vyrazy oddelene carkou (shoda staci u JEDNOHO z nich)",
                    placeholder="napr. NYHA II, NYHA III",
                    key=f"vyrazy_{len(st.session_state.kroky_hledani)}",
                )
                if vyrazy_text:
                    vyrazy = [v.strip() for v in vyrazy_text.split(",") if v.strip()]
                    if vyrazy:
                        over_text = text_search.shoda_kterykoliv_vyraz(vyrazy)
                        popis_kroku = "obsahuje " + " NEBO ".join(f"„{v}“" for v in vyrazy)
                st.caption(
                    "Vyraz v kulatych zavorkach, napr. „(EF)“, se hleda jako CELE SAMOSTATNE "
                    "SLOVO - nenajde tak „efekt“ ani „telefonat“. Bez zavorek se hleda jako "
                    "podretezec (jako dosud) - „EF“ tak najde i „efekt“."
                )
            else:
                col1, col2, col3 = st.columns(3)
                with col1:
                    klicove_slovo = st.text_input("Klicove slovo", placeholder="napr. NT-proBNP",
                                                   key=f"klicslovo_{len(st.session_state.kroky_hledani)}")
                with col2:
                    operator = st.selectbox("Porovnani", ["<", "<=", ">", ">=", "="],
                                             key=f"operator_{len(st.session_state.kroky_hledani)}")
                with col3:
                    hranice = st.number_input("Hranicni hodnota", value=0.0, step=1.0,
                                               key=f"hranice_{len(st.session_state.kroky_hledani)}")
                st.caption("Hleda cislo do ~20 znaku ZA klicovym slovem (napr. \"NT-proBNP: 180 pg/ml\" "
                           "-> 180). Jde o odhad z volneho textu - u nezvykleho zapisu muze shoda chybet, "
                           "doporucujeme namatkou zkontrolovat par ukazek nize.")
                if klicove_slovo:
                    over_text = text_search.shoda_cislo_u_klicoveho_slova(klicove_slovo, operator, hranice)
                    popis_kroku = f"„{klicove_slovo}“ {operator} {hranice}"

            if st.button("➕ Pridat tento krok", disabled=over_text is None):
                progress_bar = st.progress(0)
                stav_text = st.empty()

                def aktualizuj_progress(zlomek, zprava):
                    progress_bar.progress(min(max(zlomek, 0.0), 1.0))
                    stav_text.text(zprava)

                zaznamy = filtry.hledej_pokrocile_v_textu_mezi_pacienty(
                    aktivni_vysledek, over_text, tabulka=tabulka_kod, on_progress=aktualizuj_progress,
                    datum_od=st.session_state.casove_omezeni_od, datum_do=st.session_state.casove_omezeni_do,
                )
                progress_bar.empty()
                stav_text.empty()

                unikatni_podle_databaze = {}
                for v in zaznamy:
                    unikatni_podle_databaze.setdefault(v["databaze"], set()).add(v["idosb"])
                novy_vysledek = {nazev: sorted(idset) for nazev, idset in unikatni_podle_databaze.items()}
                novy_pocet = sum(len(v) for v in novy_vysledek.values())

                st.session_state.kroky_hledani.append({
                    "popis": popis_kroku,
                    "vysledek": novy_vysledek,
                    "pocet": novy_pocet,
                    "zaznamy": zaznamy,
                })
                st.rerun()

        # --- Vysledek posledniho kroku (pokud nejaky je) ------------------------------------
        if st.session_state.kroky_hledani:
            posledni_krok = st.session_state.kroky_hledani[-1]
            st.divider()
            st.subheader(f"Vysledek po posledním kroku: {posledni_krok['pocet']} pacientu")
            if posledni_krok["pocet"] == 0:
                st.info("Zadny pacient nevyhovel vsem krokum dohromady.")
            else:
                for v in posledni_krok["zaznamy"][:15]:
                    popisek_shody = ""
                    if "nalezene_cislo" in v:
                        popisek_shody = f" [nalezeno cislo: {v['nalezene_cislo']}]"
                    elif "nalezeny_vyraz" in v:
                        popisek_shody = f" [nalezen vyraz: {v['nalezeny_vyraz']}]"
                    st.write(f"**[{v['databaze']}]** pacient {v['idosb']} ({v['zdroj']}, {v['datum']}){popisek_shody}: "
                             f"...{v['ukazka']}...")

                st.caption("Export: jeden radek na pacienta. Kazdy krok hledani ma VLASTNI sloupec "
                           "\"Citace N (popis kroku)\" - tak jde hned videt, diky ktere citaci pacient "
                           "prosel kterym krokem. Ma-li pacient v jednom kroku vic nalezu, jsou pod sebou "
                           "v jedne bunce.")
                if st.button("📥 Exportovat vysledek posledniho kroku do CSV (s citacemi)"):
                    os.makedirs(EXPORT_SLOZKA, exist_ok=True)
                    znacka_casu = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
                    cesta = os.path.join(EXPORT_SLOZKA, f"citace_filtr_text_{znacka_casu}.csv")
                    export_zaznamy_po_krocich(st.session_state.kroky_hledani, cesta)
                    st.success(f"Ulozeno do: {cesta}")
