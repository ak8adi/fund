from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from typing import Any

from ..models import Listing, Seller
from .extract import json_blobs, walk
from .scraped import ScrapedAdapter


class TraderaAdapter(ScrapedAdapter):
    name = "tradera"
    label = "Tradera"
    base_url = "https://www.tradera.com"

    def parse(self, page: str, source: str = "tradera") -> list[Listing]:
        listings = []
        seen_ids = set()

        for blob in json_blobs(page):
            for d in walk(blob):
                if isinstance(d, dict) and "itemId" in d and "price" in d:
                    ext_id = str(d["itemId"])
                    if ext_id in seen_ids:
                        continue
                    seen_ids.add(ext_id)

                    price = float(d["price"]) if d.get("price") is not None else None
                    title = d.get("shortDescription") or d.get("title") or "Ohne Titel"
                    url = d.get("itemUrl")
                    if url and not url.startswith("http"):
                        url = f"{self.base_url}{url}"

                    image = None
                    if "imageUrlTemplate" in d:
                        image = d["imageUrlTemplate"].replace("{format}", "1080x1080")
                    elif "image" in d:
                        image = d["image"]

                    posted_at = datetime.now(timezone.utc)
                    if d.get("startDate"):
                        try:
                            posted_at = datetime.fromisoformat(d["startDate"].replace("Z", "+00:00"))
                        except:
                            pass

                    seller_name = d.get("sellerAlias")

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
                        posted_at=posted_at,
                        source=source,
                        seller=Seller(name=seller_name)
                    ))

        return listings
