"""Langzeit-Archiv: alles, was eine Suche je gefunden hat, geordnet neben der Datenbank – für spätere Auswertungen.

data/archiv/<nr>-<suche>/
    suche.json              Einstellungen der Suche (letzter Stand)
    verlauf.csv             eine Zeile pro Lauf: verfügbar, neu, weg, günstigster, Median, Neupreis …
    JJJJ-MM-TT.jsonl.gz     Tagesstand: jeder Treffer der Suche mit allen Inseratdaten, Beschreibung, KI-Bewertung,
                            Score, Endpreis, Status (auch ausgefilterte, verkaufte) und Preisverlauf – eine JSON-Zeile
                            pro Treffer. Mehrere Läufe am selben Tag: die Datei zeigt den letzten Stand des Tages.

Die Datenbank überschreibt Titel, Bewertung und Status mit dem neusten Stand; das Archiv hält jeden Tag fest.
Gelöschte Suchen behalten ihren Ordner (vor dem Löschen wird ein letzter Tagesstand geschrieben).
"""

from __future__ import annotations

import csv
import gzip
import json
import logging
import re
import statistics
from datetime import datetime, timezone
from pathlib import Path

from . import db
from .config import get_config

log = logging.getLogger(__name__)
LISTING_FIELDS = ("platform", "external_id", "source", "title", "url", "price", "first_price", "currency", "image",
                  "location", "country", "posted_at", "seller", "trust_level", "trust_reasons", "description_full",
                  "seller_chat", "first_seen", "last_seen", "checked_at", "gone_at", "reserved", "ship_flag",
                  "auction", "bids", "bid_price", "ends_at")
HIT_FIELDS = ("hit_seen", "is_new", "baseline", "hidden")
VERLAUF = ("zeit", "verfügbar_passend", "passend_gesamt", "neu_heute", "weg_heute", "günstigster_chf", "median_chf",
           "marktpreis_chf", "neupreis_chf", "neu_ab_chf", "erneuert_ab_chf")


def folder(search: dict) -> Path:
    slug = re.sub(r"[^a-z0-9]+", "-", search["query"].lower()).strip("-")[:40] or "suche"
    path = Path(get_config()["data_dir"]) / "archiv" / f"{search['id']:03d}-{slug}"
    path.mkdir(parents=True, exist_ok=True)
    return path


def _price_history(keys: list[str]) -> dict[str, list]:
    out: dict[str, list] = {}
    with db.connect() as c:
        for i in range(0, len(keys), 500):
            chunk = keys[i:i + 500]
            for r in c.execute(f"""SELECT listing_key, price, currency, seen_at FROM price_history
                                   WHERE listing_key IN ({",".join("?" * len(chunk))}) ORDER BY seen_at""", chunk):
                out.setdefault(r["listing_key"], []).append([r["seen_at"], r["price"], r["currency"]])
    return out


def snapshot(search_id: int) -> Path | None:
    """Tagesstand und Verlaufszeile einer Suche schreiben. Fehler hier dürfen nie einen Lauf stoppen."""
    try:
        return _snapshot(search_id)
    except Exception as exc:
        log.warning("Archiv für Suche %s nicht geschrieben: %s", search_id, exc)
        return None


def _snapshot(search_id: int) -> Path | None:
    from . import views
    search = db.get_search(search_id)
    if not search:
        return None
    base = folder(search)
    now = datetime.now(timezone.utc)
    (base / "suche.json").write_text(json.dumps(search, ensure_ascii=False, indent=2, default=str), encoding="utf-8")

    hits = db.hits_for_search(search_id, include_hidden=True, limit=100000)
    history = _price_history([h["key"] for h in hits])
    day_file = base / f"{now.date().isoformat()}.jsonl.gz"
    tmp = day_file.with_suffix(".tmp")
    with gzip.open(tmp, "wt", encoding="utf-8") as f:
        for h in hits:
            row = {"key": h["key"], "archiviert": now.isoformat(timespec="seconds")}
            row.update({k: h.get(k) for k in LISTING_FIELDS + HIT_FIELDS})
            row.update({k: v for k, v in h.items() if k.startswith("ai_") or k in (
                "score_reason", "total_chf", "delivery", "single_price", "single_estimated", "img_note")})
            row["preisverlauf"] = history.get(h["key"], [])
            f.write(json.dumps(row, ensure_ascii=False, default=str) + "\n")
    tmp.replace(day_file)  # erst fertig geschrieben ersetzen – nie eine halbe Datei

    v = views.build(search_id)
    pays = [o["pay"] for o in v["offers"] if not o["ko"]]
    today = now.date().isoformat()
    head = v["head"]
    line = {
        "zeit": now.isoformat(timespec="seconds"),
        "verfügbar_passend": v["counts"]["open"],
        "passend_gesamt": v["counts"]["all"],
        "neu_heute": next((b["new"] for b in v["timeline"] if b["day"] == today), 0) if v["timeline"] and v["timeline"][0]["days"] == 1 else "",
        "weg_heute": next((b["gone"] for b in v["timeline"] if b["day"] == today), 0) if v["timeline"] and v["timeline"][0]["days"] == 1 else "",
        "günstigster_chf": min(pays) if pays else "",
        "median_chf": round(statistics.median(pays)) if pays else "",
        "marktpreis_chf": round(search["market_price"]) if search.get("market_price") else "",
        "neupreis_chf": round(search["new_price"]) if search.get("new_price") else "",
        "neu_ab_chf": head["new"]["total"] if head.get("new") else "",
        "erneuert_ab_chf": head["refurb"]["total"] if head.get("refurb") else "",
    }
    verlauf = base / "verlauf.csv"
    new_file = not verlauf.exists()
    with verlauf.open("a", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=VERLAUF, delimiter=";")
        if new_file:
            w.writeheader()
        w.writerow(line)
    return day_file
