"""Auktionen: das Gebot ist kein Preis, eigener Bereich, Alarm kurz vor Ablauf."""

from datetime import datetime, timedelta, timezone

from fastapi.testclient import TestClient

from fundus import auctions, db, scoring, views
from fundus.matching import matches_query
from fundus.models import Listing


def _search_with_market(**kw) -> int:
    """Suche mit vier Festpreis-Angeboten um 1000 CHF (Marktpreis 1000)."""
    sid = db.save_search({"query": "macbook pro 16", **kw})
    for i, p in enumerate((900, 1000, 1000, 1100)):
        l = Listing(platform="tutti", external_id=f"f{i}", title=f"MacBook Pro 16 Nr. {i}", url=f"https://x/f{i}", price=p)
        db.upsert_listing(l, "neutral", [])
        db.add_hit(sid, l.key, False)
        db.update_hit_ai(sid, l.key, True, "passt", "gut", "ja")
    return sid


def _auction(sid: int, ext: str, bid: float, ends_in_min: float, price: float | None = None, bids: int = 5) -> str:
    l = Listing(platform="ricardo", external_id=ext, title=f"MacBook Pro 16 Auktion {ext}", url=f"https://r/{ext}",
                price=price or bid, auction=True, bid_price=bid, bids=bids,
                ends_at=datetime.now(timezone.utc) + timedelta(minutes=ends_in_min))
    db.upsert_listing(l, "neutral", [])
    db.add_hit(sid, l.key, False)
    db.update_hit_ai(sid, l.key, True, "passt", "gut", "ja")
    return l.key


def test_bid_is_not_a_price():
    sid = _search_with_market()
    key = _auction(sid, "a1", 200, 3 * 24 * 60)
    assert scoring.rescore_search(sid) == 1000          # das Gebot zieht den Marktpreis nicht nach unten
    d = views.build(sid)
    o = next(o for o in d["offers"] if o["key"] == key)
    assert o["bidding"] and not o["front"] and o["under"] and o["limit"] > 900
    assert d["head"]["used"]["pay"] > 900               # «gebraucht ab» ist ein Festpreis, nicht das Gebot
    assert d["auctions"] == [key] and key not in views.top_keys(d["offers"])
    h = next(h for h in db.hits_for_search(sid) if h["key"] == key)
    assert h["ai_score"] is None and "lohnt sich bis" in h["score_reason"]


def test_auction_with_buy_now_is_in_both_lists():
    sid = _search_with_market()
    key = _auction(sid, "a2", 300, 600, price=800)
    scoring.rescore_search(sid)
    d = views.build(sid)
    o = next(o for o in d["offers"] if o["key"] == key)
    assert not o["bidding"] and o["pay"] > 800 and o["bid_pay"] < 400 and o["front"]   # Sofortkauf 800 schlägt 900
    assert key in d["auctions"]


def test_alarm_before_end(monkeypatch):
    sid = _search_with_market()
    cheap = _auction(sid, "cheap", 300, 50)      # endet in 50 Min., Gebot weit unter dem Wert
    _auction(sid, "dear", 1500, 50)              # Gebot schon über dem Wert
    _auction(sid, "later", 100, 600)             # endet erst in 10 h
    scoring.rescore_search(sid)
    monkeypatch.setattr(auctions, "_refresh", lambda *a, **k: set())
    assert auctions.next_alert_time() is not None
    assert auctions.check() == 1
    assert auctions.check() == 0                 # jede Auktion nur einmal
    with TestClient(__import__("fundus.web.app", fromlist=["app"]).app) as client:
        first = client.get("/api/app/notices?after=0").json()
        urgent = [n for n in first["notices"] if n.get("urgent")]
        assert len(urgent) == 1 and "Auktion endet" in urgent[0]["title"] and "lohnt sich bis" in urgent[0]["title"]
        assert urgent[0]["url"] == "https://r/cheap"   # ohne public_url direkt zum Inserat
        again = client.get(f"/api/app/notices?after={first['last_id']}").json()
        assert not [n for n in again["notices"] if n.get("urgent")]      # genau einmal zugestellt
        # die App soll rechtzeitig vor der nächsten Auktion wieder fragen, nicht erst nach dem nächsten Scan
        nxt = datetime.fromisoformat(again["next_check"])
        assert nxt - datetime.now(timezone.utc) < timedelta(hours=10)


