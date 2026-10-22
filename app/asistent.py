import sys
import os
import re
import json
import datetime
import ollama

# Vsechny moduly aplikace (db.py, dotaz.py, ...) jsou ve stejne slozce jako tento soubor,
# takze zadne dalsi nastaveni cesty (sys.path) neni potreba - Python najde souborovou slozku
# tohoto skriptu automaticky, at uz je program spusteny odkudkoliv.
import text_search
from export import export_pacienty
import osoby
import dotaz
import schema_pravidla

# Nazev modelu v Ollame - na jednom miste, kvuli snadnemu vyzkouseni jineho modelu.
# Pred prvnim pouzitim je potreba mit model stazeny: v terminalu spustte "ollama pull qwen3:14b".
MODEL = "qwen3:14b"

KLICOVA_SLOVA_EXPORT = ["export", "exportuj", "seznam pacient", "tabulk", "uloz", "ulož", "csv", "vypis jmen", "vypiš jmén"]


def detekuj_export(otazka):
    """Pozna, jestli uzivatel v otazce zada o export seznamu (bez nutnosti ptat se LLM)."""
    otazka_lower = otazka.lower()
    return any(klicove_slovo in otazka_lower for klicove_slovo in KLICOVA_SLOVA_EXPORT)


def je_pravda(hodnota):
    """
    Bezpecne prevede hodnotu z JSON planu od AI na bool. AI obcas (i pres vynucene "format": "json")
    vrati boolean jako retezec, napr. "navaznost": "false" misto skutecneho false - a bool("false")
    je v Pythonu True (nazpazdny retezec), coz by omylem zapnulo navaznost na predchozi otazku.
    """
    if isinstance(hodnota, bool):
        return hodnota
    if isinstance(hodnota, str):
        return hodnota.strip().lower() in ("true", "1", "ano")
    return bool(hodnota)


