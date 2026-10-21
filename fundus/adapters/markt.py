from __future__ import annotations

import re
from datetime import datetime, timezone, timedelta
from urllib.parse import quote

from ..models import Listing, Seller
from .base import Adapter, AdapterError, fetch_html, BlockedError
from .extract import extract_listings, parse_price, json_blobs, walk


class MarktAdapter(Adapter):
    name = "markt"
    label = "Markt.de"
    base_url = "https://www.markt.de"

    def platform_cfg(self) -> dict:
        return self.cfg["platforms"].get(self.name, {})

    def search_url(self, query: str) -> str:
        q = quote(query.strip())
        return self.platform_cfg()["url"].format(query=q)

    def search(self, query, source, min_price, max_price) -> list[Listing]:
        url = self.search_url(query)
        try:
            page = fetch_html(url, self.platform_cfg().get("fetcher", "http"))
        except BlockedError:
            raise

        listings = self.parse(page, source)

        if not listings and "suche" not in url and "search" not in page.lower():
            raise AdapterError(
                "Seite geladen, aber keine Daten gefunden – Struktur geändert oder geblockt? "
                f"Mit «fundus probe {self.name} \"{query}\"» untersuchen."
            )

        return listings

    def parse(self, page: str, source: str = "markt") -> list[Listing]:
        listings = []
        seen_ids = set()

        # Try JSON / JSON-LD extraction first if present
        raw_json_listings = extract_listings(
            page,
            self.name,
            self.base_url,
            url_pattern=r"/a/[a-zA-Z0-9_-]+/",
        )
        for l in raw_json_listings:
            if l.external_id not in seen_ids:
                seen_ids.add(l.external_id)
                l.currency = "EUR"
                l.country = "DE"
                l.source = l.source or source
                listings.append(l)

        # HTML parsing (card / article / list item elements)
        items = re.findall(
            r'<(?:article|div|li)[^>]*(?:class="[^"]*(?:expose|record|search-result|item|teaser|listing)[^"]*"|data-id)[^>]*>.*?</(?:article|div|li)>',
            page,
            re.DOTALL | re.I,
        )

        if not items:
            items = re.findall(
                r'<a[^>]*href="([^"]*/a/[a-zA-Z0-9_-]+/[^"]*)"[^>]*>.*?</a>',
                page,
                re.DOTALL | re.I,
            )

        for item in items:
            # Skip paid, top, sponsored or promoted ads
            if any(k in item.lower() for k in ("is-topad", "top-ad", "sponsored", "hervorgehoben", "banner", "galerie-anzeige", "premium", "partner")):
                continue

            m_href = re.search(r'href="([^"]*/a/([a-zA-Z0-9_-]+)/[^"]*|/a/([a-zA-Z0-9_-]+)/?)"', item, re.I)
            if not m_href and "/a/" in item:
                m_href = re.search(r'(/a/([a-zA-Z0-9_-]+)/?)', item)
                if m_href:
                    rel_url = m_href.group(1)
                    ext_id = m_href.group(2)
                else:
                    continue
            elif m_href:
                rel_url = m_href.group(1)
                ext_id = m_href.group(2) or m_href.group(3)
            else:
                continue

            if not ext_id or ext_id in seen_ids:
                continue

            url = rel_url if rel_url.startswith("http") else f"{self.base_url}{rel_url if rel_url.startswith('/') else '/' + rel_url}"

            # Title
            m_title = re.search(r'<(?:h[2-4]|a)[^>]*class="[^"]*(?:title|subject|name)[^"]*"[^>]*>(.*?)</(?:h[2-4]|a)>', item, re.DOTALL | re.I)
            if not m_title:
                m_title = re.search(r'<h[2-4][^>]*>(.*?)</h[2-4]>', item, re.DOTALL | re.I) or re.search(r'title="([^"]+)"', item, re.I)
            title = m_title.group(1).strip() if m_title else "Ohne Titel"
            title = re.sub(r'<[^>]+>', '', title).replace("&quot;", '"').replace("&amp;", "&")

            # Price
            m_price = re.search(r'class="[^"]*(?:price|betrag)[^"]*"[^>]*>(.*?)</span>', item, re.DOTALL | re.I)
            if not m_price:
                m_price = re.search(r'(?:€|EUR)?\s*(\d+(?:[.,]\d{1,2})?)\s*(?:€|EUR|VB|Verhandlungsbasis)?', item, re.I)
            price = None
            if m_price:
                price_str = m_price.group(1).strip()
                if price_str.lower() not in ("vb", "zu verschenken", "auf anfrage"):
                    price, _ = parse_price(price_str)

            # Image
            m_img = re.search(r'<img[^>]*(?:data-src|src)="([^"]+)"', item, re.I)
            image = m_img.group(1) if m_img else None
            if image and ("placeholder" in image.lower() or "blank.gif" in image.lower()):
                image = None

            # Location / PLZ
            m_loc = re.search(r'(\d{5}\s+[A-Za-zÄÖÜäöüß\s-]+)', item)
            if not m_loc:
                m_loc = re.search(r'class="[^"]*(?:location|ort|city)[^"]*"[^>]*>(.*?)</span>', item, re.DOTALL | re.I)
            location = m_loc.group(1).strip() if m_loc else None
            if location:
                location = re.sub(r'<[^>]+>', '', location).strip()

            # Date
            posted_at = datetime.now(timezone.utc)
            m_date = re.search(r'((?:Heute|Gestern|\d{1,2}\.\d{1,2}\.\d{2,4}).*?)(?:<|$)', item, re.DOTALL | re.I)
            if m_date:
                date_str = m_date.group(1).strip()
                try:
                    if "Heute" in date_str:
                        m_time = re.search(r'(\d{1,2}):(\d{2})', date_str)
                        if m_time:
                            posted_at = posted_at.replace(hour=int(m_time.group(1)), minute=int(m_time.group(2)), second=0, microsecond=0)
                    elif "Gestern" in date_str:
                        m_time = re.search(r'(\d{1,2}):(\d{2})', date_str)
                        if m_time:
                            yesterday = posted_at - timedelta(days=1)
                            posted_at = yesterday.replace(hour=int(m_time.group(1)), minute=int(m_time.group(2)), second=0, microsecond=0)
                    else:
                        m_time = re.search(r'(\d{1,2})\.(\d{1,2})\.(\d{2,4})', date_str)
                        if m_time:
                            yr = int(m_time.group(3))
                            if yr < 100:
                                yr += 2000
                            posted_at = datetime(year=yr, month=int(m_time.group(2)), day=int(m_time.group(1)), tzinfo=timezone.utc)
                except Exception:
                    pass

            seen_ids.add(ext_id)
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
                    country="DE",
                    posted_at=posted_at,
                    source=source,
                    seller=Seller(),
                )
            )

        return listings
