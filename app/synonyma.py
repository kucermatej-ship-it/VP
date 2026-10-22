"""
Navrh synonym / alternativnich vyrazu pro textove hledani (postupne kroky ve filtr_ui.py).

DULEZITE: sem se NIKDY neposilaji zadna data pacientu - jen text, ktery si uzivatel sam
napise (napr. "NYHA II"). AI tu nema pristup k databazi ani k vysledkum filtrovani, takze
nehrozi zadna halucinace ve vysledcich ani unik dat - jde jen o napad k rucnimu vyberu
clovekem, pouziva se stejny lokalni Ollama model jako jinde v aplikaci.
"""
import re
import ollama

MODEL = "qwen3:14b"

SYSTEM_PROMPT = """
Jsi pomocnik ceskeho lekare. Uzivatel zada lekarsky vyraz, zkratku nebo frazi, kterou chce
hledat ve zdravotnickych zaznamech (dekurzy, anamnezy). Tvym ukolem je navrhnout DALSI mozne
varianty zapisu, synonyma, casto pouzivane zkratky a alternativni formulace, ktere by mohly
oznacovat totez, aby na ne clovek nezapomel pri fulltextovem hledani.

Pravidla:
- Odpovez POUZE seznamem vyrazu, jeden na radek, bez cislovani, bez odrazek, bez uvodu a bez
  vysvetlivek.
- Nabidni maximalne 15 vyrazu.
- Nabizej jen realisticke varianty bezne pouzivane v ceske zdravotnicke dokumentaci (cesky i
  bezne pouzivane latinske/anglicke zkratky), nikdy si nic nevymyslej.
- Nevkladej zpet presne to, co uzivatel jiz napsal.
"""


def navrhni_synonyma(vyraz):
    """
    Zepta se lokalniho Ollama modelu na varianty/synonyma k zadanemu vyrazu. Vraci serazeny
    seznam retezcu (bez duplicit, bez prazdnych radku, bez puvodniho vyrazu, max. 15 polozek).
    Pri jakekoliv chybe (Ollama nebezi, model neni stazeny, ...) vyhodi RuntimeError se
    srozumitelnou hlaskou - volajici (UI) si ji sam zobrazi uzivateli, zbytek aplikace na
    tom nijak nezavisi.
    """
    vyraz = (vyraz or "").strip()
    if not vyraz:
        return []

    try:
        odpoved = ollama.chat(
            model=MODEL,
            messages=[
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": vyraz},
            ],
        )
        text = odpoved["message"]["content"]
    except Exception as e:
        raise RuntimeError(
            f"Nepodarilo se ziskat navrhy od AI (bezi Ollama? je stazeny model {MODEL}?): {e}"
        ) from e

    # Qwen obcas prida pred skutecnou odpovedi blok <think>...</think> s uvahou - ten zahodime.
    text = re.sub(r"<think>.*?</think>", "", text, flags=re.DOTALL)

    puvodni_lower = vyraz.casefold()
    navrhy = []
    videne = set()
    for radek in text.splitlines():
        polozka = radek.strip()
        # Odstranime pripadne odrazky/cislovani na zacatku radku ("- ", "1. ", "* ", ...).
        polozka = re.sub(r"^[\-\*•]\s*", "", polozka)
        polozka = re.sub(r"^\d+[\.\)]\s*", "", polozka)
        polozka = polozka.strip(" \"'`")
        if not polozka:
            continue
        klic = polozka.casefold()
        if klic == puvodni_lower or klic in videne:
            continue
        videne.add(klic)
        navrhy.append(polozka)

    return navrhy[:15]
