"""Auktions-Alarm: kurz vor Ablauf nochmals das aktuelle Gebot holen und melden, wenn es noch unter dem Wert liegt.

Eine Auktion ist bis zum Schluss kein Angebot mit Preis – das Gebot vom Morgen sagt wenig. Darum merkt sich fundus
das Ende (listings.ends_at) und prüft `schedule.auction_alert_minutes` (Standard 60) vor Ablauf nochmals:

- eBay: Stand über die API (item_details).
- Ricardo: die Suchseite der Suche nochmals abrufen (eine Anfrage, wie im Prüflauf) – Inseratseiten ruft fundus dort
  nie einzeln ab (Sperre). Ist Ricardo gerade gesperrt, gilt der letzte bekannte Stand, die Meldung sagt das.

Liegt der Endpreis beim aktuellen Gebot (inkl. Versand, Gebühren, Fahrt) noch unter dem fairen Wert, gibt es eine
dringende Meldung: eigene, auffällige Notification in der App (und ntfy, falls eingeschaltet und die App nicht aktiv).
Jede Auktion wird pro Suche genau einmal entschieden (hits.auction_alert).
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from urllib.parse import quote
from zoneinfo import ZoneInfo

from . import area, db, delivery as deliv, fx as fx_rates, scoring, views
from .adapters import BlockedError, adapter_for_source, all_adapters
from .config import get_config
from .trust import as_bid, buy_now, price_in_chf

log = logging.getLogger(__name__)

MIN_LEFT = timedelta(minutes=3)   # so knapp vor Schluss lohnt eine Meldung nicht mehr
CHECK_EVERY_MIN = 5               # Takt des Zeitplan-Jobs (scheduler.py)


def lead(cfg: dict) -> timedelta:
    return timedelta(minutes=float(cfg["schedule"].get("auction_alert_minutes", 60) or 60))


def _ends(row: dict) -> datetime:
    dt = datetime.fromisoformat(row["ends_at"])
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def next_alert_time(now: datetime | None = None) -> datetime | None:
    """Wann steht die nächste Prüfung einer Auktion an? Für die App: kurz danach soll sie nachfragen."""
    now = now or datetime.now(timezone.utc)
    cfg = get_config()
    times = [max(now, _ends(r) - lead(cfg)) for r in db.auctions_to_watch() if _ends(r) - now > MIN_LEFT]
    return min(times) if times else None


def _refresh(adapters, search: dict, hits: list[dict], blocked: set[str]) -> set[str]:
    """Aktuelles Gebot holen. Gibt die Keys zurück, deren Stand NICHT aufgefrischt werden konnte."""
    stale: set[str] = set()
    ricardo = [h for h in hits if h["platform"] == "ricardo"]
    if ricardo:
        found = None
        if "ricardo" not in blocked:
            try:
                adapter = adapter_for_source(adapters, "ricardo")
                found = {l.key: l for l in adapter.search(search["query"], "ricardo", None, None)}
                complete = bool(getattr(adapter, "complete", False))
            except BlockedError as exc:
                db.mark_platform_blocked("ricardo", exc.retry_after)
            except Exception as exc:
                log.info("Auktions-Alarm: Ricardo-Suche «%s» nicht abrufbar: %s", search["query"], exc)
        for h in ricardo:
            l = (found or {}).get(h["key"])
            if l is not None:
                db.update_auction(h["key"], l.bid_price, l.bids, l.ends_at.isoformat() if l.ends_at else None,
                                  l.price if l.price and l.bid_price and l.price > l.bid_price else None)
            elif found is not None and complete:
                db.mark_listing_gone(h["key"])  # vollständige Liste, Inserat fehlt: vorzeitig beendet
            else:
                stale.add(h["key"])
    for h in hits:
        if h["platform"] != "ebay":
            if h["platform"] != "ricardo":
                stale.add(h["key"])
            continue
        try:
            d = adapter_for_source(adapters, "ebay").item_details(h["external_id"], (h.get("source") or "ebay:EBAY_DE").split(":")[-1])
            if d["gone"]:
                db.mark_listing_gone(h["key"])
            else:
                db.update_auction(h["key"], d.get("bid_price"), d.get("bids"), d.get("ends_at"))
        except Exception as exc:
            stale.add(h["key"])
            log.info("Auktions-Alarm: eBay-Stand für %s nicht abrufbar: %s", h["url"], exc)
    return stale


def _money(v: float, cur: str = "CHF") -> str:
    return f"{v:,.0f} {cur}".replace(",", "'")


def _message(cfg: dict, search: dict, h: dict, plan: deliv.Plan, value: float, limit: float, stale: bool,
             now: datetime) -> tuple[str, str, str]:
    tz = ZoneInfo(cfg["schedule"].get("timezone", "Europe/Zurich"))
    ends = _ends(h)
    mins = max(1, round((ends - now).total_seconds() / 60))
    cur = h.get("currency") or "CHF"
    bid = _money(h["price"], cur)
    # absolute Uhrzeit: die Meldung kann verzögert ankommen, «in 56 Min.» wäre dann falsch
    title = f"⏰ Auktion endet {ends.astimezone(tz):%H:%M} – Gebot {bid}, lohnt sich bis {_money(limit)}"
    bids = h.get("bids")
    lines = [
        h["title"][:90],
        f"Gebot {bid}" + (f" ({bids} Gebot{'' if bids == 1 else 'e'})" if bids is not None else "")
        + f" → {_money(plan.total)} bis zu dir"
        + (f" · Stand von {datetime.fromisoformat(h['last_seen']).astimezone(tz):%H:%M}, nicht aktualisierbar" if stale else ""),
        f"Wert für dich ≈ {_money(value)}"
        + (f" ({h['ai_condition']}{', ' + str(h['ai_year']) if h.get('ai_year') else ''})"
           if h.get("ai_condition") not in (None, "unbekannt") else ""),
        f"Noch {mins} Min. (Stand {now.astimezone(tz):%H:%M}) · {h['platform'].capitalize()} · Suche «{search['query']}»",
    ]
    public = (cfg["web"].get("public_url") or "").rstrip("/")
    url = f"{public}/go/{quote(h['key'], safe='')}" if public else h["url"]
    return title, "\n".join(lines), url


def check(now: datetime | None = None) -> int:
    """Zeitplan-Job: fällige Auktionen prüfen. Gibt die Zahl der Meldungen zurück."""
    now = now or datetime.now(timezone.utc)
    cfg = get_config()
    window = lead(cfg)
    due: dict[int, list[str]] = {}
    for r in db.auctions_to_watch():
        if MIN_LEFT < _ends(r) - now <= window:
            due.setdefault(r["search_id"], []).append(r["key"])
    if not due:
        return 0

    from . import notify, runner
    adapters = all_adapters(cfg)
    fx = fx_rates.rates(cfg)
    dcfg = cfg.get("delivery", {})
    blocked = runner._blocked_platforms()
    alerts: dict[str, tuple] = {}   # pro Inserat eine Meldung, auch wenn es in mehreren Suchen steht: die vorsichtigere
    for sid, keys in due.items():
        search = db.get_search(sid)
        if not search:
            continue
        before = {h["key"]: h for h in db.hits_for_search(sid, limit=5000) if h["key"] in keys}
        stale = _refresh(adapters, search, list(before.values()), blocked)
        hits = db.hits_for_search(sid, limit=5000)
        scoring.mark_duplicates(hits)
        reference = search.get("market_price") or (search["new_price"] * scoring.USED_SHARE if search.get("new_price") else None)
        year = scoring.typical_year(hits)
        region = area.for_search(search)
        by_key = {h["key"]: h for h in hits}
        for key in keys:
            h = by_key.get(key)
            if not h or h.get("gone_at") or not h.get("ends_at"):
                db.set_auction_alert(sid, key, "kein Alarm: beendet")
                continue
            if _ends(h) - now > window:
                continue  # verlängert: später nochmals
            if h.get("single_price"):  # Set: das Gebot gilt dem Ganzen, der Vergleichswert nur dem gesuchten Teil
                db.set_auction_alert(sid, key, "kein Alarm: Set")
                continue
            hb = as_bid(h)
            plan = deliv.plan(hb, fx, dcfg, search.get("product_size"), search.get("product_weight"))
            if plan is None or reference is None:
                db.set_auction_alert(sid, key, "kein Alarm: kein Vergleichswert")
                continue
            if why := (views.ko_reason(hb, plan.total, reference, None, search, region) or scoring.out_of_range(hb, search, fx)):
                db.set_auction_alert(sid, key, f"kein Alarm: {why}")
                continue
            value, limit = scoring.auction_limit(hb, plan, reference, fx, year, search.get("new_price"))
            if plan.total >= value:
                db.set_auction_alert(sid, key, "kein Alarm: Gebot schon über dem Wert")
                continue
            title, body, url = _message(cfg, search, hb, plan, value, limit, key in stale, now)
            if buy_now(h):
                body += f"\nSofortkauf {_money(price_in_chf(h['price'], h.get('currency') or 'CHF', fx) or h['price'])}"
            db.set_auction_alert(sid, key, f"gemeldet: Gebot {hb['price']:g}, Wert {value:.0f}, Grenze {limit:.0f}")
            if key not in alerts or limit < alerts[key][0]:
                alerts[key] = (limit, sid, title, body, url, h["url"], _ends(h).isoformat())
        scoring.rescore_search(sid)  # frische Gebote in Endpreis und «lohnt sich bis» übernehmen
    for _, sid, title, body, url, listing_url, ends_at in alerts.values():
        db.add_app_notice(sid, title, body, url, urgent=True, expires_at=ends_at)
        # ntfy.auctions: true = Auktions-Alarme zusätzlich als Push über ntfy (kommt sofort, unabhängig vom Wecker der App)
        if cfg["ntfy"].get("auctions") or not notify.app_active():
            notify.publish_urgent(cfg, title, body, listing_url)
        log.info("Auktions-Alarm: %s – %s", title, listing_url)
    return len(alerts)
