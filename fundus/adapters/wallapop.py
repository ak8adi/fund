from __future__ import annotations

import json
from datetime import datetime, timezone

from ..models import Listing, Seller
from .extract import json_blobs, walk
from .scraped import ScrapedAdapter


class WallapopAdapter(ScrapedAdapter):
    name = "wallapop"
    label = "Wallapop"
    base_url = "https://es.wallapop.com"

    def parse(self, page: str, source: str = "wallapop") -> list[Listing]:
        listings = []
        seen_ids = set()

        for blob in json_blobs(page):
            for d in walk(blob):
                if isinstance(d, dict) and d.get("id") and d.get("title") and d.get("price"):
                    ext_id = str(d["id"])
                    if ext_id in seen_ids:
                        continue

                    price = None
                    try:
                        price = float(d["price"])
                    except (ValueError, TypeError):
                        p = d.get("price", {}).get("amount")
                        if p:
                            try:
                                price = float(p)
                            except:
                                pass

                    if not price:
                        continue

                    seen_ids.add(ext_id)
                    title = d["title"]
                    url = d.get("webSlug") or d.get("slug")
                    if url and not url.startswith("http"):
                        url = f"{self.base_url}/item/{url}"

                    image = None
                    images = d.get("images", [])
                    if isinstance(images, list) and images:
                        img = images[0]
                        if isinstance(img, dict):
                            image = img.get("original") or img.get("url")
                        elif isinstance(img, str):
                            image = img

                    location = None
                    loc = d.get("location", {})
                    if isinstance(loc, dict):
                        location = loc.get("city")

                    listings.append(Listing(
                        platform=self.name,
                        external_id=ext_id,
                        title=title,
                        url=url or f"{self.base_url}/item/{ext_id}",
                        price=price,
                        currency="EUR",
                        image=image,
                        location=location,
                        country="ES",
                        posted_at=datetime.now(timezone.utc),
                        source=source,
                        seller=Seller()
                    ))

        return listings
