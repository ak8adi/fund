"""Ein Prüflauf: alle aktiven Suchen × alle eingeschalteten Quellen, je genau eine Anfrage."""

from __future__ import annotations

import logging
import random
import threading
import time
from datetime import datetime, timedelta, timezone

from . import archive, db, fx as fx_rates, newprice, scoring
from .ai import apply_platform_shipping
from .adapters import AdapterError, BlockedError, GoneError, all_adapters, adapter_for_source
from .adapters.base import fetch_html
from .adapters.extract import extract_description_from_page, shipping_from_page, status_from_page
from .config import get_config
from .matching import excluded, matches_query
from .models import Listing
from .notify import notify_hits
from .trust import assess, price_in_chf

log = logging.getLogger(__name__)

# Ricardo: nur die Suchseite. Einzelne Inseratseiten lösen nach wenigen Abrufen das Cloudflare-Ratenlimit
# aus (429) – und die Sperre gilt dann auch für die Suche. Beschreibung, Versand und Fotos dort darum nicht laden.
NO_DETAIL_PAGES = ("ebay", "ricardo")


def _paused_sources(names: list[str]) -> dict[str, datetime]:
    """Quellen in der Pause nach einer Sperre → Ende der Pause (db.mark_platform_blocked)."""
    paused = {}
    for name, st in db.platform_states().items():
        if name in names and st.get("blocked_until"):
            until = datetime.fromisoformat(st["blocked_until"])
            if until > datetime.now(timezone.utc):
                paused[name] = until
    return paused
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
                "last_partial": st.get("last_partial"),
            })
    return out


def is_running() -> bool:
    return _run_lock.locked()


# Fortschritt des laufenden Scans für die Statuszeile: Suche i von n, darin Quelle j von m, danach Angebote prüfen
_progress: dict = {}


def progress() -> dict | None:
    """{"share": 0–1, "index": i (1-basiert), "total": n, "query": …, "step": …} oder None, wenn kein Scan läuft."""
    if not is_running() or not _progress.get("total"):
        return None
    p = dict(_progress)
    sources = max(1, p.get("sources", 1))
    within = 0.8 * min(p.get("source", 0), sources) / sources + (0.2 if p.get("step") == "prüft Angebote" else 0)
    p["share"] = min(1.0, (p["index"] - 1 + within) / p["total"])
    return p


# Plattformen, deren Inseratseite die Versandangabe als Feld zeigt (extract.shipping_from_page)
STATUS_CLIENT = {"kleinanzeigen": "chrome_android"}
SHORTLIST_RECHECK_HOURS = 2    # Shortlist und «knapp dahinter»: so oft auf reserviert/weg prüfen
EBAY_DETAILS_PER_SEARCH = 25   # API-Abrufe für Beschreibungen pro Suche und Lauf
SHIP_FLAG_PLATFORMS = ("kleinanzeigen", "tutti", "anibis", "willhaben")


def check_listing(adapters, hit: dict) -> str:
    """Ist das Inserat noch zu haben? «ok», «reserved» oder «gone»; speichert das Ergebnis.
    eBay über die API, Ricardo gar nicht (Laufzeitende reicht, keine Inseratseiten – siehe NO_DETAIL_PAGES).
    BlockedError und andere Fehler gehen an den Aufrufer."""
    adapter = adapter_for_source(adapters, hit["platform"])
    if hit["platform"] == "ebay":
        d = adapter.item_details(hit["external_id"], (hit.get("source") or "ebay:EBAY_DE").split(":")[-1])
        if d["gone"]:
            db.mark_listing_gone(hit["key"])
            return "gone"
        if d["description"]:
            db.update_listing_description(hit["key"], d["description"])
        db.mark_listing_checked(hit["key"])
        return "ok"
    fetcher = adapter.platform_cfg().get("fetcher", "http") if hasattr(adapter, "platform_cfg") else "http"
    try:  # Kleinanzeigen zeigt «Reserviert»/«Gelöscht» nur auf der mobilen Seite im HTML (Desktop: per Skript)
        page = fetch_html(hit["url"], fetcher, impersonate=STATUS_CLIENT.get(hit["platform"], "chrome"))
    except GoneError:
        db.mark_listing_gone(hit["key"])
        return "gone"
    status = status_from_page(page)
    if status == "gone":
        db.mark_listing_gone(hit["key"])
        return "gone"
    reserved = status == "reserved"
    db.set_listing_reserved(hit["key"], reserved)
    apply_platform_shipping(hit["key"], shipping_from_page(page, hit["platform"]))
    if not hit.get("description_full"):
        desc = extract_description_from_page(page, hit["platform"])
        if desc:
            db.update_listing_description(hit["key"], desc)
    return "reserved" if reserved else "ok"


