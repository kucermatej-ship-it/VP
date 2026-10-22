import sys
import re
import ollama

# db.py je ve stejne slozce jako tento soubor, takze zadne dalsi nastaveni cesty neni potreba.
from db import get_connection, seznam_databazi
import schema_pravidla

# Stejny model jako v asistent.py (viz tam pro vysvetleni).
MODEL = "qwen3:14b"

POPIS_TABULEK = f"""
Mas k dispozici tyto tabulky v databazi SmartMedix (Firebird SQL):

{schema_pravidla.popis_pro_prompt()}

DULEZITE - specifika Firebird SQL:
- Firebird NEPOUZIVA LIMIT! Pro omezeni poctu radku pouzij "SELECT FIRST n ..." hned za SELECT.
- Firebird NEPODPORUJE syntaxi "CURRENT_DATE - INTERVAL '84 years'" (to je z jine databaze) - takovy dotaz spadne.
- Pro vek pouzij: EXTRACT(YEAR FROM CURRENT_DATE) - EXTRACT(YEAR FROM S_OSOBA.DATNAR)
- Pro vek v rozmezi (napr. "40 az 84 let") pouzij: WHERE EXTRACT(YEAR FROM CURRENT_DATE) - EXTRACT(YEAR FROM S_OSOBA.DATNAR) BETWEEN 40 AND 84

Napis JEDEN SQL SELECT dotaz (jen Firebird SQL), ktery odpovi na otazku.
Pouzivej JEN tyto tabulky a sloupce - jine se nepovede spustit, kontrola je vynucena mimo tebe.
Nikdy nepouzivej INSERT/UPDATE/DELETE/DROP. Nikdy nepouzivej SELECT * - vzdy vyjmenuj sloupce.
Odpovez POUZE SQL kodem v bloku ```sql ... ```, zadny dalsi text.
"""

# Slova, ktera v SQL nikdy nesmi jit spustit. Kontroluji se jako CELA SLOVA (\b...\b), ne jako
# pouhy podretezec - jinak by napr. bezny cesky lekarsky vyraz "ALTERACE" (obsahuje "ALTER")
# nebo "...DELETED..." omylem spadl do zakazu, i kdyz jde o zcela neskodny dotaz.
ZAKAZANA_SLOVA = ["INSERT", "UPDATE", "DELETE", "DROP", "ALTER", "EXECUTE", "CREATE", "GRANT", "TRUNCATE"]


def ziskej_sql(otazka):
    odpoved = ollama.chat(
        model=MODEL,
        messages=[
            {"role": "system", "content": POPIS_TABULEK},
            {"role": "user", "content": otazka},
        ],
    )
    text = odpoved["message"]["content"]
    shoda = re.search(r"```sql\s*(.*?)\s*```", text, re.DOTALL | re.IGNORECASE)
    if not shoda:
        raise ValueError(f"Qwen nevratil SQL blok. Odpoved:\n{text}")
    return shoda.group(1).strip().rstrip(";").strip()


def over_sql(sql):
    horni = sql.upper().strip()
    if not horni.startswith("SELECT"):
        raise ValueError("Dotaz musi zacinat SELECT.")
    if ";" in horni:
        raise ValueError("Dotaz obsahuje zakazany znak: ;")
    for slovo in ZAKAZANA_SLOVA:
        if re.search(r"\b" + slovo + r"\b", horni):
            raise ValueError(f"Dotaz obsahuje zakazane slovo: {slovo}")
    # Skutecna kontrola tabulek A sloupcu proti centralnimu datovemu slovniku (schema_pravidla.py) -
    # ne jen "prosba" v promptu, ale vynucene pravidlo pred spustenim na databazi.
    schema_pravidla.zkontroluj_sql(sql)
    return True


def spust_sql_s_pripojenim(sql, con, params=None):
    """
    Spusti SQL na JIZ OTEVRENEM pripojeni. Pripojeni NEZAVIRA - o to se stara volajici.
    params: volitelny seznam hodnot za zastupne znaky "?" v sql (parametrizovany dotaz) -
    pouziva se pro SQL sestavene z uzivatelskeho vstupu (napr. filtr.py), aby se hodnoty
    NIKDY nevkladaly primo do textu SQL retezce (ochrana proti SQL injection).
    """
    cur = con.cursor()
    if params:
        cur.execute(sql, params)
    else:
        cur.execute(sql)
    sloupce = [popis[0] for popis in cur.description]
    radky = cur.fetchall()
    return sloupce, radky


def spust_sql(sql, cesta, params=None):
    """Spusti SQL na JEDNE konkretni databazi (cesta k .FDB souboru) - sama si otevre a zavre pripojeni."""
    con = get_connection(cesta)
    try:
        return spust_sql_s_pripojenim(sql, con, params=params)
    finally:
        con.close()


def spust_sql_vsude(sql):
    """Spusti SQL na VSECH databazich ve slozce data/. Vrati (sloupce, {nazev_databaze: radky})."""
    vysledky_podle_databaze = {}
    sloupce = None
    for databaze in seznam_databazi():
        s, radky = spust_sql(sql, databaze["cesta"])
        sloupce = s
        vysledky_podle_databaze[databaze["nazev"]] = radky
    return sloupce, vysledky_podle_databaze


def odpoved_cesky(otazka, sloupce, radky):
    data_text = f"Sloupce: {sloupce}\nVysledky: {radky[:20]}"
    odpoved = ollama.chat(
        model=MODEL,
        messages=[
            {"role": "system", "content": "Odpovez strucne a vecne cesky na zaklade dat z databaze. Nevymyslej si nic navic."},
            {"role": "user", "content": f"Otazka: {otazka}\n\nData z databaze:\n{data_text}"},
        ],
    )
    return odpoved["message"]["content"]


def zeptej_se(otazka):
    """Standalone rezim (bez asistent.py) - pouziva jen prvni nalezenou databazi."""
    databaze = seznam_databazi()
    if not databaze:
        print("Ve slozce data/ nebyl nalezen zadny .FDB soubor.")
        return
    print(f"\n>>> Otazka: {otazka}  (databaze: {databaze[0]['nazev']})")
    sql = ziskej_sql(otazka)
    print(f"--- SQL, ktery Qwen navrhl ---\n{sql}\n")
    over_sql(sql)
    sloupce, radky = spust_sql(sql, databaze[0]["cesta"])
    print(f"--- Vysledek z databaze ({len(radky)} radku) ---")
    print(sloupce, radky[:5])
    print()
    odp = odpoved_cesky(otazka, sloupce, radky)
    print(f"--- Ceska odpoved ---\n{odp}")


if __name__ == "__main__":
    otazka = " ".join(sys.argv[1:]) or "Kolik je v databazi pacientu?"
    zeptej_se(otazka)
