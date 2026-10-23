import json
import logging
import httpx
import time
import random

from . import db
from .config import get_config
from .adapters import all_adapters, adapter_for_source
from .adapters.base import fetch_html
from .adapters.extract import extract_description_from_page

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

    excl = [w.lower() for w in (search.get("exclude_words") or []) if w]
    if excl:
        kept = []
        for h in to_evaluate:
            txt = f"{h.get('title','')} {h.get('description_full','')}".lower()
            if any(w in txt for w in excl):
                db.update_listing_ai(key=h["key"], relevant=False, tier=None, reason="Ausgeschlossener Begriff", score=None)
            else:
                kept.append(h)
        to_evaluate = kept
    to_evaluate = to_evaluate[:20]
    if not to_evaluate:
        return 0

    # Detailbeschreibungen nachladen
    desc_count = 0
    adapters = all_adapters(cfg)
    for h in to_evaluate:
        if desc_count >= 8:
            break
        if h.get("description_full"):
            continue
        if h["platform"] in ("ebay", "ricardo"):
            continue

        try:
            time.sleep(random.uniform(1, 2))
            adapter = adapter_for_source(adapters, h["platform"])
            fetcher = adapter.platform_cfg().get("fetcher", "http") if hasattr(adapter, "platform_cfg") else "http"
            page = fetch_html(h["url"], fetcher)
            desc = extract_description_from_page(page, h["platform"])

            if desc:
                db.update_listing_description(h["key"], desc)
                h["description_full"] = desc
            desc_count += 1
        except Exception as exc:
            log.warning("Konnte Beschreibung für %s nicht laden: %s", h["url"], exc)

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
- ai_score: (integer) 0 bis 100
- ai_reason: (string) Begründung"""

    def make_request(model):
        url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent?key={api_key}"
        payload = {
            "systemInstruction": {"parts": [{"text": system_instruction}]},
            "contents": [{"parts": [{"text": prompt}]}],
            "generationConfig": {"temperature": 0.2}
        }
        resp = httpx.post(url, json=payload, timeout=45)
        resp.raise_for_status()
        return resp.json()

    try:
        try:
            data = make_request("gemini-3.8-flash")
        except Exception:
            log.info("gemini-3.8-flash fehlgeschlagen, nutze Fallback gemini-2.0-flash-lite")
            data = make_request("gemini-2.0-flash-lite")

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
                reason=r.get("ai_reason"),
                score=r.get("ai_score")
            )
            count += 1
        return count
    except Exception as exc:
        log.error("Gemini API Error: %s", exc)
        return 0
