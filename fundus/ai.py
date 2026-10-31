import base64
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
from .adapters.extract import extract_description_from_page, listing_images, shipping_from_page
from . import scoring
from .matching import excluded, matches_query
from .notify import image_url, notify_top_hits

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
                 models: tuple[str, ...] = ("gemini-3.5-flash-lite", "gemini-3.7-flash"),
                 images: list[tuple[str, bytes]] | None = None):
    parts = [{"text": prompt}] + [{"inline_data": {"mime_type": mime, "data": base64.b64encode(data).decode()}}
                                  for mime, data in images or []]
    payload = {
        "systemInstruction": {"parts": [{"text": system_instruction}]},
        "contents": [{"parts": parts}],
        "generationConfig": {"temperature": 0.1, "responseMimeType": "application/json"},
    }
    last_exc = None
    for model in models:
        for attempt in range(2):  # Zeitüberschreitung oder Überlast (5xx) sind meist nach kurzer Pause weg
            try:
                resp = httpx.post(f"{GEMINI_URL}/{model}:generateContent", json=payload,
                                  headers=_gemini_headers(api_key), timeout=60)
                resp.raise_for_status()
                text = resp.json()["candidates"][0]["content"]["parts"][0]["text"].strip()
                return json.loads(text.removeprefix("```json").removeprefix("```").removesuffix("```").strip())
            except Exception as exc:
                last_exc = exc
                log.info("Gemini %s fehlgeschlagen: %s", model, exc)
                transient = isinstance(exc, httpx.TimeoutException) or (
                    isinstance(exc, httpx.HTTPStatusError) and exc.response.status_code >= 500)
                if not transient:
                    break
                time.sleep(3)
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
    shipping = listing.get("ship_flag") or shipping  # Angabe der Plattform schlägt die Schätzung aus dem Text
    if relevant and shipping == "nein" and search.get("require_shipping"):
        relevant, reason = False, db.NO_SHIPPING
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


def apply_platform_shipping(key: str, flag: str | None, rescore: bool = True) -> set[int]:
    """Versandangabe der Plattform übernehmen – gleich für alle Plattformen und alle Suchen mit dem Inserat.

    Bewertete Treffer bekommen sie sofort (Lieferweg, Endpreis); bei «Nur mit Versand» und «nein» fliegt der
    Treffer raus. Wurde er wegen «kein Versand» aussortiert und die Plattform sagt «ja», prüft die KI neu.
    Gibt die betroffenen Suchen zurück."""
    if flag not in ("ja", "nein") or not db.update_listing_shipping(key, flag):
        return set()
    changed = set()
    for hit in db.hits_of_listing(key):
        sid = hit["search_id"]
        if hit["ai_relevant"] is None:
            continue  # die KI-Auswertung übernimmt die Angabe (_store_facts)
        if flag == "ja" and hit["ai_reason"] == db.NO_SHIPPING:
            db.reset_hit_ai(sid, key)
        else:
            db.set_hit_shipping(sid, key, flag)
            if flag == "nein" and hit["require_shipping"] and hit["ai_relevant"] == 1:
                db.set_hit_relevant(sid, key, False, db.NO_SHIPPING)
        changed.add(sid)
    if rescore:
        for sid in changed:
            scoring.rescore_search(sid)
    return changed


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
        elif search.get("require_shipping") and (h.get("ship_flag") == "nein" or (
                h.get("ship_flag") != "ja" and any(t in txt for t in _NO_SHIP))):
            db.update_hit_ai(search_id, h["key"], False, db.NO_SHIPPING)
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
        if h.get("description_full") or h["platform"] not in ("tutti", "anibis", "kleinanzeigen", "willhaben", "blocket"):
            continue
        try:
            time.sleep(random.uniform(1, 2))
            adapter = adapter_for_source(adapters, h["platform"])
            page = fetch_html(h["url"], adapter.platform_cfg().get("fetcher", "http"))
            desc = extract_description_from_page(page, h["platform"])
            if desc:
                db.update_listing_description(h["key"], desc)
                h["description_full"] = desc
            apply_platform_shipping(h["key"], shipping_from_page(page, h["platform"]), rescore=False)
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

# ---------- Bildanalyse (pro Suche, nur auf Auftrag) ----------

