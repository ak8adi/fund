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
    allow_sets    INTEGER NOT NULL DEFAULT 1
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
    ai_score      INTEGER,
    ai_relevant   INTEGER,                      -- 1=True, 0=False
    ai_tier       TEXT,                         -- S, A, B, C
    ai_reason     TEXT,
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


@contextmanager
def connect() -> Iterator[sqlite3.Connection]:
    conn = sqlite3.connect(db_path(), timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    try:
        yield conn
        _ensure_chat_cols(conn)
        conn.commit()
    finally:
        conn.close()


def _ensure_chat_cols(conn):
    try: conn.execute('ALTER TABLE searches ADD COLUMN require_shipping INTEGER DEFAULT 0')
    except Exception: pass
    for col in ('seller_chat TEXT', 'draft_question TEXT'):
        try:
            conn.execute(f'ALTER TABLE listings ADD COLUMN {col}')
        except Exception:
            pass

def init_db() -> None:
    with connect() as c:
        c.execute("PRAGMA journal_mode = WAL")
        c.executescript(SCHEMA)
        cols = {r["name"] for r in c.execute("PRAGMA table_info(searches)")}
        for col in ("notify_new", "notify_weekly", "allow_sets"):  # Migration bestehender Datenbanken
            if col not in cols:
                c.execute(f"ALTER TABLE searches ADD COLUMN {col} INTEGER NOT NULL DEFAULT 1")

        l_cols = {r["name"] for r in c.execute("PRAGMA table_info(listings)")}
        if "description_full" not in l_cols:
            c.execute("ALTER TABLE listings ADD COLUMN description_full TEXT")
        if "ai_score" not in l_cols:
            c.execute("ALTER TABLE listings ADD COLUMN ai_score INTEGER")
        if "ai_relevant" not in l_cols:
            c.execute("ALTER TABLE listings ADD COLUMN ai_relevant INTEGER")
        if "ai_tier" not in l_cols:
            c.execute("ALTER TABLE listings ADD COLUMN ai_tier TEXT")
        if "ai_reason" not in l_cols:
            c.execute("ALTER TABLE listings ADD COLUMN ai_reason TEXT")

        p_cols = {r["name"] for r in c.execute("PRAGMA table_info(platforms)")}
        if "blocked_at" not in p_cols:
            c.execute("ALTER TABLE platforms ADD COLUMN blocked_at TEXT")  # letzte Sperre (403/429/Captcha)


# ---------- Suchen ----------

def list_searches(active_only: bool = False) -> list[dict]:
    sql = """SELECT s.*,
                    (SELECT COUNT(*) FROM hits h WHERE h.search_id = s.id AND h.hidden = 0) AS total,
                    (SELECT COUNT(*) FROM hits h WHERE h.search_id = s.id AND h.is_new = 1 AND h.hidden = 0) AS unseen
             FROM searches s"""
    if active_only:
        sql += " WHERE s.active = 1"
    sql += " ORDER BY s.active DESC, s.created_at DESC"
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
    return d


def save_search(data: dict, search_id: int | None = None) -> int:
    values = (
        data["query"].strip(),
        data.get("exclude", "").strip(),
        data.get("min_price"),
        data.get("max_price"),
        json.dumps(data["platforms"]) if data.get("platforms") else None,
        int(data.get("allow_sets", True)),
    )
    with connect() as c:
        if search_id is None:
            cur = c.execute(
                "INSERT INTO searches (query, exclude, min_price, max_price, platforms, allow_sets, created_at) VALUES (?,?,?,?,?,?,?)",
                (*values, now()),
            )
            return int(cur.lastrowid)
        c.execute(
            "UPDATE searches SET query=?, exclude=?, min_price=?, max_price=?, platforms=?, allow_sets=? WHERE id=?",
            (*values, search_id),
        )
        return search_id


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
    sql = """SELECT s.id, s.query,
                    (SELECT COUNT(*) FROM hits h WHERE h.search_id = s.id AND h.hidden = 0 AND h.first_seen >= ?) AS week,
                    (SELECT COUNT(*) FROM hits h WHERE h.search_id = s.id AND h.hidden = 0) AS total
             FROM searches s WHERE s.active = 1 AND s.notify_weekly = 1 AND s.baseline_done = 1
             ORDER BY s.query"""
    with connect() as c:
        return [dict(r) for r in c.execute(sql, (cutoff,))]


def delete_search(search_id: int) -> None:
    with connect() as c:
        c.execute("DELETE FROM searches WHERE id = ?", (search_id,))


def mark_search_run(search_id: int) -> None:
    with connect() as c:
        c.execute("UPDATE searches SET last_run_at = ?, baseline_done = 1 WHERE id = ?", (now(), search_id))


# ---------- Inserate & Treffer ----------

def upsert_listing(l: Listing, trust_level: str, trust_reasons: list[str]) -> None:
    seller = asdict(l.seller)
    if seller.get("member_since"):
        seller["member_since"] = l.seller.member_since.isoformat()
    ts = now()
    with connect() as c:
        # Check for price drop before upserting
        existing = c.execute("SELECT price, first_price, trust_reasons FROM listings WHERE key = ?", (l.key,)).fetchone()
        if existing and l.price and existing["price"] and l.price < existing["price"]:
            existing_reasons = json.loads(existing["trust_reasons"]) if existing["trust_reasons"] else []
            drop_msg = f"Preis-Drop: von {existing['price']} auf {l.price} {l.currency}"
            if drop_msg not in existing_reasons:
                existing_reasons.append(drop_msg)

            # Combine new trust reasons with existing ones (avoiding duplicates)
            for r in existing_reasons:
                if r not in trust_reasons:
                    trust_reasons.append(r)

            # Reset AI score to trigger re-evaluation due to price drop
            c.execute("UPDATE listings SET ai_score = NULL, ai_tier = NULL, ai_reason = NULL WHERE key = ?", (l.key,))

        c.execute(
            """INSERT INTO listings (key, platform, external_id, title, url, price, first_price, currency, image,
                                     location, country, posted_at, source, seller, trust_level, trust_reasons,
                                     description_full, first_seen, last_seen)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
               ON CONFLICT(key) DO UPDATE SET
                   title=excluded.title, url=excluded.url, price=excluded.price, currency=excluded.currency,
                   image=COALESCE(excluded.image, listings.image), location=COALESCE(excluded.location, listings.location),
                   seller=excluded.seller, trust_level=excluded.trust_level, trust_reasons=excluded.trust_reasons,
                   description_full=COALESCE(excluded.description_full, listings.description_full),
                   last_seen=excluded.last_seen""",
            (
                l.key, l.platform, l.external_id, l.title, l.url, l.price, l.price, l.currency, l.image,
                l.location, l.country, l.posted_at.isoformat() if l.posted_at else None, l.source,
                json.dumps(seller), trust_level, json.dumps(trust_reasons, ensure_ascii=False),
                l.description_full, ts, ts,
            ),
        )

def update_listing_description(key: str, description: str) -> None:
    with connect() as c:
        c.execute("UPDATE listings SET description_full = ? WHERE key = ?", (description, key))

def update_listing_ai(key: str, relevant: bool, tier: str | None, reason: str | None, score: int | None = None) -> None:
    with connect() as c:
        c.execute(
            "UPDATE listings SET ai_relevant = ?, ai_tier = ?, ai_reason = ?, ai_score = ? WHERE key = ?",
            (int(relevant), tier, reason, score, key)
        )

def update_listing_price(key: str, price: float) -> None:
    with connect() as c:
        c.execute("UPDATE listings SET price = ? WHERE key = ?", (price, key))


def get_ai_progress() -> tuple[int, int]:
    """Returns (evaluated_count, total_count) for AI evaluated listings."""
    with connect() as c:
        row = c.execute(
            "SELECT COUNT(key) as total, SUM(CASE WHEN ai_relevant IS NOT NULL OR ai_score IS NOT NULL THEN 1 ELSE 0 END) as evaluated FROM listings"
        ).fetchone()
        total = row["total"] or 0
        evaluated = row["evaluated"] or 0
        return int(evaluated), int(total)


def add_hit(search_id: int, listing_key: str, is_new: bool) -> bool:
    """Gibt True zurück, wenn der Treffer für diese Suche bisher unbekannt war."""
    with connect() as c:
        cur = c.execute(
            "INSERT OR IGNORE INTO hits (search_id, listing_key, first_seen, is_new, notified) VALUES (?,?,?,?,?)",
            (search_id, listing_key, now(), int(is_new), int(not is_new)),
        )
        return cur.rowcount == 1


def mark_notified(search_id: int, keys: list[str]) -> None:
    with connect() as c:
        c.executemany(
            "UPDATE hits SET notified = 1 WHERE search_id = ? AND listing_key = ?",
            [(search_id, k) for k in keys],
        )


def hits_for_search(search_id: int, include_hidden: bool = False, limit: int = 300) -> list[dict]:
    sql = """SELECT l.*, h.first_seen AS hit_seen, h.is_new, h.hidden, h.search_id
             FROM hits h JOIN listings l ON l.key = h.listing_key
             WHERE h.search_id = ?"""
    if not include_hidden:
        sql += " AND h.hidden = 0"
    sql += " ORDER BY h.first_seen DESC, l.posted_at DESC LIMIT ?"
    with connect() as c:
        return [_listing_row(r) for r in c.execute(sql, (search_id, limit))]


def recent_new_hits(limit: int = 30) -> list[dict]:
    sql = """SELECT l.*, h.first_seen AS hit_seen, h.is_new, h.hidden, s.query AS search_query, s.id AS search_id
             FROM hits h JOIN listings l ON l.key = h.listing_key JOIN searches s ON s.id = h.search_id
             WHERE h.is_new = 1 AND h.hidden = 0
             ORDER BY h.first_seen DESC LIMIT ?"""
    with connect() as c:
        return [_listing_row(r) for r in c.execute(sql, (limit,))]


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
    with connect() as c:
        c.execute("UPDATE hits SET is_new = 0 WHERE search_id = ? AND listing_key = ?",
                  (search_id, listing_key))
    update_listing_ai(listing_key, relevant=False, tier=None, reason="Manuell ausgeblendet", score=None)


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


def mark_platform_blocked(name: str) -> None:
    with connect() as c:
        c.execute("UPDATE platforms SET blocked_at = ? WHERE name = ?", (now(), name))


def record_platform_result(name: str, count: int | None, error: str | None) -> None:
    with connect() as c:
        c.execute(
            "UPDATE platforms SET last_run = ?, last_count = ?, last_error = ? WHERE name = ?",
            (now(), count, error, name),
        )


# ---------- Läufe ----------

def start_run(trigger: str) -> int:
    with connect() as c:
        return int(c.execute("INSERT INTO runs (trigger, started_at) VALUES (?,?)", (trigger, now())).lastrowid)


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
