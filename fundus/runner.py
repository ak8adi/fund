"""Ein Prüflauf: alle aktiven Suchen × alle eingeschalteten Quellen, je genau eine Anfrage."""

from __future__ import annotations

import logging
import random
import threading
import time

from . import db
from .adapters import AdapterError, BlockedError, all_adapters, adapter_for_source
from .adapters.base import fetch_html
from .adapters.extract import extract_description_from_page
from .config import get_config
from .models import Listing
from .notify import notify_hits
from .trust import assess, price_in_chf, reference_price

log = logging.getLogger(__name__)
_run_lock = threading.Lock()


def available_sources(cfg: dict) -> list[dict]:
    """Alle Quellen mit Status für die Oberfläche."""
    adapters = all_adapters(cfg)
    names = [s for a in adapters for s in a.sources()]
    db.ensure_platforms(names)
    states = db.platform_states()
    out = []
    for a in adapters:
        ready, hint = a.configured()
        for src in a.sources():
            st = states.get(src, {})
            out.append({
                "name": src,
                "adapter": a.name,
                "label": a.source_label(src),
                "enabled": bool(st.get("enabled", 1)),
                "ready": ready,
                "hint": hint,
                "last_run": st.get("last_run"),
                "last_count": st.get("last_count"),
                "last_error": st.get("last_error"),
            })
    return out


def is_running() -> bool:
    return _run_lock.locked()


def _filter(listings: list[Listing], search: dict, fx: dict) -> list[Listing]:
    out = []
    for l in listings:
        title = l.title.lower()
        if any(w in title for w in search["exclude_words"]):
            continue
        chf = price_in_chf(l.price, l.currency, fx)
        if chf is not None:
            if search.get("min_price") is not None and chf < search["min_price"]:
                continue
            if search.get("max_price") is not None and chf > search["max_price"]:
                continue
        out.append(l)
    return out


def run(trigger: str = "manuell", search_ids: list[int] | None = None) -> dict:
    if not _run_lock.acquire(blocking=False):
        return {"skipped": True}
    try:
        return _run(trigger, search_ids)
    finally:
        _run_lock.release()


