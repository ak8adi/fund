from __future__ import annotations

import re
from datetime import datetime, timezone

from ..models import Listing, Seller
from .extract import extract_listings, parse_price, json_blobs, walk
from .scraped import ScrapedAdapter


class WillhabenAdapter(ScrapedAdapter):
    name = "willhaben"
    label = "Willhaben"
    base_url = "https://www.willhaben.at"

    def build_url(self, d: dict, ext_id: str) -> str | None:
        url = d.get("seoUrl") or d.get("url") or d.get("href") or d.get("link") or d.get("canonicalUrl")
        if isinstance(url, str):
            if url.startswith("http"):
                return url
            if url.startswith("/"):
                return f"{self.base_url}{url}"
        if ext_id and ext_id.isdigit():
            return f"{self.base_url}/iad/kaufen-und-verkaufen/d/item-{ext_id}/"
        return None

    def parse(self, page: str, source: str = "willhaben") -> list[Listing]:
        # Collect IDs of paid/promoted ads from JSON blobs to ignore them
        promoted_ids: set[str] = set()
        for blob in json_blobs(page):
            for d in walk(blob):
                if isinstance(d, dict):
                    if d.get("upsell") or d.get("isPromoted") or d.get("isTopAd") or d.get("advertStatus") in ("PROMOTED", "TOPAD"):
                        aid = d.get("id") or d.get("id") or d.get("advertId")
                        if aid:
                            promoted_ids.add(str(aid))

        raw_listings = extract_listings(
            page,
            self.name,
            self.base_url,
            url_builder=self.build_url,
            url_pattern=r"/iad/kaufen-und-verkaufen/d/",
        )

        listings = []
        seen_ids = set()

        for l in raw_listings:
            if l.external_id in seen_ids or l.external_id in promoted_ids:
                continue
            seen_ids.add(l.external_id)
            l.currency = "EUR"
            l.country = "AT"
            l.source = l.source or source
            listings.append(l)

        if not listings:
            listings = self._parse_html(page, source)

        return listings

    def _parse_html(self, page: str, source: str) -> list[Listing]:
        listings = []
        cards = re.findall(
            r'<(?:article|div|section)[^>]*(?:id="search-result-entry-[^"]*"|data-testid="search-result-entry-[^"]*"|class="[^"]*(?:search-result-entry|Box-sc|ResultList-[^"]*)"|data-clickable)[^>]*>.*?</(?:article|div|section)>',
            page,
            re.DOTALL | re.I,
        )
        if not cards:
            cards = re.findall(
                r'<a[^>]*href="(/iad/kaufen-und-verkaufen/d/[^"]+)"[^>]*>.*?</a>',
                page,
                re.DOTALL | re.I,
            )

        seen_ids = set()
        for card in cards:
            if any(k in card.lower() for k in ("is-topad", "promoted", "sponsored", "upsell", "top-ad", "ad-banner")):
                continue

            m_href = re.search(r'href="(/iad/kaufen-und-verkaufen/d/[^"]+)"', card, re.I)
            if not m_href and card.startswith("/iad/kaufen-und-verkaufen/d/"):
                rel_url = card
            elif m_href:
                rel_url = m_href.group(1)
            else:
                continue

            url = f"{self.base_url}{rel_url}" if rel_url.startswith("/") else rel_url

            m_id = re.search(r'-(\d+)/?$', rel_url) or re.search(r'/(\d+)/?$', rel_url)
            if not m_id:
                continue
            ext_id = m_id.group(1)

            if ext_id in seen_ids:
                continue
            seen_ids.add(ext_id)

            m_title = re.search(r'<h[2-4][^>]*>(.*?)</h[2-4]>', card, re.DOTALL | re.I) or re.search(
                r'alt="([^"]+)"', card, re.I
            )
            title = m_title.group(1).strip() if m_title else "Ohne Titel"
            title = re.sub(r'<[^>]+>', '', title).replace("&quot;", '"').replace("&amp;", "&")

            m_price = re.search(
                r'(?:€|EUR)?\s*(\d+(?:[.,]\d{1,2})?)\s*(?:€|EUR|VB|Verhandlungsbasis)?', card, re.I
            )
            price = None
            if m_price:
                price, _ = parse_price(m_price.group(0))

            m_img = re.search(r'<img[^>]*src="([^"]+)"', card, re.I)
            image = m_img.group(1) if m_img else None

            m_loc = re.search(r'(\d{4}\s+[A-Za-zÄÖÜäöüß\s-]+)', card)
            location = m_loc.group(1).strip() if m_loc else None

            posted_at = datetime.now(timezone.utc)

            listings.append(
                Listing(
                    platform=self.name,
                    external_id=ext_id,
                    title=title,
                    url=url,
                    price=price,
                    currency="EUR",
                    image=image,
                    location=location,
                    country="AT",
                    posted_at=posted_at,
                    source=source,
                    seller=Seller(),
                )
            )

        return listings
