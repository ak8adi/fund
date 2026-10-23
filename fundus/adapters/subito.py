from __future__ import annotations

import json
from datetime import datetime, timezone

from ..models import Listing, Seller
from .extract import json_blobs, walk
from .scraped import ScrapedAdapter


class SubitoAdapter(ScrapedAdapter):
    name = "subito"
    label = "Subito"
    base_url = "https://www.subito.it"

    def parse(self, page: str, source: str = "subito") -> list[Listing]:
        listings = []
        seen_ids = set()

        for blob in json_blobs(page):
            for d in walk(blob):
                if isinstance(d, dict) and d.get("urn") and "item" in d.get("urn", ""):
                    ext_id = d["urn"].split(":")[-1]
                    if ext_id in seen_ids: continue

                    title = d.get("subject")
                    url = d.get("urls", {}).get("default")

                    if not title or not url: continue
                    seen_ids.add(ext_id)

                    price = None
                    features = d.get("features", {})
                    if isinstance(features, dict) and "price" in features:
                        try:
                            price = float(features["price"].get("value"))
                        except:
                            pass
                    elif d.get("price"):
                        try:
                            price = float(d["price"])
                        except:
                            pass

                    image = None
                    images = d.get("images", [])
                    if isinstance(images, list) and images:
                        img = images[0]
                        if isinstance(img, dict) and "urls" in img:
                            for size in ["scale", "default"]:
                                if size in img["urls"]:
                                    image = img["urls"][size]
                                    break

                    location = None
                    geo = d.get("geo", {})
                    if isinstance(geo, dict):
                        city = geo.get("town", {}).get("value")
                        if city: location = city

                    seller_name = None
                    advertiser = d.get("advertiser", {})
                    if isinstance(advertiser, dict):
                        seller_name = advertiser.get("name")

                    posted_at = datetime.now(timezone.utc)
                    try:
                        date_str = d.get("date")
                        if date_str:
                            posted_at = datetime.strptime(date_str, "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
                    except:
                        pass

                    listings.append(Listing(
                        platform=self.name,
                        external_id=ext_id,
                        title=title,
                        url=url if url.startswith("http") else f"{self.base_url}{url}",
                        price=price,
                        currency="EUR",
                        image=image,
                        location=location,
                        country="IT",
                        posted_at=posted_at,
                        source=source,
                        seller=Seller(name=seller_name)
                    ))

        return listings
