"""eBay über die offizielle Browse API (OAuth Client-Credentials, kostenlos).

Jedes eBay-Land ist eine eigene, einzeln abschaltbare Quelle (ebay:EBAY_DE usw.).
"""

from __future__ import annotations

import base64
import html as html_lib
import re
import threading
import time
from urllib.parse import quote

import httpx

from ..models import Listing, Seller
from .base import Adapter, AdapterError, BlockedError
from .extract import parse_date

TOKEN_URL = "https://api.ebay.com/identity/v1/oauth2/token"
SEARCH_URL = "https://api.ebay.com/buy/browse/v1/item_summary/search"
ITEM_URL = "https://api.ebay.com/buy/browse/v1/item/{item_id}"

MARKETPLACES = {
    "EBAY_CH": ("eBay CH", "CHF", "CH"),
    "EBAY_DE": ("eBay DE", "EUR", "DE"),
    "EBAY_FR": ("eBay FR", "EUR", "FR"),
    "EBAY_IT": ("eBay IT", "EUR", "IT"),
    "EBAY_AT": ("eBay AT", "EUR", "AT"),
    "EBAY_ES": ("eBay ES", "EUR", "ES"),
    "EBAY_NL": ("eBay NL", "EUR", "NL"),
    "EBAY_GB": ("eBay UK", "GBP", "GB"),
}

_token_lock = threading.Lock()
_token: dict = {"value": None, "expires": 0.0}