PLANOVACI_PROMPT_SABLONA = """
Jsi planovac dotazu nad zdravotnickou databazi SmartMedix (cesky).

Mas k dispozici:
1) SQL nad tabulkami (JINE tabulky ani sloupce NEJSOU povolene, viz seznam nize):
__POPIS_TABULEK__
2) Textove hledani v klinickych zapisech (dekurzy a anamnezy) - hledani podretezce v textu.

POZOR: dotaz se automaticky spusti napric VICE databazemi najednou -
o to se stara aplikace, ty jen napis SQL/vyraz jednou, jako by byla databaze jen jedna.

Firebird SQL specifika: pouzij "FIRST n" misto LIMIT.
DULEZITE - pocitani veku: Firebird NEPODPORUJE syntaxi "CURRENT_DATE - INTERVAL '84 years'" (to je
z jine databaze, napr. PostgreSQL) - takovy dotaz vzdy spadne s chybou. VZDY pouzij tento presny vzorec:
  presny vek pacienta: EXTRACT(YEAR FROM CURRENT_DATE) - EXTRACT(YEAR FROM DATNAR)
  vek v rozmezi (napr. "40 az 84 let"): WHERE EXTRACT(YEAR FROM CURRENT_DATE) - EXTRACT(YEAR FROM DATNAR) BETWEEN 40 AND 84
  (BETWEEN pouzij na VYSLEDEK vypoctu veku, ne primo na DATNAR ani na zadnou syntaxi s INTERVAL)

DULEZITE - NIKDY nepouzivej "SELECT *" - vzdy vyjmenuj konkretni povolene sloupce (SELECT ID, PRIJMENI FROM ... apod.), jinak dotaz spadne s chybou.

DULEZITE - nazev sloupce identifikujiciho pacienta NENI vzdy stejny: primo v tabulce S_OSOBA se
pacient identifikuje sloupcem "ID" (S_OSOBA NEMA sloupec IDOSB!). Ve VSECH OSTATNICH tabulkach
(M_STALADGN, M_MEDIREC, L_POUKAZ, P_DOKLADPOL, G_PRAPAC...) se na pacienta odkazuje sloupcem
"IDOSB". Kdyz tyto tabulky spojujes s S_OSOBA, pouzij: JOIN S_OSOBA s ON jina_tabulka.IDOSB = s.ID
- nikdy nepis "S_OSOBA.IDOSB", takovy sloupec neexistuje. Priklad SPRAVNE: "SELECT ID FROM S_OSOBA
WHERE RODCIS = ...". Priklad SPATNE: "SELECT IDOSB FROM S_OSOBA WHERE ..." (S_OSOBA nema IDOSB).

DULEZITE - velka/mala pismena: Firebird LIKE je CASE-SENSITIVE (rozlisuje velka a mala pismena)
a nazvy leku i diagnoz mohou byt v databazi ulozene v ruznem formatu (napr. "Atoris", "ATORIS",
"atoris"). Kdykoliv porovnavas text napsany uzivatelem proti sloupci NAZEV, VZDY pouzij
UPPER(sloupec) LIKE UPPER('%hodnota%') - NIKDY jen NAZEV LIKE '%Hodnota%' primo, jinak muzes
prehlednout existujici zaznamy jen kvuli jinemu velikosti pismen.

DULEZITE - hledani diagnoz: sloupec NAZEV v M_STALADGN NENI spolehlivy (muze byt prazdny/None
nebo se lisi format). Sloupec KOD (MKN-10) je VZDY spolehlivy. Kdyz otazka zmini nekterou z
techto beznych diagnoz, VZDY pouzij KOD LIKE (ne NAZEV LIKE):
  hypertenze / vysoky tlak       -> KOD LIKE 'I1%'
  diabetes / cukrovka            -> KOD LIKE 'E1%'
  hyperlipidemie / vysoky cholesterol -> KOD LIKE 'E78%'
  obezita / nadvaha              -> KOD LIKE 'E66%'
  astma                          -> KOD LIKE 'J45%'
  CHOPN                          -> KOD LIKE 'J44%'
  ischemicka choroba srdecni     -> KOD LIKE 'I2%'
  fibrilace sini                 -> KOD LIKE 'I48%'
  deprese                        -> KOD LIKE 'F3%'
  hypotyreoza                    -> KOD LIKE 'E03%'
Pro DIAGNOZY, KTERE NEJSOU V TOMTO SEZNAMU, pouzij "UPPER(NAZEV) LIKE UPPER('%vyraz%')" jako zalozni reseni.

DULEZITE - MEDIKACE (napr. "kdo uziva Atoris", "kolik pacientu bere Warfarin"): medikaci hledej
VZDY DVEMA ZPUSOBY SOUCASNE - "typ": "sql_a_text" s polem "kombinace": "sjednoceni":
  - sql: SELECT DISTINCT IDOSB FROM M_MEDIREC WHERE UPPER(NAZEV) LIKE UPPER('%nazev leku%')
  - text_vyraz: stejny nazev leku, text_tabulka: "OBOJI" (hleda se i v dekurzech a anamnezach)
Pacient se pocita jako "uzivajici lek", kdyz je nalezen BUD v M_MEDIREC, NEBO zmineny v textu -
"kombinace": "sjednoceni" znamena, ze se vysledky SJEDNOTI (union), NE proniknou. Priklad:
{"typ": "sql_a_text", "sql": "SELECT DISTINCT IDOSB FROM M_MEDIREC WHERE UPPER(NAZEV) LIKE UPPER('%Atoris%')", "text_vyraz": "Atoris", "text_tabulka": "OBOJI", "kombinace": "sjednoceni", "navaznost": false}

DULEZITE - DIAGNOZY (napr. "kdo ma diabetes", "kdo mel v poslednim roce zlomenou nohu"): stejne
jako u medikace hledej VZDY DVEMA ZPUSOBY SOUCASNE - "typ": "sql_a_text" s "kombinace": "sjednoceni":
  - sql: hledani v M_STALADGN (podle KOD ze seznamu vyse, nebo NAZEV jako zalozni reseni)
  - text_vyraz: odpovidajici vyraz, text_tabulka: "OBOJI"
Priklad pro "kdo mel zlomenou nohu" (neni v seznamu kodu, pouzij NAZEV i text soucasne):
{"typ": "sql_a_text", "sql": "SELECT DISTINCT IDOSB FROM M_STALADGN WHERE UPPER(NAZEV) LIKE UPPER('%zlomenina%')", "text_vyraz": "zlomenou nohu", "text_tabulka": "OBOJI", "kombinace": "sjednoceni", "navaznost": false}

POZOR - pole "kombinace": "sjednoceni" pouzivej JEN kdyz sql i text_vyraz hledaji TOTOZNE
kriterium (stejny lek, stejnou diagnozu) dvema ruznymi zpusoby kvuli lepsimu pokryti dat. Kdyz
otazka kombinuje DVE RUZNA kriteria (napr. "pacienti 60-70 let, kteri maji v dekurzu zminku o
bolesti na hrudi" - vek pres SQL, symptom pres text), pole "kombinace" VYNECHEJ (nebo nastav na
"prunik") - tam SQL vysledek slouzi jako FILTR pro text (jen mezi temito pacienty se hleda v
textu), ne jako druhy nezavisly zdroj stejneho kriteria.

DULEZITE - ordinace/pracoviste, ve kterem je pacient registrovan: otazky typu "kde je pacient
registrovan?", "kolik pacientu z ordinace Vinohrady...", "filtruj pacienty z ordinace Zizkov..."
se tykaji tabulky G_PRA (NE S_OSOBA primo). Pacient je na ordinaci navazan pres
S_OSOBA.IDOSBPRA = G_PRA.ID. Nazev ordinace hledej vzdy pres UPPER(G_PRA.NAZEV) LIKE UPPER('%nazev%')
kvuli velikosti pismen. Priklad SQL pro "kolik pacientu z ordinace Vinohrady":
SELECT DISTINCT s.ID FROM S_OSOBA s JOIN G_PRA g ON s.IDOSBPRA = g.ID WHERE UPPER(g.NAZEV) LIKE UPPER('%Vinohrady%')
Pro otazku "kde je pacient registrovan" (zjisteni nazvu ordinace, ne pocitani pacientu) vrat naopak
NAZEV (a pripadne ICP) ordinace, ne jen ID: SELECT g.NAZEV, g.ICP FROM S_OSOBA s JOIN G_PRA g ON s.IDOSBPRA = g.ID WHERE s.ID = ...

DULEZITE - identifikace konkretni osoby podle jmena/RC/kontaktu (S_OSOBA):
  - RODNE CISLO (RODCIS): v databazi je ulozene BEZ lomitka (jen cislice). Uzivatel ho ale casto
    napise S lomitkem (napr. "960406/1622") - v SQL VZDY porovnavej bez lomitka na obou stranach:
    REPLACE(RODCIS, '/', '') = '9604061622'  (lomitko z hodnoty, kterou dostanes, proste odeber)
  - PRIJMENI, JMENO: VZDY UPPER(sloupec) LIKE UPPER('%hodnota%') kvuli velikosti pismen,
    napr. "vsichni s prijmenim Novak" -> UPPER(PRIJMENI) LIKE UPPER('%Novak%')
  - TITUL: VZDY UPPER(TITUL) LIKE UPPER('%hodnota%'), napr. "pacienti s titulem Mgr" ->
    UPPER(TITUL) LIKE UPPER('%Mgr%')
  - ADRESA: volny text bydliste, VZDY UPPER(ADRESA) LIKE UPPER('%hodnota%'),
    napr. "pacienti bydlici na Praze 2" -> UPPER(ADRESA) LIKE UPPER('%Praha 2%')
  - TEL, MAIL: telefon a e-mail, hledej primo (LIKE pro castecnou shodu, = pro presnou)

DULEZITE - statni prislusnost (STATPRIS): S_OSOBA.STATPRIS je KOD, ne text - spoj s C_STATPRIS
a filtruj podle NAZEV (kod predem neznas): SELECT DISTINCT s.ID FROM S_OSOBA s
JOIN C_STATPRIS c ON s.STATPRIS = c.KOD WHERE UPPER(c.NAZEV) LIKE UPPER('%česk%')

DULEZITE - zdravotni pojistovna (POJ): S_OSOBA.POJ je 3-mistny kod. Kdyz uzivatel jmenuje
pojistovnu nazvem/zkratkou, preloz na kod primo (NEPOTREBUJES zadnou dalsi tabulku):
  VZP  (Vseobecna zdravotni pojistovna CR)                                      -> POJ = '111'
  VoZP (Vojenska zdravotni pojistovna CR)                                       -> POJ = '201'
  CPZP (Ceska prumyslova zdravotni pojistovna)                                  -> POJ = '205'
  OZP  (Oborova zdravotni pojistovna zamestnancu bank, pojistoven a stavebnictvi) -> POJ = '207'
  ZPS  (Zamestnanecka pojistovna SKODA)                                         -> POJ = '209'
  ZPMV (Zdravotni pojistovna ministerstva vnitra CR)                            -> POJ = '211'
  RBP  (RBP, zdravotni pojistovna, drive Revirni bratrska pokladna)             -> POJ = '213'

DULEZITE - vysledky vysetreni napojene na konkretniho pacienta (napr. "prumerna hodnota glykemie
u pacientu s diabetem"): L_VYSLEDKY NEMA odkaz na pacienta primo, jde pres poukaz:
L_VYSLEDKY.IDPOU = L_POUKAZ.ID a L_POUKAZ.IDOSB = S_OSOBA.ID. Priklad:
SELECT AVG(l.NVAL) FROM L_VYSLEDKY l JOIN L_POUKAZ p ON l.IDPOU = p.ID
WHERE p.IDOSB IN (SELECT IDOSB FROM M_STALADGN WHERE KOD LIKE 'E1%') AND UPPER(l.NAZEV) LIKE UPPER('%glykemie%')

DULEZITE - telesne hodnoty namerene pri prohlidce (BMI, krevni tlak, puls, vyska, vaha): tyto
NIKDY nehledej v L_VYSLEDKY (to jsou LABORATORNI vysledky - krev, moc apod., BMI/tlak/puls tam
NEJSOU) - VZDY pouzij tabulku M_PRISTAV, ktera ma tyto hodnoty primo jako sloupce primo napojene
na pacienta pres IDOSB, zadny JOIN na poukaz neni potreba. Priklad pro "pacienti s BMI 18 az 40":
SELECT DISTINCT IDOSB FROM M_PRISTAV WHERE BMI BETWEEN 18 AND 40
Priklad pro "pacienti s vysokym tlakem": SELECT DISTINCT IDOSB FROM M_PRISTAV WHERE TLAKSYS >= 140

DULEZITE - provedene vykony (napr. "kolik pacientu melo vykon X"): P_DOKLADPOL.IDOSB je pacient,
P_DOKLADPOL.KODVYK je kod vykonu. Cely nazev vykonu ziskas spojenim s C_VYKONY.KOD NEBO
CP_VYKONY.KOD (kod nemusi byt v obou ciselnicich - pouzij LEFT JOIN a COALESCE). Priklad:
SELECT DISTINCT d.IDOSB FROM P_DOKLADPOL d
LEFT JOIN C_VYKONY cv ON d.KODVYK = cv.KOD
LEFT JOIN CP_VYKONY cpv ON d.KODVYK = cpv.KOD
WHERE UPPER(COALESCE(cv.NAZ, cpv.NAZ)) LIKE UPPER('%vyraz%')

DULEZITE - otazky "kolik PACIENTU/LIDI...": databaze muze obsahovat VICE databazi najednou.
Pro TYTO otazky NIKDY nepouzivej COUNT(*) ani COUNT(DISTINCT ...) - misto toho VZDY vrat SEZNAM
ID pacientu:
  - dotaz jen nad S_OSOBA (napr. "kolik je zen/muzu") -> SELECT DISTINCT ID FROM S_OSOBA WHERE ...
  - dotaz nad ostatnimi tabulkami (diagnozy, leky, vysledky) -> SELECT DISTINCT IDOSB FROM ... WHERE ...
Aplikace si pocty spocita sama a spravne deduplikuje stejne osoby napric ruznymi databazemi
(podle rodneho cisla) - to SQL COUNT neumi.

Pro JINE typy otazek (prumery, soucty, pocty zaznamu/navstev/receptu BEZ vztahu k poctu
unikatnich pacientu, napr. "jaky je prumerny vysledek sodiku") muzes normalne pouzit
COUNT(*)/AVG/SUM/atd.

Podle otazky rozhodni, co je potreba, a odpovez POUZE JSON (zadny dalsi text, zadne ```), presne v tomto tvaru:
{"typ": "sql", "sql": "SELECT ...", "text_vyraz": null, "text_tabulka": null, "navaznost": false}

nebo:
{"typ": "text", "sql": null, "text_vyraz": "hledany vyraz", "text_tabulka": "DEKURS", "navaznost": false}

nebo (kombinace jako FILTR - SQL zuzi pacienty, pak se mezi nimi hleda JINE kriterium v textu):
{"typ": "sql_a_text", "sql": "SELECT DISTINCT IDOSB FROM ...", "text_vyraz": "hledany vyraz", "text_tabulka": "OBOJI", "kombinace": "prunik", "navaznost": false}

nebo (kombinace jako SJEDNOCENI - SQL a text hledaji TOTOZNE kriterium dvema zpusoby, napr. medikace/diagnoza):
{"typ": "sql_a_text", "sql": "SELECT DISTINCT IDOSB FROM ...", "text_vyraz": "hledany vyraz", "text_tabulka": "OBOJI", "kombinace": "sjednoceni", "navaznost": false}

Pravidla:
- "sql": otazka jde zodpovedet cistě strukturovanymi daty (pocty, prumery, diagnozy, leky, vysledky).
- "text": otazka hleda neco, co je jen ve volnem textu (symptom, poznamka lekare, ktera neni v kodech).
- "sql_a_text": kombinace SQL a textoveho hledani - vzdy urci i pole "kombinace" (viz vyse a podrobnosti u MEDIKACE/DIAGNOZY nize). Kdyz "kombinace" vynechas, pouzije se "prunik".
- text_tabulka: "DEKURS" (zapisy z navstev), "ANAMNEZA" (osobni/rodinna anamneza), nebo "OBOJI".

NAVAZNOST NA PREDCHOZI DOTAZ:
Pokud dostanes zpravu "KONTEXT PREDCHOZIHO DOTAZU" (predchozi otazka + pocet pacientu, kteri
vyhovovali), a aktualni otazka na ni odkazuje slovy jako "z nich", "z techto pacientu", "mezi nimi",
"kolik z vyse zminenych", "dale", "a kolik z nich" apod., pridej do JSON navic pole "navaznost": true
a naplanuj SQL/text_vyraz POUZE pro NOVE kriterium v aktualni otazce (napr. jen "uziva Atoris").
Filtr na predchozi skupinu pacientu se aplikuje automaticky mimo tebe - NEPRIDAVEJ ho sam do SQL
a neopakuj v SQL puvodni podminku (napr. vek), jinak by se prunik udelal dvakrat zbytecne.
Pokud otazka na predchozi kontext neodkazuje (je to nova, nezavisla otazka), nastav "navaznost": false.
Kdyz zadny KONTEXT PREDCHOZIHO DOTAZU neprijde, "navaznost" vzdy false.
"""

