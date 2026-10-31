import json
import logging
import re
import httpx
import time
from datetime import datetime
import random

from . import db
from .config import get_config
from .adapters import all_adapters, adapter_for_source
from .adapters.base import GoneError, fetch_html
from .adapters.extract import extract_description_from_page
from . import scoring
from .matching import excluded, matches_query
from .notify import notify_top_hits

log = logging.getLogger(__name__)
GEMINI_URL = "https://generativelanguage.googleapis.com/v1beta/models"


def _gemini_headers(api_key: str) -> dict:
    # Schlüssel im Header statt in der URL – URLs landen in Logs und Fehlermeldungen
    return {"x-goog-api-key": api_key}


_NO_SHIP = ("kein versand", "nur abholung", "nur selbstabholung", "no shipping", "collection only", "pickup only", "pas d'envoi", "solo ritiro", "non spedisco", "ingen frakt")
CONDITIONS = ("neu", "neuwertig", "sehr gut", "gut", "gebraucht", "defekt", "unbekannt")

FACTS_INSTRUCTION = """Antworte AUSSCHLIESSLICH mit gültigem JSON: ein Array mit einem Objekt pro Inserat, Felder:
- key: (string) der Inserat-Key
- relevant: (boolean) ist es wirklich das gesuchte Produkt (nicht Zubehör, Ersatzteil, anderes Modell)?
- reason: (string) ein Satz: warum (nicht) relevant, plus Auffälliges (Mängel, Set-Inhalt, Alter, Rechnung)
- condition: einer von "neu", "neuwertig", "sehr gut", "gut", "gebraucht", "defekt", "unbekannt"
- shipping: "ja" (Versand angeboten), "nein" (nur Abholung) oder "unbekannt"
- shipping_cost: (number oder null) Versandkosten, nur wenn im Inserat genannt, in der Währung des Inserats
- year: (integer oder null) Modelljahr bzw. Kaufjahr, nur wenn im Inserat genannt
- set_items: (string oder null) nur wenn das Inserat mehr als das gesuchte Produkt enthält (Set, Paket, Bundle):
  kurz, was alles dabei ist, z.B. "2 Kites EVO SLS 9 & 11 m, Trust Bar"; sonst null
- single_price: (number oder null) bei Sets der Preis des gesuchten Produkts allein, in der Währung des Inserats.
  Steht er im Text, diesen nehmen. Sonst schätzen: Setpreis nach den typischen Gebrauchtpreisen der Teile
  aufteilen (dein Wissen über das Produkt und die übrigen Teile). Kein Set → null
- size: Versandgrösse des Inserats, verpackt (bei Sets alles zusammen): "S" (Päckchen bis 35×25×10 cm),
  "M" (bis 60×30×15 cm), "L" (bis 120×60×60 cm und 31 kg), "XL" (sperrig, bis 200 cm lang),
  "fracht" (Möbel o.ä., zu gross oder schwer für ein Paket)
- weight_kg: (number) geschätztes Versandgewicht in kg
- single_price_estimated: (boolean) true, wenn single_price geschätzt und nicht im Text genannt ist
- req: nur wenn Anforderungen genannt sind: "ja" (klar erfüllt), "nein" (klar nicht erfüllt) oder "unklar"
  (Angabe fehlt im Inserat); sonst null
- req_note: (string oder null) die gefundene Angabe zu den Anforderungen, ganz kurz, z.B. "Länge 140 cm",
  "Länge nicht angegeben", "180 × 90 cm"
- scam: (boolean) true bei klaren Hinweisen auf Betrug in Beschreibung oder seller_chat, z.B. Vorauskasse per
  Freunde-Zahlung, Kontakt nur über WhatsApp/E-Mail, Kurierdienst-Masche oder ein Vermerk wie «Betrug»; sonst false
Bewerte NICHT, ob der Preis gut ist – das rechnet das Programm selbst."""


