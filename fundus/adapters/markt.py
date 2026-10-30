"""Markt.de: Suchseite als HTML, eine Karte pro Inserat (<li class="clsy-c-result-list-item">)."""

from __future__ import annotations

import html as html_lib
import re
from datetime import datetime, timezone
from urllib.parse import quote

from ..models import Listing, Seller
from .base import Adapter, fetch_html

ITEM_RE = re.compile(r'<li class="clsy-c-result-list-item"[^>]*id="markt_result_(\w+)"[^>]*>(.*?)</li>', re.S)
NO_RESULTS = "Leider wurden keine Anzeigen gefunden"  # danach folgen nur «Die neuesten Anzeigen» als Füllmaterial


def _text(fragment: str | None) -> str:
    return re.sub(r"\s+", " ", html_lib.unescape(re.sub(r"<[^>]+>", " ", fragment or ""))).strip()


def _field(item: str, cls: str) -> str | None:
    m = re.search(rf'class="clsy-c-result-list-item__{cls}[^"]*"[^>]*>(.*?)</(?:div|a|span)>', item, re.S)
    return _text(m.group(1)) if m else None


def parse_price_de(value: str | None) -> float | None:
    """'1.180,00 €' → 1180.0, '500 €' → 500.0, 'Zu verschenken' → None."""
    m = re.search(r"\d[\d.]*(?:,\d{1,2})?", value or "")
    if not m:
        return None
    return float(m.group(0).replace(".", "").replace(",", "."))


def _matches(query: str, text: str) -> bool:
    """Alle Suchwörter müssen in Titel oder Beschreibung vorkommen (Markt.de sucht sehr unscharf)."""
    text = text.lower()
    return all(w in text for w in re.findall(r"\w{2,}", query.lower()))


class MarktAdapter(Adapter):
    name = "markt"
    label = "Markt.de"
    base_url = "https://www.markt.de"

    def platform_cfg(self) -> dict:
        return self.cfg["platforms"].get(self.name, {})

    def search_url(self, query: str) -> str:
        return self.platform_cfg()["url"].format(query=quote(query.strip()))

    def search(self, query, source, min_price, max_price) -> list[Listing]:
        page = fetch_html(self.search_url(query), self.platform_cfg().get("fetcher", "http"))
        return self.parse(page, source, query)

    def parse(self, page: str, source: str = "markt", query: str = "") -> list[Listing]:
        if NO_RESULTS in page:
            return []
        listings = []
        for ext_id, item in ITEM_RE.findall(page):
            if "__partner" in item:  # Partner-Anzeigen: Immobilien, Jobs … unabhängig vom Suchbegriff
                continue
            m_link = re.search(r'<a href="([^"]+)" class="clsy-c-result-list-item__link', item)
            title = _field(item, "link")
            if not m_link or not title:
                continue
            if query and not _matches(query, f"{title} {_field(item, 'description') or ''}"):
                continue
            url = html_lib.unescape(m_link.group(1)).split("?")[0]
            m_img = re.search(r'<img src="([^"]+)"', item)
            posted_at = None
            m_date = re.search(r"(\d{1,2})\.(\d{1,2})\.(\d{4})", _field(item, "date") or "")
            if m_date:
                posted_at = datetime(int(m_date[3]), int(m_date[2]), int(m_date[1]), tzinfo=timezone.utc)
            listings.append(Listing(
                platform=self.name,
                external_id=ext_id,
                title=title[:200],
                url=url if url.startswith("http") else self.base_url + url,
                price=parse_price_de(_field(item, "price-amount")),
                currency="EUR",
                image=m_img.group(1) if m_img else None,
                location=(_field(item, "location") or None),
                country="DE",
                posted_at=posted_at,
                source=source,
                seller=Seller(),
            ))
        return listings
