"""Benachrichtigung via ntfy (App für iOS/Android/Mac, Server ntfy.sh oder selbst gehostet)."""

from __future__ import annotations

import logging
import re

import httpx

log = logging.getLogger(__name__)

TRUST_TAG = {"ok": "green_circle", "neutral": "white_circle", "check": "yellow_circle", "warn": "red_circle"}


def _publish(cfg: dict, payload: dict) -> bool:
    n = cfg["ntfy"]
    if not n.get("topic"):
        return False
    headers = {"Authorization": f"Bearer {n['token']}"} if n.get("token") else {}
    try:
        resp = httpx.post(n["server"].rstrip("/"), json={"topic": n["topic"], **payload}, headers=headers, timeout=15)
        resp.raise_for_status()
        return True
    except httpx.HTTPError as exc:
        log.warning("ntfy fehlgeschlagen: %s", exc)
        return False


def format_price(price: float | None, currency: str) -> str:
    if price is None:
        return "Preis offen"
    if price == int(price):
        return f"{currency} {int(price):,}".replace(",", "'")
    return f"{currency} {price:,.2f}".replace(",", "'")


def image_url(url: str | None, large: bool = True) -> str | None:
    """Grössere Bildvariante der Plattform statt des kleinen Vorschaubilds (large: für Push, sonst für die Karte)."""
    if not url:
        return url
    if "c.tutti.ch/thumbnail/" in url or "c.anibis.ch/thumbnail/" in url:
        return url.replace("/thumbnail/", "/big/" if large else "/images/")
    if "img.kleinanzeigen.de" in url:
        return re.sub(r"rule=\$_\d+\.AUTO", "rule=$_59.AUTO", url)
    if "img.ricardostatic.ch" in url:
        return re.sub(r"/t_\d+x\d+/", "/t_1000x750/" if large else "/t_400x300/", url)
    if "cache.willhaben.at" in url and large:
        return url.replace("_hoved.jpg", ".jpg")
    return url


def _amount(price: float | None, currency: str | None) -> str:
    """«135 CHF», «1'350 EUR» – wie auf der Website."""
    if price is None:
        return "Preis offen"
    return f"{price:,.0f} {currency or 'CHF'}".replace(",", "'")


TIER_PRIORITY = {"S": 5, "A": 4, "B": 3}
TIER_TAG = {"S": "star_struck", "A": "fire", "B": "+1"}


def _hit_message(h: dict, search: dict, platform_labels: dict[str, str]) -> str:
    """Kurze, klare Push-Nachricht: Preis, Lieferung, Fakten der KI, Ort."""
    from . import fx as fx_rates
    from .config import get_config
    from .scoring import effective_price_chf

    lines = []
    price = _amount(h.get("single_price") or h["price"], h["currency"])
    if h.get("single_price") and h.get("price") and h["single_price"] < h["price"]:
        price = ("≈ " if h.get("single_estimated") else "") + price + f" (aus Setpreis {_amount(h['price'], h['currency'])})"
    chf = effective_price_chf(h, fx_rates.rates(get_config()))
    if search.get("new_price") and chf:
        price += f" · {chf / search['new_price'] * 100:.0f} % vom Neupreis"
    lines.append(f"💰 {price}")
    if h.get("delivery"):
        lines.append(f"📦 {h['delivery']}" + (f" → {_amount(round(h['total_chf']), 'CHF')} bis zu dir" if h.get("total_chf") else ""))
    facts = [x for x in (
        (h.get("ai_condition") or "").capitalize() if h.get("ai_condition") not in (None, "unbekannt") else "",
        str(h["ai_year"]) if h.get("ai_year") else "",
        h.get("ai_req_note") if h.get("ai_req") == "ja" else "",
    ) if x]
    if facts:
        lines.append("✅ " + " · ".join(facts))
    if h.get("ai_req") == "unklar":
        lines.append(f"❔ {h.get('ai_req_note') or 'Anforderung im Inserat nicht angegeben'}")
    if h.get("ai_reason"):
        lines.append(f"💬 {h['ai_reason'][:220]}")
    where = " · ".join(p for p in (platform_labels.get(h["platform"], h["platform"]), h.get("location")) if p)
    lines.append(f"📍 {where}")
    if h.get("trust_level") in ("check", "warn"):
        reasons = ", ".join(h.get("trust_reasons", [])[:2])
        lines.append(f"⚠️ {h.get('trust_label', '')}{': ' + reasons if reasons else ''}")
    return "\n".join(lines)