def _facts_prompt(search: dict, items: list[dict]) -> str:
    prompt = f"""Ich suche: '{search["query"]}'. Lies die Inserate und liefere Fakten pro Inserat.
Fehltreffer (mehrdeutige Begriffe, blosses Zubehör, Ersatzteile, anderes Modell/Grösse) → relevant = false.
"""
    if not search.get("allow_sets", True):
        prompt += """Ich suche NUR das Einzelprodukt. Reine Sets → relevant = false, ausser im Text steht, dass das gesuchte
Produkt auch einzeln verkauft wird: dann relevant = true und single_price setzen.
"""
    else:
        prompt += """Sets mit dem gesuchten Produkt sind relevant; set_items und single_price (genannt oder geschätzt) setzen.
"""
    if (search.get("requirements") or "").strip():
        prompt += f"""Meine Anforderungen: «{search["requirements"].strip()}»
Prüfe sie an Titel, Beschreibung und Verkäufer-Chat. Rechne Einheiten um (z.B. 1,4 m = 140 cm; «140x80» bei Tischen
heisst meist Länge × Breite). Klar nicht erfüllt → req = "nein", reason nennt den Wert. Steht die Angabe nirgends
→ req = "unklar". WICHTIG: relevant beurteilt nur, ob es das gesuchte Produkt ist – unabhängig von den Anforderungen,
die wertet das Programm über req selbst aus.
"""
    return prompt + "Inserate:\n" + json.dumps(items, ensure_ascii=False)


def _gemini_json(prompt: str, system_instruction: str, api_key: str,
                 models: tuple[str, ...] = ("gemini-3.5-flash-lite", "gemini-3.7-flash")):
    payload = {
        "systemInstruction": {"parts": [{"text": system_instruction}]},
        "contents": [{"parts": [{"text": prompt}]}],
        "generationConfig": {"temperature": 0.1, "responseMimeType": "application/json"},
    }
    last_exc = None
    for model in models:
        try:
            resp = httpx.post(f"{GEMINI_URL}/{model}:generateContent", json=payload,
                              headers=_gemini_headers(api_key), timeout=60)
            resp.raise_for_status()
            text = resp.json()["candidates"][0]["content"]["parts"][0]["text"].strip()
            return json.loads(text.removeprefix("```json").removeprefix("```").removesuffix("```").strip())
        except Exception as exc:
            last_exc = exc
            log.info("Gemini %s fehlgeschlagen: %s", model, exc)
    raise RuntimeError(f"Gemini nicht erreichbar: {last_exc}")


_SCAM_NOTE = re.compile(r"(?<!kein )(?<!keine )(?<!nicht )\b(betrug|betrüg|scam|fake|abzocke|fälschung)", re.I)


def flags_scam(note: str | None) -> bool:
    """Eigener Vermerk im Feld «Antwort des Verkäufers», z.B. «betrugsmasche» – gilt ohne Rückfrage an die KI."""
    return bool(note and _SCAM_NOTE.search(note))


def _store_facts(search: dict, r: dict) -> bool:
    key = r.get("key")
    if not key:
        return False
    relevant = bool(r.get("relevant", True))
    reason = r.get("reason")
    listing = db.get_listing(key) or {}
    if r.get("scam") is True or flags_scam(listing.get("seller_chat")):
        # Ausblenden statt nur abwerten: bei Sortierung nach Preis stünde es sonst trotzdem zuoberst
        relevant, reason = False, f"{db.SCAM} – {reason}" if reason else db.SCAM
    condition = r.get("condition") if r.get("condition") in CONDITIONS else "unbekannt"
    shipping = r.get("shipping") if r.get("shipping") in ("ja", "nein", "unbekannt") else "unbekannt"
    if relevant and shipping == "nein" and search.get("require_shipping"):
        relevant, reason = False, "Kein Versand (nur Abholung)"
    try:
        single = float(r["single_price"]) if r.get("single_price") is not None else None
    except (TypeError, ValueError):
        single = None
    estimated = bool(single and r.get("single_price_estimated"))
    size = r.get("size") if r.get("size") in ("S", "M", "L", "XL", "fracht") else None
    try:
        weight = float(r["weight_kg"]) if r.get("weight_kg") else None
    except (TypeError, ValueError):
        weight = None
    set_items = str(r["set_items"])[:120] if r.get("set_items") else None
    try:
        year = int(r["year"]) if r.get("year") else None
    except (TypeError, ValueError):
        year = None
    if year is not None and not 1980 <= year <= datetime.now().year + 1:
        year = None
    try:
        ship_cost = float(r["shipping_cost"]) if r.get("shipping_cost") is not None else None
    except (TypeError, ValueError):
        ship_cost = None
    req = r.get("req") if r.get("req") in ("ja", "nein", "unklar") else None
    if not (search.get("requirements") or "").strip():
        req = None
    note = str(r["req_note"])[:80] if req and r.get("req_note") else None
    if req == "nein" and relevant:
        relevant, reason = False, f"{db.REQ_FAILED} ({note or 'laut Inserat'})"
    db.update_hit_ai(search["id"], key, relevant, reason, condition, shipping, single, year, ship_cost, req, note,
                     set_items, estimated, size, weight)
    return True


