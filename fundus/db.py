"""SQLite-Speicher. Eine Datei, eine Verbindung pro Vorgang (thread-sicher genug für einen Nutzer)."""

from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from dataclasses import asdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Iterator

from .config import get_config
from .models import Listing

SCHEMA = """
CREATE TABLE IF NOT EXISTS searches (
    id            INTEGER PRIMARY KEY,
    query         TEXT NOT NULL,
    exclude       TEXT NOT NULL DEFAULT '',     -- kommagetrennte Wörter, die nicht im Titel stehen dürfen
    min_price     REAL,
    max_price     REAL,
    platforms     TEXT,                         -- JSON-Liste oder NULL = alle aktiven
    active        INTEGER NOT NULL DEFAULT 1,
    baseline_done INTEGER NOT NULL DEFAULT 0,   -- erster Lauf meldet nichts, merkt sich nur den Bestand
    created_at    TEXT NOT NULL,
    last_run_at   TEXT,
    notify_new    INTEGER NOT NULL DEFAULT 1,   -- Meldung bei neuem Angebot
    notify_weekly INTEGER NOT NULL DEFAULT 1,   -- in der Wochenübersicht aufführen
    allow_sets    INTEGER NOT NULL DEFAULT 1,
    market_price  REAL                          -- Median CHF der relevanten Treffer (scoring.py)
);
CREATE TABLE IF NOT EXISTS listings (
    key           TEXT PRIMARY KEY,
    platform      TEXT NOT NULL,
    external_id   TEXT NOT NULL,
    title         TEXT NOT NULL,
    url           TEXT NOT NULL,
    price         REAL,
    first_price   REAL,
    currency      TEXT,
    image         TEXT,
    location      TEXT,
    country       TEXT,
    posted_at     TEXT,
    source        TEXT,
    seller        TEXT,                         -- JSON
    trust_level   TEXT,
    trust_reasons TEXT,                         -- JSON-Liste
    description_full TEXT,
    first_seen    TEXT NOT NULL,
    last_seen     TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS hits (
    search_id     INTEGER NOT NULL REFERENCES searches(id) ON DELETE CASCADE,
    listing_key   TEXT NOT NULL REFERENCES listings(key),
    first_seen    TEXT NOT NULL,
    is_new        INTEGER NOT NULL DEFAULT 1,   -- noch nicht angeschaut
    notified      INTEGER NOT NULL DEFAULT 0,
    hidden        INTEGER NOT NULL DEFAULT 0,
    -- KI-Merkmale pro Suche: dasselbe Inserat kann für eine Suche passen, für eine andere nicht
    ai_relevant   INTEGER,                      -- 1/0, NULL = noch nicht bewertet
    ai_reason     TEXT,
    ai_condition  TEXT,                         -- neu, neuwertig, sehr gut, gut, gebraucht, defekt, unbekannt
    ai_shipping   TEXT,                         -- ja, nein, unbekannt
    single_price  REAL,                         -- Einzelpreis aus Set-Inserat (Währung des Inserats)
    ai_year       INTEGER,                      -- Modell-/Kaufjahr laut Inserat
    ai_shipping_cost REAL,                      -- Versandkosten laut Inserat (Währung des Inserats)
    -- vom Programm berechnet (scoring.py)
    ai_score      INTEGER,                      -- Deal-Score 0–100
    ai_tier       TEXT,                         -- S, A, B, C
    score_reason  TEXT,
    total_chf     REAL,                         -- Endpreis inkl. Versand und Gebühren (delivery.py)
    PRIMARY KEY (search_id, listing_key)
);
CREATE TABLE IF NOT EXISTS platforms (
    name          TEXT PRIMARY KEY,             -- z.B. "ricardo" oder "ebay:EBAY_DE"
    enabled       INTEGER NOT NULL DEFAULT 1,
    last_run      TEXT,
    last_count    INTEGER,
    last_error    TEXT
);
CREATE TABLE IF NOT EXISTS runs (
    id            INTEGER PRIMARY KEY,
    trigger       TEXT NOT NULL,
    started_at    TEXT NOT NULL,
    finished_at   TEXT,
    new_hits      INTEGER,
    errors        INTEGER
);
"""


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def db_path() -> Path:
    return Path(get_config()["data_dir"]) / "fundus.db"


_writes = 0   # zählt Schreibvorgänge dieses Prozesses – Zwischenspeicher (views.build) gelten nur für einen Stand


def version() -> tuple[str, int]:
    """Kennung des Datenstands: ändert sich mit jedem Schreibvorgang (und mit der Datenbankdatei, z.B. in Tests)."""
    return str(db_path()), _writes


