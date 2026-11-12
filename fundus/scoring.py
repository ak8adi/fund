"""Deal-Score: wie gut ist ein Angebot im Vergleich zum aktuellen Markt?

Die KI liefert nur Fakten (relevant, Zustand, Jahrgang, Versand, Einzelpreis). Das Ranking rechnet dieses
Modul, damit Scores über Stapel und Zeit vergleichbar sind und ein neues, besseres Angebot
ältere automatisch nach unten schiebt. Verglichen wird der Endpreis (delivery.py) mit dem fairen Preis.

Score 50 = Endpreis entspricht dem fairen Preis (Marktpreis, angepasst an Zustand und Modelljahr).
Halber Preis → 90, ein Drittel → 100, doppelter Preis → 10.
"""

from __future__ import annotations

import math
import statistics

import re

from . import db, fx as fx_rates
from .config import get_config
from . import delivery as deliv
from .matching import matches_search
from .trust import bidding, listing_price, price_in_chf

# Wert relativ zu neu; «gut» ist der typische Zustand im Gebrauchtmarkt
CONDITION_VALUE = {"neu": 1.0, "neuwertig": 0.95, "sehr gut": 0.9, "gut": 0.8, "gebraucht": 0.7, "defekt": 0.3}
TYPICAL = 0.8
MIN_PRICES = 3        # darunter kein Marktpreis
SCAM_RATIO = 0.35     # Preis unter 35 % des Marktpreises: typisches Betrugsmuster
TRUST_PENALTY = {"warn": 15, "check": 5}
YEARLY_LOSS = 0.10    # Wertverlust pro Modelljahr Unterschied zum mittleren Jahrgang
SLOPE = 40            # Punkte pro Verdoppelung des Preis-Leistungs-Verhältnisses
USED_SHARE = 0.6     # ohne Vergleichspreise: Marktpreis ≈ 60 % des Neupreises


def effective_price_chf(h: dict, fx: dict) -> float | None:
    """Einzelpreis aus Set-Inseraten hat Vorrang vor dem Inseratpreis."""
    return price_in_chf(listing_price(h), h.get("currency") or "CHF", fx)


def pay_price_chf(h: dict, fx: dict) -> float | None:
    """Was du bezahlst: Endpreis inkl. Versand und Gebühren, sonst der Preis in CHF (Abholung, ohne Preisplan)."""
    return h.get("total_chf") or effective_price_chf(h, fx)


def _chf(amount: float) -> str:
    return f"{amount:,.0f}".replace(",", "'")


def tier_for(score: int) -> str:
    return "S" if score >= 80 else "A" if score >= 65 else "B" if score >= 45 else "C"


def fair_value(h: dict, market: float, typical_year: float | None = None, new_price: float | None = None) -> float:
    """Fairer Preis dieses Angebots in CHF: Marktpreis, angepasst an Zustand und Modelljahr, nie über neu."""
    cond = h.get("ai_condition") or "unbekannt"
    value = market * CONDITION_VALUE.get(cond, TYPICAL) / TYPICAL
    year = h.get("ai_year")
    if year and typical_year:
        value *= (1 - YEARLY_LOSS) ** max(-6, min(6, typical_year - year))
    elif typical_year:
        value *= 1 - YEARLY_LOSS  # ohne Jahrgang vorsichtig rechnen: ältere Modelle nennen ihn seltener
    if new_price:
        value = min(value, new_price * CONDITION_VALUE.get(cond, TYPICAL))  # gebraucht nie mehr wert als neu
    return value


def score_hit(h: dict, market: float, fx: dict, typical_year: float | None = None,
              delivery: dict | None = None, new_price: float | None = None,
              plan: deliv.Plan | None = None, market_estimated: bool = False) -> tuple[int, str, float]:
    """(Score 0–100, Begründung, Endpreis CHF) für einen relevanten Treffer mit Preis."""
    price = effective_price_chf(h, fx)
    plan = plan or deliv.plan(h, fx, delivery or {})
    total = plan.total
    cond = h.get("ai_condition") or "unbekannt"
    year = h.get("ai_year")
    value = fair_value(h, market, typical_year, new_price)
    score = 50 + SLOPE * math.log2(value / total) - plan.penalty
    diff = (1 - total / market) * 100
    parts = [f"Endpreis {abs(diff):.0f} % {'unter' if diff >= 0 else 'über'} "
             + (f"geschätztem Marktpreis ({_chf(market)} CHF, 60 % vom Neupreis)" if market_estimated
                else f"Marktpreis ({_chf(market)} CHF)")]
    if new_price:
        if price > new_price:
            score = min(score, 30)
            parts.append(f"teurer als neu ({_chf(new_price)} CHF)")
        else:
            parts.append(f"{price / new_price * 100:.0f} % vom Neupreis")
    if cond != "unbekannt":
        parts.append(f"Zustand {cond}")
    if year:
        parts.append(f"Jahrgang {year}")
    if h.get("single_price"):
        parts.append("Einzelpreis aus Set geschätzt" if h.get("single_estimated") else "Einzelpreis aus Set")
    if total / market < SCAM_RATIO:  # gleiche Schwelle und gleicher Preis (Endpreis) wie der K.-o. in views.ko_reason
        score = min(score, 40)
        parts.append("auffällig billig – Betrugsmuster prüfen")
    penalty = TRUST_PENALTY.get(h.get("trust_level"), 0)
    if penalty:
        score -= penalty
        parts.append("Verkäufer genauer anschauen")
    return max(0, min(100, round(score))), ", ".join(parts), total


