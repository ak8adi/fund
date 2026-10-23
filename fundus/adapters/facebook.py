from __future__ import annotations

import json
import re
from datetime import datetime, timezone
import logging

from ..models import Listing, Seller
from .scraped import ScrapedAdapter
from .base import fetch_html, BlockedError, AdapterError

log = logging.getLogger(__name__)

class FacebookAdapter(ScrapedAdapter):
    name = "facebook"
    label = "Facebook Marketplace"
    base_url = "https://www.facebook.com"

    def search(self, query: str, source: str, min_price: float | None, max_price: float | None) -> list[Listing]:
        url = f"{self.base_url}/marketplace/search/?query={query}"

        headers = {}
        fb_cookie = self.cfg.get("facebook", {}).get("fb_cookie")
        if fb_cookie:
            headers["cookie"] = fb_cookie

        try:
            # Facebook will likely block automated requests without cookies.
            # We catch any exception and log it instead of throwing, so the app doesn't crash.
            page = fetch_html(url, fetcher="http", headers=headers)
            return self.parse(page, source)
        except (BlockedError, AdapterError) as e:
            log.info(f"Facebook Marketplace access blocked or failed: {e}")
            return []
        except Exception as e:
            log.warning(f"Unexpected error when fetching Facebook: {e}")
            return []

    def parse(self, page: str, source: str = "facebook") -> list[Listing]:
        listings = []
        seen_ids = set()

        # Very basic attempt to parse FB's complex GraphQL initial state.
        # This will only grab basic elements if they happen to be in the HTML.
        # FB heavily obfuscates its Next/React state.

        matches = re.findall(r'"node":\{"__typename":"MarketplaceListing","id":"(\d+)","listing_value":\{"__typename":"MarketplaceListingValue","primary_listing_value":\{"text":"(.*?)"\}.*?"marketplace_listing_title":"(.*?)".*?"primary_photo":\{"image":\{"uri":"(.*?)"\}\}', page)

        for m in matches:
            ext_id = m[0]
            if ext_id in seen_ids: continue
            seen_ids.add(ext_id)

            price_text = m[1]
            title = m[2]
            image = m[3]

            price = None
            price_match = re.search(r'[\d\.,]+', price_text)
            if price_match:
                try:
                    price = float(price_match.group(0).replace('.', '').replace(',', '.'))
                except:
                    pass

            url = f"{self.base_url}/marketplace/item/{ext_id}/"

            listings.append(Listing(
                platform=self.name,
                external_id=ext_id,
                title=title,
                url=url,
                price=price,
                currency="EUR", # Or local currency based on fb response
                image=image.replace('\\/', '/'),
                location=None,
                country="Unknown",
                posted_at=datetime.now(timezone.utc),
                source=source,
                seller=Seller()
            ))

        return listings