# Popis povolenych tabulek/sloupcu se dosazuje az tady (str.replace, ne f-string) - JSON ukazky
# nahore obsahuji { } zavorky, ktere by se s f-stringem musely komplikovane escapovat.
PLANOVACI_PROMPT = PLANOVACI_PROMPT_SABLONA.replace(
    "__POPIS_TABULEK__", schema_pravidla.popis_pro_prompt()
)


def popis_kontextu_pro_llm(kontext):
    """Sestavi kratky textovy popis predchoziho dotazu pro planovac (kolik pacientu vyhovovalo, podle databaze)."""
    if not kontext or not kontext.get("pacienti"):
        return None
    pocty = ", ".join(f"{n}: {len(ids)}" for n, ids in kontext["pacienti"].items())
    return (
        "KONTEXT PREDCHOZIHO DOTAZU:\n"
        f"Predchozi otazka: {kontext['otazka']}\n"
        f"Pocet pacientu, kteri predchozi otazce vyhovovali (podle databaze): {pocty}"
    )


def _je_plan_pouzitelny(plan):
    """
    Overi, ze plan od AI ma pouzitelny tvar - obsahuje aspon jedno ze dvou ocekavanych kriterii
    ("sql" nebo "text_vyraz"). Bez teto kontroly by se mohlo stat (a v praxi se stalo), ze AI vrati
    JSON v jinem tvaru, nez je pozadovano (napr. s klicem "query" misto "sql", nebo odkazujici na
    neexistujici tabulku) - takovy plan by pak tise prosel jako "zadne kriterium k vyhledani" a
    aplikace by nesmyslne odpovedela "zadna data nenalezena", aniz by se cokoliv skutecne prohledalo.
    """
    return isinstance(plan, dict) and (bool(plan.get("sql")) or bool(plan.get("text_vyraz")))