@contextmanager
def connect() -> Iterator[sqlite3.Connection]:
    global _writes
    conn = sqlite3.connect(db_path(), timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    try:
        yield conn
        conn.commit()
        if conn.total_changes:
            _writes += 1
    finally:
        conn.close()


def _ensure_chat_cols(conn):
    """Spalten aus späteren Versionen – einmal beim Start (init_db), nicht bei jeder Verbindung."""
    s_cols = {r["name"] for r in conn.execute("PRAGMA table_info(searches)")}
    if "require_shipping" not in s_cols:
        conn.execute("ALTER TABLE searches ADD COLUMN require_shipping INTEGER DEFAULT 0")
    l_cols = {r["name"] for r in conn.execute("PRAGMA table_info(listings)")}
    for col in ("seller_chat", "draft_question"):
        if col not in l_cols:
            conn.execute(f"ALTER TABLE listings ADD COLUMN {col} TEXT")

HIT_AI_SELECT = ", ".join(f"h.{col}" for col in (
    "ai_relevant", "ai_reason", "ai_condition", "ai_shipping", "single_price", "ai_year", "ai_shipping_cost", "ai_score",
    "ai_tier", "score_reason", "total_chf", "ai_req", "ai_req_note", "ai_set_items", "single_estimated", "ai_size", "ai_weight", "delivery", "img_note", "img_done"))
HIT_AI_COLS = {"ai_relevant": "INTEGER", "ai_reason": "TEXT", "ai_condition": "TEXT", "ai_shipping": "TEXT",
               "single_price": "REAL", "ai_year": "INTEGER", "ai_shipping_cost": "REAL", "total_chf": "REAL", "ai_score": "INTEGER", "ai_tier": "TEXT", "score_reason": "TEXT",
               "ai_req": "TEXT", "ai_req_note": "TEXT",
               "ai_set_items": "TEXT", "single_estimated": "INTEGER",   # Set-Inhalt; single_price von der KI geschätzt
               "ai_size": "TEXT", "ai_weight": "REAL",                # Versandgrösse (delivery.SIZES) und kg
               "delivery": "TEXT",
               "img_note": "TEXT", "img_done": "TEXT"}                # Bildanalyse: Befund; mit welchem Auftrag geprüft                                    # beste Lieferart, kurz (delivery.plan)  # ai_req: Freitext-Anforderungen erfüllt? ja/nein/unklar


def _migrate_ai_to_hits(c: sqlite3.Connection, l_cols: set[str]) -> None:
    """Okt. 2026: KI-Bewertung lag pro Inserat in `listings`, gilt aber pro Suche → nach `hits` verschieben."""
    h_cols = {r["name"] for r in c.execute("PRAGMA table_info(hits)")}
    for col, typ in HIT_AI_COLS.items():
        if col not in h_cols:
            c.execute(f"ALTER TABLE hits ADD COLUMN {col} {typ}")
    old = [col for col in ("ai_relevant", "ai_reason", "ai_score", "ai_tier", "single_price") if col in l_cols]
    if not old:
        return
    sets = ", ".join(f"{col} = (SELECT l.{col} FROM listings l WHERE l.key = hits.listing_key)" for col in old)
    c.execute(f"UPDATE hits SET {sets} WHERE ai_relevant IS NULL")
    for col in old:
        c.execute(f"ALTER TABLE listings DROP COLUMN {col}")
    _reassess_trust(c)


def _reassess_trust(c: sqlite3.Connection) -> None:
    """Ampel ohne den alten Preis-Median neu berechnen (Preisvergleich steckt jetzt im Deal-Score)."""
    from .models import Seller
    from .trust import assess

    rows = c.execute("SELECT key, platform, external_id, title, url, price, currency, image, seller, trust_reasons FROM listings")
    updates = []
    for r in rows.fetchall():
        sd = json.loads(r["seller"]) if r["seller"] else {}
        if sd.get("member_since"):
            sd["member_since"] = datetime.fromisoformat(sd["member_since"])
        listing = Listing(platform=r["platform"], external_id=r["external_id"], title=r["title"], url=r["url"],
                          price=r["price"], currency=r["currency"] or "CHF", image=r["image"],
                          seller=Seller(**{k: v for k, v in sd.items() if k in Seller.__dataclass_fields__}))
        t = assess(listing)
        drops = [x for x in json.loads(r["trust_reasons"] or "[]") if x.startswith("Preis-Drop")]
        updates.append((t.level, json.dumps(t.reasons + drops, ensure_ascii=False), r["key"]))
    c.executemany("UPDATE listings SET trust_level = ?, trust_reasons = ? WHERE key = ?", updates)


# Quellenname wie im Prüflauf (runner: src["name"]) – eBay-Inserate speichern nur «EBAY_DE»
_SOURCE = "CASE WHEN l.platform = 'ebay' AND l.source NOT LIKE 'ebay:%' THEN 'ebay:' || l.source ELSE COALESCE(l.source, l.platform) END"
BASELINE_WINDOW_H = 3   # Bestand: was in den ersten Stunden einer neuen Quelle kam …
BASELINE_MIN = 3        # … und mindestens so viele waren (ein einzelnes späteres Angebot ist echt neu)


def _backfill_baseline(c: sqlite3.Connection) -> None:
    """Bestehende Daten: Erst-Scan je Suche und Quelle aus dem ersten Auftauchen ableiten."""
    rows = c.execute(f"""SELECT h.search_id, {_SOURCE} AS src, MIN(h.first_seen) AS first
                         FROM hits h JOIN listings l ON l.key = h.listing_key GROUP BY 1, 2""").fetchall()
    for r in rows:
        c.execute("INSERT OR IGNORE INTO search_sources (search_id, source, first_scan) VALUES (?,?,?)",
                  (r["search_id"], r["src"], r["first"]))
        until = (datetime.fromisoformat(r["first"]) + timedelta(hours=BASELINE_WINDOW_H)).isoformat(timespec="seconds")
        keys = [k[0] for k in c.execute(
            f"""SELECT h.listing_key FROM hits h JOIN listings l ON l.key = h.listing_key
                WHERE h.search_id = ? AND {_SOURCE} = ? AND h.first_seen <= ?""",
            (r["search_id"], r["src"], until))]
        if len(keys) >= BASELINE_MIN:
            c.executemany("UPDATE hits SET baseline = 1 WHERE search_id = ? AND listing_key = ?",
                          [(r["search_id"], k) for k in keys])


def init_db() -> None:
    with connect() as c:
        c.execute("PRAGMA journal_mode = WAL")
        c.executescript(SCHEMA)
        _ensure_chat_cols(c)
        cols = {r["name"] for r in c.execute("PRAGMA table_info(searches)")}
        for col in ("notify_new", "notify_weekly", "allow_sets"):  # Migration bestehender Datenbanken
            if col not in cols:
                c.execute(f"ALTER TABLE searches ADD COLUMN {col} INTEGER NOT NULL DEFAULT 1")

        l_cols = {r["name"] for r in c.execute("PRAGMA table_info(listings)")}
        if "description_full" not in l_cols:
            c.execute("ALTER TABLE listings ADD COLUMN description_full TEXT")
        if "gone_at" not in l_cols:
            c.execute("ALTER TABLE listings ADD COLUMN gone_at TEXT")  # Inseratseite antwortet 404/410
        if "checked_at" not in l_cols:
            c.execute("ALTER TABLE listings ADD COLUMN checked_at TEXT")  # letzte Verfügbarkeitsprüfung
        if "ship_flag" not in l_cols:
            c.execute("ALTER TABLE listings ADD COLUMN ship_flag TEXT")  # Versand laut Plattform: ja/nein
        for col, typ in (("auction", "INTEGER"), ("bids", "INTEGER"), ("bid_price", "REAL"), ("ends_at", "TEXT"),
                         ("reserved", "INTEGER")):
            if col not in l_cols:  # Auktionen und Laufzeitende (Ricardo, eBay)
                c.execute(f"ALTER TABLE listings ADD COLUMN {col} {typ}")
        _migrate_ai_to_hits(c, l_cols)
        if "position" not in cols:  # Reihenfolge in der Übersicht (per Ziehen sortiert), kleiner = weiter oben
            c.execute("ALTER TABLE searches ADD COLUMN position INTEGER")
        for col, typ in (("near_km", "REAL"), ("near_place", "TEXT"), ("route_to", "TEXT"), ("route_km", "REAL")):
            if col not in cols:  # Gebiet: nur in der Nähe (Umkreis um near_place), optional entlang der Strecke nach route_to
                c.execute(f"ALTER TABLE searches ADD COLUMN {col} {typ}")
        if "group_name" not in cols:  # Entweder-oder-Gruppe: mehrere Suchen, von denen nur eines gekauft wird
            c.execute("ALTER TABLE searches ADD COLUMN group_name TEXT")
        if "manual_run_at" not in cols:
            c.execute("ALTER TABLE searches ADD COLUMN manual_run_at TEXT")  # letzter manueller Start
        if "market_price" not in cols:
            c.execute("ALTER TABLE searches ADD COLUMN market_price REAL")
        if "requirements" not in cols:
            c.execute("ALTER TABLE searches ADD COLUMN requirements TEXT NOT NULL DEFAULT ''")  # Freitext für die KI
        for col, typ in (("new_price", "REAL"), ("new_price_note", "TEXT"), ("new_price_manual", "INTEGER NOT NULL DEFAULT 0"),
                         ("new_price_at", "TEXT"), ("product_size", "TEXT"), ("product_weight", "REAL"),
                         ("new_price_offers", "TEXT")):
            if col not in cols:  # Neupreis (newprice.py) und typische Versandgrösse des Produkts
                c.execute(f"ALTER TABLE searches ADD COLUMN {col} {typ}")
        c.execute("""CREATE TABLE IF NOT EXISTS geo (
                         query TEXT PRIMARY KEY, lat REAL, lon REAL, minutes INTEGER, checked_at TEXT NOT NULL)""")
        c.execute("""CREATE TABLE IF NOT EXISTS app_notices (      -- Meldungen, die die Android-App abholt
                         id INTEGER PRIMARY KEY AUTOINCREMENT, created_at TEXT NOT NULL, search_id INTEGER,
                         title TEXT NOT NULL, body TEXT NOT NULL, url TEXT)""")
        c.execute("CREATE TABLE IF NOT EXISTS kv (key TEXT PRIMARY KEY, value TEXT)")
        # Erster erfolgreicher Abruf je Suche und Quelle: was dabei kommt, ist Bestand, nicht «neu» (Statistik)
        c.execute("""CREATE TABLE IF NOT EXISTS search_sources (
                         search_id INTEGER NOT NULL, source TEXT NOT NULL, first_scan TEXT NOT NULL,
                         PRIMARY KEY (search_id, source))""")
        if "deep_scan" not in {r["name"] for r in c.execute("PRAGMA table_info(search_sources)")}:
            # Bestand einmal tiefer gelesen (mehrere Seiten)? Sonst kennt fundus nur die neusten ~25 Inserate
            c.execute("ALTER TABLE search_sources ADD COLUMN deep_scan TEXT")
        h_cols = {r["name"] for r in c.execute("PRAGMA table_info(hits)")}
        if "auction_alert" not in h_cols:  # Auktions-Alarm kurz vor Ablauf: wann geprüft bzw. gemeldet (auctions.py)
            c.execute("ALTER TABLE hits ADD COLUMN auction_alert TEXT")
        n_cols = {r["name"] for r in c.execute("PRAGMA table_info(app_notices)")}
        if "urgent" not in n_cols:  # dringend = eigene, auffällige Meldung (Auktion endet), sofort und einzeln zugestellt
            c.execute("ALTER TABLE app_notices ADD COLUMN urgent INTEGER NOT NULL DEFAULT 0")
            c.execute("ALTER TABLE app_notices ADD COLUMN delivered_at TEXT")
        if "expires_at" not in {r["name"] for r in c.execute("PRAGMA table_info(app_notices)")}:
            c.execute("ALTER TABLE app_notices ADD COLUMN expires_at TEXT")  # danach nie mehr zustellen (Auktion vorbei)
        if "baseline" not in h_cols:
            c.execute("ALTER TABLE hits ADD COLUMN baseline INTEGER NOT NULL DEFAULT 0")
            _backfill_baseline(c)
        c.execute("UPDATE OR IGNORE search_sources SET source = 'ebay:' || source WHERE source LIKE 'EBAY\\_%' ESCAPE '\\'")
        c.execute("""CREATE TABLE IF NOT EXISTS price_history (    -- jede Preisänderung eines Inserats
                         listing_key TEXT NOT NULL, price REAL NOT NULL, currency TEXT, seen_at TEXT NOT NULL)""")
        c.execute("CREATE INDEX IF NOT EXISTS price_history_key ON price_history(listing_key)")
        if not c.execute("SELECT 1 FROM price_history LIMIT 1").fetchone():  # Bestand: erster und aktueller Preis
            c.execute("""INSERT INTO price_history SELECT key, COALESCE(first_price, price), currency, first_seen
                         FROM listings WHERE COALESCE(first_price, price) IS NOT NULL""")
            c.execute("""INSERT INTO price_history SELECT key, price, currency, last_seen FROM listings
                         WHERE price IS NOT NULL AND first_price IS NOT NULL AND price != first_price""")
        if "sort_by" not in cols:
            c.execute("ALTER TABLE searches ADD COLUMN sort_by TEXT NOT NULL DEFAULT 'score'")  # score | price
        if "image_prompt" not in cols:  # Bildanalyse: Auftrag an die KI; image_auto = auch neue Angebote prüfen
            c.execute("ALTER TABLE searches ADD COLUMN image_prompt TEXT NOT NULL DEFAULT ''")
            c.execute("ALTER TABLE searches ADD COLUMN image_auto INTEGER NOT NULL DEFAULT 0")

        # willhaben-Links ohne /iad/ führten auf 404 und galten deshalb fälschlich als verkauft
        c.execute("""UPDATE listings SET url = replace(url, 'willhaben.at/kaufen-und-verkaufen/', 'willhaben.at/iad/kaufen-und-verkaufen/'),
                                         gone_at = NULL, checked_at = NULL
                     WHERE platform = 'willhaben' AND url LIKE '%willhaben.at/kaufen-und-verkaufen/%'""")

        p_cols = {r["name"] for r in c.execute("PRAGMA table_info(platforms)")}
        if "blocked_at" not in p_cols:
            c.execute("ALTER TABLE platforms ADD COLUMN blocked_at TEXT")  # letzte Sperre (403/429/Captcha)
        if "blocked_until" not in p_cols:  # Pause nach Sperre; block_count: Sperren in Folge (für längere Pausen)
            c.execute("ALTER TABLE platforms ADD COLUMN blocked_until TEXT")
            c.execute("ALTER TABLE platforms ADD COLUMN block_count INTEGER NOT NULL DEFAULT 0")


# ---------- Suchen ----------

# Treffer, die zählen: nicht ausgeblendet, nicht als Fehltreffer erkannt, noch verfügbar
RELEVANT = "h.hidden = 0 AND (h.ai_relevant IS NULL OR h.ai_relevant = 1) AND l.gone_at IS NULL"


def list_searches(active_only: bool = False) -> list[dict]:
    sql = f"""SELECT s.*,
                    (SELECT COUNT(*) FROM hits h JOIN listings l ON l.key = h.listing_key
                      WHERE h.search_id = s.id AND {RELEVANT}) AS total,
                    (SELECT COUNT(*) FROM hits h JOIN listings l ON l.key = h.listing_key
                      WHERE h.search_id = s.id AND h.is_new = 1 AND {RELEVANT}) AS unseen
             FROM searches s"""
    if active_only:
        sql += " WHERE s.active = 1"
    sql += " ORDER BY s.position IS NULL, s.position, s.active DESC, s.created_at DESC"  # eigene Reihenfolge zuerst
    with connect() as c:
        return [_search_row(r) for r in c.execute(sql)]


def get_search(search_id: int) -> dict | None:
    with connect() as c:
        r = c.execute("SELECT * FROM searches WHERE id = ?", (search_id,)).fetchone()
        return _search_row(r) if r else None


def _search_row(r: sqlite3.Row) -> dict:
    d = dict(r)
    d["platforms"] = json.loads(d["platforms"]) if d.get("platforms") else None
    d["exclude_words"] = [w.strip().lower() for w in d["exclude"].split(",") if w.strip()]
    d["allow_sets"] = bool(d.get("allow_sets", 1))
    d["new_price_offers"] = json.loads(d["new_price_offers"]) if d.get("new_price_offers") else []
    return d


def save_search(data: dict, search_id: int | None = None) -> int:
    values = (
        data["query"].strip(),
        data.get("exclude", "").strip(),
        data.get("min_price"),
        data.get("max_price"),
        json.dumps(data["platforms"]) if data.get("platforms") else None,
        int(data.get("allow_sets", True)),
        int(data.get("require_shipping", False)),
        (data.get("requirements") or "").strip(),
        data.get("near_km") or None,
        (data.get("near_place") or "").strip() or None,
        (data.get("route_to") or "").strip() or None,
        data.get("route_km") or None,
        (data.get("group_name") or "").strip() or None,
    )
    with connect() as c:
        if search_id is None:
            cur = c.execute(
                "INSERT INTO searches (query, exclude, min_price, max_price, platforms, allow_sets, require_shipping, requirements,"
                " near_km, near_place, route_to, route_km, group_name, created_at, position)"
                " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?, (SELECT COALESCE(MIN(position), 0) - 1 FROM searches))",  # neu: ganz oben
                (*values, now()),
            )
            return int(cur.lastrowid)
        c.execute(
            "UPDATE searches SET query=?, exclude=?, min_price=?, max_price=?, platforms=?, allow_sets=?, require_shipping=?,"
            " requirements=?, near_km=?, near_place=?, route_to=?, route_km=?, group_name=? WHERE id=?",
            (*values, search_id),
        )
        return search_id


def set_new_price(search_id: int, price: float | None, note: str | None, manual: bool,
                  size: str | None = None, weight: float | None = None, offers: list[dict] | None = None) -> None:
    """Neupreis speichern. size/weight/offers None = bisherige Werte behalten."""
    with connect() as c:
        c.execute("""UPDATE searches SET new_price = ?, new_price_note = ?, new_price_manual = ?, new_price_at = ?,
                                         product_size = COALESCE(?, product_size), product_weight = COALESCE(?, product_weight),
                                         new_price_offers = COALESCE(?, new_price_offers)
                     WHERE id = ?""", (price, note, int(manual), now(), size, weight,
                                       json.dumps(offers, ensure_ascii=False) if offers is not None else None, search_id))


_geo: dict = {"path": None, "rows": {}}   # ganze Tabelle im Speicher: die Übersicht fragt pro Aufruf ~2000 Orte ab


def geo_get(query: str) -> dict | None:
    path = str(db_path())
    if _geo["path"] != path:
        with connect() as c:
            _geo["rows"] = {r["query"]: dict(r) for r in c.execute("SELECT * FROM geo")}
        _geo["path"] = path
    return _geo["rows"].get(query)


def geo_put(query: str, lat: float | None, lon: float | None, minutes: int | None) -> None:
    with connect() as c:
        c.execute("INSERT OR REPLACE INTO geo (query, lat, lon, minutes, checked_at) VALUES (?,?,?,?,?)",
                  (query, lat, lon, minutes, now()))
    _geo["path"] = None  # beim nächsten Lesen neu laden


def set_search_sort(search_id: int, sort_by: str) -> None:
    with connect() as c:
        c.execute("UPDATE searches SET sort_by = ? WHERE id = ?", ("price" if sort_by == "price" else "score", search_id))


def set_search_active(search_id: int, active: bool) -> None:
    with connect() as c:
        c.execute("UPDATE searches SET active = ? WHERE id = ?", (int(active), search_id))


def set_search_notify(search_id: int, notify_new: bool, notify_weekly: bool) -> None:
    with connect() as c:
        c.execute("UPDATE searches SET notify_new = ?, notify_weekly = ? WHERE id = ?",
                  (int(notify_new), int(notify_weekly), search_id))


def weekly_summary(days: int = 7) -> list[dict]:
    """Aktive Suchen mit Wochenübersicht: Treffer der letzten Tage und Gesamtzahl."""
    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat(timespec="seconds")
    sql = f"""SELECT s.id, s.query,
                    (SELECT COUNT(*) FROM hits h JOIN listings l ON l.key = h.listing_key
                      WHERE h.search_id = s.id AND {RELEVANT} AND h.first_seen >= ?) AS week,
                    (SELECT COUNT(*) FROM hits h JOIN listings l ON l.key = h.listing_key
                      WHERE h.search_id = s.id AND {RELEVANT}) AS total
             FROM searches s WHERE s.active = 1 AND s.notify_weekly = 1 AND s.baseline_done = 1
             ORDER BY s.query"""
    with connect() as c:
        return [dict(r) for r in c.execute(sql, (cutoff,))]


def set_search_order(ids: list[int]) -> None:
    """Reihenfolge der Übersicht speichern: ids von oben nach unten; nicht genannte bleiben dahinter."""
    with connect() as c:
        for pos, sid in enumerate(ids):
            c.execute("UPDATE searches SET position = ? WHERE id = ?", (pos, sid))


def delete_search(search_id: int) -> None:
    with connect() as c:
        c.execute("DELETE FROM searches WHERE id = ?", (search_id,))


def mark_search_run(search_id: int, manual: bool = False) -> None:
    with connect() as c:
        c.execute("UPDATE searches SET last_run_at = ?, baseline_done = 1 WHERE id = ?", (now(), search_id))
        if manual:
            c.execute("UPDATE searches SET manual_run_at = ? WHERE id = ?", (now(), search_id))


# ---------- Inserate & Treffer ----------

def upsert_listing(l: Listing, trust_level: str, trust_reasons: list[str]) -> None:
    seller = asdict(l.seller)
    if seller.get("member_since"):
        seller["member_since"] = l.seller.member_since.isoformat()
    ts = now()
    with connect() as c:
        # Check for price drop before upserting
        existing = c.execute("SELECT price, first_price, trust_reasons FROM listings WHERE key = ?", (l.key,)).fetchone()
        if l.price is not None and (existing is None or existing["price"] != l.price):
            c.execute("INSERT INTO price_history (listing_key, price, currency, seen_at) VALUES (?,?,?,?)",
                      (l.key, l.price, l.currency, ts))
        if existing and l.price and existing["price"] and l.price < existing["price"]:
            existing_reasons = json.loads(existing["trust_reasons"]) if existing["trust_reasons"] else []
            drop_msg = f"Preis-Drop: von {existing['price']} auf {l.price} {l.currency}"
            if drop_msg not in existing_reasons:
                existing_reasons.append(drop_msg)

            # Combine new trust reasons with existing ones (avoiding duplicates)
            for r in existing_reasons:
                if r not in trust_reasons:
                    trust_reasons.append(r)

            # Deal-Score passt sich beim nächsten scoring.rescore_search() an, KI-Merkmale bleiben gültig

        c.execute(
            """INSERT INTO listings (key, platform, external_id, title, url, price, first_price, currency, image,
                                     location, country, posted_at, source, seller, trust_level, trust_reasons,
                                     description_full, first_seen, last_seen, auction, bids, bid_price, ends_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
               ON CONFLICT(key) DO UPDATE SET
                   title=excluded.title, url=excluded.url, price=excluded.price, currency=excluded.currency,
                   image=COALESCE(excluded.image, listings.image), location=COALESCE(excluded.location, listings.location),
                   seller=excluded.seller, trust_level=excluded.trust_level, trust_reasons=excluded.trust_reasons,
                   description_full=COALESCE(excluded.description_full, listings.description_full),
                   auction=COALESCE(excluded.auction, listings.auction), bids=COALESCE(excluded.bids, listings.bids),
                   bid_price=COALESCE(excluded.bid_price, listings.bid_price),
                   ends_at=COALESCE(excluded.ends_at, listings.ends_at),
                   last_seen=excluded.last_seen, gone_at=NULL""",
            (
                l.key, l.platform, l.external_id, l.title, l.url, l.price, l.price, l.currency, l.image,
                l.location, l.country, l.posted_at.isoformat() if l.posted_at else None, l.source,
                json.dumps(seller), trust_level, json.dumps(trust_reasons, ensure_ascii=False),
                l.description_full, ts, ts, None if l.auction is None else int(l.auction), l.bids, l.bid_price,
                l.ends_at.isoformat() if l.ends_at else None,
            ),
        )

def mark_listing_gone(key: str) -> None:
    with connect() as c:
        c.execute("UPDATE listings SET gone_at = ?, checked_at = ? WHERE key = ?", (now(), now(), key))


def expire_ended_listings() -> int:
    """Inserate mit abgelaufener Laufzeit (Ricardo, eBay) als weg markieren – ganz ohne Anfrage an die Plattform.
    Taucht ein Inserat danach wieder in den Suchergebnissen auf (verlängert), setzt upsert_listing es zurück."""
    with connect() as c:
        return c.execute("""UPDATE listings SET gone_at = ends_at
                            WHERE gone_at IS NULL AND ends_at IS NOT NULL AND ends_at < ?""", (now(),)).rowcount


def mark_listing_checked(key: str) -> None:
    with connect() as c:
        c.execute("UPDATE listings SET checked_at = ? WHERE key = ?", (now(), key))


def top_hits_to_check(search_id: int, limit: int = 8, recheck_hours: float = 6) -> list[dict]:
    """Bestbewertete passende Treffer, die in diesem Lauf nicht in den Suchergebnissen waren
    (nur Seite 1 wird abgefragt – «nicht gesehen» heisst nicht «verkauft») und seit recheck_hours nicht geprüft wurden.
    Was in den Suchergebnissen steht, ist online; die Plattformen entfernen gelöschte Inserate dort sofort."""
    sql = """SELECT l.key, l.url, l.platform, l.source, l.external_id, l.description_full FROM hits h JOIN listings l ON l.key = h.listing_key
             WHERE h.search_id = ? AND h.hidden = 0 AND h.ai_relevant = 1 AND l.gone_at IS NULL
               AND l.last_seen < strftime('%Y-%m-%dT%H:%M:%S+00:00', 'now', '-30 minutes')
               AND (l.checked_at IS NULL OR l.checked_at < strftime('%Y-%m-%dT%H:%M:%S+00:00', 'now', ?))
             ORDER BY h.ai_score DESC LIMIT ?"""
    with connect() as c:
        return [dict(r) for r in c.execute(sql, (search_id, f"-{recheck_hours * 60:.0f} minutes", limit))]


def set_listing_reserved(key: str, reserved: bool) -> None:
    with connect() as c:
        c.execute("UPDATE listings SET reserved = ?, checked_at = ? WHERE key = ?", (int(reserved), now(), key))


def update_listing_shipping(key: str, flag: str) -> bool:
    """Versandangabe der Plattform speichern. True, wenn sie neu oder anders ist."""
    with connect() as c:
        return c.execute("UPDATE listings SET ship_flag = ? WHERE key = ? AND ship_flag IS NOT ?",
                         (flag, key, flag)).rowcount > 0


def hits_of_listing(key: str) -> list[dict]:
    with connect() as c:
        return [dict(r) for r in c.execute(
            """SELECT h.search_id, h.ai_relevant, h.ai_reason, s.require_shipping, s.near_km FROM hits h
               JOIN searches s ON s.id = h.search_id WHERE h.listing_key = ?""", (key,))]


def set_hit_shipping(search_id: int, key: str, flag: str) -> None:
    with connect() as c:
        c.execute("UPDATE hits SET ai_shipping = ? WHERE search_id = ? AND listing_key = ?", (flag, search_id, key))


def top_hits_without_ship_flag(search_id: int, platforms: tuple[str, ...], limit: int = 5) -> list[dict]:
    """Bestbewertete Treffer, deren Versandangabe der Plattform noch fehlt – die Karte zeigt sie nicht immer."""
    marks = ",".join("?" * len(platforms))
    sql = f"""SELECT l.key, l.url, l.platform FROM hits h JOIN listings l ON l.key = h.listing_key
              WHERE h.search_id = ? AND h.hidden = 0 AND h.ai_relevant = 1 AND l.gone_at IS NULL
                AND l.ship_flag IS NULL AND l.platform IN ({marks})
              ORDER BY h.ai_score DESC LIMIT ?"""
    with connect() as c:
        return [dict(r) for r in c.execute(sql, (search_id, *platforms, limit))]


def update_listing_description(key: str, description: str) -> None:
    with connect() as c:
        c.execute("UPDATE listings SET description_full = ? WHERE key = ?", (description, key))

def update_hit_ai(search_id: int, key: str, relevant: bool, reason: str | None, condition: str | None = None,
                  shipping: str | None = None, single_price: float | None = None, year: int | None = None,
                  shipping_cost: float | None = None, req: str | None = None, req_note: str | None = None,
                  set_items: str | None = None, single_estimated: bool = False, size: str | None = None,
                  weight: float | None = None) -> None:
    """KI-Merkmale eines Treffers für eine Suche. Der Deal-Score kommt danach aus scoring.rescore_search()."""
    with connect() as c:
        c.execute(
            """UPDATE hits SET ai_relevant = ?, ai_reason = ?, ai_condition = ?, ai_shipping = ?, single_price = ?,
                               ai_year = ?, ai_shipping_cost = ?, ai_req = ?, ai_req_note = ?, ai_set_items = ?,
                               single_estimated = ?, ai_size = ?, ai_weight = ?, img_note = NULL, img_done = NULL
               WHERE search_id = ? AND listing_key = ?""",
            (int(relevant), reason, condition, shipping, single_price, year, shipping_cost, req, req_note, set_items,
             int(single_estimated), size, weight, search_id, key),
        )


def reset_hit_ai(search_id: int | None, key: str) -> None:
    """Bewertung verwerfen, damit die KI neu prüft. search_id=None: in allen Suchen."""
    sql = """UPDATE hits SET ai_relevant = NULL, ai_reason = NULL, ai_condition = NULL, ai_shipping = NULL,
                             single_price = NULL, ai_year = NULL, ai_shipping_cost = NULL, total_chf = NULL, ai_score = NULL, ai_tier = NULL, score_reason = NULL,
                             ai_req = NULL, ai_req_note = NULL, ai_set_items = NULL, single_estimated = NULL,
                             ai_size = NULL, ai_weight = NULL, delivery = NULL, img_note = NULL, img_done = NULL
             WHERE listing_key = ?"""
    with connect() as c:
        if search_id is None:
            c.execute(sql, (key,))
        else:
            c.execute(sql + " AND search_id = ?", (key, search_id))


REQ_FAILED = "Erfüllt deine Anforderung nicht"
SCAM = "Betrugsverdacht"
NO_SHIPPING = "Kein Versand (nur Abholung)"


def reset_search_ai(search_id: int) -> None:
    """Bewertungen nach geänderten Anforderungen verwerfen – nur dort, wo die Anforderung etwas ändern kann.

    Fehltreffer (Zubehör, anderes Modell, ausgeschlossene Wörter …) bleiben Fehltreffer: ob es das gesuchte Produkt
    ist, hängt nicht von den Anforderungen ab. Sonst stünden sie bis zur neuen KI-Prüfung wieder in der Liste –
    bei Sortierung nach Preis ganz oben. Von Hand Ein- oder Ausgeblendetes bleibt ebenfalls."""
    with connect() as c:
        c.execute("""UPDATE hits SET ai_relevant = NULL, ai_reason = NULL, ai_condition = NULL, ai_shipping = NULL,
                                     single_price = NULL, ai_year = NULL, ai_shipping_cost = NULL, total_chf = NULL,
                                     ai_score = NULL, ai_tier = NULL, score_reason = NULL, ai_req = NULL, ai_req_note = NULL,
                                     ai_set_items = NULL, single_estimated = NULL, ai_size = NULL, ai_weight = NULL,
                                     delivery = NULL, img_note = NULL, img_done = NULL
                     WHERE search_id = ? AND hidden = 0 AND ai_reason IS NOT 'Manuell eingeblendet'
                       AND (ai_relevant IS NULL OR ai_relevant = 1 OR ai_req = 'nein' OR ai_reason LIKE ? || '%')""",
                  (search_id, REQ_FAILED))


def set_hit_relevant(search_id: int, key: str, relevant: bool, reason: str) -> None:
    """Manuelles Ein-/Ausblenden: Zustand, Versand usw. aus der KI bleiben erhalten."""
    with connect() as c:
        c.execute("UPDATE hits SET ai_relevant = ?, ai_reason = ? WHERE search_id = ? AND listing_key = ?",
                  (int(relevant), reason, search_id, key))


def set_scores(search_id: int, market_price: float | None, scores: list[tuple]) -> None:
    """scores: (score, tier, reason, total_chf, delivery, key)."""
    with connect() as c:
        c.execute("UPDATE searches SET market_price = ? WHERE id = ?", (market_price, search_id))
        c.executemany(
            "UPDATE hits SET ai_score = ?, ai_tier = ?, score_reason = ?, total_chf = ?, delivery = ?"
            " WHERE listing_key = ? AND search_id = ?",
            [(*row, search_id) for row in scores],
        )


def set_image_analysis(search_id: int, prompt: str, auto: bool) -> None:
    with connect() as c:
        c.execute("UPDATE searches SET image_prompt = ?, image_auto = ? WHERE id = ?", (prompt, int(auto), search_id))


def hits_for_image_check(search_id: int, prompt: str, limit: int = 1000) -> list[dict]:
    """Passende, noch verfügbare Treffer mit Bild, die mit diesem Auftrag noch nicht geprüft sind. Wegen nicht
    erfüllter Anforderung Aussortierte gehören dazu – das Bild kann die fehlende Angabe liefern."""
    sql = f"""SELECT l.*, h.search_id, {HIT_AI_SELECT} FROM hits h JOIN listings l ON l.key = h.listing_key
              WHERE h.search_id = ? AND h.hidden = 0 AND l.gone_at IS NULL AND l.image IS NOT NULL
                AND (h.ai_relevant = 1 OR (h.ai_relevant = 0 AND h.ai_reason LIKE ? || '%'))
                AND h.img_done IS NOT ?
              ORDER BY h.ai_score DESC LIMIT ?"""
    with connect() as c:
        return [_listing_row(r) for r in c.execute(sql, (search_id, REQ_FAILED, prompt, limit))]


def image_progress(search_id: int, prompt: str) -> tuple[int, int]:
    """(geprüft, gesamt) der Bildanalyse einer Suche."""
    with connect() as c:
        r = c.execute("""SELECT COUNT(*) AS total, SUM(h.img_done IS ?) AS done FROM hits h
                          JOIN listings l ON l.key = h.listing_key
                          WHERE h.search_id = ? AND h.hidden = 0 AND l.gone_at IS NULL AND l.image IS NOT NULL
                            AND (h.ai_relevant = 1 OR h.img_done IS ?
                                 OR (h.ai_relevant = 0 AND h.ai_reason LIKE ? || '%'))""",
                      (prompt, search_id, prompt, REQ_FAILED)).fetchone()
        return int(r["done"] or 0), int(r["total"] or 0)


def set_hit_image_result(search_id: int, key: str, prompt: str, note: str | None, relevant: bool, reason: str | None,
                         condition: str | None, year: int | None, req: str | None, req_note: str | None) -> None:
    with connect() as c:
        c.execute("""UPDATE hits SET img_done = ?, img_note = ?, ai_relevant = ?, ai_reason = ?, ai_condition = ?,
                                     ai_year = ?, ai_req = ?, ai_req_note = ?
                     WHERE search_id = ? AND listing_key = ?""",
                  (prompt, note, int(relevant), reason, condition, year, req, req_note, search_id, key))


def get_ai_progress() -> tuple[int, int]:
    """(bewertet, gesamt) über alle sichtbaren Treffer."""
    with connect() as c:
        row = c.execute(  # nur was die KI überhaupt prüft: aktive Suchen, noch verfügbare Inserate
            """SELECT COUNT(*) AS total, SUM(h.ai_relevant IS NOT NULL) AS evaluated
               FROM hits h JOIN listings l ON l.key = h.listing_key JOIN searches s ON s.id = h.search_id
               WHERE h.hidden = 0 AND s.active = 1 AND l.gone_at IS NULL"""
        ).fetchone()
        return int(row["evaluated"] or 0), int(row["total"] or 0)


def add_hit(search_id: int, listing_key: str, is_new: bool, baseline: bool = False) -> bool:
    """Gibt True zurück, wenn der Treffer für diese Suche bisher unbekannt war.
    baseline: kam beim ersten Abruf einer Quelle (Bestand) – zählt in der Statistik nicht als «neu»."""
    with connect() as c:
        cur = c.execute(
            "INSERT OR IGNORE INTO hits (search_id, listing_key, first_seen, is_new, notified, baseline) VALUES (?,?,?,?,?,?)",
            (search_id, listing_key, now(), int(is_new), int(not is_new), int(baseline)),
        )
        return cur.rowcount == 1


def first_scans(search_id: int) -> set[str]:
    """Quellen, die für diese Suche schon einmal erfolgreich abgefragt wurden."""
    with connect() as c:
        return {r["source"] for r in c.execute("SELECT source FROM search_sources WHERE search_id = ?", (search_id,))}


def deep_scans(search_id: int) -> set[str]:
    """Quellen, deren Bestand für diese Suche schon einmal über mehrere Seiten gelesen wurde."""
    with connect() as c:
        return {r["source"] for r in c.execute(
            "SELECT source FROM search_sources WHERE search_id = ? AND deep_scan IS NOT NULL", (search_id,))}


def record_deep_scan(search_id: int, source: str) -> None:
    with connect() as c:
        c.execute("INSERT OR IGNORE INTO search_sources (search_id, source, first_scan) VALUES (?,?,?)",
                  (search_id, source, now()))
        c.execute("UPDATE search_sources SET deep_scan = ? WHERE search_id = ? AND source = ?", (now(), search_id, source))


def hit_keys(search_id: int) -> set[str]:
    """Alle Inserate, die diese Suche schon kennt (auch ausgefilterte und verschwundene)."""
    with connect() as c:
        return {r["listing_key"] for r in c.execute("SELECT listing_key FROM hits WHERE search_id = ?", (search_id,))}


def record_first_scan(search_id: int, source: str) -> None:
    with connect() as c:
        c.execute("INSERT OR IGNORE INTO search_sources (search_id, source, first_scan) VALUES (?,?,?)",
                  (search_id, source, now()))


def pending_top_hits(search_id: int, min_score: int, max_age_days: int = 3) -> list[dict]:
    """Neue, noch nicht gemeldete Top-Angebote einer Suche."""
    sql = f"""SELECT l.*, h.first_seen AS hit_seen, h.is_new, h.baseline, h.hidden, h.search_id, {HIT_AI_SELECT}
              FROM hits h JOIN listings l ON l.key = h.listing_key
              WHERE h.search_id = ? AND h.notified = 0 AND h.hidden = 0 AND h.ai_relevant = 1
                AND h.ai_score >= ? AND l.gone_at IS NULL AND COALESCE(l.reserved, 0) = 0
                AND h.first_seen >= strftime('%Y-%m-%dT%H:%M:%S+00:00', 'now', ?)
              ORDER BY h.ai_score DESC"""
    with connect() as c:
        return [_listing_row(r) for r in c.execute(sql, (search_id, min_score, f"-{max_age_days} days"))]


def kv_get(key: str) -> str | None:
    with connect() as c:
        r = c.execute("SELECT value FROM kv WHERE key = ?", (key,)).fetchone()
        return r["value"] if r else None


def kv_set(key: str, value: str) -> None:
    with connect() as c:
        c.execute("INSERT INTO kv (key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                  (key, value))


def add_app_notice(search_id: int | None, title: str, body: str, url: str | None, urgent: bool = False,
                   expires_at: str | None = None) -> int:
    with connect() as c:
        return c.execute(
            "INSERT INTO app_notices (created_at, search_id, title, body, url, urgent, expires_at) VALUES (?,?,?,?,?,?,?)",
            (now(), search_id, title, body, url, int(urgent), expires_at)).lastrowid


def take_urgent_notices(max_age_minutes: int = 180) -> list[dict]:
    """Dringende Meldungen (Auktion endet), die die App noch nicht abgeholt hat – jede wird genau einmal geliefert.
    Ältere sind überholt: die Auktion ist dann vorbei."""
    with connect() as c:
        rows = [dict(r) for r in c.execute(
            """SELECT * FROM app_notices WHERE urgent = 1 AND delivered_at IS NULL
                 AND created_at >= strftime('%Y-%m-%dT%H:%M:%S+00:00', 'now', ?) ORDER BY id""",
            (f"-{max_age_minutes} minutes",))]
        c.executemany("UPDATE app_notices SET delivered_at = ? WHERE id = ?", [(now(), r["id"]) for r in rows])
        # Fragt die App zu spät (Handy im Stromsparmodus, Tailscale aus), ist die Auktion vorbei: dann gar nicht melden –
        # ein Alarm nach dem Ende ist schlimmer als keiner
        return [r for r in rows if not r.get("expires_at") or r["expires_at"] > now()]


# ---------- Auktionen ----------

def auctions_to_watch() -> list[dict]:
    """Laufende Auktionen in aktiven Suchen, die passen und für die noch kein Alarm entschieden ist:
    {search_id, key, ends_at}. Ob es reine Auktionen sind und ob sie sich lohnen, entscheidet auctions.py."""
    with connect() as c:
        return [dict(r) for r in c.execute(
            """SELECT h.search_id, l.key, l.ends_at FROM hits h JOIN listings l ON l.key = h.listing_key
               JOIN searches s ON s.id = h.search_id
               WHERE s.active = 1 AND h.hidden = 0 AND h.ai_relevant = 1 AND h.auction_alert IS NULL
                 AND l.auction = 1 AND l.gone_at IS NULL AND l.ends_at IS NOT NULL AND l.ends_at > ?
               ORDER BY l.ends_at""", (now(),))]


def set_auction_alert(search_id: int, key: str, result: str) -> None:
    """result: «gemeldet …» oder «kein Alarm: …» mit Zeitpunkt – danach wird die Auktion nicht nochmals geprüft."""
    with connect() as c:
        c.execute("UPDATE hits SET auction_alert = ? WHERE search_id = ? AND listing_key = ?",
                  (f"{now()} {result}", search_id, key))


def update_auction(key: str, bid_price: float | None, bids: int | None, ends_at: str | None = None,
                   price: float | None = None) -> None:
    """Frischer Stand einer Auktion (Gebot, Anzahl Gebote, evtl. verlängertes Ende)."""
    with connect() as c:
        c.execute("""UPDATE listings SET bid_price = COALESCE(?, bid_price), bids = COALESCE(?, bids),
                                        ends_at = COALESCE(?, ends_at), price = COALESCE(?, price), last_seen = ?
                     WHERE key = ?""", (bid_price, bids, ends_at, price, now(), key))


def app_notices_after(after: int, limit: int = 20) -> list[dict]:
    """Noch nicht abgeholte Meldungen (id > after), höchstens 3 Tage alt – ältere sind überholt."""
    with connect() as c:
        return [dict(r) for r in c.execute(
            """SELECT * FROM app_notices WHERE id > ? AND urgent = 0
                 AND created_at >= strftime('%Y-%m-%dT%H:%M:%S+00:00', 'now', '-3 days')
               ORDER BY id LIMIT ?""", (after, limit))]


def last_app_notice_id() -> int:
    with connect() as c:
        r = c.execute("SELECT MAX(id) AS m FROM app_notices").fetchone()
        return r["m"] or 0


def mark_notified(search_id: int, keys: list[str]) -> None:
    with connect() as c:
        c.executemany(
            "UPDATE hits SET notified = 1 WHERE search_id = ? AND listing_key = ?",
            [(search_id, k) for k in keys],
        )


def hits_for_search(search_id: int, include_hidden: bool = False, limit: int = 1_000_000) -> list[dict]:
    sql = f"""SELECT l.*, h.first_seen AS hit_seen, h.is_new, h.baseline, h.hidden, h.search_id, {HIT_AI_SELECT}
             FROM hits h JOIN listings l ON l.key = h.listing_key
             WHERE h.search_id = ?"""
    if not include_hidden:
        sql += " AND h.hidden = 0"
    sql += " ORDER BY h.first_seen DESC, l.posted_at DESC LIMIT ?"
    with connect() as c:
        return [_listing_row(r) for r in c.execute(sql, (search_id, limit))]


def _listing_row(r: sqlite3.Row) -> dict:
    d = dict(r)
    d["seller"] = json.loads(d["seller"]) if d.get("seller") else {}
    d["trust_reasons"] = json.loads(d["trust_reasons"]) if d.get("trust_reasons") else []
    return d

def get_listing(key: str) -> dict | None:
    with connect() as c:
        r = c.execute("SELECT * FROM listings WHERE key = ?", (key,)).fetchone()
        return _listing_row(r) if r else None


def mark_seen(search_id: int) -> None:
    with connect() as c:
        c.execute("UPDATE hits SET is_new = 0 WHERE search_id = ?", (search_id,))


def hide_hit(search_id: int, listing_key: str) -> None:
    """Nur in dieser Suche als unpassend markieren (landet bei «Ausgefiltert», kann eingeblendet werden)."""
    with connect() as c:
        c.execute("""UPDATE hits SET is_new = 0, ai_relevant = 0, ai_reason = 'Manuell ausgeblendet'
                     WHERE search_id = ? AND listing_key = ?""", (search_id, listing_key))


# ---------- Plattform-Schalter ----------

def platform_states() -> dict[str, dict]:
    with connect() as c:
        return {r["name"]: dict(r) for r in c.execute("SELECT * FROM platforms")}


def ensure_platforms(names: list[str]) -> None:
    with connect() as c:
        c.executemany("INSERT OR IGNORE INTO platforms (name) VALUES (?)", [(n,) for n in names])


def set_platform_enabled(name: str, enabled: bool) -> None:
    with connect() as c:
        c.execute("UPDATE platforms SET enabled = ? WHERE name = ?", (int(enabled), name))


BLOCK_PAUSE_FIRST = timedelta(hours=1)   # erste Sperre: kurz warten – ein Ratenlimit ist meist bald vorbei
BLOCK_PAUSE_MAX = timedelta(hours=24)    # jede weitere Sperre in Folge verdoppelt die Pause bis hierhin


def mark_platform_blocked(name: str, retry_after: int | None = None) -> datetime:
    """Sperre vermerken und Pause festlegen: Retry-After des Servers, mindestens aber 1 h, 2 h, 4 h … 24 h
    bei Sperren in Folge. Gibt das Ende der Pause zurück."""
    with connect() as c:
        row = c.execute("SELECT block_count FROM platforms WHERE name = ?", (name,)).fetchone()
        count = (row["block_count"] if row else 0) + 1
        pause = min(BLOCK_PAUSE_FIRST * 2 ** (count - 1), BLOCK_PAUSE_MAX)
        if retry_after:
            pause = max(pause, timedelta(seconds=retry_after))
        until = datetime.now(timezone.utc) + pause
        c.execute("UPDATE platforms SET blocked_at = ?, blocked_until = ?, block_count = ? WHERE name = ?",
                  (now(), until.isoformat(timespec="seconds"), count, name))
        return until


def record_platform_result(name: str, count: int | None, error: str | None) -> None:
    with connect() as c:
        c.execute(
            "UPDATE platforms SET last_run = ?, last_count = ?, last_error = ? WHERE name = ?",
            (now(), count, error, name),
        )
        if count is not None and error is None:  # hat wieder geliefert: nächste Sperre beginnt mit kurzer Pause
            c.execute("UPDATE platforms SET block_count = 0 WHERE name = ?", (name,))


# ---------- Läufe ----------

def start_run(trigger: str) -> int:
    with connect() as c:
        return int(c.execute("INSERT INTO runs (trigger, started_at) VALUES (?,?)", (trigger, now())).lastrowid)


def close_stale_runs() -> int:
    """Läufe ohne Ende (Server-Neustart mitten im Lauf) als abgebrochen schliessen."""
    with connect() as c:
        return c.execute("UPDATE runs SET finished_at = ?, errors = -1 WHERE finished_at IS NULL", (now(),)).rowcount


def searches_with_unevaluated_hits() -> list[int]:
    with connect() as c:
        return [r[0] for r in c.execute(
            """SELECT DISTINCT h.search_id FROM hits h JOIN listings l ON l.key = h.listing_key
               JOIN searches s ON s.id = h.search_id
               WHERE s.active = 1 AND h.hidden = 0 AND h.ai_relevant IS NULL AND l.gone_at IS NULL""")]


def finish_run(run_id: int, new_hits: int, errors: int) -> None:
    with connect() as c:
        c.execute("UPDATE runs SET finished_at = ?, new_hits = ?, errors = ? WHERE id = ?",
                  (now(), new_hits, errors, run_id))


def last_runs(limit: int = 5) -> list[dict]:
    with connect() as c:
        return [dict(r) for r in c.execute("SELECT * FROM runs ORDER BY id DESC LIMIT ?", (limit,))]


def update_listing_chat(key: str, seller_chat: str) -> None:
    with connect() as conn:
        conn.execute("UPDATE listings SET seller_chat = ? WHERE key = ?", (seller_chat, key))
        conn.commit()

def update_listing_draft(key: str, draft_question: str) -> None:
    with connect() as conn:
        conn.execute("UPDATE listings SET draft_question = ? WHERE key = ?", (draft_question, key))
        conn.commit()
