import re
from datetime import datetime, timezone, timedelta
from urllib.parse import quote

from ..models import Listing, Seller
from .base import Adapter, AdapterError, fetch_html, BlockedError


class KleinanzeigenAdapter(Adapter):
    name = "kleinanzeigen"
    label = "Kleinanzeigen"
    base_url = "https://www.kleinanzeigen.de"

    def platform_cfg(self) -> dict:
        return self.cfg["platforms"].get(self.name, {})

    def search_url(self, query: str) -> str:
        # e.g., https://www.kleinanzeigen.de/s-macbook/k0 (spaces become hyphens)
        q = quote(query.strip().replace(" ", "-"))
        return self.platform_cfg()["url"].format(query=q)

    def search(self, query, source, min_price, max_price) -> list[Listing]:
        url = self.search_url(query)
        try:
            page = fetch_html(url, self.platform_cfg().get("fetcher", "http"))
        except BlockedError:
            raise

        listings = self.parse(page, source)

        if not listings and "srchrslt-adtable" not in page:
            raise AdapterError(
                "Seite geladen, aber keine Daten gefunden – Struktur geändert oder geblockt? "
                f"Mit «fundus probe {self.name} \"{query}\"» untersuchen."
            )

        return listings

    def parse(self, page: str, source: str) -> list[Listing]:
        listings = []

        # Kleinanzeigen search results are mostly inside <li> tags with data-clickable="card"
        lis = re.findall(r'<li[^>]*data-clickable="card"[^>]*>.*?</li>', page, re.DOTALL)

        for li in lis:
            # Ignore Top-Ads (they usually have 'bg-accentContainer')
            if 'bg-accentContainer' in li or 'is-topad' in li:
                continue

            m_id = re.search(r'data-adid="(\d+)"', li)
            if not m_id:
                continue
            ext_id = m_id.group(1)

            m_href = re.search(r'data-href="([^"]+)"', li)
            url = f"{self.base_url}{m_href.group(1)}" if m_href else None

            m_title = re.search(r'<h3[^>]*>.*?<a[^>]*>(.*?)</a>', li, re.DOTALL)
            title = m_title.group(1).strip() if m_title else "Ohne Titel"

            # Replace encoded characters if needed, but it's usually fine or handled by frontend
            title = title.replace("&quot;", '"').replace("&amp;", "&")

            m_price = re.search(r'text-title3 font-strong text-secondary.*?>(.*?)</p>', li, re.DOTALL)
            price = None
            if m_price:
                price_str = m_price.group(1).strip()
                if price_str.lower() != "vb" and price_str.lower() != "zu verschenken":
                    # Remove anything that isn't a digit or comma
                    p_clean = re.sub(r'[^0-9,]', '', price_str).replace(',', '.')
                    try:
                        if p_clean:
                            price = float(p_clean)
                    except ValueError:
                        pass

            m_img = re.search(r'<img[^>]*src="([^"]+)"', li)
            image = m_img.group(1) if m_img else None
            if image and "fallback" in image.lower(): # Just in case they use placeholders
                image = None

            m_loc = re.search(r'<svg viewBox="0 0 24 24"[^>]*>.*?<span>(.*?)</span>', li, re.DOTALL)
            location = m_loc.group(1).strip() if m_loc else None

            # Seller info: very minimal on the search page, maybe "PRO" or "Gewerblicher Nutzer"
            m_seller = re.search(r'Zum shop .*?><span>(.*?)</span>', li)
            seller_name = m_seller.group(1).strip() if m_seller else None

            is_business = 'PRO<' in li or 'Gewerblich' in li or bool(seller_name)

            seller = Seller(
                name=seller_name,
                is_business=is_business,
                # We have no rating or member_since on the search page for normal users
                rating_pct=None,
                rating_count=None,
                member_since=None
            )

            posted_at = datetime.now(timezone.utc)
            m_date = re.search(r'<span>((?:Heute|Gestern|\d{1,2}\.\d{1,2}\.\d{4}).*?)</span>', li, re.DOTALL)
            if m_date:
                date_str = m_date.group(1)
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
                        m_time = re.search(r'(\d{1,2})\.(\d{1,2})\.(\d{4})', date_str)
                        if m_time:
                            posted_at = datetime(year=int(m_time.group(3)), month=int(m_time.group(2)), day=int(m_time.group(1)), tzinfo=timezone.utc)
                except Exception:
                    pass

            listings.append(
                Listing(
                    platform=self.name,
                    external_id=ext_id,
                    title=title,
                    url=url or f"{self.base_url}/s-anzeige/{ext_id}",
                    price=price,
                    currency="EUR",
                    image=image,
                    location=location,
                    country="DE",
                    posted_at=posted_at,
                    source=source,
                    seller=seller,
                )
            )

        return listings
