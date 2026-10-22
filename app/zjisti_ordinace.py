"""
Diagnostika: kde vsude v databazi muze byt informace o ordinaci (G_PRA)?

Jen CTE - nic nemeni. Vypisuje JEN strukturu databaze (nazvy tabulek/sloupcu) a SOUHRNNE
POCTY - zadna jmena, rodna cisla ani texty zaznamu. Vystup lze proto bez obav sdilet.

Spusteni (ve stejnem prostredi jako aplikace):
    cd ~/smartmedix-ai
    source .venv-x64/bin/activate
    export DYLD_FALLBACK_LIBRARY_PATH="/Library/Frameworks/Firebird.framework/Versions/A/Resources/lib"
    arch -x86_64 python app/zjisti_ordinace.py
"""
from db import get_connection, seznam_databazi

# Tabulka, u ktere chceme zjistit, jestli by z ni sla ordinace doplnit.
ZKOUMANA_TABULKA = "M_DEKURS"

# Vsechny cizi klice (FOREIGN KEY) v databazi, ktere odkazuji na G_PRA: (tabulka, sloupec).
SQL_ODKAZY_NA_G_PRA = """
SELECT TRIM(rc.RDB$RELATION_NAME), TRIM(seg.RDB$FIELD_NAME)
FROM RDB$RELATION_CONSTRAINTS rc
JOIN RDB$REF_CONSTRAINTS ref ON ref.RDB$CONSTRAINT_NAME = rc.RDB$CONSTRAINT_NAME
JOIN RDB$RELATION_CONSTRAINTS pk ON pk.RDB$CONSTRAINT_NAME = ref.RDB$CONST_NAME_UQ
JOIN RDB$INDEX_SEGMENTS seg ON seg.RDB$INDEX_NAME = rc.RDB$INDEX_NAME
WHERE rc.RDB$CONSTRAINT_TYPE = 'FOREIGN KEY' AND TRIM(pk.RDB$RELATION_NAME) = 'G_PRA'
ORDER BY 1, 2
"""

# Sloupce uzivatelskych tabulek, ktere maji v nazvu "PRA" (pracoviste) - kandidati i bez cizich klicu.
SQL_SLOUPCE_PRA = """
SELECT TRIM(f.RDB$RELATION_NAME), TRIM(f.RDB$FIELD_NAME)
FROM RDB$RELATION_FIELDS f
JOIN RDB$RELATIONS r ON r.RDB$RELATION_NAME = f.RDB$RELATION_NAME
WHERE COALESCE(r.RDB$SYSTEM_FLAG, 0) = 0 AND f.RDB$FIELD_NAME CONTAINING 'PRA'
ORDER BY 1, 2
"""

# Vsechny sloupce zkoumane tabulky.
SQL_SLOUPCE_TABULKY = """
SELECT TRIM(RDB$FIELD_NAME) FROM RDB$RELATION_FIELDS
WHERE TRIM(RDB$RELATION_NAME) = ? ORDER BY RDB$FIELD_POSITION
"""


def jedna_hodnota(cur, sql, params=None):
    cur.execute(sql, params or [])
    return cur.fetchone()[0]