IMAGE_INSTRUCTION = """Du siehst die Fotos eines Inserats und einen Prüfauftrag. Antworte AUSSCHLIESSLICH mit einem
gültigen JSON-Objekt mit den Feldern:
- finding: (string) Antwort auf den Prüfauftrag anhand der Fotos, höchstens 12 Wörter, mit dem sichtbaren Merkmal,
  z.B. "Jahrgang 2022 (Logo-Design am Chickenloop)". Zeigen die Fotos es nicht: "auf den Fotos nicht erkennbar"
- relevant: (boolean) false NUR, wenn die Fotos klar ein anderes Produkt zeigen; sonst true
- year: (integer oder null) Modelljahr, nur wenn auf den Fotos bestimmbar
- condition: einer von "neu", "neuwertig", "sehr gut", "gut", "gebraucht", "defekt" – nur wenn die Fotos den Zustand
  klar zeigen, sonst null
- req: nur wenn Anforderungen genannt sind: "ja" / "nein" (nach Fotos und Text klar erfüllt bzw. nicht erfüllt)
  oder "unklar"; sonst null
- req_note: (string oder null) ganz kurz die Angabe zu den Anforderungen
- reason: (string) ein Satz Fazit zum Inserat inkl. Fotobefund
Erfinde nichts: was die Fotos nicht zeigen, bleibt null bzw. «nicht erkennbar»."""

IMAGE_BATCH = 8          # Inserate pro Durchgang, danach kurze Pause
MAX_IMAGE_BYTES = 4_000_000
_DETAIL_IMAGE_PLATFORMS = ("tutti", "anibis", "kleinanzeigen", "willhaben", "blocket")


def _image_prompt(search: dict, h: dict) -> str:
    prompt = f"""Ich suche: '{search["query"]}'.
Prüfauftrag für die Fotos: «{search["image_prompt"].strip()}»
"""
    if (search.get("requirements") or "").strip():
        prompt += f"Meine Anforderungen: «{search['requirements'].strip()}»\n"
    facts = {k: h.get(k) for k in ("ai_reason", "ai_condition", "ai_year", "ai_req", "ai_req_note") if h.get(k)}
    return prompt + "Inserat:\n" + json.dumps({
        "title": h["title"],
        "price": f"{h['price']:g} {h.get('currency') or ''}".strip() if h.get("price") else "Preis offen",
        "description": (h.get("description_full") or "")[:800],
        "bisherige_bewertung_aus_dem_text": facts,
    }, ensure_ascii=False)


def _fetch_images(h: dict, adapters) -> list[tuple[str, bytes]]:
    """Bis 4 Fotos: die Galerie der Inseratseite, wo die Plattform Einzelabrufe zulässt, sonst das Vorschaubild gross."""
    urls = []
    if h["platform"] in _DETAIL_IMAGE_PLATFORMS:
        try:
            adapter = adapter_for_source(adapters, h["platform"])
            page = fetch_html(h["url"], adapter.platform_cfg().get("fetcher", "http"))
            urls = listing_images(page, h["platform"], h["external_id"])
            apply_platform_shipping(h["key"], shipping_from_page(page, h["platform"]), rescore=False)
        except GoneError:
            db.mark_listing_gone(h["key"])
            return []
        except Exception as exc:
            log.info("Galerie von %s nicht geladen: %s", h["url"], exc)
    if not urls and h.get("image"):
        urls = [image_url(h["image"], large=True)]
    images = []
    for url in urls:
        try:
            r = httpx.get(url, timeout=20, follow_redirects=True, headers={"User-Agent": "Mozilla/5.0"})
            mime = r.headers.get("content-type", "").split(";")[0]
            if r.status_code == 200 and mime.startswith("image/") and len(r.content) <= MAX_IMAGE_BYTES:
                images.append((mime, r.content))
        except Exception as exc:
            log.info("Bild %s nicht geladen: %s", url, exc)
    return images


