"""Inserate aus eingebettetem JSON einer Suchseite herauslesen.

Moderne Marktplätze (Next.js etc.) liefern die Suchresultate als JSON in der Seite mit
(__NEXT_DATA__, application/ld+json, window.__STATE__ …). Statt fragiler CSS-Selektoren
wird dieses JSON rekursiv nach Objekten durchsucht, die wie ein Inserat aussehen.
Das übersteht Layout-Änderungen deutlich besser.
"""

from __future__ import annotations

import html as html_lib
import json
import re
from datetime import datetime, timezone
from typing import Any, Callable, Iterator

from ..models import Listing, Seller

SCRIPT_RE = re.compile(r"<script\b([^>]*)>(.*?)</script>", re.S | re.I)
STATE_RE = re.compile(r"window\.__[A-Z0-9_]+__\s*=\s*(\{.*?\})\s*;?\s*$", re.S | re.M)
FLIGHT_PUSH_RE = re.compile(r"self\.__next_f\.push\((\[.*\])\)\s*;?\s*$", re.S)
FLIGHT_ROW_RE = re.compile(r"^[0-9a-f]+:(?=[\[{])")

ID_KEYS = ("listingID", "listingId", "articleId", "itemId", "adId", "id")
TITLE_KEYS = ("title", "subject", "name", "heading", "description")
PRICE_KEYS = ("price", "formattedPrice", "buyNowPrice", "currentPrice", "currentBidPrice", "bidPrice",
              "startPrice", "priceInfo", "offers", "amount")
URL_KEYS = ("url", "href", "link", "seoUrl", "canonicalUrl", "permalink")
IMAGE_KEYS = ("image", "imageUrl", "thumbnail", "thumbnailUrl", "images", "pictures", "photo", "mainImage")
DATE_KEYS = ("timestamp", "createdAt", "publishedAt", "startDate", "creationDate", "insertionDate", "datePosted")
LOCATION_KEYS = ("location", "locationName", "city", "postcodeInformation", "address", "zipCity")
SELLER_KEYS = ("seller", "sellerInfo", "user", "owner", "vendor", "publisher")


def json_blobs(page: str) -> Iterator[Any]:
    """Alle JSON-Strukturen aus <script>-Tags und window.__X__-Zuweisungen."""
    flight: list[str] = []
    for attrs, body in SCRIPT_RE.findall(page):
        body = body.strip()
        if not body:
            continue
        m = FLIGHT_PUSH_RE.match(body)
        if m:  # Next.js App Router: Daten stecken in self.__next_f.push([1, "…"])
            try:
                chunk = json.loads(m.group(1))
            except ValueError:
                continue
            if len(chunk) > 1 and chunk[0] == 1 and isinstance(chunk[1], str):
                flight.append(chunk[1])
            continue
        if "json" in attrs.lower() or body[:1] in "{[":
            try:
                yield json.loads(html_lib.unescape(body) if "&quot;" in body[:200] else body)
                continue
            except ValueError:
                pass
        for m in STATE_RE.finditer(body):
            try:
                yield json.loads(m.group(1))
            except ValueError:
                pass
    # Flight-Zeilen sind «id:JSON»; einzelne Chunks brechen mitten in einer Zeile um, daher erst zusammensetzen.
    for line in "".join(flight).split("\n"):
        m = FLIGHT_ROW_RE.match(line)
        if m:
            try:
                yield json.loads(line[m.end():])
            except ValueError:
                pass


def walk(obj: Any, depth: int = 0) -> Iterator[dict]:
    if depth > 40:
        return
    if isinstance(obj, dict):
        yield obj
        for v in obj.values():
            yield from walk(v, depth + 1)
    elif isinstance(obj, list):
        for v in obj:
            yield from walk(v, depth + 1)


def _first(d: dict, keys: tuple[str, ...]) -> Any:
    for k in keys:
        if k in d and d[k] not in (None, "", [], {}):
            return d[k]
    return None


def parse_price(value: Any) -> tuple[float | None, str | None]:
    """Akzeptiert 120, '120.–', 'CHF 1'200.50', {'amount': 12}, {'value': '12.0', 'currency': 'EUR'}."""
    if value is None or isinstance(value, bool):
        return None, None
    if isinstance(value, (int, float)):
        return float(value), None
    if isinstance(value, dict):
        cur = value.get("currency") or value.get("priceCurrency") or value.get("currencyCode")
        for k in ("amount", "value", "price", "formatted", "formattedPrice", "buyNowPrice", "lowPrice"):
            if k in value:
                p, c = parse_price(value[k])
                if p is not None:
                    return p, cur or c
        return None, cur
    if isinstance(value, list):
        for v in value:
            p, c = parse_price(v)
            if p is not None:
                return p, c
        return None, None
    if isinstance(value, str):
        s = value.replace("'", "").replace("’", "").replace(" ", " ")
        cur = None
        for code in ("CHF", "EUR", "€", "Fr."):
            if code in s:
                cur = "EUR" if code == "€" else ("CHF" if code == "Fr." else code)
        m = re.search(r"\d+(?:[.,]\d{1,2})?", s.replace(" ", ""))
        if not m:
            return None, cur  # "Gratis", "Auf Anfrage"
        num = m.group(0).replace(",", ".")
        try:
            return float(num), cur
        except ValueError:
            return None, cur
    return None, None


