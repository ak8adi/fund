from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from typing import Any

from ..models import Listing, Seller
from .extract import extract_listings, parse_price, json_blobs, walk
from .scraped import ScrapedAdapter


class LeboncoinAdapter(ScrapedAdapter):
    name = "leboncoin"
    label = "leboncoin"
    base_url = "https://www.leboncoin.fr"

    def build_url(self, d: dict, ext_id: str) -> str | None:
        url = d.get("url")
        if url:
            if not url.startswith("http"):
                return f"{self.base_url}{url}"
            return url
        return f"{self.base_url}/ad/{ext_id}"

    def parse(self, page: str, source: str = "leboncoin") -> list[Listing]:
        listings = []
        seen_ids = set()

        for blob in json_blobs(page):
            for d in walk(blob):
                if isinstance(d, dict) and d.get("list_id") and "subject" in d and "price" in d:
                    ext_id = str(d["list_id"])
                    if ext_id in seen_ids:
                        continue
                    seen_ids.add(ext_id)

                    price_val = d["price"]
                    if isinstance(price_val, (int, float)):
                        price = float(price_val)
                    elif isinstance(price_val, list) and price_val:
                        price = float(price_val[0])
                    else:
                        price = None

                    url = self.build_url(d, ext_id)
                    title = d.get("subject") or "Ohne Titel"
                    image_url = None
                    images = d.get("images", {})
                    if isinstance(images, dict):
                        urls = images.get("urls")
                        if isinstance(urls, list) and urls:
                            image_url = urls[0]
                        elif isinstance(images.get("thumb_url"), str):
                            image_url = images["thumb_url"]

                    location = None
                    loc_data = d.get("location", {})
                    if isinstance(loc_data, dict):
                        city = loc_data.get("city")
                        zipcode = loc_data.get("zipcode")
                        if city and zipcode:
                            location = f"{zipcode} {city}"
                        elif city:
                            location = city

                    seller_name = None
                    owner = d.get("owner", {})
                    if isinstance(owner, dict):
                        seller_name = owner.get("name")

                    posted_at = datetime.now(timezone.utc)
                    try:
                        index_date = d.get("index_date")
                        if index_date:
                            posted_at = datetime.strptime(index_date, "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
                    except Exception:
                        pass

                    listings.append(Listing(
                        platform=self.name,
                        external_id=ext_id,
                        title=title,
                        url=url,
                        price=price,
                        currency="EUR",
                        image=image_url,
                        location=location,
                        country="FR",
                        posted_at=posted_at,
                        source=source,
                        seller=Seller(name=seller_name)
                    ))

        if not listings:
            raw = extract_listings(page, self.name, self.base_url, url_pattern=r'/ad/', url_builder=self.build_url)
            for l in raw:
                l.currency = "EUR"
                l.country = "FR"
                l.source = source
                listings.append(l)

        return listings