class EbayAdapter(Adapter):
    name = "ebay"
    label = "eBay"

    def sources(self) -> list[str]:
        return [f"ebay:{m}" for m in self.cfg["ebay"]["marketplaces"] if m in MARKETPLACES]

    def source_label(self, source: str) -> str:
        return MARKETPLACES.get(source.split(":", 1)[-1], (source,))[0]

    def configured(self) -> tuple[bool, str]:
        e = self.cfg["ebay"]
        if e.get("app_id") and e.get("cert_id"):
            return True, ""
        return False, "Bitte unter Einstellungen eBay-Schlüssel eingeben"

    def _get_token(self) -> str:
        with _token_lock:
            if _token["value"] and time.time() < _token["expires"] - 120:
                return _token["value"]
            e = self.cfg["ebay"]
            basic = base64.b64encode(f"{e['app_id']}:{e['cert_id']}".encode()).decode()
            resp = httpx.post(
                TOKEN_URL,
                headers={"Authorization": f"Basic {basic}", "Content-Type": "application/x-www-form-urlencoded"},
                data={"grant_type": "client_credentials", "scope": "https://api.ebay.com/oauth/api_scope"},
                timeout=20,
            )
            if resp.status_code != 200:
                raise AdapterError(f"eBay-Login fehlgeschlagen (HTTP {resp.status_code}) – Schlüssel prüfen")
            data = resp.json()
            _token["value"] = data["access_token"]
            _token["expires"] = time.time() + int(data.get("expires_in", 7200))
            return _token["value"]

    def search(self, query, source, min_price, max_price) -> list[Listing]:
        marketplace = source.split(":", 1)[1]
        _, currency, country = MARKETPLACES[marketplace]
        rate = float(self.cfg.get("fx_to_chf", {}).get(currency, 1.0))
        e = self.cfg["ebay"]

        filters = []
        if min_price is not None or max_price is not None:
            lo = f"{min_price / rate:.0f}" if min_price is not None else ""
            hi = f"{max_price / rate:.0f}" if max_price is not None else ""
            filters += [f"price:[{lo}..{hi}]", f"priceCurrency:{currency}"]
        if e.get("used_only", True):
            filters.append("conditions:{USED}")
        if e.get("home_country_only", True):
            filters.append(f"itemLocationCountry:{country}")

        params = {"q": query, "sort": "newlyListed", "limit": "50"}
        if filters:
            params["filter"] = ",".join(filters)
        resp = httpx.get(
            SEARCH_URL,
            params=params,
            headers={"Authorization": f"Bearer {self._get_token()}", "X-EBAY-C-MARKETPLACE-ID": marketplace},
            timeout=25,
        )
        if resp.status_code == 429:
            raise BlockedError("eBay-Tageslimit erreicht")
        if resp.status_code != 200:
            raise AdapterError(f"eBay {marketplace}: HTTP {resp.status_code} {resp.text[:200]}")
        return [self._to_listing(i, marketplace) for i in resp.json().get("itemSummaries", [])]

    def item_details(self, item_id: str, marketplace: str = "EBAY_DE") -> dict:
        """Beschreibung und Status eines Inserats über die API (zählt zum Tageskontingent, kein Scraping).
        Rückgabe: {"description": str|None, "location": str|None, "gone": bool} und für Auktionen der frische Stand
        {"bid_price", "bids", "ends_at"} (None, wenn die API nichts nennt)."""
        resp = httpx.get(
            ITEM_URL.format(item_id=quote(item_id, safe="")),
            params={"fieldgroups": "PRODUCT"},
            headers={"Authorization": f"Bearer {self._get_token()}", "X-EBAY-C-MARKETPLACE-ID": marketplace},
            timeout=25,
        )
        if resp.status_code == 404:
            return {"description": None, "location": None, "gone": True, "bid_price": None, "bids": None, "ends_at": None}
        if resp.status_code == 429:
            raise BlockedError("eBay-Tageslimit erreicht")
        if resp.status_code != 200:
            raise AdapterError(f"eBay Inserat {item_id}: HTTP {resp.status_code}")
        d = resp.json()
        text = d.get("description") or d.get("shortDescription") or ""
        text = re.sub(r"(?is)<(script|style)\b.*?</\1>", " ", text)
        text = re.sub(r"(?i)<br\s*/?>|</p>|</div>|</li>", "\n", text)
        text = html_lib.unescape(re.sub(r"<[^>]+>", " ", text))
        text = re.sub(r"[ \t\xa0]+", " ", re.sub(r"\n\s*\n+", "\n\n", text)).strip()
        loc = d.get("itemLocation") or {}
        ended = d.get("itemEndDate") and parse_date(d["itemEndDate"]) and parse_date(d["itemEndDate"]).timestamp() < time.time()
        bid = (d.get("currentBidPrice") or {}).get("value")
        end = parse_date(d["itemEndDate"]) if d.get("itemEndDate") else None
        return {"bid_price": float(bid) if bid else None, "bids": d.get("bidCount"),
                "ends_at": end.isoformat() if end else None,
                "description": text[:6000] or None,
                "location": " ".join(p for p in (loc.get("postalCode"), loc.get("city")) if p) or None,
                "gone": bool(ended) or d.get("estimatedAvailabilities", [{}])[0].get("estimatedAvailabilityStatus") == "OUT_OF_STOCK"}

    @staticmethod
    def _to_listing(item: dict, marketplace: str) -> Listing:
        price = item.get("price") or item.get("currentBidPrice") or {}
        seller = item.get("seller") or {}
        loc = item.get("itemLocation") or {}
        try:
            rating_pct = float(seller["feedbackPercentage"]) if seller.get("feedbackPercentage") else None
        except ValueError:
            rating_pct = None
        return Listing(
            platform="ebay",
            external_id=item["itemId"],
            title=item.get("title", "")[:200],
            url=item.get("itemWebUrl") or item.get("itemAffiliateWebUrl") or "",
            price=float(price["value"]) if price.get("value") else None,
            currency=price.get("currency", "EUR"),
            image=(item.get("image") or {}).get("imageUrl")
            or next((i.get("imageUrl") for i in item.get("thumbnailImages", [])), None),
            location=" ".join(p for p in (loc.get("postalCode"), loc.get("city")) if p) or None,
            country=loc.get("country"),
            posted_at=parse_date(item.get("itemCreationDate")),
            source=marketplace,
            auction="AUCTION" in (item.get("buyingOptions") or []),
            bids=item.get("bidCount"),
            bid_price=float(item["currentBidPrice"]["value"]) if (item.get("currentBidPrice") or {}).get("value") else None,
            ends_at=parse_date(item.get("itemEndDate")),
            seller=Seller(
                name=seller.get("username"),
                rating_pct=rating_pct,
                rating_count=seller.get("feedbackScore"),
                is_business=(item.get("seller", {}).get("sellerAccountType") == "BUSINESS") or None,
            ),
        )