def zkoumej_databazi(nazev, con):
    cur = con.cursor()
    print(f"\n=== Databaze {nazev} ===")

    # 1) Jak moc chybi registrace v S_OSOBA.IDOSBPRA
    celkem = jedna_hodnota(cur, "SELECT COUNT(*) FROM S_OSOBA")
    bez_odkazu = jedna_hodnota(cur, "SELECT COUNT(*) FROM S_OSOBA WHERE IDOSBPRA IS NULL")
    bez_nazvu = jedna_hodnota(cur, """
        SELECT COUNT(*) FROM S_OSOBA s LEFT JOIN G_PRA g ON s.IDOSBPRA = g.ID
        WHERE s.IDOSBPRA IS NOT NULL AND (g.ID IS NULL OR g.NAZEV IS NULL OR TRIM(g.NAZEV) = '')""")
    print(f"Pacientu celkem: {celkem}")
    print(f"  bez ordinace (IDOSBPRA prazdne):                 {bez_odkazu}")
    print(f"  s odkazem na neexistujici/bezejmennou ordinaci:  {bez_nazvu}")
    print(f"  ordinaci v G_PRA: {jedna_hodnota(cur, 'SELECT COUNT(*) FROM G_PRA')}")

    # 2) Kde vsude se na G_PRA odkazuje
    cur.execute(SQL_ODKAZY_NA_G_PRA)
    odkazy = cur.fetchall()
    print("\nCizi klice odkazujici na G_PRA (tabulka.sloupec):")
    for tabulka, sloupec in odkazy:
        print(f"  {tabulka}.{sloupec}")
    if not odkazy:
        print("  (zadne - databaze cizi klice nepouziva, proto nize hledame i podle nazvu sloupcu)")

    cur.execute(SQL_SLOUPCE_PRA)
    print("\nSloupce s 'PRA' v nazvu (mozni kandidati):")
    for tabulka, sloupec in cur.fetchall():
        print(f"  {tabulka}.{sloupec}")

    # 3) Sloupce zkoumane tabulky a kandidati na ordinaci v ni
    cur.execute(SQL_SLOUPCE_TABULKY, [ZKOUMANA_TABULKA])
    sloupce = [s for (s,) in cur.fetchall()]
    print(f"\nVsechny sloupce tabulky {ZKOUMANA_TABULKA}: {', '.join(sloupce)}")

    kandidati = sorted({s for t, s in odkazy if t == ZKOUMANA_TABULKA} | {s for s in sloupce if "PRA" in s})
    if not kandidati:
        print(f"V {ZKOUMANA_TABULKA} neni zadny sloupec, ktery by zjevne odkazoval na ordinaci.")
        return

    zaznamu = jedna_hodnota(cur, f"SELECT COUNT(*) FROM {ZKOUMANA_TABULKA}")
    for sloupec in kandidati:
        # Nazev sloupce pochazi ze systemovych tabulek databaze (ne od uzivatele); uvozovky pro jistotu.
        s = f'd."{sloupec}"'
        print(f"\nKandidat {ZKOUMANA_TABULKA}.{sloupec}:")
        try:
            zkoumej_kandidata(cur, s, zaznamu, bez_odkazu)
        except Exception as chyba:
            print(f"  nelze vyhodnotit (napr. sloupec neni cislo): {chyba}")


def zkoumej_kandidata(cur, s, zaznamu, bez_odkazu):
    """Souhrnne pocty pro jeden kandidatni sloupec s (ve tvaru d."NAZEV") zkoumane tabulky."""
    vyplneno = jedna_hodnota(cur, f"SELECT COUNT({s}) FROM {ZKOUMANA_TABULKA} d")
    do_g_pra = jedna_hodnota(cur, f"SELECT COUNT(*) FROM {ZKOUMANA_TABULKA} d WHERE {s} IN (SELECT ID FROM G_PRA)")
    doplnitelnych = jedna_hodnota(cur, f"""
        SELECT COUNT(DISTINCT d.IDOSB) FROM {ZKOUMANA_TABULKA} d JOIN S_OSOBA o ON o.ID = d.IDOSB
        WHERE o.IDOSBPRA IS NULL AND {s} IN (SELECT ID FROM G_PRA WHERE NAZEV IS NOT NULL)""")
    print(f"  vyplneno v {vyplneno} z {zaznamu} zaznamu")
    print(f"  z toho hodnota odpovida nejake ordinaci v G_PRA: {do_g_pra}")
    print(f"  pacientu BEZ registrace, kterym by sla ordinace doplnit: {doplnitelnych} (z {bez_odkazu})")


if __name__ == "__main__":
    databaze = seznam_databazi()
    if not databaze:
        print("Ve slozce data/ nebyl nalezen zadny .FDB soubor.")
    for o in databaze:
        try:
            con = get_connection(o["cesta"])
        except Exception as chyba:
            print(f"\n=== Databaze {o['nazev']}: pripojeni se nepovedlo ({chyba}) ===")
            continue
        try:
            zkoumej_databazi(o["nazev"], con)
        except Exception as chyba:
            print(f"!!! Chyba pri zkoumani databaze {o['nazev']}: {chyba}")
        finally:
            con.close()