def _run(trigger: str, search_ids: list[int] | None) -> dict:
    cfg = get_config()
    fx = cfg.get("fx_to_chf", {})
    adapters = all_adapters(cfg)
    sources = [s for s in available_sources(cfg) if s["enabled"] and s["ready"]]
    labels = {a.name: a.label for a in adapters}
    searches = [s for s in db.list_searches(active_only=True) if search_ids is None or s["id"] in search_ids]

    run_id = db.start_run(trigger)
    total_new, errors = 0, 0
    source_counts: dict[str, int] = {}
    source_errors: dict[str, str] = {}
    blocked: set[str] = set()
    lo, hi = cfg.get("request_delay", [4, 11])
    first_request = True

    for search in searches:
        collected: list[Listing] = []
        for src in sources:
            if search["platforms"] and src["adapter"] not in search["platforms"]:
                continue
            if src["name"] in blocked:
                continue  # einmal geblockt → in diesem Lauf nicht weiter anklopfen
            if not first_request:
                time.sleep(random.uniform(lo, hi))
            first_request = False
            adapter = adapter_for_source(adapters, src["name"])
            try:
                found = adapter.search(search["query"], src["name"], search.get("min_price"), search.get("max_price"))
                for l in found:
                    l.source = l.source or src["name"]
                collected.extend(found)
                source_counts[src["name"]] = source_counts.get(src["name"], 0) + len(found)
                log.info("%s / %s: %d Treffer", search["query"], src["name"], len(found))
            except AdapterError as exc:
                if isinstance(exc, BlockedError):
                    blocked.add(src["name"])
                errors += 1
                source_errors[src["name"]] = str(exc)
                log.warning("%s / %s: %s", search["query"], src["name"], exc)
            except Exception as exc:  # Netzwerk, unerwartete Antwort …
                errors += 1
                source_errors[src["name"]] = f"{type(exc).__name__}: {exc}"
                log.exception("%s / %s fehlgeschlagen", search["query"], src["name"])

        collected = _filter(collected, search, fx)
        median = reference_price(collected, fx)
        new_hits: list[dict] = []
        for l in collected:
            t = assess(l, median, fx)
            db.upsert_listing(l, t.level, t.reasons)
            if db.add_hit(search["id"], l.key, is_new=bool(search["baseline_done"])) and search["baseline_done"]:
                new_hits.append({
                    "key": l.key, "title": l.title, "price": l.price, "currency": l.currency, "url": l.url,
                    "image": l.image, "platform": l.platform, "location": l.location,
                    "trust_level": t.level, "trust_label": t.label, "trust_reasons": t.reasons,
                })
        db.mark_search_run(search["id"])
        total_new += len(new_hits)
        if new_hits:
            db.mark_notified(search["id"], notify_hits(cfg, search, new_hits, labels))

        # Detailbeschreibungen im Hintergrund für max 15 Inserate laden
        desc_count = 0
        for hit in new_hits:
            if desc_count >= 15:
                break
            if hit["platform"] in ("ebay", "ricardo"):
                continue # eBay hat oft keine oder nur umständliche iFrames für Descriptions, Fokus auf CH/DE Plattformen

            url = hit["url"]
            key = hit["key"]
            try:
                time.sleep(random.uniform(2, 4))

                adapter = adapter_for_source(adapters, hit["platform"])
                fetcher = adapter.platform_cfg().get("fetcher", "http") if hasattr(adapter, "platform_cfg") else "http"
                page = fetch_html(url, fetcher)
                desc = extract_description_from_page(page, hit["platform"])

                if desc:
                    db.update_listing_description(key, desc)
                desc_count += 1
            except Exception as exc:
                log.warning("Konnte Beschreibung für %s nicht laden: %s", url, exc)

        if desc_count < 15:
            existing_hits = db.hits_for_search(search["id"])
            new_keys = {h["key"] for h in new_hits}

            excl = [w.lower() for w in (search.get("exclude_words") or []) if w]

            for hit in existing_hits:
                if desc_count >= 15:
                    break
                if hit["key"] in new_keys or hit.get("description_full"):
                    continue
                if hit["platform"] in ("ebay", "ricardo"):
                    continue

                # Titel-Vorabcheck für existierende Inserate
                if excl and hit.get("title"):
                    if any(w in hit["title"].lower() for w in excl):
                        db.update_listing_ai(key=hit["key"], relevant=False, tier=None, reason="Ausgeschlossener Begriff im Titel (Pre-Check)", score=None)
                        continue

                try:
                    time.sleep(random.uniform(2, 4))
                    adapter = adapter_for_source(adapters, hit["platform"])
                    fetcher = adapter.platform_cfg().get("fetcher", "http") if hasattr(adapter, "platform_cfg") else "http"
                    page = fetch_html(hit["url"], fetcher)
                    desc = extract_description_from_page(page, hit["platform"])

                    if desc:
                        db.update_listing_description(hit["key"], desc)
                    desc_count += 1
                except Exception as exc:
                    log.warning("Konnte Beschreibung für bestehenden Treffer %s nicht laden: %s", hit["url"], exc)

        # Automatische KI-Auswertung der neuen Treffer durchführen, falls Gemini API Key hinterlegt ist
        if cfg.get("ai", {}).get("gemini_api_key"):
            try:
                from .ai import start_background_evaluation
                start_background_evaluation(search["id"])
            except Exception as exc:
                log.warning("Automatische KI-Auswertung fehlgeschlagen: %s", exc)

    for src in sources:
        if any(not s["platforms"] or src["adapter"] in s["platforms"] for s in searches):
            db.record_platform_result(src["name"], source_counts.get(src["name"], 0), source_errors.get(src["name"]))
    db.finish_run(run_id, total_new, errors)
    return {"searches": len(searches), "new": total_new, "errors": errors}


def run_in_background(trigger: str = "manuell", search_ids: list[int] | None = None) -> bool:
    if is_running():
        return False
    threading.Thread(target=run, args=(trigger, search_ids), daemon=True).start()
    return True