def shortlist_to_check(search_id: int, rest: int = 6, max_age: timedelta | None = None) -> list[dict]:
    """Beste Angebote und die nächsten Angebote der Suchseite, deren letzte Prüfung älter als max_age
    (Standard SHORTLIST_RECHECK_HOURS) ist."""
    from . import views
    keys = views.top_keys(views.build(search_id)["offers"], rest)
    limit = datetime.now(timezone.utc) - (max_age or timedelta(hours=SHORTLIST_RECHECK_HOURS))
    out = []
    for key in keys:
        l = db.get_listing(key)
        if l and not l.get("gone_at") and l["platform"] != "ricardo" and (
                not l.get("checked_at") or datetime.fromisoformat(l["checked_at"]) < limit):
            out.append(l)
    return out


# ---------- Verfügbarkeits-Wächter: was oben auf der Suchseite steht, muss zu haben sein ----------

_watch_lock = threading.Lock()
_watching: set[int] = set()


def _blocked_platforms() -> set[str]:
    now = datetime.now(timezone.utc)
    return {name for name, st in db.platform_states().items()
            if st.get("blocked_until") and datetime.fromisoformat(st["blocked_until"]) > now}


def _twins(search_id: int, listing: dict) -> list[dict]:
    """Dasselbe Angebot unter anderer Nummer (neu eingestellt, Combo mehrfach inseriert): gleicher Titel und Preis."""
    sig = (" ".join((listing.get("title") or "").lower().split()), listing.get("price"))
    return [h for h in db.hits_for_search(search_id)
            if h["key"] != listing["key"] and not h.get("gone_at") and h["platform"] != "ricardo"
            and (" ".join((h.get("title") or "").lower().split()), h.get("price")) == sig]


def watch_search(search_id: int, max_age: timedelta | None = None, rounds: int = 3) -> int:
    """Prüft die Angebote oben auf der Suchseite (beste Angebote, knapp dahinter). Fällt eines weg, rückt das nächste
    nach – darum mehrere Runden, bis alles Sichtbare geprüft ist. Doppelinserate eines verschwundenen Angebots werden
    gleich mitgeprüft. Rückgabe: Anzahl weg/reserviert gefundener Inserate."""
    adapters = all_adapters(get_config())
    blocked = _blocked_platforms()
    failed: set[str] = set()
    changed = 0
    for _ in range(rounds):
        todo = [h for h in shortlist_to_check(search_id, max_age=max_age)
                if h["platform"] not in blocked and h["key"] not in failed]
        if not todo:
            break
        queue = list(todo)
        while queue:
            h = queue.pop(0)
            try:
                if h["platform"] != "ebay":
                    time.sleep(random.uniform(1.5, 3))
                status = check_listing(adapters, h)
            except BlockedError as exc:
                blocked.add(h["platform"])
                if h["platform"] != "ebay":
                    db.mark_platform_blocked(h["platform"], exc.retry_after)
                continue
            except Exception as exc:
                failed.add(h["key"])
                log.info("Verfügbarkeit von %s nicht prüfbar: %s", h["url"], exc)
                continue
            if status != "ok":
                changed += 1
                log.info("Wächter: %s ist %s", h["url"], "reserviert" if status == "reserved" else "weg")
                queue += [t for t in _twins(search_id, h) if t["key"] not in failed and t["platform"] not in blocked
                          and all(t["key"] != q["key"] for q in queue)]
    if changed:
        scoring.rescore_search(search_id)
    return changed


def watch_all() -> None:
    """Zeitplan-Job (tagsüber alle 30 Min.): alle aktiven Suchen. Läuft gerade ein Scan, übernimmt der das."""
    if is_running() or not _watch_lock.acquire(blocking=False):
        return
    try:
        for s in db.list_searches(active_only=True):
            if s["id"] in _watching:
                continue
            _watching.add(s["id"])
            try:
                watch_search(s["id"])
            finally:
                _watching.discard(s["id"])
    finally:
        _watch_lock.release()


