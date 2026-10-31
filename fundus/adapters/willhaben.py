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

        # Check attributes/attribute for SEO_URL
        attrs = d.get("attributes", {}).get("attribute", [])
        if isinstance(attrs, list):
            for attr in attrs:
                if isinstance(attr, dict) and attr.get("name") == "SEO_URL":
                    vals = attr.get("values")
                    if isinstance(vals, list) and vals:
                        url = vals[0]
                    elif "value" in attr:
                        url = attr["value"]

        if isinstance(url, str):
            if url.startswith("http"):
                return url
            if not url.startswith("/"):
                url = "/" + url
            if not url.startswith("/iad/"):  # SEO_URL kommt ohne /iad – ohne das Präfix antwortet willhaben mit 404
                url = "/iad" + url
            return f"{self.base_url}{url}"
        return None

    def parse(self, page: str, source: str = "willhaben") -> list[Listing]:
        listings = []
        seen_ids = set()

        import json
        import re
        from datetime import datetime, timezone
        from ..models import Seller

        m = re.search(r'<script id="__NEXT_DATA__" type="application/json">(.+?)</script>', page, re.S)
        if m:
            try:
                data = json.loads(m.group(1))

                # Check for query text from URL to validate relevance (avoid recommendation filler)

                query_text = ""
                # Optional: try to grab from JSON directly if available
                if "query" in data and isinstance(data["query"], dict) and "keyword" in data["query"]:
                    query_text = data["query"]["keyword"].lower()
                elif "props" in data and isinstance(data["props"], dict) and "pageProps" in data["props"] and isinstance(data["props"]["pageProps"], dict) and "searchResult" in data["props"]["pageProps"] and isinstance(data["props"]["pageProps"]["searchResult"], dict) and "query" in data["props"]["pageProps"]["searchResult"] and isinstance(data["props"]["pageProps"]["searchResult"]["query"], dict) and "keyword" in data["props"]["pageProps"]["searchResult"]["query"]:
                    query_text = data["props"]["pageProps"]["searchResult"]["query"]["keyword"].lower()


                # Try to extract advertSummaries from multiple possible locations inside JSON
                from .extract import walk
                advert_summaries = []
                for d in walk(data):
                    if isinstance(d, dict) and "advertSummaryList" in d and isinstance(d["advertSummaryList"], dict) and "advertSummary" in d["advertSummaryList"]:
                        advert_summaries.extend(d["advertSummaryList"]["advertSummary"])
                    elif isinstance(d, dict) and "advertSummary" in d and isinstance(d["advertSummary"], list):
                        advert_summaries.extend(d["advertSummary"])

                for d in advert_summaries:
                    if not isinstance(d, dict): continue

                    if d.get("upsell") or d.get("isPromoted") or d.get("isTopAd") or d.get("advertStatus") in ("PROMOTED", "TOPAD"):
                        continue

                    ext_id = str(d.get("id", ""))
                    if not ext_id or ext_id in seen_ids: continue


                    title = d.get("description", "Ohne Titel")
                    attrs = d.get("attributes", {}).get("attribute", [])
                    if isinstance(attrs, list):
                        for attr in attrs:
                            if attr.get("name") == "HEADING":
                                vals = attr.get("values")
                                if isinstance(vals, list) and vals:
                                    title = vals[0]
                                    break



                    if query_text:
                        query_words = query_text.split()
                        if not any(w in title.lower() for w in query_words):
                            continue # Skip fillers not matching query


                    seen_ids.add(ext_id)

                    url = self.build_url(d, ext_id)
                    if not url:
                        url = f"{self.base_url}/iad/kaufen-und-verkaufen/d/{ext_id}"

                    price = None
                    if "price" in d:
                        try: price = float(d["price"])
                        except: pass
                    else:
                        attrs = d.get("attributes", {}).get("attribute", [])
                        if isinstance(attrs, list):
                            for attr in attrs:
                                if attr.get("name") == "PRICE/AMOUNT" or attr.get("name") == "PRICE/MAIN" or attr.get("name") == "PRICE":
                                    vals = attr.get("values")
                                    if isinstance(vals, list) and vals:
                                        try: price = float(vals[0])
                                        except: pass
                                    elif "value" in attr:
                                        try: price = float(attr["value"])
                                        except: pass
                                    if price is not None:
                                        break

                    image = None
                    if "image" in d:
                        image = d["image"]
                    else:
                        img_list = d.get("advertImageList", {}).get("advertImage", [])
                        if isinstance(img_list, list) and img_list:
                            img_node = img_list[0]
                            if isinstance(img_node, dict):
                                image = img_node.get("mainImageUrl") or img_node.get("referenceImageUrl")

                    location = None
                    if "postcode" in d and "city" in d:
                        location = f'{d["postcode"]} {d["city"]}'
                    else:
                        attrs = d.get("attributes", {}).get("attribute", [])
                        loc_post = ""
                        loc_city = ""
                        if isinstance(attrs, list):
                            for attr in attrs:
                                if attr.get("name") == "POSTCODE":
                                    vals = attr.get("values")
                                    if isinstance(vals, list) and vals: loc_post = vals[0]
                                elif attr.get("name") == "LOCATION":
                                    vals = attr.get("values")
                                    if isinstance(vals, list) and vals: loc_city = vals[0]
                        if loc_post or loc_city:
                            location = f"{loc_post} {loc_city}".strip()
                        else:
                            context_links = d.get("contextLinkList", {}).get("contextLink", [])
                            if isinstance(context_links, list):
                                for cl in context_links:
                                    if cl.get("id") == "Zips":
                                        location = cl.get("description")
                                    elif cl.get("id") == "Districts" and not location:
                                        location = cl.get("description")

                    listings.append(Listing(
                        platform=self.name,
                        external_id=ext_id,
                        title=title,
                        url=url,
                        price=price,
                        currency="EUR",
                        image=image,
                        location=location,
                        country="AT",
                        posted_at=datetime.now(timezone.utc),
                        source=source,
                        seller=Seller()
                    ))
            except Exception as e:
                pass

        if not listings:
            # Fallback for old behaviour if NEXT_DATA fails or is not present
            return self._parse_html(page, source)

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