def ziskej_plan(otazka, kontext=None):
    zpravy = [{"role": "system", "content": PLANOVACI_PROMPT}]
    popis = popis_kontextu_pro_llm(kontext)
    if popis:
        zpravy.append({"role": "system", "content": popis})
    zpravy.append({"role": "user", "content": otazka})

    text = None
    # Az DVA pokusy - mensi modely obcas i pres "format": "json" vratej JSON v jinem tvaru, nez je
    # pozadovano (spatne nazvy poli apod.). Pri prvnim neuspechu se zeptame znovu s upozornenim,
    # co presne bylo spatne, misto abychom rovnou vzdali nebo (horsi) tise pokracovali s prazdnym planem.
    for pokus in range(2):
        odpoved = ollama.chat(
            model=MODEL,
            messages=zpravy,
            format="json",
            keep_alive="30m",
        )
        text = odpoved["message"]["content"]
        shoda = re.search(r"\{.*\}", text, re.DOTALL)

        if shoda:
            try:
                plan = json.loads(shoda.group(0))
            except json.JSONDecodeError:
                plan = None
        else:
            plan = None

        if plan is not None and _je_plan_pouzitelny(plan):
            return plan

        if pokus == 0:
            zpravy.append({"role": "assistant", "content": text})
            zpravy.append({"role": "user", "content": (
                "Tvoje predchozi odpoved nebyla pouzitelna - bud nebyla platny JSON, nebo neobsahovala "
                "pole \"sql\" ani \"text_vyraz\". Odpovez ZNOVA POUZE JSON planem, presne v tvaru "
                "popsanem v systemove zprave: s poli \"typ\", \"sql\", \"text_vyraz\", \"text_tabulka\", "
                "\"navaznost\" - zadny dalsi text, zadne ```."
            )})

    raise ValueError(
        "AI nevratila pouzitelny plan dotazu (chybi pole 'sql' i 'text_vyraz') ani po opakovanem "
        f"pokusu. Zkuste otazku preformulovat. Posledni odpoved od AI:\n{text}"
    )


def ziskej_idosb_z_vysledku(sloupce, radky):
    sloupce_upper = [s.upper() for s in sloupce]
    for nazev in ("IDOSB", "ID"):
        if nazev in sloupce_upper:
            idx = sloupce_upper.index(nazev)
            return [row[idx] for row in radky]
    return None


def zformuluj_odpoved(otazka, evidence_text, mysleni=True):
    odpoved = ollama.chat(
        model=MODEL,
        think=mysleni,
        keep_alive="30m",
        messages=[
            {"role": "system", "content": (
                "Odpovez cesky, hovorove a prirozene, jako byste to rikali kolegovi - "
                "kratkymi vetami, bez zbytecnych odrazek a nadpisu, jen 2-4 vety souvisleho textu. "
                "DULEZITE: pocet 'zaznamu' NENI totez co pocet 'pacientu' - jeden pacient muze "
                "mit vic zaznamu. POKUD ale poskytnuta data vyslovne rikaji, ze SQL dotaz jiz "
                "pouziva COUNT(DISTINCT ...), pak vysledne cislo UZ JE spravny pocet unikatnich "
                "pacientu a NEPRIDAVEJ zbytecne varovani o zaznamech vs. pacientech. "
                "Data mohou pochazet z VICE databazi najednou. "
                "POKUD evidence obsahuje radek 'DEDUPLIKACE PODLE RODNEHO CISLA', deduplikace UZ PROBEHLA - "
                "pouzij cislo oznacene jako 'SKUTECNY POCET UNIKATNICH OSOB' jako hlavni odpoved a "
                "NEVAROVAT znovu pred vicenasobnym zapocitanim, uz je to vyreseno. "
                "Pokud takovy radek NENI, ale soucty jsou rozepsane po databazich, strucne zminí, "
                "ze stejny clovek navstevujici vice databazi muze byt zapocitan vicekrat. "
                "POKUD evidence obsahuje radek 'POZOR - U NEKTERYCH DATABAZI SE DOTAZ NEPOVEDL', strucne "
                "na to uzivatele upozorni (vysledek muze byt neuplny) - nezamlcuj to. "
                "Na uplny konec pripoj jeden kratky radek se zdrojem (tucne 'Zdroj:'). Nevymyslej si nic navic."
            )},
            {"role": "user", "content": f"Otazka: {otazka}\n\nData:\n{evidence_text}"},
        ],
    )
    return odpoved["message"]["content"]


