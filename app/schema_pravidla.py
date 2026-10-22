"""
Centralni "datovy slovnik" databaze SmartMedix - JEDINY zdroj pravdy pro to,
jake tabulky a sloupce smi AI a generovane SQL pouzivat.

Pridani noveho sloupce nebo tabulky:
    Staci pridat/upravit polozku ve slovniku SCHEMA nize. Nic dalsiho nastavovat
    netreba - text pro AI prompt (popis_pro_prompt) i skutecna kontrola SQL
    pred spustenim (zkontroluj_sql) se z tohoto slovniku odvozuji automaticky.

Format:
    "NAZEV_TABULKY": {
        "popis": "kratky popis tabulky",
        "sloupce": {
            "NAZEV_SLOUPCE": "co sloupec znamena a jak se s nim pracuje",
            ...
        },
    }
"""
import re
import sqlglot
from sqlglot import exp

SCHEMA = {
    "S_OSOBA": {
        "popis": "pacient - zakladni osobni udaje",
        "sloupce": {
            "ID": "jednoznacne cislo pacienta v ramci jedne databaze (mezi databazemi NEporovnatelne). POZOR: v S_OSOBA se pacient identifikuje POUZE sloupcem ID, sloupec IDOSB zde NEEXISTUJE - IDOSB se pouziva jen v JINYCH tabulkach jako odkaz NA S_OSOBA.ID",
            "DATNAR": "datum narozeni - pro vek pouzij EXTRACT(YEAR FROM CURRENT_DATE) - EXTRACT(YEAR FROM DATNAR)",
            "POHLAVI": "pohlavi pacienta: 'M' = muz, 'F' = zena. Hodnota 'Z' se NIKDY nevyskytuje",
            "IDOSBPRA": "odkaz na ordinaci, ve ktere je pacient registrovany (G_PRA.ID) - pro dotazy na 'ordinaci pacienta' spoj s G_PRA",
            "RODCIS": "rodne cislo pacienta (bez lomitka) - pro identifikaci konkretni osoby, viz pravidlo pro RODCIS v promptu",
            "PRIJMENI": "prijmeni pacienta - VZDY pouzij UPPER(PRIJMENI) LIKE UPPER('%...%') kvuli velikosti pismen",
            "JMENO": "krestni jmeno pacienta - VZDY pouzij UPPER(JMENO) LIKE UPPER('%...%') kvuli velikosti pismen",
            "TITUL": "akademicky/profesni titul pacienta - VZDY pouzij UPPER(TITUL) LIKE UPPER('%...%')",
            "STATPRIS": "kod statni prislusnosti (S_OSOBA.STATPRIS = C_STATPRIS.KOD) - pro filtrovani podle nazvu spoj s C_STATPRIS",
            "ADRESA": "adresa bydliste (volny text) - VZDY pouzij UPPER(ADRESA) LIKE UPPER('%...%')",
            "POJ": "3-mistny kod zdravotni pojistovny - viz seznam kodu v promptu",
            "TEL": "telefonni cislo pacienta",
            "MAIL": "e-mailova adresa pacienta",
        },
    },
    "G_PRA": {
        "popis": "ordinace/pracoviste (NE osoba pacienta) - pouzij pro dotazy typu 'kde je pacient registrovan', 'kolik pacientu z ordinace X', 'filtruj pacienty z ordinace Y'",
        "sloupce": {
            "ID": "jednoznacne cislo ordinace - spojuje se s S_OSOBA.IDOSBPRA",
            "NAZEV": "nazev ordinace (napr. 'Vinohrady', 'Zizkov') - VZDY pouzij UPPER(NAZEV) LIKE UPPER('%...%') kvuli velikosti pismen",
            "ICP": "identifikacni cislo pracoviste (kod ordinace pridelovany pojistovnami)",
        },
    },
    "G_PRAZDR": {
        "popis": "pracoviste + zdravotnik - propojka mezi zaznamem (napr. dekurzem) a ordinaci",
        "sloupce": {
            "ID": "jednoznacne cislo - odkazuje na nej napr. M_DEKURS.IDPRAZDR",
            "IDPRA": "odkaz na ordinaci (G_PRA.ID)",
        },
    },
    "M_DEKURS": {
        "popis": "dekurzy (zapisy z navstev) - JEN udaje o navsteve, text dekurzu se hleda zvlast textovym hledanim",
        "sloupce": {
            "ID": "jednoznacne cislo dekurzu",
            "IDOSB": "odkaz na pacienta (S_OSOBA.ID)",
            "DATUM": "datum navstevy",
            "IDPRAZDR": "odkaz na pracoviste a zdravotnika (G_PRAZDR.ID) - pres G_PRAZDR.IDPRA vede na ordinaci",
        },
    },
    "C_STATPRIS": {
        "popis": "ciselnik statnich prislusnosti - pouzij pro spojeni s S_OSOBA.STATPRIS, kdyz se otazka pta na statni prislusnost podle nazvu (napr. 'ceska')",
        "sloupce": {
            "KOD": "kod statni prislusnosti - spojuje se s S_OSOBA.STATPRIS",
            "NAZEV": "nazev statni prislusnosti (napr. 'Ceska republika') - VZDY pouzij UPPER(NAZEV) LIKE UPPER('%...%')",
        },
    },
    "M_STALADGN": {
        "popis": "trvale diagnozy pacienta",
        "sloupce": {
            "IDOSB": "odkaz na pacienta (S_OSOBA.ID)",
            "KOD": "kod diagnozy podle MKN-10 - VZDY spolehlivy zdroj pro hledani diagnoz",
            "NAZEV": "textovy nazev diagnozy - muze byt prazdny/nespolehlivy, nespolehat se jen na nej",
            "DATOD": "od kdy diagnoza plati",
            "DATDO": "do kdy diagnoza plati (prazdne = stale plati)",
        },
    },
    "M_MEDIREC": {
        "popis": "recepty (predepsane leky) - hlavni zdroj pro dotazy na uzivanou medikaci",
        "sloupce": {
            "IDOSB": "odkaz na pacienta (S_OSOBA.ID)",
            "DATUM": "datum vystaveni receptu",
            "NAZEV": "nazev predepsaneho leku - VZDY pouzij UPPER(NAZEV) LIKE UPPER('%...%') kvuli velikosti pismen",
            "DGN": "diagnoza uvedena na receptu (kod nebo text)",
            "DS": "davkovani leku (textovy popis)",
        },
    },
    "M_PRISTAV": {
        "popis": "namerene hodnoty pri preventivni prohlidce (tlak, puls, vaha, BMI...) - jeden radek = jedno mereni v konkretnim datu",
        "sloupce": {
            "ID": "jednoznacne cislo mereni",
            "IDOSB": "odkaz na pacienta (S_OSOBA.ID)",
            "DATUM": "datum mereni",
            "TLAKSYS": "systolicky (horni) krevni tlak",
            "TLAKDIA": "diastolicky (dolni) krevni tlak",
            "PULS": "tepova frekvence (puls)",
            "VYSKA": "vyska pacienta v cm",
            "VAHA": "vaha pacienta v kg",
            "BMI": "index telesne hmotnosti (BMI)",
        },
    },
    "L_POUKAZ": {
        "popis": "poukaz na vysetreni - propojka mezi pacientem a jeho vysledky v L_VYSLEDKY (L_VYSLEDKY sama o sobe nema odkaz na pacienta primo)",
        "sloupce": {
            "ID": "jednoznacne cislo poukazu - odkazuje na nej L_VYSLEDKY.IDPOU",
            "IDOSB": "odkaz na pacienta (S_OSOBA.ID)",
        },
    },
    "L_VYSLEDKY": {
        "popis": "vysledky laboratornich vysetreni - k pacientovi se pripoj pres IDPOU -> L_POUKAZ.ID -> L_POUKAZ.IDOSB",
        "sloupce": {
            "IDPOU": "odkaz na poukaz (L_POUKAZ.ID) - pres nej se vysledek pripoji ke konkretnimu pacientovi",
            "KOD": "kod laboratorniho vysetreni",
            "NAZEV": "nazev laboratorniho vysetreni",
            "NVAL": "numericka hodnota vysledku",
            "JEDN": "jednotka hodnoty (napr. mmol/l)",
            "DATVYS": "datum vysledku",
        },
    },
    "P_DOKLADPOL": {
        "popis": "provedene vykony/ukony u pacienta",
        "sloupce": {
            "IDOSB": "odkaz na pacienta (S_OSOBA.ID)",
            "KODVYK": "kod provedeneho vykonu - cely nazev ziskas spojenim s C_VYKONY.KOD nebo CP_VYKONY.KOD",
            "DATUM": "datum, kdy byl vykon proveden",
        },
    },
    "C_VYKONY": {
        "popis": "obecny ciselnik zdravotnich vykonu (nazvy k kodum z P_DOKLADPOL.KODVYK)",
        "sloupce": {
            "KOD": "kod vykonu - spojuje se s P_DOKLADPOL.KODVYK",
            "NAZ": "cely nazev vykonu",
        },
    },
    "CP_VYKONY": {
        "popis": "pracovistni ciselnik zdravotnich vykonu (alternativa k C_VYKONY - kod nemusi byt v obou, zkus obe tabulky)",
        "sloupce": {
            "KOD": "kod vykonu - spojuje se s P_DOKLADPOL.KODVYK",
            "NAZ": "cely nazev vykonu",
        },
    },
}


