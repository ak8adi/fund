"""Ricardo, Tutti und Anibis: eine Suchseite pro Abfrage, Daten aus dem eingebetteten JSON."""

from __future__ import annotations

from urllib.parse import quote

from ..models import Listing
from .base import Adapter, AdapterError, fetch_html
from .extract import extract_listings, json_blobs, walk


class ScrapedAdapter(Adapter):
    base_url = ""
    url_pattern: str | None = None
    require_price = False

    def platform_cfg(self) -> dict:
        return self.cfg["platforms"].get(self.name, {})

    def build_url(self, d: dict, ext_id: str) -> str | None:
        return None

    page_param: str | None = None   # URL-Parameter für weitere Ergebnisseiten, wo die Plattform ihn beachtet

    @property
    def paged(self) -> bool:
        return bool(self.page_param)

    def search_url(self, query: str, page: int = 1) -> str:
        url = self.platform_cfg()["url"].format(query=quote(query.strip()))
        if page > 1 and self.page_param:
            url += ("&" if "?" in url else "?") + f"{self.page_param}={page}"
        return url

    def parse(self, page: str) -> list[Listing]:
        return extract_listings(page, self.name, self.base_url, self.build_url, self.url_pattern, self.require_price)

    def search(self, query, source, min_price, max_price, page: int = 1) -> list[Listing]:
        page = fetch_html(self.search_url(query, page), self.platform_cfg().get("fetcher", "http"))
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

    complete = False   # hat die letzte Suche ALLE Treffer geliefert (nicht nur Seite 1)?

    def build_url(self, d: dict, ext_id: str) -> str | None:
        url = d.get("url") or d.get("href")
        if isinstance(url, str) and "/a/" in url:
            return url
        return f"{self.base_url}/de/a/{ext_id}/" if ext_id.isdigit() else None

    def parse(self, page: str) -> list[Listing]:
        self.complete = False
        for blob in json_blobs(page):
            for d in walk(blob):
                if isinstance(d, dict) and isinstance(d.get("articles"), list) and isinstance(d.get("totalArticlesCount"), int):
                    # Ganze Ergebnisliste auf einer Seite: was hier fehlt, ist verkauft oder beendet (siehe runner)
                    self.complete = d["totalArticlesCount"] <= len(d["articles"])
                    break
        return super().parse(page)


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