def test_alarm_uses_fresh_bid(monkeypatch):
    sid = _search_with_market()
    key = _auction(sid, "risen", 300, 40)
    scoring.rescore_search(sid)

    def risen(adapters, search, hits, blocked):   # beim Nachsehen steht das Gebot schon bei 1400
        db.update_auction(key, 1400, 30)
        return set()
    monkeypatch.setattr(auctions, "_refresh", risen)
    assert auctions.check() == 0
    assert not db.take_urgent_notices()


def test_ntfy_disabled_keeps_app_notices(monkeypatch):
    from fundus import notify
    from fundus.config import get_config
    cfg = get_config()
    cfg["ntfy"].update(topic="t", enabled=False)
    sent = []
    monkeypatch.setattr(notify.httpx, "post", lambda *a, **k: sent.append(a))
    assert notify.send_test(cfg) is False and not sent
    sid = _search_with_market()
    l = Listing(platform="tutti", external_id="top", title="MacBook Pro 16 Schnäppchen", url="https://x/top", price=500)
    db.upsert_listing(l, "neutral", [])
    db.add_hit(sid, l.key, True)
    db.update_hit_ai(sid, l.key, True, "passt", "gut", "ja")
    scoring.rescore_search(sid)
    assert notify.notify_top_hits(sid) == [l.key]
    assert not sent and db.app_notices_after(0)          # nichts an ntfy, aber die Meldung liegt für die App bereit


def test_specs_are_not_model_numbers():
    assert not matches_query("Mac book pro 16 m", 'Apple MacBook Pro 14 Zoll M5 16GB RAM 512GB SSD')
    assert not matches_query("Mac book pro 16 m", 'APPLE MacBook Pro 14", M1 Pro, 16 GB RAM, 512 GB SSD')
    assert matches_query("Mac book pro 16 m", 'MacBook Pro 16" M1 Pro, 16 GB/512 GB')
    assert matches_query("Mac book pro 16 m", "Apple MacBook Pro 16 2TB")                 # «mac book» = «MacBook»
    assert matches_query("MacBook Air 15 16GB", "MacBook Air 15 zoll 16/512 GB (100% Akku)")
    assert matches_query("MacBook Air 15 16GB", "Macbook Air M3, 15 Zoll, 256 GB")        # SSD-Grösse ≠ RAM
    assert not matches_query("MacBook Air 15 16GB", 'Apple MacBook Air 15" M3 | 8GB RAM | 512GB SSD')


def test_price_range_applies_to_existing_hits():
    sid = _search_with_market()
    db.save_search({**db.get_search(sid), "max_price": 950}, sid)
    scoring.rescore_search(sid)
    d = views.build(sid)
    assert d["counts"]["open"] == 1 and all(o["pay"] < 1000 for o in d["offers"])


def test_expired_alarm_is_never_delivered():
    """Fragt die App erst nach dem Ende der Auktion, kommt keine Meldung mehr (11.11.2026: Alarm 1 h zu spät)."""
    past = (datetime.now(timezone.utc) - timedelta(minutes=5)).isoformat(timespec="seconds")
    soon = (datetime.now(timezone.utc) + timedelta(minutes=30)).isoformat(timespec="seconds")
    db.add_app_notice(1, "vorbei", "x", None, urgent=True, expires_at=past)
    db.add_app_notice(1, "läuft", "x", None, urgent=True, expires_at=soon)
    assert [n["title"] for n in db.take_urgent_notices()] == ["läuft"]
    assert db.take_urgent_notices() == []


def test_alarm_title_has_absolute_time(monkeypatch):
    sid = _search_with_market()
    _auction(sid, "t", 300, 50)
    scoring.rescore_search(sid)
    monkeypatch.setattr(auctions, "_refresh", lambda *a, **k: set())
    assert auctions.check() == 1
    n = db.take_urgent_notices()[0]
    assert __import__("re").search(r"Auktion endet \d\d:\d\d –", n["title"]) and n["expires_at"]