def parse_date(value: Any) -> datetime | None:
    if value in (None, ""):
        return None
    if isinstance(value, (int, float)):
        ts = float(value)
        if ts > 1e12:  # Millisekunden
            ts /= 1000
        if 1e9 < ts < 4e9:
            return datetime.fromtimestamp(ts, tz=timezone.utc)
        return None
    if isinstance(value, str):
        s = value.strip().replace("Z", "+00:00")
        try:
            dt = datetime.fromisoformat(s)
        except ValueError:
            return parse_date(float(s)) if s.isdigit() else None
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    return None


def _image(value: Any) -> str | None:
    if isinstance(value, str):
        return value if value.startswith(("http", "//")) else None
    if isinstance(value, list):
        for v in value:
            img = _image(v)
            if img:
                return img
    if isinstance(value, dict):
        for k in ("url", "src", "imageUrl", "normalRendition", "large", "medium", "thumbnail", "contentUrl", "uri"):
            if k in value:
                img = _image(value[k])
                if img:
                    return img
    return None


def _location(value: Any) -> str | None:
    if isinstance(value, str):
        return value[:80]
    if isinstance(value, dict):
        parts = [value.get(k) for k in ("postcode", "postalCode", "zipCode", "zip")]
        parts += [value.get(k) for k in ("locationName", "city", "addressLocality", "name")]
        canton = value.get("canton")
        if isinstance(canton, dict):
            canton = canton.get("shortName") or canton.get("name")
        parts.append(canton)
        text = " ".join(str(p) for p in parts if p)
        return text[:80] or None
    return None


def parse_seller(d: dict) -> Seller:
    raw = _first(d, SELLER_KEYS)
    if not isinstance(raw, dict):
        raw = {}
    s = Seller()
    s.name = _first(raw, ("alias", "nickname", "username", "userName", "displayName", "name"))
    if s.name is not None:
        s.name = str(s.name)[:60]
    rating = _first(raw, ("feedbackPercentage", "positiveRatingPercentage", "ratingPercentage", "positivePercent"))
    if rating is None:
        r = raw.get("rating")
        if isinstance(r, dict):
            rating = _first(r, ("percentage", "positivePercentage", "value"))
            count = _first(r, ("count", "total", "numberOfRatings"))
            if count is not None:
                raw = {**raw, "ratingCount": count}
        elif isinstance(r, (int, float)) and r <= 5:
            rating = r * 20  # Sterne → Prozent
        elif isinstance(r, (int, float)):
            rating = r
    if rating is not None:
        try:
            s.rating_pct = float(rating)
        except (TypeError, ValueError):
            pass
    count = _first(raw, ("ratingCount", "feedbackScore", "numberOfRatings", "ratingsCount", "reviewCount"))
    if count is not None:
        try:
            s.rating_count = int(count)
        except (TypeError, ValueError):
            pass
    s.member_since = parse_date(_first(raw, ("memberSince", "registrationDate", "registeredSince", "createdAt")))
    verified = _first(raw, ("verified", "isVerified", "phoneVerified", "idVerified"))
    s.verified = bool(verified) if verified is not None else None
    business = _first(raw, ("isCompany", "isBusiness", "isProfessional", "company"))
    if business is None and isinstance(raw.get("type"), str):
        business = raw["type"].lower() in ("business", "company", "professional", "pro")
    s.is_business = bool(business) if business is not None else None
    return s


def _id_from_url(d: dict) -> str | None:
    url = _first(d, URL_KEYS)
    if isinstance(url, str):
        m = re.findall(r"\d{5,}", url)
        if m:
            return m[-1]
    return None


def looks_like_listing(d: dict) -> bool:
    if not isinstance(_first(d, TITLE_KEYS), str):
        return False
    if _first(d, ID_KEYS) is None:
        # schema.org-Produkte haben oft keine id, aber eine Inserat-URL mit Nummer
        return _first(d, PRICE_KEYS) is not None and _id_from_url(d) is not None
    return _first(d, PRICE_KEYS) is not None or isinstance(_first(d, URL_KEYS), str)


