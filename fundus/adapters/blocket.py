from __future__ import annotations

import html
import re
from datetime import datetime, timezone

from ..models import Listing, Seller
from .extract import json_blobs
from .scraped import ScrapedAdapter


class BlocketAdapter(ScrapedAdapter):
    name = "blocket"
    label = "Blocket"
    base_url = "https://www.blocket.se"

    def parse(self, page: str, source: str = "blocket") -> list[Listing]:
        listings = []
        seen_ids = set()

        for blob in json_blobs(page):
            if isinstance(blob, dict) and blob.get("@type") == "ItemList":
                items = blob.get("itemListElement", [])
                if isinstance(items, list):
                    for item in items:
                        if not isinstance(item, dict): continue
                        d = item.get("item")
                        if not isinstance(d, dict) or d.get("@type") != "Product": continue

                        url = d.get("url")
                        if not url: continue

                        ext_id = url.rstrip("/").split("/")[-1]
                        if ext_id in seen_ids: continue
                        seen_ids.add(ext_id)

                        title = d.get("name") or "Ohne Titel"
                        image = d.get("image")

                        price = None
                        offers = d.get("offers")
                        if isinstance(offers, dict):
                            try: price = float(offers.get("price"))
                            except: pass

                        listings.append(Listing(
                            platform=self.name,
                            external_id=ext_id,
                            title=title,
                            url=url,
                            price=price,
                            currency="SEK",
                            image=image,
                            location=None,
                            country="SE",
                            posted_at=datetime.now(timezone.utc),
                            source=source,
                            seller=Seller()
                        ))

            if isinstance(blob, dict) and blob.get("@type") == "BreadcrumbList":
                pass

            # fallback
            if isinstance(blob, dict) and "mainEntity" in blob:
                items = blob["mainEntity"].get("itemListElement", [])
                if isinstance(items, list):
                    for item in items:
                        if not isinstance(item, dict): continue
                        d = item.get("item")
                        if not isinstance(d, dict) or d.get("@type") != "Product": continue

                        url = d.get("url")
                        if not url: continue

                        ext_id = url.rstrip("/").split("/")[-1]
                        if ext_id in seen_ids: continue
                        seen_ids.add(ext_id)

                        title = d.get("name") or "Ohne Titel"
                        image = d.get("image")

                        price = None
                        offers = d.get("offers")
                        if isinstance(offers, dict):
                            try: price = float(offers.get("price"))
                            except: pass

                        listings.append(Listing(
                            platform=self.name,
                            external_id=ext_id,
                            title=title,
                            url=url,
                            price=price,
                            currency="SEK",
                            image=image,
                            location=None,
                            country="SE",
                            posted_at=datetime.now(timezone.utc),
                            source=source,
                            seller=Seller()
                        ))

        for l in listings:  # Ort steht nur in der Ergebnis-Karte, nicht in den Strukturdaten
            i = page.find(f'id="{l.external_id}"')
            m = re.search(r'<span class="whitespace-nowrap truncate mr-8">([^<]{2,60})</span>', page[i:i + 8000]) if i >= 0 else None
            if m:
                l.location = html.unescape(m.group(1)).strip()
        return listings