def aplikuj_navaznost(finalni_pacienti, kontext):
    """
    Pokud je aktualni dotaz navazujici na predchozi otazku, provede prunik seznamu pacientu
    S KAZDOU DATABAZI ZVLAST (ID pacientu jsou mezi databazemi neporovnatelna, viz db.py).
    - finalni_pacienti: vysledek aktualniho dotazu (dict {databaze: [idosb,...]}) nebo None,
      pokud aktualni otazka mela jen navazujici kriterium bez vlastniho SQL/textu.
    - kontext: {"otazka": str, "pacienti": {databaze: [idosb,...]}}
    """
    predchozi = kontext.get("pacienti") or {}
    if finalni_pacienti is None:
        # Otazka sama o sobe nevratila zadny seznam (napr. jen "a kolik z nich?") - vrat predchozi seznam beze zmeny.
        return {n: list(ids) for n, ids in predchozi.items()}
    vysledek = {}
    for nazev, ids in finalni_pacienti.items():
        predchozi_ids = set(predchozi.get(nazev, []))
        vysledek[nazev] = [i for i in ids if i in predchozi_ids]
    return vysledek


def exportuj_pacienty_z_vysledku(pacienti, navazujici=False, spojeni=None):
    """
    Ulozi CSV z jiz spocitaneho seznamu pacientu (dict {databaze: [idosb,...]}).
    Pouziva se jak pri automatickem exportu behem zeptej_se(), tak dodatecne -
    napr. z tlacitka v UI, kdyz uzivatel vidi vysledek a AZ POTOM se rozhodne exportovat.
    Vraci cestu k souboru, nebo None, pokud neni co exportovat.

    spojeni: volitelny dict {nazev_databaze: jiz_otevrene_pripojeni} - viz export.export_pacienty().
    """
    if not pacienti or not any(pacienti.values()):
        return None
    from db import EXPORT_SLOZKA
    os.makedirs(EXPORT_SLOZKA, exist_ok=True)
    znacka_casu = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    pripona_nazvu = "_navazujici" if navazujici else ""
    cesta = os.path.join(EXPORT_SLOZKA, f"pacienti_{znacka_casu}{pripona_nazvu}.csv")
    export_pacienty(pacienti, cesta, spojeni=spojeni)
    return cesta


