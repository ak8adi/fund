"""Gemeinsames Datenmodell für alle Plattformen."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime


@dataclass
class Seller:
    name: str | None = None
    rating_pct: float | None = None      # 0–100, Anteil positiver Bewertungen
    rating_count: int | None = None      # Anzahl Bewertungen
    member_since: datetime | None = None
    verified: bool | None = None
    is_business: bool | None = None


@dataclass
class Listing:
    platform: str                         # "ricardo", "tutti", "anibis", "ebay"
    external_id: str
    title: str
    url: str
    price: float | None = None
    currency: str = "CHF"
    image: str | None = None
    location: str | None = None
    country: str | None = None
    posted_at: datetime | None = None
    source: str | None = None             # Unterquelle, z.B. "EBAY_DE"
    seller: Seller = field(default_factory=Seller)

    @property
    def key(self) -> str:
        return f"{self.platform}:{self.external_id}"
