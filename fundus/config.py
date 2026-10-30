"""Konfiguration: config.yaml + Umgebungsvariablen (FUNDUS_<ABSCHNITT>_<SCHLÜSSEL>)."""

from __future__ import annotations

import copy
import os
from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml

DEFAULTS: dict[str, Any] = {
    "data_dir": "./data",
    "web": {"host": "127.0.0.1", "port": 8100, "public_url": ""},
    "schedule": {"times": ["07:40", "18:20"], "jitter_minutes": 25, "timezone": "Europe/Zurich"},
    "request_delay": [4, 11],
    "ntfy": {
        "server": "https://ntfy.sh", "topic": "", "token": "", "max_per_search": 5,
        "on_new": True,                                  # Meldung bei jedem neuen Angebot
        "weekly": True, "weekly_day": "mon", "weekly_time": "08:00",  # Wochenübersicht
    },
    "ebay": {
        "app_id": "",
        "cert_id": "",
        "marketplaces": ["EBAY_CH", "EBAY_DE", "EBAY_FR", "EBAY_IT", "EBAY_AT"],
        "used_only": True,
        "home_country_only": True,
    },
    "ai": {
        "gemini_api_key": "",
    },
    # Grobe Umrechnung für Preisfilter und Preisvergleich – gelegentlich anpassen
    "fx_to_chf": {"CHF": 1.0, "EUR": 0.94, "GBP": 1.08, "SEK": 0.082},
    "platforms": {
        "ricardo": {"url": "https://www.ricardo.ch/de/s/{query}/?sort=newest", "fetcher": "http"},
        "tutti": {"url": "https://www.tutti.ch/de/q/suche?query={query}&sorting=newest", "fetcher": "http"},
        "anibis": {"url": "https://www.anibis.ch/de/q/suche?query={query}&sorting=newest", "fetcher": "http"},
        "kleinanzeigen": {"url": "https://www.kleinanzeigen.de/s-{query}/k0", "fetcher": "http"},
        "willhaben": {"url": "https://www.willhaben.at/iad/kaufen-und-verkaufen/marktplatz?keyword={query}", "fetcher": "http"},
        "markt": {"url": "https://www.markt.de/suche/{query}/", "fetcher": "http"},
        "blocket": {"url": "https://www.blocket.se/annonser/hela_sverige?q={query}", "fetcher": "http"},
    },
}


def _merge(base: dict, override: dict) -> dict:
    out = copy.deepcopy(base)
    for key, value in (override or {}).items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = _merge(out[key], value)
        else:
            out[key] = value
    return out


def _apply_env(cfg: dict) -> dict:
    """FUNDUS_NTFY_TOPIC=xyz setzt cfg['ntfy']['topic']; Listen kommagetrennt."""
    for name, raw in os.environ.items():
        if not name.startswith("FUNDUS_") or name == "FUNDUS_CONFIG":
            continue
        parts = name[len("FUNDUS_"):].lower().split("_", 1)
        if parts[0] in cfg and isinstance(cfg[parts[0]], dict) and len(parts) == 2:
            target, key = cfg[parts[0]], parts[1]
        else:
            target, key = cfg, name[len("FUNDUS_"):].lower()
        current = target.get(key)
        if isinstance(current, list):
            target[key] = [v.strip() for v in raw.split(",") if v.strip()]
        elif isinstance(current, int) and not isinstance(current, bool):
            target[key] = int(raw)
        else:
            target[key] = raw
    return cfg


@lru_cache(maxsize=1)
def get_config() -> dict[str, Any]:
    path = Path(os.environ.get("FUNDUS_CONFIG", "config.yaml"))
    user: dict = {}
    if path.exists():
        user = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    cfg = _apply_env(_merge(DEFAULTS, user))
    Path(cfg["data_dir"]).mkdir(parents=True, exist_ok=True)
    return cfg


def save_settings(section: str, values: dict[str, Any]) -> None:
    """Werte in config.yaml schreiben und in der laufenden Konfiguration übernehmen (ohne Neustart).
    Hinweis: Kommentare in config.yaml gehen dabei verloren."""
    cfg = get_config()
    path = Path(os.environ.get("FUNDUS_CONFIG", "config.yaml"))
    user: dict = {}
    if path.exists():
        user = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    user.setdefault(section, {}).update(values)
    path.write_text(yaml.safe_dump(user, allow_unicode=True, sort_keys=False), encoding="utf-8")
    cfg[section].update(values)