def zeptej_se(otazka, exportovat=True, on_progress=None, kontext=None):
    def hlas(zlomek, zprava):
        zlomek = min(max(zlomek, 0.0), 1.0)
        if on_progress:
            on_progress(zlomek, zprava)
        print(f"[{int(zlomek * 100)}%] {zprava}")

    print(f"\n>>> Otazka: {otazka}")

    hlas(0.02, "Hledam dostupne databaze...")
    from db import seznam_databazi, get_connection
    databaze = seznam_databazi()
    if not databaze:
        print("Ve slozce data/ nebyl nalezen zadny .FDB soubor.")
        hlas(1.0, "Hotovo (zadna databaze nenalezena).")
        return
    print(f"--- Nalezene databaze: {', '.join(o['nazev'] for o in databaze)} ---")

    if kontext:
        print(f"--- Navazuji na predchozi otazku: '{kontext['otazka']}' "
              f"({sum(len(v) for v in kontext['pacienti'].values())} pacientu celkem) ---")

    # Pripojeni ke vsem databazim otevreme JEDNOU na zacatku a pouzijeme opakovane pro SQL dotaz,
    # textove hledani, deduplikaci i export - misto aby se k tem samym souborum pripojovalo
    # a odpojovalo znovu pro kazdy krok zvlast. Kdyz pripojeni k nektere databazi selze (napr.
    # poskozeny/zamceny soubor), tuto jednu databazi pro danou otazku jen preskocime a pokracujeme
    # s ostatnimi - misto aby kvuli jedne vadne databazi spadla cela odpoved.
    hlas(0.04, f"Nalezeno {len(databaze)} databazi. Pripojuji se k nim...")
    spojeni = {}
    selhane_databaze = []
    for o in databaze:
        try:
            spojeni[o["nazev"]] = get_connection(o["cesta"])
        except Exception as chyba:
            print(f"!!! Databaze {o['nazev']}: pripojeni se nepovedlo, pro tuto otazku se preskoci ({chyba})")
            selhane_databaze.append((o["nazev"], "pripojeni", str(chyba)))

    try:
        hlas(0.06, "Ptam se AI na plan dotazu...")
        plan = ziskej_plan(otazka, kontext=kontext)
        hlas(0.15, "Plan pripraven.")
        print(f"--- Plan od Qwen ---\n{json.dumps(plan, ensure_ascii=False, indent=2)}\n")
        je_navazujici = je_pravda(plan.get("navaznost")) and kontext is not None

        idosb_filtr_pro_text = None
        idosb_z_sql = None
        finalni_pacienti = None
        evidence = []

        kombinace = plan.get("kombinace")
        if kombinace not in ("prunik", "sjednoceni"):
            # Chybejici nebo neceakana hodnota - bezpecny/puvodni vychozi je "prunik" (SQL jako filtr).
            kombinace = "prunik"

        ma_sql = bool(plan.get("sql"))
        ma_text = bool(plan.get("text_vyraz"))
        if ma_sql and ma_text:
            podil_sql, podil_text = 0.25, 0.50
        elif ma_sql:
            podil_sql, podil_text = 0.75, 0.0
        elif ma_text:
            podil_sql, podil_text = 0.0, 0.75
        else:
            podil_sql, podil_text = 0.0, 0.0

        if plan.get("sql"):
            dotaz.over_sql(plan["sql"])
            vysledky_podle_databaze = {o["nazev"]: [] for o in databaze}
            sloupce = None
            for i, o in enumerate(databaze):
                nazev = o["nazev"]
                hlas(0.15 + podil_sql * (i / len(databaze)), f"SQL dotaz: databaze {nazev} ({i + 1}/{len(databaze)})")
                if nazev not in spojeni:
                    continue
                try:
                    s, radky = dotaz.spust_sql_s_pripojenim(plan["sql"], spojeni[nazev])
                except Exception as chyba:
                    print(f"!!! Databaze {nazev}: SQL dotaz selhal, databaze se pro tuto otazku preskoci ({chyba})")
                    selhane_databaze.append((nazev, "SQL dotaz", str(chyba)))
                    continue
                sloupce = s
                vysledky_podle_databaze[nazev] = radky
            hlas(0.15 + podil_sql, "SQL dotazy dokonceny.")
            celkem_radku = sum(len(r) for r in vysledky_podle_databaze.values())
            print(f"--- SQL vysledek napric {len(vysledky_podle_databaze)} databazemi, celkem {celkem_radku} radku ---")
            for nazev, radky in vysledky_podle_databaze.items():
                print(f"  {nazev}: {len(radky)} radku, ukazka: {radky[:3]}")
            sql_upper_bez_mezer = plan["sql"].upper().replace(" ", "")
            pouzival_distinct = "COUNT(DISTINCT" in sql_upper_bez_mezer
            jen_s_osoba = bool(re.search(r"FROM\s+S_OSOBA\b", plan["sql"], re.IGNORECASE)) and "JOIN" not in plan["sql"].upper()
            if pouzival_distinct or jen_s_osoba:
                poznamka_distinct = (
                    "Tento SQL dotaz JIZ vraci spravny pocet UNIKATNICH pacientu "
                    "(bud pouziva COUNT(DISTINCT...), nebo se dotazuje jen na S_OSOBA, "
                    "kde je presne jeden radek na pacienta) - vysledek tedy JE pocet pacientu, "
                    "zadne dalsi varovani ani deduplikace neni potreba."
                )
            else:
                poznamka_distinct = "Tento SQL dotaz nepouziva DISTINCT ani se netyka jen S_OSOBA - vysledek muze byt pocet zaznamu, ne nutne unikatnich pacientu."
            evidence.append(
                f"Pouzity SQL dotaz: {plan['sql']}. "
                f"{poznamka_distinct} "
                f"Sloupce: {sloupce}. Celkem {celkem_radku} radku. "
                "Skutecne vysledky (hodnoty) podle databaze: " +
                "; ".join(f"{n}: {radky[:10]}" for n, radky in vysledky_podle_databaze.items())
            )

            if sloupce is None:
                # Ani jedna databaze SQL dotaz uspesne nedokoncila.
                idosb_podle_databaze_sql = {nazev: [] for nazev in vysledky_podle_databaze}
            else:
                idosb_podle_databaze_sql = {
                    nazev: (ziskej_idosb_z_vysledku(sloupce, radky) or [])
                    for nazev, radky in vysledky_podle_databaze.items()
                }

            # O tom, jestli se ma SQL vysledek pouzit jako filtr pro nasledujici textove hledani,
            # rozhodujeme podle toho, co plan SKUTECNE obsahuje (sql I text_vyraz), ne podle pole
            # "typ" - kdyby si tato dve pole u AI planu nekdy neodpovidala, puvodni kod by tise
            # zahodil SQL vysledek a prepsal ho vysledkem textoveho hledani bez filtru.
            idosb_z_sql = idosb_podle_databaze_sql
            if plan.get("sql") and plan.get("text_vyraz"):
                if kombinace == "sjednoceni":
                    # SQL a text hledaji TOTOZNE kriterium (napr. medikace, diagnoza) dvema zpusoby -
                    # text se NEOMEZUJE na SQL vysledek, oba se spocitaji nezavisle a az po textovem
                    # hledani se vysledky SJEDNOTI (viz nize), ne proreznou.
                    print("--- Kombinace 'sjednoceni': SQL i text se hledaji nezavisle, vysledky se pak sjednoti ---")
                else:
                    idosb_filtr_pro_text = idosb_podle_databaze_sql
                    celkem_kandidatu = sum(len(v) for v in idosb_podle_databaze_sql.values())
                    print(f"--- Pouziji celkem {celkem_kandidatu} pacientu (napric databazemi) jako filtr pro text ---")
            elif any(idosb_podle_databaze_sql.values()):
                finalni_pacienti = idosb_podle_databaze_sql

        # Navazujici otazka s textovym hledanim (napr. "kolik z nich ma v dekurzu zminku o X?") -
        # seznam pacientu z PREDCHOZI otazky pouzijeme jako filtr uz PRED samotnym textovym hledanim,
        # ne az po nem. Bez tohoto kroku by se textove hledani spustilo nad CELOU databazi (nebo nad
        # celym vysledkem noveho SQL kriteria, pokud v teto otazce nejake je) a teprve nakonec by se
        # vysledek proriznul s predchozi skupinou - zbytecne pomale a navic to zpusobuje, ze progress
        # bar ukazuje prohledavani desitek tisic zaznamu misto jen tech nekolika desitek/stovek
        # pacientu, na ktere se uzivatel ptal.
        if je_navazujici and plan.get("text_vyraz"):
            predchozi_pacienti = kontext.get("pacienti") or {}
            if idosb_filtr_pro_text is not None:
                idosb_filtr_pro_text = {
                    nazev: [i for i in ids if i in set(predchozi_pacienti.get(nazev, []))]
                    for nazev, ids in idosb_filtr_pro_text.items()
                }
            else:
                idosb_filtr_pro_text = {nazev: list(ids) for nazev, ids in predchozi_pacienti.items()}
            celkem_kandidatu = sum(len(v) for v in idosb_filtr_pro_text.values())
            print(f"--- Navazujici textove hledani: filtruji uz PRED hledanim na {celkem_kandidatu} pacientu z predchozi otazky ---")

        if plan.get("text_vyraz"):
            vyraz = plan["text_vyraz"]
            tabulka = plan.get("text_tabulka") or "OBOJI"
            if tabulka not in ("DEKURS", "ANAMNEZA", "OBOJI"):
                # AI vratila neceakavou hodnotu - bezpecny fallback je prohledat obe tabulky.
                tabulka = "OBOJI"
            vysledky_textu = []
            zacatek_progress = 0.15 + podil_sql
            zlomek_na_databazi = (podil_text / len(databaze)) if databaze else 0

            # Ktere tabulky se maji prohledat - dvojice (popisek pro uzivatele, funkce z text_search.py,
            # znacka zdroje pro vysledky). Kvuli progresu je potreba vedet, kolik tabulek se bude pro
            # danou databazi prohledavat (1 nebo 2), aby se dal jejich podil na progres baru rozpocitat.
            tabulky_k_prohledani = []
            if tabulka in ("DEKURS", "OBOJI"):
                tabulky_k_prohledani.append(("dekurzy", text_search.hledej_v_dekurzech_jedna, "dekurz"))
            if tabulka in ("ANAMNEZA", "OBOJI"):
                tabulky_k_prohledani.append(("anamnézy", text_search.hledej_v_anamnezach_jedna, "anamneza"))

            for i, o in enumerate(databaze):
                nazev = o["nazev"]
                zaklad_databaze = zacatek_progress + zlomek_na_databazi * i
                hlas(zaklad_databaze, f"Textove hledani: databaze {nazev} ({i + 1}/{len(databaze)})")
                if nazev not in spojeni:
                    continue
                idosb_filtr = None
                if idosb_filtr_pro_text is not None:
                    idosb_filtr = idosb_filtr_pro_text.get(nazev, [])
                    if not idosb_filtr:
                        continue
                try:
                    for tabulka_i, (popis_tabulky, hledejici_funkce, zdroj) in enumerate(tabulky_k_prohledani):
                        zaklad_tabulky = zaklad_databaze + zlomek_na_databazi * (tabulka_i / len(tabulky_k_prohledani))
                        zlomek_na_tabulku = zlomek_na_databazi / len(tabulky_k_prohledani)

                        def progres(zpracovano, celkem, zaklad_tabulky=zaklad_tabulky,
                                    zlomek_na_tabulku=zlomek_na_tabulku, popis_tabulky=popis_tabulky, nazev=nazev):
                            hlas(
                                zaklad_tabulky + zlomek_na_tabulku * (zpracovano / celkem),
                                f"Textove hledani: databaze {nazev}, {popis_tabulky} - hotovo {int(100 * zpracovano / celkem)} % ({zpracovano}/{celkem})"
                            )

                        for v in hledejici_funkce(spojeni[nazev], vyraz, idosb_seznam=idosb_filtr, on_progress=progres):
                            vysledky_textu.append(dict(v, zdroj=zdroj, databaze=nazev))
                except Exception as chyba:
                    print(f"!!! Databaze {nazev}: textove hledani selhalo, databaze se pro tuto otazku preskoci ({chyba})")
                    selhane_databaze.append((nazev, "textove hledani", str(chyba)))
            hlas(zacatek_progress + podil_text, "Textove hledani dokonceno.")

            unikatni_podle_databaze = {}
            for v in vysledky_textu:
                unikatni_podle_databaze.setdefault(v["databaze"], set()).add(v["idosb"])
            celkem_unikatnich = sum(len(s) for s in unikatni_podle_databaze.values())
            vysledek_z_textu = {nazev: sorted(idset) for nazev, idset in unikatni_podle_databaze.items()}

            print(f"--- Textove hledani: {len(vysledky_textu)} zaznamu celkem, {celkem_unikatnich} unikatnich pacientu (soucet napric databazemi) ---")
            for nazev, idset in unikatni_podle_databaze.items():
                print(f"  {nazev}: {len(idset)} unikatnich pacientu")
            for v in vysledky_textu[:5]:
                print(f"  [{v['databaze']}] pacient {v['idosb']} ({v['zdroj']}, {v['datum']}): ...{v['ukazka']}...")

            if kombinace == "sjednoceni" and idosb_z_sql is not None:
                # SQL a text hledaly TOTOZNE kriterium (napr. medikace/diagnoza) dvema zpusoby -
                # vysledny pacient je "nalezen", pokud vyhovel SQL NEBO textu (sjednoceni, ne prunik).
                vsechny_databaze = set(vysledek_z_textu) | set(idosb_z_sql)
                finalni_pacienti = {
                    nazev: sorted(set(vysledek_z_textu.get(nazev, [])) | set(idosb_z_sql.get(nazev, [])))
                    for nazev in vsechny_databaze
                }
                celkem_sql = sum(len(v) for v in idosb_z_sql.values())
                celkem_po_sjednoceni = sum(len(v) for v in finalni_pacienti.values())
                print(f"--- Sjednoceni SQL ({celkem_sql} pacientu) a textu ({celkem_unikatnich} pacientu): "
                      f"celkem {celkem_po_sjednoceni} unikatnich pacientu (pred deduplikaci RC) ---")
                evidence.append(
                    f"Kriterium '{plan['text_vyraz']}' bylo hledano DVEMA ZPUSOBY SOUCASNE a vysledky "
                    f"byly SJEDNOCENY (union, ne prunik): strukturovane SQL data nasla {celkem_sql} pacientu, "
                    f"volny text (dekurzy/anamnezy) nasel {celkem_unikatnich} pacientu, po sjednoceni "
                    f"(pacient se pocita, pokud vyhovel ALESPON jednomu ze zpusobu) je to {celkem_po_sjednoceni} "
                    "pacientu celkem (soucet napric databazemi, pred deduplikaci podle rodneho cisla). "
                    "Ukazky z textoveho hledani: " + (" | ".join(f"[{v['databaze']}] {v['ukazka']}" for v in vysledky_textu[:5]) or "zadne")
                )
            else:
                finalni_pacienti = vysledek_z_textu
                evidence.append(
                    f"Textove hledani vyrazu '{plan['text_vyraz']}' naslo {len(vysledky_textu)} zaznamu celkem "
                    f"u {celkem_unikatnich} unikatnich pacientu (soucet napric databazemi; POZOR: stejny clovek "
                    "navstevujici vice databazi muze byt zapocitan vicekrat, protoze ID pacientu nejsou mezi "
                    "databazemi porovnatelna). Rozpad podle databazi: " +
                    ", ".join(f"{n}: {len(s)}" for n, s in unikatni_podle_databaze.items()) + ". "
                    "Ukazky: " + " | ".join(f"[{v['databaze']}] {v['ukazka']}" for v in vysledky_textu[:5])
                )

        if je_navazujici:
            ma_vlastni_kriterium = bool(plan.get("sql")) or bool(plan.get("text_vyraz"))
            if finalni_pacienti is None and ma_vlastni_kriterium:
                # Vlastni kriterium (napr. SQL na "uziva Atoris") nenaslo NIKOHO v zadne databazi -
                # prunik s predchozim seznamem tedy musi vyjit 0, ne vratit cely predchozi seznam.
                finalni_pacienti = {}
            pred_navaznosti = sum(len(v) for v in (finalni_pacienti or {}).values())
            finalni_pacienti = aplikuj_navaznost(finalni_pacienti, kontext)
            po_navaznosti = sum(len(v) for v in finalni_pacienti.values())
            print(f"--- Navaznost: pred pruniekem s predchozim dotazem {pred_navaznosti}, "
                  f"po pruniku {po_navaznosti} pacientu ---")
            evidence.append(
                f"NAVAZUJICI DOTAZ: toto je navazujici otazka na predchozi dotaz '{kontext['otazka']}'. "
                f"Vysledek byl automaticky proniknut jen s pacienty, kteri vyhovovali i predchozi otazce "
                f"(celkem {po_navaznosti} pacientu napric databazemi po pruniku)."
            )

        if finalni_pacienti and len(finalni_pacienti) > 0:
            naivni_soucet = sum(len(v) for v in finalni_pacienti.values())
            pocet_databazi_s_vysledky = sum(1 for v in finalni_pacienti.values() if v)
            if pocet_databazi_s_vysledky > 1:
                hlas(0.90, "Overuji rodna cisla napric databazemi (deduplikace)...")
                skutecny_pocet_osob, pocet_shod = osoby.pocet_unikatnich_osob(finalni_pacienti, spojeni=spojeni)
                print(f"--- Deduplikace podle rodneho cisla: naivni soucet {naivni_soucet}, skutecnych unikatnich osob {skutecny_pocet_osob} (nalezeno {pocet_shod} shod mezi databazemi) ---")
                evidence.append(
                    f"DEDUPLIKACE PODLE RODNEHO CISLA: naivni soucet pres vsechny databaze byl {naivni_soucet} zaznamu, "
                    f"ale {pocet_shod} z nich se ukazalo byt STEJNA OSOBA v jine databazi (stejne rodne cislo). "
                    f"SKUTECNY POCET UNIKATNICH OSOB je proto {skutecny_pocet_osob}. "
                    f"Pouzij TOTO cislo ({skutecny_pocet_osob}) jako hlavni odpoved na otazku typu 'kolik lidi/pacientu', "
                    f"ne naivni soucet {naivni_soucet}."
                )

        if selhane_databaze:
            popis_selhani = "; ".join(f"{n} ({fx}: {ch})" for n, fx, ch in selhane_databaze)
            evidence.append(
                f"POZOR - U NEKTERYCH DATABAZI SE DOTAZ NEPOVEDL: {popis_selhani}. "
                "Data z techto databazi proto v odpovedi CHYBI a vysledek muze byt neuplny."
            )
            print(f"--- Nedokoncene databaze: {popis_selhani} ---")

        evidence_text = "\n\n".join(evidence) if evidence else "Zadna data nenalezena."
        # Pri textovem hledani obsahuje evidence useky z anamnez/dekurzu, ktere je potreba
        # peclive precist a interpretovat - tam necháme premysleni zapnute. Pri cistě
        # cislenych vysledcich (jen SQL) staci rychla formulace bez premysleni.
        potrebuje_mysleni = bool(plan.get("text_vyraz"))
        print()
        hlas(0.92, "Formuluji ceskou odpoved...")
        odp = zformuluj_odpoved(otazka, evidence_text, mysleni=potrebuje_mysleni)
        print(f"--- Ceska odpoved ---\n{odp}")

        exportovana_cesta = None
        if exportovat and finalni_pacienti:
            hlas(0.97, "Exportuji seznam pacientu...")
            exportovana_cesta = exportuj_pacienty_z_vysledku(finalni_pacienti, navazujici=je_navazujici, spojeni=spojeni)
            if exportovana_cesta:
                pocet = sum(len(v) for v in finalni_pacienti.values())
                popis_navaznosti = " (navazujici, jiz proniknuty seznam)" if je_navazujici else ""
                print(f"\n--- Export: {pocet} pacientu (napric databazemi){popis_navaznosti} ulozeno do souboru {os.path.abspath(exportovana_cesta)} ---")

        hlas(1.0, "Hotovo.")

        # Vratime vysledek i jako pripravu na navazujici otazku - volajici (ui.py, CLI smycka) si ho
        # muze ulozit a poslat jako "kontext" do pristiho volani zeptej_se(), pokud chce navazat.
        return {
            "otazka": otazka,
            "odpoved": odp,
            "pacienti": finalni_pacienti,
            "export_soubor": exportovana_cesta,
            "navazujici": je_navazujici,
        }
    finally:
        # Vsechna pripojeni otevrena na zacatku ted zavreme - at uz dotaz dopadl jakkoliv
        # (i pri chybe behem zpracovani), aby se pripojeni nehromadila.
        for con in spojeni.values():
            con.close()