def market_price(hits: list[dict], fx: dict) -> float | None:
    """Median der Angebotspreise relevanter Treffer – auch verkaufter, die zeigen, was der Markt bezahlt.
    Geschätzte Set-Anteile zählen nicht mit: sie sind selbst vom Markt abgeleitet. Reine Auktionen auch nicht:
    ihr Gebot ist ein Zwischenstand (meist weit unter dem Endpreis) und zöge den Marktpreis nach unten."""
    prices = [p for p in (effective_price_chf(h, fx) for h in hits
                          if h.get("ai_relevant") == 1 and not h.get("_dup") and not h.get("single_estimated")
                          and not bidding(h)) if p]
    return statistics.median(prices) if len(prices) >= MIN_PRICES else None


def auction_limit(h: dict, plan: deliv.Plan, reference: float, fx: dict, typical_year: float | None = None,
                  new_price: float | None = None) -> tuple[float, float]:
    """Reine Auktion: (Wert für dich, höchstes lohnendes Gebot) in CHF. Bis zu diesem Gebot liegt der Endpreis
    (Gebot + Versand, Gebühren, Fahrt) nicht über dem fairen Wert nach Zustand und Jahrgang."""
    value = fair_value(h, reference, typical_year, new_price)
    extras = plan.total - (effective_price_chf(h, fx) or 0)
    return value, max(0.0, value - extras)


def out_of_range(h: dict, search: dict, fx: dict) -> str | None:
    """Liegt der Preis ausserhalb des Preisrahmens der Suche? Der Vorfilter prüft das nur beim ersten Fund –
    ändert sich der Rahmen oder der Preis, gilt er hier weiter. Bei reinen Auktionen sagt das Gebot nichts
    über den Mindestpreis (es steigt noch), nur die Obergrenze zählt."""
    price = effective_price_chf(h, fx)
    if price is None:
        return None
    lo, hi = search.get("min_price"), search.get("max_price")
    if hi is not None and price > hi:
        return f"Über deinem Höchstpreis ({_chf(hi)} CHF)"
    if lo is not None and price < lo and not bidding(h):
        return f"Unter deinem Mindestpreis ({_chf(lo)} CHF)"
    return None


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
    search = db.get_search(search_id) or {}
    for h in hits:  # Vorfilter gilt auch für Bestand: nach einer Regeländerung stünde sonst Altes weiter als passend da
        if h.get("ai_relevant") == 1 and h.get("ai_reason") != "Manuell eingeblendet" \
                and not matches_search(search, h.get("title") or ""):
            db.set_hit_relevant(search_id, h["key"], False, "Suchwörter fehlen im Titel")
            h["ai_relevant"] = 0
    mark_duplicates(hits)
    market = market_price(hits, fx)
    year = typical_year(hits)
    new_price = search.get("new_price")
    reference = market or (new_price * USED_SHARE if new_price else None)
    dcfg = cfg.get("delivery", {})
    rows = []
    for h in hits:
        if h.get("ai_relevant") != 1:
            rows.append((None, None, None, None, None, h["key"]))
            continue
        p = deliv.plan(h, fx, dcfg, search.get("product_size"), search.get("product_weight"))
        label = p.label if p else None
        if p and p.import_vat:  # nicht im Endpreis – nur ausgewiesen, was eine Verzollung kosten würde
            label += f" · Einfuhr-MWST {_chf(p.import_vat)} CHF separat"
        if h.get("_dup"):
            rows.append((None, None, "Doppelt inseriert – gleiches Angebot weiter oben", None, label, h["key"]))
        elif p is None:
            rows.append((None, None, "Preis offen", None, label, h["key"]))
        elif why := out_of_range(h, search, fx):
            rows.append((None, None, why, p.total, label, h["key"]))
        elif bidding(h):  # kein Score: der Preis steht erst am Ende fest (Auktions-Bereich, auctions.py)
            bid = f"aktuelles Gebot {_chf(effective_price_chf(h, fx))} CHF"
            if reference is None:
                rows.append((None, None, f"Auktion – {bid}, der Endpreis steht noch nicht fest", p.total, label, h["key"]))
            else:
                value, limit = auction_limit(h, p, reference, fx, year, new_price)
                rows.append((None, None, f"Auktion – {bid}. Wert für dich ≈ {_chf(value)} CHF: lohnt sich bis "
                                         f"{_chf(limit)} CHF Gebot", p.total, label, h["key"]))
        elif reference is None:
            rows.append((None, None, "Noch zu wenig Vergleichspreise für einen Marktpreis", p.total, label, h["key"]))
        else:
            score, reason, total = score_hit(h, reference, fx, year, dcfg, new_price, p, market is None)
            rows.append((score, tier_for(score), reason, total, label, h["key"]))
    db.set_scores(search_id, market, rows)
    if deliv.pending():  # neue Abholorte: Fahrzeiten nachschlagen, danach neu rechnen
        deliv.resolve_pending(dcfg, rescore_all)
    return market


def rescore_all() -> None:
    for s in db.list_searches():
        rescore_search(s["id"])
