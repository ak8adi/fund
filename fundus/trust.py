"""Vertrauens-Check als Ampel – regelbasiert, ohne KI, mit kurzer Begründung.

Wichtig: Das ist ein Warnsignal-Detektor, keine Garantie. Wie aussagekräftig er ist,
hängt davon ab, wie viele Verkäuferdaten eine Plattform in der Trefferliste mitliefert
(eBay viel, Tutti/Anibis wenig).
"""

from __future__ import annotations

import re
import statistics
from dataclasses import dataclass, field
from datetime import datetime, timezone

from .models import Listing

SUSPICIOUS = [
    (re.compile(r"whats\s?app|telegram", re.I), "Kontakt über WhatsApp/Telegram verlangt"),
    (re.compile(r"vorkasse|vorauskasse|vorauszahlung|western union|moneygram", re.I), "Vorauszahlung verlangt"),
    (re.compile(r"paypal\s*(freunde|friends|f&f)", re.I), "PayPal Freunde (kein Käuferschutz)"),
    (re.compile(r"nur\s+versand|kein\s+abholen|keine\s+abholung", re.I), "nur Versand, keine Abholung"),
    (re.compile(r"\b(replica|1:1|nachbau)\b", re.I), "möglicherweise Fälschung"),
]

LEVELS = {
    "ok": "Vertrauenswürdig",
    "neutral": "Keine Auffälligkeiten",
    "check": "Genauer anschauen",
    "warn": "Vorsicht",
}


@dataclass
class TrustResult:
    level: str
    reasons: list[str] = field(default_factory=list)

    @property
    def label(self) -> str:
        return LEVELS[self.level]


def price_in_chf(price: float | None, currency: str, fx: dict) -> float | None:
    if price is None or price <= 0:
        return None
    return price * float(fx.get(currency, 1.0))


def reference_price(listings: list[Listing], fx: dict) -> float | None:
    prices = [p for p in (price_in_chf(l.price, l.currency, fx) for l in listings) if p]
    return statistics.median(prices) if len(prices) >= 5 else None


def assess(l: Listing, median_chf: float | None, fx: dict) -> TrustResult:
    score = 0
    reasons: list[str] = []
    s = l.seller
    has_data = False

    if s.rating_count is not None or s.rating_pct is not None:
        has_data = True
        count = s.rating_count or 0
        pct = s.rating_pct
        text = f"{count} Bewertungen" + (f", {pct:.1f} % positiv" if pct is not None else "")
        if pct is not None and count >= 5 and pct < 90:
            score -= 2
            reasons.append(f"schwache Bewertungen ({text})")
        elif count >= 50 and (pct is None or pct >= 98):
            score += 2
            reasons.append(text)
        elif count >= 10 and (pct is None or pct >= 95):
            score += 1
            reasons.append(text)
        elif count < 3:
            score -= 1
            reasons.append("kaum Bewertungen")
        else:
            reasons.append(text)

    if s.member_since:
        has_data = True
        days = (datetime.now(timezone.utc) - s.member_since).days
        if days < 30:
            score -= 2
            reasons.append(f"Konto erst {max(days, 0)} Tage alt")
        elif days > 3 * 365:
            score += 1
            reasons.append(f"Mitglied seit {s.member_since.year}")

    if s.verified:
        has_data = True
        score += 1
        reasons.append("verifiziert")

    chf = price_in_chf(l.price, l.currency, fx)
    if chf and median_chf:
        ratio = chf / median_chf
        if ratio < 0.35:
            score -= 2
            reasons.append(f"Preis {100 - ratio * 100:.0f} % unter dem Median – typisches Betrugsmuster")
        elif ratio < 0.55:
            score -= 1
            reasons.append(f"Preis deutlich unter dem Median ({ratio * 100:.0f} %)")

    for pattern, text in SUSPICIOUS:
        if pattern.search(l.title):
            score -= 2
            reasons.append(text)

    if not l.image:
        score -= 1
        reasons.append("kein Foto")

    if score <= -2:
        level = "warn"
    elif score < 0:
        level = "check"
    elif score >= 1 and has_data:
        level = "ok"
    else:
        level = "neutral"
        if not has_data:
            reasons.append("Plattform liefert in der Liste keine Verkäuferdaten")
    return TrustResult(level, reasons)