def evaluate_listings(search_id: int) -> int:
    """Bewertet bis zu 20 offene Treffer einer Suche. Gibt die Zahl erledigter Treffer zurück (0 = fertig)."""
    cfg = get_config()
    api_key = cfg.get("ai", {}).get("gemini_api_key")
    search = db.get_search(search_id)
    if not api_key or not search:
        return 0

    hits = db.hits_for_search(search_id, limit=1000)
    open_hits = [h for h in hits if h.get("ai_relevant") is None and not h.get("gone_at")]

    # Günstige Vorprüfungen ohne Gemini
    done = 0
    to_evaluate = []
    for h in open_hits:
        txt = f"{h.get('title','')} {h.get('description_full') or ''}".lower()
        if excluded(search.get("exclude_words") or [], h.get("title")):
            db.update_hit_ai(search_id, h["key"], False, "Ausgeschlossener Begriff")
            done += 1
        elif not matches_query(search["query"], h.get("title", "")):
            # Treffer von vor dem Vorfilter (runner._filter)
            db.update_hit_ai(search_id, h["key"], False, "Suchwörter fehlen im Titel")
            done += 1
        elif search.get("require_shipping") and any(t in txt for t in _NO_SHIP):
            db.update_hit_ai(search_id, h["key"], False, "Kein Versand (nur Abholung)")
            done += 1
        else:
            to_evaluate.append(h)
    to_evaluate = to_evaluate[:20]
    if not to_evaluate:
        if done:
            scoring.rescore_search(search_id)
        return done

    # Detailbeschreibungen nachladen – Ricardo nicht: sperrt bei Einzelabrufen schnell,
    # der Prüflauf lädt dort nur neue Treffer (runner.py)
    adapters = all_adapters(cfg)
    for h in to_evaluate:
        if h.get("description_full") or h["platform"] not in ("tutti", "kleinanzeigen", "willhaben"):
            continue
        try:
            time.sleep(random.uniform(1, 2))
            adapter = adapter_for_source(adapters, h["platform"])
            page = fetch_html(h["url"], adapter.platform_cfg().get("fetcher", "http"))
            desc = extract_description_from_page(page, h["platform"])
            if desc:
                db.update_listing_description(h["key"], desc)
                h["description_full"] = desc
        except GoneError:
            db.mark_listing_gone(h["key"])
        except Exception as exc:
            log.warning("Konnte Beschreibung für %s nicht laden: %s", h["url"], exc)

    items = [{
        "key": h["key"],
        "title": h["title"],
        "price": f"{h['price']:g} {h.get('currency') or ''}".strip() if h.get("price") else "Preis offen",
        "description": (h.get("description_full") or "")[:1000],
        "seller_chat": (h.get("seller_chat") or "")[:1000],
    } for h in to_evaluate]
    try:
        results = _gemini_json(_facts_prompt(search, items), FACTS_INSTRUCTION, api_key)
    except Exception as exc:
        log.error("KI-Auswertung fehlgeschlagen: %s", exc)
        return done
    for r in results if isinstance(results, list) else []:
        if isinstance(r, dict) and _store_facts(search, r):
            done += 1
    scoring.rescore_search(search_id)
    return done

import threading
AI_RUNNING = False
_active_evaluations = set()
_eval_lock = threading.Lock()