def watch_in_background(search_id: int, max_age: timedelta = timedelta(minutes=30)) -> bool:
    """Beim Öffnen einer Suche: was länger als max_age ungeprüft oben steht, sofort im Hintergrund prüfen."""
    if search_id in _watching:
        return True
    if not shortlist_to_check(search_id, max_age=max_age):
        return False
    _watching.add(search_id)

    def work():
        try:
            watch_search(search_id, max_age=max_age)
        finally:
            _watching.discard(search_id)
    threading.Thread(target=work, daemon=True).start()
    return True


def is_watching(search_id: int) -> bool:
    return search_id in _watching


def _filter(listings: list[Listing], search: dict, fx: dict) -> list[Listing]:
    out = []
    for l in listings:
        if excluded(search["exclude_words"], l.title):
            continue
        if not matches_query(search["query"], l.title):
            continue
        chf = price_in_chf(l.price, l.currency, fx)
        if chf is not None:
            pure_auction = l.auction and not (l.bid_price and l.price and l.price > l.bid_price)
            if search.get("min_price") is not None and chf < search["min_price"] and not pure_auction:
                continue  # reine Auktion: das Gebot steigt noch, ein tiefer Stand sagt nichts über den Mindestpreis
            if search.get("max_price") is not None and chf > search["max_price"]:
                continue
        out.append(l)
    return out


AUTO_TRIGGERS = ("zeitplan", "nachgeholt")
DEEP_PAGES = 3    # Bestand beim ersten Abruf einer Quelle: so viele Seiten lesen (die besten Angebote sind oft nicht die neusten)
MORE_PAGES = 3    # später: höchstens bis zu dieser Seite nachblättern, solange eine Seite nur Unbekanntes zeigt


def _search_pages(adapter, search: dict, source: str, deep: bool, known: set[str], pause) -> tuple[list[Listing], set[str]]:
    """Seite 1 – und weitere Seiten, wo die Plattform sie liefert (adapter.paged):
    - deep (Bestand noch nie tief gelesen): bis DEEP_PAGES Seiten. Sonst kennt fundus nur die neusten ~25 Inserate
      und beantwortet «was ist das beste Angebot?» aus einem Ausschnitt.
    - sonst: nachblättern, solange eine volle Seite nur Unbekanntes zeigt – dann sind seit dem letzten Lauf mehr
      Inserate dazugekommen, als auf eine Seite passen (Kleinanzeigen bei MacBook-Suchen).
    Rückgabe: (Inserate, Keys aus tieferen Seiten des Bestands – zählen nicht als «neu»)."""
    args = (search["query"], source, search.get("min_price"), search.get("max_price"))
    found = adapter.search(*args)
    stock: set[str] = set()
    if not getattr(adapter, "paged", False):
        return found, stock
    last, seen = found, {l.key for l in found}
    for page in range(2, (DEEP_PAGES if deep else MORE_PAGES) + 1):
        if len(last) < 15 or not (deep or all(l.key not in known for l in last)):
            break  # letzte Seite war nicht voll bzw. enthielt Bekanntes: nichts verpasst
        pause()
        try:
            last = [l for l in adapter.search(*args, page=page) if l.key not in seen]
        except BlockedError:
            raise
        except AdapterError as exc:
            log.info("%s / %s Seite %d: %s", search["query"], source, page, exc)
            break
        if not last:
            break
        seen.update(l.key for l in last)
        found += last
        if deep:
            stock.update(l.key for l in last)
    return found, stock


def recently_manual(search: dict, hours: float, now: datetime | None = None) -> bool:
    """Wurde die Suche vor kurzem von Hand gestartet? Dann lässt der Zeitplan sie aus (keine Doppelsuche)."""
    if not hours or not search.get("manual_run_at"):
        return False
    started = datetime.fromisoformat(search["manual_run_at"])
    return (now or datetime.now(timezone.utc)) - started < timedelta(hours=hours)


def run(trigger: str = "manuell", search_ids: list[int] | None = None) -> dict:
    if not _run_lock.acquire(blocking=False):
        return {"skipped": True}
    try:
        return _run(trigger, search_ids)
    finally:
        _run_lock.release()


