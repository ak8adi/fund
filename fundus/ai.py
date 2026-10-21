import json
import logging
import httpx

from . import db
from .config import get_config

log = logging.getLogger(__name__)

def evaluate_listings(search_id: int) -> int:
    cfg = get_config()
    api_key = cfg.get("ai", {}).get("gemini_api_key")
    if not api_key:
        return 0

    search = db.get_search(search_id)
    if not search:
        return 0

    # Hole alle bisher unbewerteten Hits
    hits = db.hits_for_search(search_id)
    to_evaluate = [h for h in hits if h.get("ai_tier") is None and h.get("ai_relevant") is None]

    if not to_evaluate:
        return 0

    prompt = f"""Analysiere diese Inserate für meine Suche nach '{search["query"]}' und erstelle eine Tier-List mit Fokus auf die besten 30 %.
a) Relevanz-Check: Prüfe, ob das Inserat wirklich das gesuchte Produkt ist oder ein Fehltreffer durch mehrdeutige Begriffe oder bloßes Zubehör/Ersatzteil. Markiere Fehltreffer mit ai_relevant = false und kurzem Grund (ai_reason).
b) Tier-List: Bewerte alle relevanten Inserate in Tiers ("S" / "A" für die besten 30 % mit detaillierter Begründung zu Preis-Leistung und Zustand in ai_reason, sowie "B" und "C" für den Rest mit knapper Einschätzung).

Hier sind die Inserate im JSON Format:
"""
    items = []
    for h in to_evaluate:
        desc = h.get("description_full")
        desc = desc[:1000] if desc else ""
        items.append({
            "key": h["key"],
            "title": h["title"],
            "price": f"{h['price']} {h['currency']}" if h.get("price") else "Kein Preis",
            "description": desc
        })
    prompt += json.dumps(items, ensure_ascii=False)

    system_instruction = """Antworte AUSSCHLIESSLICH mit gültigem JSON, das ein Array von Objekten ist, ohne Markdown Blocks (z.B. kein ```json). Jedes Objekt muss die folgenden Felder haben:
- key: (string) der Inserat key
- ai_relevant: (boolean)
- ai_tier: (string) "S", "A", "B", oder "C" (null if irrelevant)
- ai_reason: (string) Begründung"""

    url = f"https://generativelanguage.googleapis.com/v1beta/models/gemini-2.5-flash:generateContent?key={api_key}"
    payload = {
        "systemInstruction": {"parts": [{"text": system_instruction}]},
        "contents": [{"parts": [{"text": prompt}]}],
        "generationConfig": {"temperature": 0.2}
    }

    try:
        resp = httpx.post(url, json=payload, timeout=45)
        resp.raise_for_status()
        data = resp.json()

        text = data["candidates"][0]["content"]["parts"][0]["text"].strip()
        if text.startswith("```json"):
            text = text[7:]
        if text.endswith("```"):
            text = text[:-3]

        results = json.loads(text.strip())

        count = 0
        for r in results:
            key = r.get("key")
            if not key: continue

            db.update_listing_ai(
                key=key,
                relevant=bool(r.get("ai_relevant", True)),
                tier=r.get("ai_tier"),
                reason=r.get("ai_reason")
            )
            count += 1
        return count
    except Exception as exc:
        log.error("Gemini API Error: %s", exc)
        return 0