def _store_image_result(search: dict, h: dict, r: dict, prompt: str) -> None:
    """Bildbefund über die Text-Bewertung legen: Jahrgang, Zustand und Anforderung können sich ändern,
    ein klar anderes Produkt fliegt raus, eine nur am Text gescheiterte Anforderung kann jetzt erfüllt sein."""
    finding = str(r.get("finding") or "")[:120] or None
    has_req = bool((search.get("requirements") or "").strip())
    req = r.get("req") if has_req and r.get("req") in ("ja", "nein") else h.get("ai_req")
    req_note = (str(r["req_note"])[:80] if has_req and r.get("req") in ("ja", "nein") and r.get("req_note")
                else h.get("ai_req_note"))
    old_reason = h.get("ai_reason") or ""
    relevant = h.get("ai_relevant") == 1 or (old_reason.startswith(db.REQ_FAILED) and req == "ja")
    reason = str(r.get("reason") or "")[:300] or old_reason
    if r.get("relevant") is False:
        relevant, reason = False, f"Laut Fotos nicht das gesuchte Produkt – {finding or reason}"
    elif req == "nein":
        relevant, reason = False, f"{db.REQ_FAILED} ({req_note or finding or 'laut Fotos'})"
    try:
        year = int(r["year"]) if r.get("year") else h.get("ai_year")
    except (TypeError, ValueError):
        year = h.get("ai_year")
    if year is not None and not 1980 <= year <= datetime.now().year + 1:
        year = h.get("ai_year")
    condition = r.get("condition") if r.get("condition") in CONDITIONS[:-1] else h.get("ai_condition")
    db.set_hit_image_result(search["id"], h["key"], prompt, finding, relevant, reason, condition, year, req, req_note)


def analyze_images(search_id: int) -> int:
    """Wertet die Fotos von bis zu IMAGE_BATCH offenen Treffern aus. Gibt die Zahl erledigter zurück (0 = fertig)."""
    api_key = get_config().get("ai", {}).get("gemini_api_key")
    search = db.get_search(search_id)
    prompt = (search or {}).get("image_prompt", "").strip()
    if not api_key or not prompt:
        return 0
    todo = db.hits_for_image_check(search_id, prompt, limit=IMAGE_BATCH)
    adapters = all_adapters(get_config())
    done = 0
    for h in todo:
        images = _fetch_images(h, adapters)
        if not images:  # nichts zu sehen: als geprüft markieren, sonst käme der Treffer immer wieder
            db.set_hit_image_result(search_id, h["key"], prompt, "keine Fotos ladbar", h.get("ai_relevant") == 1,
                                    h.get("ai_reason"), h.get("ai_condition"), h.get("ai_year"), h.get("ai_req"),
                                    h.get("ai_req_note"))
            done += 1
            continue
        try:
            r = _gemini_json(_image_prompt(search, h), IMAGE_INSTRUCTION, api_key,
                             models=("gemini-3.7-flash", "gemini-3.5-flash-lite"), images=images)
        except Exception as exc:
            log.error("Bildanalyse fehlgeschlagen: %s", exc)
            break  # Gemini nicht erreichbar: beim nächsten Durchgang weiter
        if isinstance(r, list):
            r = r[0] if r and isinstance(r[0], dict) else {}
        if isinstance(r, dict):
            _store_image_result(search, h, r, prompt)
            done += 1
        time.sleep(random.uniform(1, 2))
    if done:
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
                    search = db.get_search(search_id) or {}
                    if search.get("image_auto") and search.get("image_prompt"):
                        # Bildanalyse ist für diese Suche aktiv: neue (und neu bewertete) Angebote gleich mit prüfen
                        while analyze_images(search_id):
                            time.sleep(3)
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
    hits = db.hits_for_search(search_id, include_hidden=True, limit=5000)
    h = next((x for x in hits if x["key"] == key), None)
    if not h:
        return ""
    search = db.get_search(search_id) or {}
    req = (search.get("requirements") or "").strip()
    req_line = f"Meine Anforderungen: {req} (Stand laut KI: {h.get('ai_req') or 'offen'}, {h.get('ai_req_note') or '–'})\n" if req else ""
    prompt = f"""Gesuchtes Produkt: {search.get('query', '')}
{req_line}Inserat-Titel: {h.get('title', '')}
Preis: {h.get('price')} {h.get('currency')}
Beschreibung: {(h.get('description_full') or '')[:1000]}
Bisheriges KI-Fazit: {h.get('ai_reason') or ''}
Bisheriger Chat: {h.get('seller_chat') or ''}

Formuliere eine kurze, höfliche, direkte Nachricht an den Verkäufer (2-4 Sätze, ohne Platzhalter in eckigen Klammern), die die wichtigsten offenen Punkte klärt (z.B. unklare Anforderungen, Zustand/Mängel, Einzelkauf falls Set, Versand in die Schweiz bzw.
nach Jestetten (DE), Alter/Rechnung oder ein realistisches Preisangebot). Schreibe in der Sprache des Inserats."""
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
