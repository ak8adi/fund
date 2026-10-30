"""Benachrichtigung via ntfy (App für iOS/Android/Mac, Server ntfy.sh oder selbst gehostet)."""

from __future__ import annotations

import logging

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


def notify_hits(cfg: dict, search: dict, hits: list[dict], platform_labels: dict[str, str]) -> list[str]:
    """Schickt bis zu max_per_search Einzelmeldungen, sonst eine Sammelmeldung. Gibt gemeldete Keys zurück."""
    if not hits or not cfg["ntfy"].get("topic"):
        return []
    if not cfg["ntfy"].get("on_new", True) or not search.get("notify_new", 1):
        return []
    limit = int(cfg["ntfy"].get("max_per_search", 5))
    public = (cfg["web"].get("public_url") or "").rstrip("/")
    sent: list[str] = []

    if len(hits) > limit:
        lines = [f"• {h['title'][:60]} – {format_price(h['price'], h['currency'])}" for h in hits[:8]]
        if len(hits) > 8:
            lines.append(f"… und {len(hits) - 8} weitere")
        payload = {
            "title": f"{len(hits)} neue Treffer: {search['query']}",
            "message": "\n".join(lines),
            "tags": ["mag"],
        }
        if public:
            payload["click"] = f"{public}/search/{search['id']}"
        if _publish(cfg, payload):
            sent = [h["key"] for h in hits]
        return sent

    for h in hits:
        where = " · ".join(p for p in (platform_labels.get(h["platform"], h["platform"]), h.get("location")) if p)
        trust = f"{h['trust_label']}: {', '.join(h['trust_reasons'][:2])}" if h["trust_reasons"] else h["trust_label"]
        deal = f"\n{h['ai_tier']}-Angebot ({h['ai_score']} Pkt.): {h['score_reason']}" if h.get("score_reason") else ""
        payload = {
            "title": h["title"][:120],
            "message": f"{format_price(h['price'], h['currency'])} · {where}{deal}\n{trust}\nSuche: {search['query']}",
            "click": h["url"],
            "tags": [TRUST_TAG.get(h["trust_level"], "white_circle")],
        }
        if h.get("image"):
            payload["attach"] = h["image"]
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