def notify_hits(cfg: dict, search: dict, hits: list[dict], platform_labels: dict[str, str]) -> list[str]:
    """Schickt bis zu max_per_search Einzelmeldungen, sonst eine Sammelmeldung. Gibt gemeldete Keys zurück."""
    if not hits or not cfg["ntfy"].get("topic"):
        return []
    if not cfg["ntfy"].get("on_new", True) or not search.get("notify_new", 1):
        return []
    limit = int(cfg["ntfy"].get("max_per_search", 5))
    public = (cfg["web"].get("public_url") or "").rstrip("/")
    search_url = f"{public}/search/{search['id']}" if public else None
    sent: list[str] = []

    if len(hits) > limit:
        lines = [f"• {h.get('ai_tier') or '–'} · {_amount(h['price'], h['currency'])} – {h['title'][:55]}" for h in hits[:8]]
        if len(hits) > 8:
            lines.append(f"… und {len(hits) - 8} weitere")
        payload = {
            "title": f"{len(hits)} gute Angebote: {search['query']}",
            "message": "\n".join(lines),
            "tags": ["mag"],
        }
        if search_url:
            payload["click"] = search_url
        if _publish(cfg, payload):
            sent = [h["key"] for h in hits]
        return sent

    for h in hits:
        tier = h.get("ai_tier")
        head = f"{tier} · {h['ai_score']} Pkt · " if tier else ""
        payload = {
            "title": f"{head}{search['query']}"[:120],
            "message": f"{h['title'][:120]}\n" + _hit_message(h, search, platform_labels),
            "click": h["url"],
            "priority": TIER_PRIORITY.get(tier, 3),
            "tags": [TIER_TAG.get(tier, TRUST_TAG.get(h.get("trust_level"), "white_circle"))],
            "actions": [{"action": "view", "label": "Inserat öffnen", "url": h["url"], "clear": True}]
                       + ([{"action": "view", "label": "Alle Angebote", "url": search_url}] if search_url else []),
        }
        if h.get("image"):
            payload["attach"] = image_url(h["image"])
            payload["filename"] = "foto.jpg"
        if _publish(cfg, payload):
            sent.append(h["key"])
    return sent


def notify_top_hits(search_id: int) -> list[str]:
    """Nach der KI-Prüfung: neue Angebote ab Score notify_min_score (Standard 65 = A-Angebot) melden."""
    from . import db
    from .adapters import all_adapters
    from .config import get_config
    from .trust import LEVELS

    cfg = get_config()
    search = db.get_search(search_id)
    if not search:
        return []
    hits = db.pending_top_hits(search_id, int(cfg["ntfy"].get("notify_min_score", 65)))
    for h in hits:
        h["trust_label"] = LEVELS.get(h.get("trust_level"), "")
    sent = notify_hits(cfg, search, hits, {a.name: a.label for a in all_adapters(cfg)})
    db.mark_notified(search_id, sent)
    return sent


def send_test(cfg: dict) -> bool:
    return _publish(cfg, {"title": "fundus", "message": "Test – Benachrichtigungen funktionieren.", "tags": ["tada"]})


def send_weekly(cfg: dict, rows: list[dict]) -> bool:
    """Wochenübersicht: eine Zeile pro Suche."""
    if not rows:
        return False
    lines = [f"• {r['query']}: {r['week']} neu, {r['total']} gesamt" for r in rows]
    total = sum(r["week"] for r in rows)
    payload = {"title": f"Wochenübersicht: {total} neue Treffer", "message": "\n".join(lines), "tags": ["calendar"]}
    public = (cfg["web"].get("public_url") or "").rstrip("/")
    if public:
        payload["click"] = public
    return _publish(cfg, payload)
