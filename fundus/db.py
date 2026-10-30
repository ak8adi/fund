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
    total_chf     REAL,                         -- Endpreis inkl. Versand und Einfuhr-MWST (costs.py)
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

HIT_AI_SELECT = ", ".join(f"h.{col}" for col in (
    "ai_relevant", "ai_reason", "ai_condition", "ai_shipping", "single_price", "ai_year", "ai_shipping_cost", "ai_score",
    "ai_tier", "score_reason", "total_chf"))
HIT_AI_COLS = {"ai_relevant": "INTEGER", "ai_reason": "TEXT", "ai_condition": "TEXT", "ai_shipping": "TEXT",
               "single_price": "REAL", "ai_year": "INTEGER", "ai_shipping_cost": "REAL", "total_chf": "REAL", "ai_score": "INTEGER", "ai_tier": "TEXT", "score_reason": "TEXT"}


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
        if "gone_at" not in l_cols:
            c.execute("ALTER TABLE listings ADD COLUMN gone_at TEXT")  # Inseratseite antwortet 404/410
        if "checked_at" not in l_cols:
            c.execute("ALTER TABLE listings ADD COLUMN checked_at TEXT")  # letzte Verfügbarkeitsprüfung
        _migrate_ai_to_hits(c, l_cols)
        if "market_price" not in cols:
            c.execute("ALTER TABLE searches ADD COLUMN market_price REAL")

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

            # Deal-Score passt sich beim nächsten scoring.rescore_search() an, KI-Merkmale bleiben gültig

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
                   last_seen=excluded.last_seen, gone_at=NULL""",
            (
                l.key, l.platform, l.external_id, l.title, l.url, l.price, l.price, l.currency, l.image,
                l.location, l.country, l.posted_at.isoformat() if l.posted_at else None, l.source,
                json.dumps(seller), trust_level, json.dumps(trust_reasons, ensure_ascii=False),
                l.description_full, ts, ts,
            ),
        )

def mark_listing_gone(key: str) -> None:
    with connect() as c:
        c.execute("UPDATE listings SET gone_at = ?, checked_at = ? WHERE key = ?", (now(), now(), key))


def mark_listing_checked(key: str) -> None:
    with connect() as c:
        c.execute("UPDATE listings SET checked_at = ? WHERE key = ?", (now(), key))


def top_hits_to_check(search_id: int, limit: int = 3) -> list[dict]:
    """Bestbewertete Treffer, die in diesem Lauf nicht in den Suchergebnissen waren und
    seit einem Tag nicht geprüft wurden."""
    sql = """SELECT l.key, l.url, l.platform FROM hits h JOIN listings l ON l.key = h.listing_key
             WHERE h.search_id = ? AND h.hidden = 0 AND h.ai_relevant = 1 AND l.gone_at IS NULL
               AND l.last_seen < datetime('now', '-1 hour')
               AND (l.checked_at IS NULL OR l.checked_at < datetime('now', '-1 day'))
             ORDER BY h.ai_score DESC LIMIT ?"""
    with connect() as c:
        return [dict(r) for r in c.execute(sql, (search_id, limit))]


def update_listing_description(key: str, description: str) -> None:
    with connect() as c:
        c.execute("UPDATE listings SET description_full = ? WHERE key = ?", (description, key))

def update_hit_ai(search_id: int, key: str, relevant: bool, reason: str | None, condition: str | None = None,
                  shipping: str | None = None, single_price: float | None = None, year: int | None = None,
                  shipping_cost: float | None = None) -> None:
    """KI-Merkmale eines Treffers für eine Suche. Der Deal-Score kommt danach aus scoring.rescore_search()."""
    with connect() as c:
        c.execute(
            """UPDATE hits SET ai_relevant = ?, ai_reason = ?, ai_condition = ?, ai_shipping = ?, single_price = ?,
                               ai_year = ?, ai_shipping_cost = ?
               WHERE search_id = ? AND listing_key = ?""",
            (int(relevant), reason, condition, shipping, single_price, year, shipping_cost, search_id, key),
        )


def reset_hit_ai(search_id: int | None, key: str) -> None:
    """Bewertung verwerfen, damit die KI neu prüft. search_id=None: in allen Suchen."""
    sql = """UPDATE hits SET ai_relevant = NULL, ai_reason = NULL, ai_condition = NULL, ai_shipping = NULL,
                             single_price = NULL, ai_year = NULL, ai_shipping_cost = NULL, total_chf = NULL, ai_score = NULL, ai_tier = NULL, score_reason = NULL
             WHERE listing_key = ?"""
    with connect() as c:
        if search_id is None:
            c.execute(sql, (key,))
        else:
            c.execute(sql + " AND search_id = ?", (key, search_id))


def set_hit_relevant(search_id: int, key: str, relevant: bool, reason: str) -> None:
    """Manuelles Ein-/Ausblenden: Zustand, Versand usw. aus der KI bleiben erhalten."""
    with connect() as c:
        c.execute("UPDATE hits SET ai_relevant = ?, ai_reason = ? WHERE search_id = ? AND listing_key = ?",
                  (int(relevant), reason, search_id, key))


def set_scores(search_id: int, market_price: float | None, scores: list[tuple]) -> None:
    """scores: (score, tier, reason, total_chf, key)."""
    with connect() as c:
        c.execute("UPDATE searches SET market_price = ? WHERE id = ?", (market_price, search_id))
        c.executemany(
            "UPDATE hits SET ai_score = ?, ai_tier = ?, score_reason = ?, total_chf = ? WHERE listing_key = ? AND search_id = ?",
            [(*row, search_id) for row in scores],
        )


def get_ai_progress() -> tuple[int, int]:
    """(bewertet, gesamt) über alle sichtbaren Treffer."""
    with connect() as c:
        row = c.execute(
            "SELECT COUNT(*) AS total, SUM(ai_relevant IS NOT NULL) AS evaluated FROM hits WHERE hidden = 0"
        ).fetchone()
        return int(row["evaluated"] or 0), int(row["total"] or 0)


def add_hit(search_id: int, listing_key: str, is_new: bool) -> bool:
    """Gibt True zurück, wenn der Treffer für diese Suche bisher unbekannt war."""
    with connect() as c:
        cur = c.execute(
            "INSERT OR IGNORE INTO hits (search_id, listing_key, first_seen, is_new, notified) VALUES (?,?,?,?,?)",
            (search_id, listing_key, now(), int(is_new), int(not is_new)),
        )
        return cur.rowcount == 1


def pending_top_hits(search_id: int, min_score: int, max_age_days: int = 3) -> list[dict]:
    """Neue, noch nicht gemeldete Top-Angebote einer Suche."""
    sql = f"""SELECT l.*, h.first_seen AS hit_seen, h.is_new, h.hidden, h.search_id, {HIT_AI_SELECT}
              FROM hits h JOIN listings l ON l.key = h.listing_key
              WHERE h.search_id = ? AND h.notified = 0 AND h.hidden = 0 AND h.ai_relevant = 1
                AND h.ai_score >= ? AND l.gone_at IS NULL AND h.first_seen >= datetime('now', ?)
              ORDER BY h.ai_score DESC"""
    with connect() as c:
        return [_listing_row(r) for r in c.execute(sql, (search_id, min_score, f"-{max_age_days} days"))]


def mark_notified(search_id: int, keys: list[str]) -> None:
    with connect() as c:
        c.executemany(
            "UPDATE hits SET notified = 1 WHERE search_id = ? AND listing_key = ?",
            [(search_id, k) for k in keys],
        )


def hits_for_search(search_id: int, include_hidden: bool = False, limit: int = 300) -> list[dict]:
    sql = f"""SELECT l.*, h.first_seen AS hit_seen, h.is_new, h.hidden, h.search_id, {HIT_AI_SELECT}
             FROM hits h JOIN listings l ON l.key = h.listing_key
             WHERE h.search_id = ?"""
    if not include_hidden:
        sql += " AND h.hidden = 0"
    sql += " ORDER BY h.first_seen DESC, l.posted_at DESC LIMIT ?"
    with connect() as c:
        return [_listing_row(r) for r in c.execute(sql, (search_id, limit))]


def best_hits(per_search: int = 1) -> list[dict]:
    """Die besten verfügbaren Angebote jeder aktiven Suche (höchster Deal-Score)."""
    sql = f"""SELECT * FROM (
                SELECT l.*, h.first_seen AS hit_seen, h.is_new, h.hidden, s.query AS search_query, s.id AS search_id,
                       {HIT_AI_SELECT},
                       ROW_NUMBER() OVER (PARTITION BY h.search_id ORDER BY h.ai_score DESC) AS rank
                FROM hits h JOIN listings l ON l.key = h.listing_key JOIN searches s ON s.id = h.search_id
                WHERE s.active = 1 AND h.hidden = 0 AND h.ai_relevant = 1 AND h.ai_score IS NOT NULL
                  AND l.gone_at IS NULL)
             WHERE rank <= ? ORDER BY ai_score DESC"""
    with connect() as c:
        return [_listing_row(r) for r in c.execute(sql, (per_search,))]


def recent_new_hits(limit: int = 30) -> list[dict]:
    sql = f"""SELECT l.*, h.first_seen AS hit_seen, h.is_new, h.hidden, s.query AS search_query, s.id AS search_id,
                    {HIT_AI_SELECT}
             FROM hits h JOIN listings l ON l.key = h.listing_key JOIN searches s ON s.id = h.search_id
             WHERE h.is_new = 1 AND h.hidden = 0 AND (h.ai_relevant IS NULL OR h.ai_relevant = 1) AND l.gone_at IS NULL
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
