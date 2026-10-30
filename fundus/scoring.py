"""Deal-Score: wie gut ist ein Angebot im Vergleich zum aktuellen Markt?

Die KI liefert nur Fakten (relevant, Zustand, Jahrgang, Versand, Einzelpreis). Das Ranking rechnet dieses
Modul, damit Scores über Stapel und Zeit vergleichbar sind und ein neues, besseres Angebot
ältere automatisch nach unten schiebt. Verglichen wird der Endpreis (costs.py) mit dem fairen Preis.

Score 50 = Endpreis entspricht dem fairen Preis (Marktpreis, angepasst an Zustand und Modelljahr).
Halber Preis → 90, ein Drittel → 100, doppelter Preis → 10.
"""

from __future__ import annotations

import math
import statistics

import re

from . import db, fx as fx_rates
from .config import get_config
from .costs import total_cost_chf
from .trust import price_in_chf

# Wert relativ zu neu; «gut» ist der typische Zustand im Gebrauchtmarkt
CONDITION_VALUE = {"neu": 1.0, "neuwertig": 0.95, "sehr gut": 0.9, "gut": 0.8, "gebraucht": 0.7, "defekt": 0.3}
TYPICAL = 0.8
MIN_PRICES = 3        # darunter kein Marktpreis
SCAM_RATIO = 0.35     # Preis unter 35 % des Marktpreises: typisches Betrugsmuster
TRUST_PENALTY = {"warn": 15, "check": 5}
YEARLY_LOSS = 0.10    # Wertverlust pro Modelljahr Unterschied zum mittleren Jahrgang
SLOPE = 40            # Punkte pro Verdoppelung des Preis-Leistungs-Verhältnisses
PICKUP_ABROAD_PENALTY = 10


def effective_price_chf(h: dict, fx: dict) -> float | None:
    """Einzelpreis aus Set-Inseraten hat Vorrang vor dem Inseratpreis."""
    return price_in_chf(h.get("single_price") or h.get("price"), h.get("currency") or "CHF", fx)


def _chf(amount: float) -> str:
    return f"{amount:,.0f}".replace(",", "'")


def tier_for(score: int) -> str:
    return "S" if score >= 80 else "A" if score >= 65 else "B" if score >= 45 else "C"


def score_hit(h: dict, market: float, fx: dict, typical_year: float | None = None,
              delivery: dict | None = None) -> tuple[int, str, float]:
    """(Score 0–100, Begründung, Endpreis CHF) für einen relevanten Treffer mit Preis."""
    price = effective_price_chf(h, fx)
    total, notes = total_cost_chf(h, fx, delivery or {})
    cond = h.get("ai_condition") or "unbekannt"
    value = market * CONDITION_VALUE.get(cond, TYPICAL) / TYPICAL   # fairer Preis für diesen Zustand
    year = h.get("ai_year")
    if year and typical_year:
        value *= (1 - YEARLY_LOSS) ** max(-6, min(6, typical_year - year))
    elif typical_year:
        value *= 1 - YEARLY_LOSS  # ohne Jahrgang vorsichtig rechnen: ältere Modelle nennen ihn seltener
    score = 50 + SLOPE * math.log2(value / total)
    diff = (1 - total / market) * 100
    parts = [f"Endpreis ≈ {_chf(total)} CHF" + (f" ({', '.join(notes)})" if notes else ""),
             f"{abs(diff):.0f} % {'unter' if diff >= 0 else 'über'} Marktpreis ({_chf(market)} CHF)"]
    if any(n.startswith("nur Abholung in") for n in notes):
        score -= PICKUP_ABROAD_PENALTY
    if cond != "unbekannt":
        parts.append(f"Zustand {cond}")
    if year:
        parts.append(f"Jahrgang {year}")
    elif typical_year:
        parts.append("Jahrgang unbekannt")
    if h.get("single_price"):
        parts.append("Einzelpreis aus Set geschätzt" if h.get("single_estimated") else "Einzelpreis aus Set")
    if price / market < SCAM_RATIO:
        score = min(score, 40)
        parts.append("auffällig billig – Betrugsmuster prüfen")
    penalty = TRUST_PENALTY.get(h.get("trust_level"), 0)
    if penalty:
        score -= penalty
        parts.append("Verkäufer genauer anschauen")
    return max(0, min(100, round(score))), ", ".join(parts), total


def market_price(hits: list[dict], fx: dict) -> float | None:
    """Median der Angebotspreise relevanter Treffer – auch verkaufter, die zeigen, was der Markt bezahlt.
    Geschätzte Set-Anteile zählen nicht mit: sie sind selbst vom Markt abgeleitet."""
    prices = [p for p in (effective_price_chf(h, fx) for h in hits
                          if h.get("ai_relevant") == 1 and not h.get("_dup") and not h.get("single_estimated")) if p]
    return statistics.median(prices) if len(prices) >= MIN_PRICES else None


def typical_year(hits: list[dict]) -> float | None:
    years = [h["ai_year"] for h in hits if h.get("ai_relevant") == 1 and h.get("ai_year")]
    return statistics.median(years) if len(years) >= MIN_PRICES else None


def mark_duplicates(hits: list[dict]) -> None:
    """Gleicher Titel und Preis = dasselbe Angebot (neu eingestellt oder auf mehreren Plattformen).
    Der zuletzt gefundene zählt, die anderen bekommen _dup und fliessen nicht in den Marktpreis ein."""
    seen: set[tuple] = set()
    for h in sorted(hits, key=lambda x: x.get("hit_seen") or "", reverse=True):
        if h.get("ai_relevant") != 1:
            continue
        sig = (" ".join(re.findall(r"\w+", (h.get("title") or "").lower())), h.get("price"))
        if sig in seen:
            h["_dup"] = True
        seen.add(sig)


def rescore_search(search_id: int) -> float | None:
    cfg = get_config()
    fx = fx_rates.rates(cfg)
    hits = db.hits_for_search(search_id, limit=5000)
    mark_duplicates(hits)
    market = market_price(hits, fx)
    year = typical_year(hits)
    rows = []
    for h in hits:
        if h.get("ai_relevant") != 1:
            rows.append((None, None, None, None, h["key"]))
        elif h.get("_dup"):
            rows.append((None, None, "Doppelt inseriert – gleiches Angebot weiter oben", None, h["key"]))
        elif effective_price_chf(h, fx) is None:
            rows.append((None, None, "Preis offen", None, h["key"]))
        elif market is None:
            total, _ = total_cost_chf(h, fx, cfg.get("delivery", {}))
            rows.append((None, None, "Noch zu wenig Vergleichspreise für einen Marktpreis", total, h["key"]))
        else:
            score, reason, total = score_hit(h, market, fx, year, cfg.get("delivery", {}))
            rows.append((score, tier_for(score), reason, total, h["key"]))
    db.set_scores(search_id, market, rows)
    return market


def rescore_all() -> None:
    for s in db.list_searches():
        rescore_search(s["id"])