def extract_listings(
    page: str,
    platform: str,
    base_url: str,
    url_builder: Callable[[dict, str], str | None] | None = None,
    url_pattern: str | None = None,
    require_price: bool = False,
) -> list[Listing]:
    """url_builder(rohobjekt, id) baut die Inserat-URL; url_pattern / require_price filtern falsche Treffer
    (z.B. Kategorien oder Navigationseinträge, die auch id + name haben)."""
    pattern = re.compile(url_pattern) if url_pattern else None
    found: dict[str, Listing] = {}
    for blob in json_blobs(page):
        for d in walk(blob):
            if d.get("@type") == "ListItem" and isinstance(d.get("item"), dict):
                d = {**d["item"], **({"url": d["url"]} if "url" in d and "url" not in d["item"] else {})}
            if not looks_like_listing(d):
                continue
            listing = _to_listing(d, platform, base_url, url_builder)
            if listing is None:
                continue
            if pattern and not pattern.search(listing.url):
                continue
            if require_price and listing.price is None:
                continue
            prev = found.get(listing.external_id)
            if prev is None or _richness(listing) > _richness(prev):
                found[listing.external_id] = listing
    return list(found.values())


def _richness(l: Listing) -> int:
    return sum(x is not None for x in (l.price, l.image, l.location, l.posted_at, l.seller.name))


def extract_description_from_page(page: str, platform: str) -> str | None:
    if platform == "kleinanzeigen":
        m = re.search(r'<p[^>]*id="viewad-description-text"[^>]*>(.*?)</p>', page, re.DOTALL)
        if m:
            text = m.group(1)
            text = re.sub(r'<br\s*/?>', '\n', text)
            text = re.sub(r'<[^>]+>', ' ', text)
            return html_lib.unescape(text).strip()

    if platform == "willhaben":
        m = re.search(
            r'(?:id="attribute-description-text-[^"]*"|data-testid="ad-description"[^>]*|class="[^"]*description[^"]*")[^>]*>(.*?)</(?:div|section|p)>',
            page,
            re.DOTALL | re.I,
        )
        if m:
            text = m.group(1)
            text = re.sub(r'<br\s*/?>', '\n', text)
            text = re.sub(r'<[^>]+>', ' ', text)
            clean = html_lib.unescape(text).strip()
            if len(clean) > 10:
                return clean

    if platform == "markt":
        m = re.search(
            r'(?:class="[^"]*description[^"]*"|id="description")[^>]*>(.*?)</(?:div|section|p)>',
            page,
            re.DOTALL | re.I,
        )
        if m:
            text = m.group(1)
            text = re.sub(r'<br\s*/?>', '\n', text)
            text = re.sub(r'<[^>]+>', ' ', text)
            clean = html_lib.unescape(text).strip()
            if len(clean) > 10:
                return clean

    best_desc = None
    for blob in json_blobs(page):
        for d in walk(blob):
            if isinstance(d, dict):
                for k in ("description", "descriptionText", "body"):
                    if k in d and isinstance(d[k], str):
                        val = d[k].strip()
                        if len(val) > 20:
                            if not best_desc or len(val) > len(best_desc):
                                best_desc = val
    if best_desc:
        best_desc = re.sub(r'<br\s*/?>', '\n', best_desc)
        best_desc = re.sub(r'<[^>]+>', ' ', best_desc)
        return html_lib.unescape(best_desc).strip()
    return None


def _to_listing(d: dict, platform: str, base_url: str, url_builder) -> Listing | None:
    raw_id = _first(d, ID_KEYS) or _id_from_url(d)
    title = str(_first(d, TITLE_KEYS)).strip()
    url = _first(d, URL_KEYS)
    if not isinstance(url, str):
        url = None
    ext_id = str(raw_id)
    if url and (ext_id.startswith("http") or not ext_id.strip()):
        m = re.findall(r"\d{5,}", url)
        ext_id = m[-1] if m else url
    if url_builder:
        url = url_builder(d, ext_id) or url
    if not url:
        return None
    if url.startswith("//"):
        url = "https:" + url
    elif url.startswith("/"):
        url = base_url.rstrip("/") + url
    price, currency = parse_price(_first(d, PRICE_KEYS))
    currency = currency or d.get("currency") or d.get("priceCurrency") or "CHF"
    return Listing(
        platform=platform,
        external_id=ext_id,
        title=html_lib.unescape(title)[:200],
        url=url,
        price=price,
        currency=str(currency),
        image=_image(_first(d, IMAGE_KEYS)),
        location=_location(d if any(k in d for k in ("postcode", "postalCode", "zipCode", "zip")) else _first(d, LOCATION_KEYS)),
        posted_at=parse_date(_first(d, DATE_KEYS)),
        seller=parse_seller(d),
    )