def _run(trigger: str, search_ids: list[int] | None) -> dict:
    cfg = get_config()
    fx = fx_rates.rates(cfg)
    adapters = all_adapters(cfg)
    sources = [s for s in available_sources(cfg) if s["enabled"] and s["ready"]]
    labels = {a.name: a.label for a in adapters}
    searches = [s for s in db.list_searches(active_only=True) if search_ids is None or s["id"] in search_ids]
    if trigger in AUTO_TRIGGERS:
        hours = float(cfg["schedule"].get("pause_after_manual_hours", 3) or 0)
        skipped = [s for s in searches if recently_manual(s, hours)]
        for s in skipped:
            log.info("%s: vor weniger als %g h von Hand gestartet – Zeitplan lässt sie aus", s["query"], hours)
        searches = [s for s in searches if s not in skipped]
        if not searches:
            return {"searches": 0, "new": 0, "errors": 0, "skipped_manual": len(skipped)}

    run_id = db.start_run(trigger)
    db.expire_ended_listings()
    total_new, errors = 0, 0
    source_counts: dict[str, int] = {}
    source_errors: dict[str, str] = {}
    source_tries: dict[str, int] = {}   # Abfragen je Quelle in diesem Lauf (für «1 von 13 fehlgeschlagen»)
    source_fails: dict[str, int] = {}
    paused = _paused_sources([s["name"] for s in sources])
    blocked: set[str] = set(paused)
    for name, until in paused.items():
        log.info("%s pausiert nach Sperre bis %s", name, until.isoformat(timespec="minutes"))
    lo, hi = cfg.get("request_delay", [4, 11])
    first_request = True

    _progress.clear()
    _progress.update(total=len(searches), index=0)
    for search in searches:
        collected: list[Listing] = []
        complete_sources: set[str] = set()
        returned: set[str] = set()   # alles, was die Plattformen geliefert haben – vor jedem Filter
        scanned_before = db.first_scans(search["id"])
        deep_before = db.deep_scans(search["id"])
        known = db.hit_keys(search["id"])
        stock: set[str] = set()       # Bestand: Treffer aus dem ersten Abruf einer Quelle (zählen nicht als «neu»)
        mine = [s for s in sources if not search["platforms"] or s["adapter"] in search["platforms"]]
        _progress.update(index=_progress["index"] + 1, query=search["query"], sources=len(mine), source=0, step="sucht")
        for src in sources:
            if search["platforms"] and src["adapter"] not in search["platforms"]:
                continue
            _progress["source"] += 1
            _progress["platform"] = src["label"]
            if src["name"] in blocked:
                continue  # einmal geblockt → in diesem Lauf nicht weiter anklopfen
            if not first_request:
                time.sleep(random.uniform(lo, hi))
            first_request = False
            adapter = adapter_for_source(adapters, src["name"])
            source_tries[src["name"]] = source_tries.get(src["name"], 0) + 1
            try:
                deep = getattr(adapter, "paged", False) and src["name"] not in deep_before
                found, deeper = _search_pages(adapter, search, src["name"], deep, known,
                                              lambda: time.sleep(random.uniform(lo, hi)))
                stock.update(deeper)  # Bestand aus tieferen Seiten: keine Meldungsflut, zählt nicht als «neu»
                if deep:
                    db.record_deep_scan(search["id"], src["name"])
                for l in found:
                    l.source = l.source or src["name"]
                collected.extend(found)
                returned.update(l.key for l in found)
                if src["name"] not in scanned_before:
                    stock.update(l.key for l in found)
                    db.record_first_scan(search["id"], src["name"])
                if getattr(adapter, "complete", False):  # Ricardo: vollständige Liste → Fehlende sind weg
                    complete_sources.add(src["adapter"])
                source_counts[src["name"]] = source_counts.get(src["name"], 0) + len(found)
                log.info("%s / %s: %d Treffer", search["query"], src["name"], len(found))
            except AdapterError as exc:
                if isinstance(exc, BlockedError):
                    blocked.add(src["name"])
                    db.mark_platform_blocked(src["name"], exc.retry_after)
                errors += 1
                source_fails[src["name"]] = source_fails.get(src["name"], 0) + 1
                source_errors[src["name"]] = str(exc)
                log.warning("%s / %s: %s", search["query"], src["name"], exc)
            except Exception as exc:  # Netzwerk, unerwartete Antwort …
                errors += 1
                source_fails[src["name"]] = source_fails.get(src["name"], 0) + 1
                source_errors[src["name"]] = f"{type(exc).__name__}: {exc}"
                log.exception("%s / %s fehlgeschlagen", search["query"], src["name"])

        collected = _filter(collected, search, fx)
        new_hits: list[dict] = []
        for l in collected:
            t = assess(l)
            db.upsert_listing(l, t.level, t.reasons)
            fresh = bool(search["baseline_done"]) and l.key not in stock  # neue Quelle: Bestand, keine Meldungsflut
            if db.add_hit(search["id"], l.key, is_new=fresh, baseline=not fresh) and fresh:
                new_hits.append({
                    "key": l.key, "title": l.title, "price": l.price, "currency": l.currency, "url": l.url,
                    "image": l.image, "platform": l.platform, "location": l.location,
                    "trust_level": t.level, "trust_label": t.label, "trust_reasons": t.reasons,
                })
        _progress.update(step="prüft Angebote", platform=None)
        for l in collected:  # Versand laut Suchergebnis-Karte (Kleinanzeigen, Willhaben, Blocket)
            apply_platform_shipping(l.key, l.shipping, rescore=False)
        for platform in complete_sources:
            for h in db.hits_for_search(search["id"]):
                if h["platform"] == platform and h["key"] not in returned and not h.get("gone_at") \
                        and matches_query(search["query"], h.get("title") or ""):
                    db.mark_listing_gone(h["key"])
                    log.info("%s: nicht mehr in der vollständigen Trefferliste – verkauft oder beendet: %s", platform, h["url"])
        db.mark_search_run(search["id"], manual=trigger not in AUTO_TRIGGERS)
        if newprice.is_stale(search):  # Neupreis beim ersten Lauf, danach monatlich auffrischen
            newprice.refresh(search["id"])
        scoring.rescore_search(search["id"])  # Preise können sich geändert haben
        if not cfg.get("ai", {}).get("gemini_api_key"):
            archive.snapshot(search["id"])  # ohne KI: gleich archivieren (sonst nach der Bewertung, ai.py)
        total_new += len(new_hits)
        if new_hits and not cfg.get("ai", {}).get("gemini_api_key"):
            # Ohne KI sofort melden; mit KI erst nach der Prüfung und nur Top-Angebote (notify.notify_top_hits)
            db.mark_notified(search["id"], notify_hits(cfg, search, new_hits, labels))

        # Detailbeschreibungen im Hintergrund für max 15 Inserate laden
        desc_count = 0
        for hit in new_hits:
            if desc_count >= 15:
                break
            if hit["platform"] in NO_DETAIL_PAGES or hit["platform"] in blocked:
                continue  # eBay: Beschreibung nur im iFrame; Ricardo: siehe NO_DETAIL_PAGES

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
                apply_platform_shipping(key, shipping_from_page(page, hit["platform"]))
                desc_count += 1
            except GoneError:
                db.mark_listing_gone(key)
                log.info("Inserat nicht mehr verfügbar: %s", url)
            except BlockedError as exc:
                blocked.add(hit["platform"])
                db.mark_platform_blocked(hit["platform"], exc.retry_after)
                log.warning("Konnte Beschreibung für %s nicht laden: %s", url, exc)
            except Exception as exc:
                log.warning("Konnte Beschreibung für %s nicht laden: %s", url, exc)

        if desc_count < 15:
            existing_hits = db.hits_for_search(search["id"])
            new_keys = {h["key"] for h in new_hits}

            for hit in existing_hits:
                if desc_count >= 15:
                    break
                if hit["key"] in new_keys or hit.get("description_full"):
                    continue
                if hit.get("ai_relevant") == 0 or hit.get("gone_at"):
                    continue  # Fehltreffer und verkaufte Inserate brauchen keine Beschreibung
                if hit["platform"] in NO_DETAIL_PAGES or hit["platform"] in blocked:
                    continue

                # Titel-Vorabcheck für existierende Inserate
                if excluded(search["exclude_words"], hit.get("title")):
                    db.update_hit_ai(search["id"], hit["key"], False, "Ausgeschlossener Begriff")
                    continue

                try:
                    time.sleep(random.uniform(2, 4))
                    adapter = adapter_for_source(adapters, hit["platform"])
                    fetcher = adapter.platform_cfg().get("fetcher", "http") if hasattr(adapter, "platform_cfg") else "http"
                    page = fetch_html(hit["url"], fetcher)
                    desc = extract_description_from_page(page, hit["platform"])

                    if desc:
                        db.update_listing_description(hit["key"], desc)
                    apply_platform_shipping(hit["key"], shipping_from_page(page, hit["platform"]))
                    desc_count += 1
                except GoneError:
                    db.mark_listing_gone(hit["key"])
                    log.info("Inserat nicht mehr verfügbar: %s", hit["url"])
                except BlockedError as exc:
                    blocked.add(hit["platform"])
                    db.mark_platform_blocked(hit["platform"], exc.retry_after)
                    log.warning("Konnte Beschreibung für bestehenden Treffer %s nicht laden: %s", hit["url"], exc)
                except Exception as exc:
                    log.warning("Konnte Beschreibung für bestehenden Treffer %s nicht laden: %s", hit["url"], exc)

        # eBay: Beschreibung und Ort über die API (Tageskontingent 5000, kein Scraping) – vor der KI-Bewertung
        ebay_todo = [h for h in db.hits_for_search(search["id"])
                     if h["platform"] == "ebay" and not h.get("description_full") and not h.get("gone_at")
                     and h.get("ai_relevant") != 0][:EBAY_DETAILS_PER_SEARCH]
        if ebay_todo and not any(b.startswith("ebay") for b in blocked):
            try:
                for hit in ebay_todo:
                    check_listing(adapters, hit)
            except BlockedError:
                blocked.add("ebay")
            except Exception as exc:
                log.warning("eBay-Details nicht ladbar: %s", exc)

        # Die besten Treffer, die nicht mehr in den Suchergebnissen auftauchen, direkt prüfen:
        # Nur Seite 1 wird abgefragt, «nicht gesehen» heisst also nicht «verkauft».
        # Dazu die besten Treffer ohne Versandangabe der Plattform: die Suchkarte zeigt «Nur Abholung» nicht immer.
        to_check = {h["key"]: h for h in db.top_hits_to_check(search["id"])}
        # Alles, was oben auf der Suchseite steht (beste Angebote + knapp dahinter), auch wenn es noch in den
        # Suchergebnissen auftaucht: Kleinanzeigen zeigt reservierte Inserate dort teils weiter an.
        for h in shortlist_to_check(search["id"]):
            to_check.setdefault(h["key"], h)
        for h in db.top_hits_without_ship_flag(search["id"], SHIP_FLAG_PLATFORMS):
            to_check.setdefault(h["key"], h)
        for hit in to_check.values():
            if hit["platform"] == "ricardo" or hit["platform"] in blocked \
                    or any(b.startswith(hit["platform"] + ":") for b in blocked):
                continue
            try:
                if hit["platform"] != "ebay":
                    time.sleep(random.uniform(2, 4))
                if check_listing(adapters, hit) != "ok":
                    log.info("Inserat nicht mehr zu haben: %s", hit["url"])
            except BlockedError as exc:
                blocked.add(hit["platform"])
                if hit["platform"] != "ebay":  # eBay-Tageslimit: Quellen heissen ebay:EBAY_xx, Suche läuft separat
                    db.mark_platform_blocked(hit["platform"], exc.retry_after)
            except Exception as exc:
                log.warning("Verfügbarkeit von %s nicht prüfbar: %s", hit["url"], exc)

        # Automatische KI-Auswertung der neuen Treffer durchführen, falls Gemini API Key hinterlegt ist
        if cfg.get("ai", {}).get("gemini_api_key"):
            try:
                from .ai import start_background_evaluation
                start_background_evaluation(search["id"])
            except Exception as exc:
                log.warning("Automatische KI-Auswertung fehlgeschlagen: %s", exc)

    for src in sources:
        if src["name"] in paused:
            until = paused[src["name"]].astimezone().strftime("%d.%m. %H:%M")
            db.record_platform_result(src["name"], None, f"Pausiert nach Sperre bis {until}")
        elif any(not s["platforms"] or src["adapter"] in s["platforms"] for s in searches):
            db.record_platform_result(src["name"], source_counts.get(src["name"], 0), source_errors.get(src["name"]),
                                      fails=source_tries.get(src["name"], 0) if src["name"] in blocked   # Sperre: nie «teilweise»
                                      else source_fails.get(src["name"], 0), tries=source_tries.get(src["name"], 0))
    db.finish_run(run_id, total_new, errors)
    return {"searches": len(searches), "new": total_new, "errors": errors}


def run_in_background(trigger: str = "manuell", search_ids: list[int] | None = None) -> bool:
    if is_running():
        return False
    threading.Thread(target=run, args=(trigger, search_ids), daemon=True).start()
    return True
