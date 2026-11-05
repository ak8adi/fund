"""Daten für die Darstellungs-Entwürfe einer Suche (/search/{id}/entwuerfe).

Idee: erst hart aussortieren (K.-o.), dann jedes Angebot auf zwei Achsen stellen –
«was ich zahle» (Endpreis bis zu mir) und «was ich bekomme» (fairer Wert nach Zustand und Jahrgang).
Die besten Gebote sind die Pareto-Front: kein anderes Angebot ist zugleich billiger und mindestens so gut.
"""

from __future__ import annotations

import statistics
from collections import Counter
from datetime import datetime, timedelta, timezone

from . import db, fx as fx_rates, scoring
from .config import get_config


def _when(h: dict) -> datetime | None:
    raw = h.get("posted_at") or h.get("hit_seen")
    if not raw:
        return None
    dt = datetime.fromisoformat(raw)
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _quartiles(values: list[float]) -> tuple[float, float, float] | None:
    if len(values) < 4:
        return None
    q = statistics.quantiles(values, n=4)
    return q[0], statistics.median(values), q[2]


def ko_reason(h: dict, pay: float | None = None, reference: float | None = None) -> str | None:
    """Harte Ausschlüsse: was hier steht, kommt nie in die Auswahl der besten Gebote."""
    if pay and reference and pay < scoring.SCAM_RATIO * reference:
        return "unrealistisch billig"
    if h.get("ai_req") == "nein":
        return "Anforderung nicht erfüllt"
    if h.get("trust_level") == "warn":
        return "Verkäufer fragwürdig"
    if h.get("reserved"):
        return "reserviert"
    return None


def pareto(offers: list[dict]) -> None:
    """Markiert front=True bei allen nicht dominierten Angeboten (ohne K.-o.)."""
    pool = [o for o in offers if not o["ko"] and o["value"] is not None]
    for o in pool:
        o["front"] = not any(p is not o and p["pay"] <= o["pay"] and p["value"] >= o["value"]
                             and (p["pay"] < o["pay"] or p["value"] > o["value"]) for p in pool)


def build(search_id: int) -> dict:
    search = db.get_search(search_id) or {}
    fx = fx_rates.rates(get_config())
    hits = db.hits_for_search(search_id, limit=5000)
    scoring.mark_duplicates(hits)
    market = search.get("market_price")
    new_price = search.get("new_price")
    reference = market or (new_price * scoring.USED_SHARE if new_price else None)
    year = scoring.typical_year(hits)
    now = datetime.now(timezone.utc)

    offers = []
    for h in hits:
        if h.get("ai_relevant") != 1 or h.get("gone_at") or h.get("_dup") or h.get("hidden"):
            continue
        pay = scoring.pay_price_chf(h, fx)
        if not pay:
            continue
        value = scoring.fair_value(h, reference, year, new_price) if reference else None
        when = _when(h)
        label = h.get("delivery") or ""
        offers.append({
            "key": h["key"], "title": h["title"], "img": h.get("image"), "platform": h["platform"],
            "location": h.get("location"), "pay": round(pay), "price": h.get("price"), "currency": h.get("currency"),
            "value": round(value) if value else None, "ratio": round(value / pay, 2) if value else None,
            "score": h.get("ai_score"), "tier": h.get("ai_tier"), "cond": h.get("ai_condition") or "unbekannt",
            "year": h.get("ai_year"), "req": h.get("ai_req"), "req_note": h.get("ai_req_note"),
            "delivery": label, "pickup": label.startswith("Abholung"),
            "route": "pickup" if label.startswith("Abholung") else "import" if "Paket4you" in label else "ch",
            "age_h": round((now - when).total_seconds() / 3600, 1) if when else None,
            "auction": bool(h.get("auction")), "bids": h.get("bids"), "ends_at": h.get("ends_at"),
            "reserved": bool(h.get("reserved")), "trust": h.get("trust_level"),
            "drop": bool(h.get("first_price") and h.get("price") and h["price"] < h["first_price"]),
            "reason": h.get("score_reason"), "ko": ko_reason(h, pay, reference), "front": False,
        })
    pareto(offers)
    offers.sort(key=lambda o: (not o["front"], -(o["ratio"] or 0), o["pay"]))

    # Zeitreihe: neue und verschwundene passende Angebote pro Tag (letzte 30 Tage)
    start = (now - timedelta(days=29)).date()
    new_per_day, gone_per_day = Counter(), Counter()
    for h in hits:
        if h.get("ai_relevant") != 1:
            continue
        if h.get("hit_seen"):
            new_per_day[h["hit_seen"][:10]] += 1
        if h.get("gone_at"):
            gone_per_day[h["gone_at"][:10]] += 1
    days = [(start + timedelta(days=i)).isoformat() for i in range(30)]

    pays = [o["pay"] for o in offers]
    return {
        "search": {"id": search_id, "query": search.get("query"), "market": market, "new_price": new_price,
                   "reference": round(reference) if reference else None, "typical_year": year,
                   "requirements": search.get("requirements")},
        "offers": offers,
        "band": _quartiles(pays),
        "timeline": [{"day": d, "new": new_per_day.get(d, 0), "gone": gone_per_day.get(d, 0)} for d in days],
        "counts": {"all": len([h for h in hits if h.get("ai_relevant") == 1]), "open": len(offers),
                   "front": sum(o["front"] for o in offers), "ko": sum(bool(o["ko"]) for o in offers)},
    }
