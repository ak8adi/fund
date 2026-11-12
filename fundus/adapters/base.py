"""Basis für Plattform-Adapter: gemeinsamer HTTP-Client, Sperr-Erkennung, Browser-Fallback."""

from __future__ import annotations

import random
import threading
import time
from abc import ABC, abstractmethod
from email.utils import parsedate_to_datetime
from urllib.parse import urlsplit

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
# Plattformen, die gelöschte Inserate nicht mit 404 melden, sondern auf eine Übersicht umleiten
GONE_MARKERS = ("srchrslt-adexpired",     # Kleinanzeigen (Desktop): «Die gewünschte Anzeige ist nicht mehr verfügbar.»
                "Die gewünschte Anzeige ist nicht mehr verfügbar")  # Kleinanzeigen (mobil): Umleitung auf die Kategorie


class AdapterError(Exception):
    """Plattform hat nicht wie erwartet geantwortet."""


class BlockedError(AdapterError):
    """Plattform blockt die Anfrage (Captcha, 403, 429). retry_after: Wartezeit in s laut Server, falls genannt."""

    def __init__(self, message: str, retry_after: int | None = None):
        super().__init__(message)
        self.retry_after = retry_after


# Mindestabstand zwischen zwei Anfragen an denselben Host, über alle Aufrufer (Prüflauf, KI, Weboberfläche).
# Ricardo (Cloudflare-Ratenlimit) sperrt mit 429, wenn Anfragen dicht aufeinander folgen.
HOST_MIN_GAP = {"www.ricardo.ch": 60.0}
_last_request: dict[str, float] = {}
_gap_lock = threading.Lock()


def _wait_for_host(url: str) -> None:
    host = urlsplit(url).hostname or ""
    gap = HOST_MIN_GAP.get(host)
    if not gap:
        return
    with _gap_lock:  # Reihenfolge einhalten, auch wenn mehrere Threads gleichzeitig abrufen
        wait = _last_request.get(host, 0.0) + gap - time.monotonic()
        if wait > 0:
            time.sleep(wait)
        _last_request[host] = time.monotonic()


def _retry_after(value: str | None) -> int | None:
    """Retry-After: Sekunden oder HTTP-Datum."""
    if not value:
        return None
    if value.strip().isdigit():
        return int(value.strip())
    try:
        return max(0, int(parsedate_to_datetime(value).timestamp() - time.time()))
    except (TypeError, ValueError):
        return None


class GoneError(AdapterError):
    """Seite existiert nicht mehr (404, 410) – bei einer Inseratseite: verkauft oder gelöscht."""


class Adapter(ABC):
    name: str = ""
    label: str = ""

    def __init__(self, cfg: dict):
        self.cfg = cfg

    paged = False   # kann search(…, page=n) weitere Ergebnisseiten liefern? (runner: erster Abruf, volle Seiten)

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
        proxy=proxy(),
    )


TRANSIENT = (500, 502, 504)
RETRY_WAIT = 8.0   # Sekunden vor dem einen Neuversuch


def proxy() -> str | None:
    """network.proxy aus config.yaml: ein fester, eigener Ausgang für fundus (kein Wechsel, keine Tarnung) – damit eine
    Sperre gegen fundus nicht die Heim-IP trifft, mit der man selbst surft. None = direkt."""
    from ..config import get_config
    return (get_config().get("network") or {}).get("proxy") or None


def _get(url: str, impersonate: str, headers: dict | None):
    if CURL_AVAILABLE:
        return curl_requests.get(url, impersonate=impersonate, timeout=25, headers=headers, proxy=proxy())
    with http_client() as client:
        if headers:
            client.headers.update(headers)
        return client.get(url)


def fetch_html(url: str, fetcher: str = "http", headers: dict | None = None, impersonate: str = "chrome") -> str:
    """Eine einzige Seitenabfrage. fetcher='browser' nutzt Playwright (optional installiert).
    impersonate: Browser-Fingerabdruck von curl_cffi, z.B. «chrome_android» für die mobile Seite."""
    if fetcher == "browser":
        return _fetch_with_browser(url)
    # Vorübergehende Störung (502/500/504, Zeitüberschreitung, Verbindung): genau ein Neuversuch nach kurzer Pause.
    # Sperren (403/429/503) nie wiederholen – dort hilft nur Abstand.
    for attempt in (1, 2):
        _wait_for_host(url)
        try:
            resp = _get(url, impersonate, headers)
        except Exception as e:
            if attempt == 1:
                time.sleep(RETRY_WAIT)
                continue
            raise AdapterError(f"HTTP Fehler für {url}: {e}")
        if resp.status_code in TRANSIENT and attempt == 1:
            time.sleep(RETRY_WAIT)
            continue
        break

    if resp.status_code in (403, 429, 503):
        kind = "Bot-Schutz (Cloudflare-Challenge)" if resp.headers.get("cf-mitigated") else (
            "zu viele Anfragen" if resp.status_code == 429 else "Plattform blockt vermutlich automatisierte Anfragen")
        raise BlockedError(f"HTTP {resp.status_code} – {kind}", _retry_after(resp.headers.get("retry-after")))
    if resp.status_code in (404, 410):
        raise GoneError(f"HTTP {resp.status_code} für {url}")
    if resp.status_code >= 400:
        raise AdapterError(f"HTTP {resp.status_code} für {url}")
    text = resp.text
    _check_blocked(text)
    _check_gone(text, url)
    return text


def _check_gone(html: str, url: str) -> None:
    if any(m in html for m in GONE_MARKERS):
        raise GoneError(f"Inserat nicht mehr verfügbar (umgeleitet): {url}")


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
        browser = p.chromium.launch(headless=True, proxy={"server": proxy()} if proxy() else None)
        try:
            page = browser.new_page(locale="de-CH", user_agent=random.choice(USER_AGENTS))
            page.goto(url, wait_until="domcontentloaded", timeout=45000)
            page.wait_for_timeout(2500)
            html = page.content()
        finally:
            browser.close()
    _check_blocked(html)
    return html
