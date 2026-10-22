# SmartMedix – předávací poznámky pro nové vlákno

Uživatel není programátor. Komunikace česky. Vše běží lokálně na jeho Macu (16 GB RAM)
nad reálnými daty pacientů. Nic nemáš k dispozici přímo (Firebird ani Ollama) – kód testuj
mocky, `py_compile`, a uživatel ho ověří u sebe.

## Pravidla spolupráce
- Po změně posílej **jen upravené soubory** (ne celý projekt), pokud uživatel neřekne jinak.
- Heslo k DB (`SYSDBA`/`masterkey` v `db.py`) zůstává napevno – výslovné přání uživatele, neměnit.
- Žádné SQL generované AI v nové aplikaci – SQL se skládá ručně v Pythonu, parametry přes `?`.
- Každá změna: implementace → test mockem → kontrola syntaxe → předání souboru.

## Dvě aplikace ve složce `app/`
1. **Stará AI aplikace** (`ui.py`, `asistent.py`, `dotaz.py`) – Ollama (`qwen3:14b`) převádí
   dotaz na SQL. Kvůli halucinacím se dál nerozvíjí.
2. **Nová filtrovací aplikace** (hlavní vývoj): `filtr_ui.py` (Streamlit UI), `filtry.py`
   (deterministické SQL, filtry), `text_search.py` (hledání v dekurzech/anamnézách), `ordinace.py` (zařazení pacienta do ordinace),
   `export.py` (CSV), `synonyma.py` (AI návrh synonym v bočním panelu – posílá se jen
   zadaný výraz, nikdy data pacientů).
   Sdílené: `db.py` (připojení), `schema_pravidla.py` (whitelist tabulek/sloupců – kontrola
   každého SQL), `osoby.py` (deduplikace pacientů napříč DB podle RODCIS).

Spuštění: `SmartMedix-Filtr.command` na ploše, nebo ručně:
```
cd ~/smartmedix-ai
source .venv-x64/bin/activate
export DYLD_FALLBACK_LIBRARY_PATH="/Library/Frameworks/Firebird.framework/Versions/A/Resources/lib"
arch -x86_64 streamlit run app/filtr_ui.py
```

## Databáze (Firebird)
- Více `.FDB` souborů ve složce `data/`, každý = samostatná databáze s vlastním číslováním ID.
  ID se **nesmí** porovnávat mezi databázemi; jediný společný klíč je RODCIS.
- `charset="WIN1250"`, DSN `localhost/3050:<cesta>`, `firebird-driver` 2.x, běh přes x86_64 (Rosetta).
- Pole DEKURS/TXTANAM: číst přes `CAST(... AS BLOB SUB_TYPE 0)` a dekódovat
  `cp1250, errors="replace"` (obsahují i bajty neplatné v cp1250, např. 0x98).
- Živá databáze: při dlouhém čtení může přijít `DatabaseError: BLOB not found`.
  Takový záznam se přeskočí (`text_search._nacti_pripravene_radky`), hledání nesmí spadnout.
- Část textu má chybnou mezeru před diakritikou – opravuje `db.oprav_text()`.

## Jak funguje nová aplikace
- **Krok 0:** SQL filtry (věk, pohlaví, ordinace, období, BMI/váha/výška/tlak/puls z M_PRISTAV,
  diagnóza M_STALADGN, medikace M_MEDIREC, výkony P_DOKLADPOL + C_VYKONY/CP_VYKONY,
  pojišťovna, státní příslušnost, RČ/jméno/příjmení/adresa).
- **Společné období (od–do)** platí pro všechny filtry s datem i pro textové kroky.
  Výjimka – diagnóza: platí „od DATOD dále“, počítá se, když DATOD <= konec období,
  dolní hranice se neuplatní.
- **Ordinace** (`ordinace.py`): registrace `S_OSOBA.IDOSBPRA` chybí u 80–98 % pacientů, proto
  ordinace pacienta = ordinace jeho **posledního dekurzu** (`M_DEKURS.IDPRAZDR → G_PRAZDR.IDPRA → G_PRA`),
  zápisy „Systém“ (G_PRA.ID 0) se ignorují, při shodě data rozhoduje vyšší `M_DEKURS.ID`.
  S obdobím rozhoduje poslední dekurz v období; bez období je náhradou registrace.
  Ordinace se **stejným IČP = jedna ordinace, i napříč databázemi**; bez IČP klíč databáze + ID.
  Filtr se uplatní až po SQL (v Pythonu), přiřazení se v UI cachuje. Export: jeden sloupec „Ordinace“.
- **Kroky 1+:** textové hledání, mezi kroky A ZÁROVEŇ, uvnitř kroku NEBO.
  Dva typy: „libovolný z výrazů“ a „klíčové slovo + číslo pod/nad hranicí“.
  Hledání nerozlišuje velká a malá písmena a hledá podřetězec. Výraz v závorkách,
  např. `(EF)`, se hledá jako celé samostatné slovo.
  Pozor: zkrácené slovo funguje jako „žolík“ jen na konci výrazu – `atopick ekzém` nenajde
  „atopický ekzém“, správně je samotné `atopick`.
- **Export CSV:** `;`, utf-8-sig, jeden řádek na pacienta, sloupec Ordinace,
  jeden sloupec „Citace N“ na každý krok, více nálezů pod sebou v jedné buňce.

## Odložené nápady
- Integrace online modelu (ChatGPT/Claude API) pro návrh synonym jako volitelná alternativa
  k Ollamě. Uživatel zatím nechce. Přes API odchází jen zadaný výraz, ale jde to do USA,
  takže je potřeba zohlednit GDPR.
