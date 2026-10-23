"""Basis für Plattform-Adapter: gemeinsamer HTTP-Client, Sperr-Erkennung, Browser-Fallback."""

from __future__ import annotations

import random
from abc import ABC, abstractmethod

import httpx
try:
    from curl_cffi import requests as curl_requests
    CURL_AVAILABLE = True
except ImportError:
    CURL_AVAILABLE = False

from ..models import Listing

USER_AGENTS = [
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/18.5 Safari/605.1.15",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/139.0.0.0 Safari/537.36",
]

BLOCK_MARKERS = ("cf-challenge", "challenge-platform", "captcha", "Access Denied", "px-captcha", "datadome")


class AdapterError(Exception):
    """Plattform hat nicht wie erwartet geantwortet."""


class BlockedError(AdapterError):
    """Plattform blockt die Anfrage (Captcha, 403, 429)."""


class Adapter(ABC):
    name: str = ""
    label: str = ""

    def __init__(self, cfg: dict):
        self.cfg = cfg

    def sources(self) -> list[str]:
        """Einzeln abschaltbare Quellen dieses Adapters (z.B. eBay-Länder)."""
        return [self.name]

    def source_label(self, source: str) -> str:
        return self.label

    def configured(self) -> tuple[bool, str]:
        """(bereit, Hinweis) – z.B. fehlende API-Schlüssel."""
        return True, ""

    @abstractmethod
    def search(self, query: str, source: str, min_price: float | None, max_price: float | None) -> list[Listing]:
        ...


def http_client() -> httpx.Client:
    return httpx.Client(
        timeout=httpx.Timeout(25.0),
        follow_redirects=True,
        headers={
            "User-Agent": random.choice(USER_AGENTS),
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "de-CH,de;q=0.9,en;q=0.6",
        },
        http2=False,
    )


def fetch_html(url: str, fetcher: str = "http", headers: dict | None = None) -> str:
    """Eine einzige Seitenabfrage. fetcher='browser' nutzt Playwright (optional installiert)."""
    if fetcher == "browser":
        return _fetch_with_browser(url)

    if CURL_AVAILABLE:
        try:
            resp = curl_requests.get(
                url,
                impersonate="chrome",
                timeout=25,
                headers=headers
            )
        except Exception as e:
            raise AdapterError(f"HTTP Fehler für {url}: {e}")
    else:
        with http_client() as client:
            if headers:
                client.headers.update(headers)
            resp = client.get(url)

    if resp.status_code in (403, 429, 503):
        raise BlockedError(f"HTTP {resp.status_code} – Plattform blockt vermutlich automatisierte Anfragen")
    if resp.status_code >= 400:
        raise AdapterError(f"HTTP {resp.status_code} für {url}")
    text = resp.text
    _check_blocked(text)
    return text


def _check_blocked(html: str) -> None:
    head = html[:20000]
    if len(html) < 30000 and any(m.lower() in head.lower() for m in BLOCK_MARKERS):
        raise BlockedError("Captcha/Bot-Schutz erkannt – in config.yaml fetcher: browser versuchen")


def _fetch_with_browser(url: str) -> str:
    try:
        from playwright.sync_api import sync_playwright
    except ImportError as exc:  # pragma: no cover
        raise AdapterError("fetcher: browser braucht Playwright: pip install 'fundus[browser]' && playwright install chromium") from exc
    with sync_playwright() as p:  # pragma: no cover - braucht echten Browser
        browser = p.chromium.launch(headless=True)
        try:
            page = browser.new_page(locale="de-CH", user_agent=random.choice(USER_AGENTS))
            page.goto(url, wait_until="domcontentloaded", timeout=45000)
            page.wait_for_timeout(2500)
            html = page.content()
        finally:
            browser.close()
    _check_blocked(html)
    return html