if __name__ == "__main__":
    argumenty = sys.argv[1:]
    rucni_export = "--export" in argumenty
    argumenty = [a for a in argumenty if a != "--export"]
    otazka_z_prikazove_radky = " ".join(argumenty)

    if otazka_z_prikazove_radky:
        # Jednorazovy dotaz zadany rovnou v prikazove radce - beze zmeny puvodniho chovani.
        exportovat = rucni_export or detekuj_export(otazka_z_prikazove_radky)
        if exportovat:
            print("(rozpoznan pozadavek na export seznamu)")
        zeptej_se(otazka_z_prikazove_radky, exportovat=exportovat)
    else:
        # Interaktivni rezim - kazda dalsi otazka muze navazat na tu predchozi
        # (napr. "Kolik pacientu je starsich 60 let?" -> "a kolik z nich uziva Atoris?").
        print("Interaktivni rezim. Napiste otazku a stisknete Enter.")
        print("Prikaz 'nova' zrusi navaznost na predchozi otazku, 'konec' ukonci program.\n")
        kontext = None
        while True:
            try:
                otazka = input("> ").strip()
            except (EOFError, KeyboardInterrupt):
                print()
                break
            if not otazka:
                continue
            if otazka.lower() in ("konec", "exit", "quit"):
                break
            if otazka.lower() == "nova":
                kontext = None
                print("(navaznost na predchozi otazku zrusena)\n")
                continue

            exportovat = rucni_export or detekuj_export(otazka)
            vysledek = zeptej_se(otazka, exportovat=exportovat, kontext=kontext)
            if vysledek and vysledek.get("pacienti"):
                kontext = {"otazka": vysledek["otazka"], "pacienti": vysledek["pacienti"]}
                pocet = sum(len(v) for v in vysledek["pacienti"].values())
                # Pokud se export nespustil uz automaticky (klicove slovo v otazce), nabidni ho
                # az TED, kdyz uzivatel uz vidi vysledek a pocet pacientu.
                if pocet > 0 and not vysledek.get("export_soubor"):
                    odp_export = input(f"Exportovat techto {pocet} pacientu do CSV? [a/N]: ").strip().lower()
                    if odp_export == "a":
                        cesta = exportuj_pacienty_z_vysledku(vysledek["pacienti"], navazujici=vysledek.get("navazujici", False))
                        if cesta:
                            print(f"--- Export ulozen do souboru {os.path.abspath(cesta)} ---")
            print()