def povolene_tabulky():
    """Mnozina nazvu vsech povolenych tabulek (odvozeno ze SCHEMA)."""
    return set(SCHEMA.keys())


def popis_pro_prompt():
    """Sestavi textovy popis vsech povolenych tabulek a sloupcu pro system prompt AI."""
    radky = []
    for tabulka, info in SCHEMA.items():
        sloupce_text = ", ".join(f"{nazev} ({popis})" for nazev, popis in info["sloupce"].items())
        radky.append(f"{tabulka} ({info['popis']}) - sloupce: {sloupce_text}")
    return "\n".join(radky)


def _pripravit_pro_parser(sql):
    """
    Firebird "SELECT FIRST n" neni bezna SQL syntaxe a obecny SQL parser by na ni spadl.
    Pro ucely analyzy pouzitych sloupcu (NE pro skutecne spusteni) ji docasne odstranime -
    na vykonavane SQL se puvodni text nemeni.
    """
    return re.sub(r"\bFIRST\s+\d+\b", " ", sql, flags=re.IGNORECASE)


def zkontroluj_sql(sql):
    """
    Overi, ze SQL pouziva JEN povolene tabulky a v nich JEN povolene sloupce (viz SCHEMA).
    Vyhodi ValueError se srozumitelnou hlaskou, pokud narazi na cokoliv mimo tento seznam -
    vcetne pripadu, kdy se SQL nepodari rozparsovat (radsi bezpecne odmitnout, nez propustit
    neoverene SQL na databazi s citlivymi udaji).
    """
    try:
        strom = sqlglot.parse_one(_pripravit_pro_parser(sql))
    except Exception as chyba:
        raise ValueError(f"SQL se nepodarilo analyzovat kvuli kontrole sloupcu: {chyba}")

    # SELECT * (na nejvyssi urovni) by obesel kontrolu jednotlivych sloupcu - zakazano.
    # COUNT(*) je v poradku, hvezdicka tam je "schovana" uvnitr funkce, ne primo v projekci.
    for vyber in strom.find_all(exp.Select):
        for polozka in vyber.expressions:
            if isinstance(polozka, exp.Star):
                raise ValueError("Dotaz pouziva SELECT * - musi vyjmenovat konkretni povolene sloupce.")

    # Mapovani alias/tabulka -> skutecny nazev tabulky (napr. "s" -> "S_OSOBA")
    alias_na_tabulku = {}
    pouzite_tabulky = set()
    for tabulka_uzel in strom.find_all(exp.Table):
        nazev_tabulky = tabulka_uzel.name.upper()
        pouzite_tabulky.add(nazev_tabulky)
        alias_na_tabulku[nazev_tabulky] = nazev_tabulky
        if tabulka_uzel.alias:
            alias_na_tabulku[tabulka_uzel.alias.upper()] = nazev_tabulky

    nepovolene_tabulky = pouzite_tabulky - povolene_tabulky()
    if nepovolene_tabulky:
        raise ValueError(f"Dotaz pouziva nepovolene tabulky: {sorted(nepovolene_tabulky)}")

    for sloupec_uzel in strom.find_all(exp.Column):
        nazev_sloupce = sloupec_uzel.name.upper()

        if sloupec_uzel.table:
            # Sloupec je kvalifikovany (tabulka.sloupec nebo alias.sloupec)
            tabulka = alias_na_tabulku.get(sloupec_uzel.table.upper())
            if tabulka is None:
                raise ValueError(f"Dotaz odkazuje na neznamy alias/tabulku '{sloupec_uzel.table}'.")
            povolene = SCHEMA.get(tabulka, {}).get("sloupce", {})
            if nazev_sloupce not in povolene:
                raise ValueError(
                    f"Sloupec '{sloupec_uzel.table}.{nazev_sloupce}' neni v povolenem seznamu pro tabulku {tabulka}. "
                    f"Povolene sloupce: {sorted(povolene.keys())}"
                )
        else:
            # Sloupec bez prefixu tabulky - musi patrit alespon do jedne z pouzitych tabulek
            kandidati = [
                t for t in pouzite_tabulky
                if nazev_sloupce in SCHEMA.get(t, {}).get("sloupce", {})
            ]
            if not kandidati:
                raise ValueError(
                    f"Sloupec '{nazev_sloupce}' neni v povolenem seznamu pro zadnou z pouzitych "
                    f"tabulek ({', '.join(sorted(pouzite_tabulky))})."
                )

    return True