def start_background_evaluation(search_id: int):
    with _eval_lock:
        if search_id in _active_evaluations:
            return
        _active_evaluations.add(search_id)

    def _eval_loop():
        global AI_RUNNING
        AI_RUNNING = True
        log.info("Starte Hintergrund-Auswertung für Suche %s", search_id)
        try:
            while True:
                processed = evaluate_listings(search_id)
                if processed == 0:
                    with _eval_lock:
                        if len(_active_evaluations) <= 1:
                            AI_RUNNING = False
                    log.info("Hintergrund-Auswertung für Suche %s beendet", search_id)
                    notify_top_hits(search_id)
                    break
                time.sleep(7) # 5-10 seconds pause
        finally:
            with _eval_lock:
                _active_evaluations.discard(search_id)
                if not _active_evaluations:
                    AI_RUNNING = False

    threading.Thread(target=_eval_loop, daemon=True).start()


def _call_gemini_text(prompt: str, system_instruction: str = "") -> str:
    cfg = get_config()
    api_key = cfg.get("ai", {}).get("gemini_api_key")
    if not api_key:
        return ""
    payload = {
        "contents": [{"parts": [{"text": prompt}]}],
        "generationConfig": {"temperature": 0.3}
    }
    if system_instruction:
        payload["systemInstruction"] = {"parts": [{"text": system_instruction}]}
    for model in ("gemini-3.5-flash-lite", "gemini-3.7-flash"):
        url = f"{GEMINI_URL}/{model}:generateContent"
        try:
            for _ in range(2):
                resp = httpx.post(url, json=payload, headers=_gemini_headers(api_key), timeout=45)
                if resp.status_code != 503:
                    break
                time.sleep(2)
            resp.raise_for_status()
            data = resp.json()
            return data["candidates"][0]["content"]["parts"][0]["text"].strip()
        except Exception as exc:
            log.warning("Gemini Call (%s) fehlgeschlagen: %s", model, exc)
    return ""

def generate_seller_question(key: str, search_id: int) -> str:
    hits = db.hits_for_search(search_id)
    h = next((x for x in hits if x["key"] == key), None)
    if not h:
        return ""
    search = db.get_search(search_id) or {}
    prompt = f"""Gesuchtes Produkt: {search.get('query', '')}
Inserat-Titel: {h.get('title', '')}
Preis: {h.get('price')} {h.get('currency')}
Beschreibung: {(h.get('description_full') or '')[:1000]}
Bisheriges KI-Fazit: {h.get('ai_reason') or ''}
Bisheriger Chat: {h.get('seller_chat') or ''}

Formuliere eine kurze, höfliche, direkte Nachricht an den Verkäufer (2-4 Sätze, ohne Platzhalter in eckigen Klammern), die die wichtigsten offenen Punkte klärt (z.B. Zustand/Mängel, Einzelkauf falls Set, Alter/Rechnung oder ein realistisches Preisangebot)."""
    draft = _call_gemini_text(prompt, "Du bist ein erfahrener Gebrauchtwaren-Käufer. Antworte nur mit dem direkten Nachrichtentext.")
    if draft:
        db.update_listing_draft(key, draft)
    return draft

def reevaluate_single_listing(key: str, search_id: int) -> bool:
    """Nach einer Verkäufer-Antwort: Fakten für dieses eine Inserat neu erheben."""
    api_key = get_config().get("ai", {}).get("gemini_api_key")
    search = db.get_search(search_id)
    h = next((x for x in db.hits_for_search(search_id, limit=1000) if x["key"] == key), None)
    if not api_key or not search or not h:
        return False
    item = {
        "key": key,
        "title": h["title"],
        "price": f"{h['price']:g} {h.get('currency') or ''}".strip() if h.get("price") else "Preis offen",
        "description": (h.get("description_full") or "")[:1000],
        "seller_chat": (h.get("seller_chat") or "")[:1500],
    }
    prompt = _facts_prompt(search, [item]) + ("\nseller_chat ist die Antwort des Verkäufers oder eine Notiz von mir"
                                              " – beides hat Vorrang vor der Beschreibung.")
    try:
        results = _gemini_json(prompt, FACTS_INSTRUCTION, api_key)
    except Exception as exc:
        log.warning("Fehler bei Einzel-Neubewertung: %s", exc)
        return False
    r = results[0] if isinstance(results, list) and results else results
    if not isinstance(r, dict) or not _store_facts(search, {**r, "key": key}):
        return False
    scoring.rescore_search(search_id)
    return True
