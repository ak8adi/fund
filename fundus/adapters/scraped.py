"""Ricardo, Tutti und Anibis: eine Suchseite pro Abfrage, Daten aus dem eingebetteten JSON."""

from __future__ import annotations

from urllib.parse import quote

from ..models import Listing
from .base import Adapter, AdapterError, fetch_html
from .extract import extract_listings, json_blobs


class ScrapedAdapter(Adapter):
    base_url = ""
    url_pattern: str | None = None
    require_price = False

    def platform_cfg(self) -> dict:
        return self.cfg["platforms"].get(self.name, {})

    def build_url(self, d: dict, ext_id: str) -> str | None:
        return None

    def search_url(self, query: str) -> str:
        return self.platform_cfg()["url"].format(query=quote(query.strip()))

    def parse(self, page: str) -> list[Listing]:
        return extract_listings(page, self.name, self.base_url, self.build_url, self.url_pattern, self.require_price)

    def search(self, query, source, min_price, max_price) -> list[Listing]:
        page = fetch_html(self.search_url(query), self.platform_cfg().get("fetcher", "http"))
        listings = self.parse(page)
        if not listings and next(json_blobs(page), None) is None:
            raise AdapterError(
                "Seite geladen, aber keine Daten gefunden – Struktur geändert? "
                f"Mit «fundus probe {self.name} \"{query}\"» untersuchen."
            )
        return listings


class RicardoAdapter(ScrapedAdapter):
    name = "ricardo"
    label = "Ricardo"
    base_url = "https://www.ricardo.ch"
    require_price = True

    def build_url(self, d: dict, ext_id: str) -> str | None:
        url = d.get("url") or d.get("href")
        if isinstance(url, str) and "/a/" in url:
            return url
        return f"{self.base_url}/de/a/{ext_id}/" if ext_id.isdigit() else None


class _SmgAdapter(ScrapedAdapter):
    """Tutti und Anibis laufen beide bei der SMG Swiss Marketplace Group, mit ähnlicher Datenstruktur."""

    url_pattern = r"/vi/"

    def build_url(self, d: dict, ext_id: str) -> str | None:
        url = d.get("url") or d.get("href") or d.get("link")
        if isinstance(url, str) and "/vi/" in url:
            return url
        seo = d.get("seoInformation") or {}
        slug = seo.get("deSlug") or seo.get("slug") if isinstance(seo, dict) else None
        if slug:
            return f"{self.base_url}/de/vi/{slug.strip('/')}/{ext_id}"
        return f"{self.base_url}/de/vi/{ext_id}" if ext_id.isdigit() and "listingID" in d else None


class TuttiAdapter(_SmgAdapter):
    name = "tutti"
    label = "Tutti"
    base_url = "https://www.tutti.ch"


class AnibisAdapter(_SmgAdapter):
    name = "anibis"
    label = "Anibis"
    base_url = "https://www.anibis.ch"
