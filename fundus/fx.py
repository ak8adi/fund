"""Wechselkurse in CHF: tagesaktuell (stündlich gecacht), sonst die Werte aus der Konfiguration.

Eine Quelle für alles – Preisfilter, Ranking und Anzeige rechnen mit denselben Kursen.
"""

from __future__ import annotations

import logging
import threading
import time

import httpx

log = logging.getLogger(__name__)

URL = "https://open.er-api.com/v6/latest/CHF"
MAX_AGE = 3600
_cache: dict = {"ts": 0.0, "rates": {}}
_lock = threading.Lock()


def rates(cfg: dict) -> dict:
    """{Währung: CHF pro Einheit}. Live-Kurse überschreiben die Konfiguration, wenn erreichbar."""
    with _lock:
        if time.time() - _cache["ts"] > MAX_AGE:
            _cache["ts"] = time.time()   # auch bei Fehler erst in einer Stunde wieder versuchen
            try:
                r = httpx.get(URL, timeout=3.5)
                r.raise_for_status()
                _cache["rates"] = {k.upper(): 1.0 / float(v) for k, v in r.json().get("rates", {}).items() if v}
            except Exception as e:
                log.info("Live-Wechselkurse nicht erreichbar, nutze Konfiguration: %s", e)
        live = dict(_cache["rates"])
    return {**cfg.get("fx_to_chf", {}), **live, "CHF": 1.0}
