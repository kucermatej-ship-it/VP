"""
Pripojeni k SmartMedix (Firebird) databazim - podpora VICE databazi najednou.

Zjisteni:
- Pripojeni vyzaduje charset="WIN1250" (ceska diakritika).
- Pole M_DEKURS.DEKURS (a mozna dalsi rich-text pole) obsahuji chybu:
  pred kazdym znakem s diakritikou je navic vlozena mezera. Opravuje se automaticky.
- VICE DATABAZI: kazdy .FDB soubor ve slozce data/ predstavuje jednu databazi (jeden uzel).
  V ramci jedne databaze muze byt dat z vice fyzickych ordinaci - rozliseni na konkretni
  ordinaci zatim neni k dispozici, aplikace zatim pracuje na urovni cele databaze.
  Kazda ma vlastni, nezavisle cislovane ID pacientu (S_OSOBA.ID) - proto NIKDY
  neporovnavejte cisla ID mezi ruznymi databazemi, jsou to nahodne shody.
  Jedine spolehlive univerzalni spojeni mezi databazemi je RODCIS.
"""
import html
import os
import re
from firebird.driver import driver_config, connect

FB_CLIENT_LIBRARY = "/Library/Frameworks/Firebird.framework/Versions/A/Resources/lib/libfbclient.dylib"
DB_HOST = "localhost/3050"
DB_USER = "SYSDBA"
DB_PASSWORD = "masterkey"

driver_config.fb_client_library.value = FB_CLIENT_LIBRARY

_TENTO_SOUBOR = os.path.abspath(__file__)
_PROJEKT_SLOZKA = os.path.dirname(os.path.dirname(_TENTO_SOUBOR))
DATA_SLOZKA = os.path.join(_PROJEKT_SLOZKA, "data")
# Slozka pro CSV exporty - vzdy vedle data/ v korenu projektu, bez ohledu na to, odkud
# (z jake slozky v terminalu) uzivatel program spusti.
EXPORT_SLOZKA = os.path.join(_PROJEKT_SLOZKA, "export")

_DIAK = "áéěíóúůýčďňřšťžÁÉĚÍÓÚŮÝČĎŇŘŠŤŽ"
_PATTERN = re.compile(r"(?<=\S) (?=[" + _DIAK + "])")
_DIAK_ZNAK = re.compile("[" + _DIAK + "]")

# Chyba v datech vklada mezeru pred KAZDY znak s diakritikou (i uprostred slova, napr.
# "léčba" -> "l é čba"). V cistem textu se tento vzor (mezera hned pred znakem s diakritikou)
# objevi jen na zacatku slov po jednopismenne predlozce (v, k, u, z, s...) - coz je u diakritickych
# znaku jen mensina jejich vsech vyskytu v textu. Proto pocitame podil VUCI VSEM diakritickym
# znakum v textu (ne vuci celkove delce textu) - u poskozeneho textu vyjde tento podil vysoky
# (skoro kazdy diakritický znak ma pred sebou mezeru navic), u ciste cestiny zustane nizky.
# Overeno na realistickem klinickem textu: cisty text ~5 %, uměle poškozený ~95 %.
_PODIL_PRAH = 0.3

# U velmi kratkych useku textu s malo diakritikou je podil nespolehlivy (napr. jediny vyskyt
# "v čase" na zacatku vety by dal podil 100 %). Pod touto hranici radeji text nechame beze zmeny,
# nez abychom riskovali smazani skutecne mezery v cistem textu.
_MIN_DIAKRITICKYCH_ZNAKU = 8


def oprav_text(text):
    """Opravi chybne vlozene mezery pred diakritikou, pokud je text poskozeny."""
    if text is None or not isinstance(text, str) or len(text) == 0:
        return text
    # Pocitani pres regex je vyrazne rychlejsi nez prochazet text znak po znaku v Pythonu -
    # tato funkce bezi pro kazdy prohledavany dekurz/anamnezu.
    pocet_diakritickych_znaku = len(_DIAK_ZNAK.findall(text))
    if pocet_diakritickych_znaku < _MIN_DIAKRITICKYCH_ZNAKU:
        return text
    matches = _PATTERN.findall(text)
    podil = len(matches) / pocet_diakritickych_znaku
    if podil > _PODIL_PRAH:
        return _PATTERN.sub("", text)
    return text


_HTML_TAG = re.compile(r"<[^>]+>")
_WHITESPACE = re.compile(r"[ \t]+")


def ocisti_html(text):
    """Odstrani HTML znacky a sjednoti bile znaky. Pouziva se po oprav_text()."""
    if text is None or not isinstance(text, str) or len(text) == 0:
        return text
    bez_znacek = _HTML_TAG.sub(" ", text)
    rozbaleno = html.unescape(bez_znacek)
    ocistene = _WHITESPACE.sub(" ", rozbaleno)
    return ocistene.strip()


def pripravit_text(text):
    """Kombinuje oba kroky: nejdriv opravi diakritiku, pak odstrani HTML."""
    return ocisti_html(oprav_text(text))


def seznam_databazi():
    """Najde vsechny .FDB soubory ve slozce data/. Nazev databaze = nazev souboru bez pripony."""
    vysledek = []
    if not os.path.isdir(DATA_SLOZKA):
        return vysledek
    for soubor in sorted(os.listdir(DATA_SLOZKA)):
        if soubor.upper().endswith(".FDB"):
            nazev = os.path.splitext(soubor)[0]
            vysledek.append({"nazev": nazev, "cesta": os.path.join(DATA_SLOZKA, soubor)})
    return vysledek


def get_connection(cesta):
    """Vrati nove pripojeni k databazi na dane ceste."""
    return connect(
        f"{DB_HOST}:{cesta}",
        user=DB_USER,
        password=DB_PASSWORD,
        charset="WIN1250",
    )


def rozdel_na_davky(seznam, velikost=1000):
    """Rozdeli seznam na davky (Firebird ma limit 1500 hodnot ve WHERE ... IN (...))."""
    seznam = list(seznam)
    for i in range(0, len(seznam), velikost):
        yield seznam[i:i + velikost]


def pripojeni_podle_databaze(nazvy, spojeni=None):
    """
    Postupne vraci dvojice (nazev_databaze, pripojeni) pro zadane nazvy databazi.

    spojeni: volitelny dict {nazev_databaze: jiz_otevrene_pripojeni} - kdyz je predany
    (typicky z asistent.py), pouzije se rovnou a NEZAVIRA se (o to se stara volajici).
    Kdyz neni predany, pripojeni se otevre a po zpracovani dane databaze hned zase zavre.
    Databaze, ktera neni k dispozici (neni ve spojeni / ve slozce data/), se preskoci.
    """
    if spojeni is not None:
        for nazev in nazvy:
            if nazev in spojeni:
                yield nazev, spojeni[nazev]
        return
    cesty = {o["nazev"]: o["cesta"] for o in seznam_databazi()}
    for nazev in nazvy:
        if nazev not in cesty:
            continue
        con = get_connection(cesty[nazev])
        try:
            yield nazev, con
        finally:
            con.close()


def nacti_podle_id(con, sql_pred_seznamem, idcka):
    """
    Spusti "sql_pred_seznamem (id1,id2,...)" po davkach (viz rozdel_na_davky) a vrati vsechny
    radky. ID se pred vlozenim do SQL prevedou na int - do textu dotazu se tak nikdy nedostane
    nic jineho nez cisla. Priklad: nacti_podle_id(con, "SELECT ID, RODCIS FROM S_OSOBA WHERE ID IN", ids)
    """
    cur = con.cursor()
    radky = []
    for davka in rozdel_na_davky(sorted(set(int(i) for i in idcka))):
        cur.execute(f"{sql_pred_seznamem} ({','.join(str(i) for i in davka)})")
        radky.extend(cur.fetchall())
    return radky
