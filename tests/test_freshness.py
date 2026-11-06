"""Auktionen, Laufzeitende, Prüfung beim Klick und Ort aus der Ricardo-Suchkarte."""

import json
from datetime import datetime, timedelta, timezone

from fastapi.testclient import TestClient

from fundus import db, runner
from fundus.adapters.base import GoneError
from fundus.adapters.extract import reserved_from_page, status_from_page
from fundus.adapters.scraped import RicardoAdapter
from fundus.config import get_config
from fundus.models import Listing

RICARDO_CARD = {"id": "1333387716", "title": "Duotone Trust Bar 2024", "endDate": "2026-11-07T18:00:00Z",
                "conditionKey": "like_new", "image": "https://img.ricardostatic.ch/images/x/t_265x200/bar",
                "hasBuyNow": True, "bidsCount": 2, "hasAuction": True, "bidPrice": 1400, "buyNowPrice": 1600,
                "shipping": [{"key": "get_by_buyer", "cost": 0, "zipCode": "3008", "city": "Bern"},
                             {"key": "parcel_b_2kg", "cost": 9, "zipCode": "3008", "city": "Bern"}]}


def _page(*cards):
    state = {"state": {"queries": [{"state": {"data": {"articles": list(cards)}}}]}}
    return f'<script id="__NEXT_DATA__" type="application/json">{json.dumps(state)}</script>'


def test_ricardo_card_auction_location_shipping():
    [l] = RicardoAdapter(get_config()).parse(_page(RICARDO_CARD))
    assert l.price == 1600 and l.auction is True and l.bids == 2 and l.bid_price == 1400
    assert l.location == "3008 Bern" and l.shipping == "ja"
    assert l.ends_at == datetime(2026, 11, 7, 18, tzinfo=timezone.utc)
    pickup = {**RICARDO_CARD, "id": "1333387717", "hasAuction": False, "bidPrice": None,
              "shipping": [{"key": "get_by_buyer", "cost": 0, "zipCode": "8808", "city": "Pfäffikon SZ"}]}
    [p] = RicardoAdapter(get_config()).parse(_page(pickup))
    assert p.auction is False and p.bid_price is None and p.shipping == "nein"


def _listing(ext, platform="kleinanzeigen", **kw):
    l = Listing(platform=platform, external_id=ext, title=f"Bar {ext}", url=f"https://x/{ext}", price=100, **kw)
    db.upsert_listing(l, "ok", [])
    return l


def test_ended_listings_expire_without_request():
    past = _listing("1", "ricardo", ends_at=datetime.now(timezone.utc) - timedelta(minutes=5))
    future = _listing("2", "ricardo", ends_at=datetime.now(timezone.utc) + timedelta(days=1))
    assert db.expire_ended_listings() == 1
    assert db.get_listing(past.key)["gone_at"] and not db.get_listing(future.key)["gone_at"]
    _listing("1", "ricardo", ends_at=datetime.now(timezone.utc) + timedelta(days=7))  # verlängert
    assert not db.get_listing(past.key)["gone_at"]


def test_top_hits_to_check_compares_iso_times():
    # Früher: «…T…» gegen «… …» verglichen → Bedingung nie wahr, nichts wurde geprüft
    sid = db.save_search({"query": "bar"})
    l = _listing("9")
    db.add_hit(sid, l.key, False)
    db.update_hit_ai(sid, l.key, True, "Bar")
    with db.connect() as c:
        c.execute("UPDATE listings SET last_seen = ? WHERE key = ?",
                  ((datetime.now(timezone.utc) - timedelta(hours=2)).isoformat(timespec="seconds"), l.key))
    assert [h["key"] for h in db.top_hits_to_check(sid)] == [l.key]
    db.mark_listing_checked(l.key)
    assert db.top_hits_to_check(sid) == []


def test_go_checks_before_opening(monkeypatch):
    from fundus.web import app as webapp
    gone, live, reserved = _listing("20"), _listing("21"), _listing("22")

    def fake_fetch(url, fetcher="http", impersonate="chrome"):
        if url.endswith("/20"):
            raise GoneError("weg")
        return ('<div class="badge-unavailable"><i class="icon icon-reserved-flag-light-gray"></i> Reserviert </div>'
                if url.endswith("/22") else "<html>ok</html>")

    monkeypatch.setattr(runner, "fetch_html", fake_fetch)
    with TestClient(webapp.app) as client:
        r = client.get(f"/go/{gone.key}", follow_redirects=False)
        assert r.status_code == 200 and "nicht mehr verfügbar" in r.text
        assert db.get_listing(gone.key)["gone_at"]
        r = client.get(f"/go/{live.key}", follow_redirects=False)
        assert r.status_code == 302 and r.headers["location"] == live.url
        r = client.get(f"/go/{reserved.key}", follow_redirects=False)
        assert "reserviert" in r.text and db.get_listing(reserved.key)["reserved"] == 1


def test_kleinanzeigen_mobile_status():
    # Ausschnitte der mobilen Inseratseite (November 2026)
    reserved = ('<div class="badge-unavailable"> <i class="icon large icon-reserved-flag-light-gray" '
                'style="margin-right: 8px;"></i> Reserviert </div>')
    deleted = ('<div class="badge-unavailable"> <i class="icon large icon-trash-light-gray"></i> Gelöscht </div>')
    assert status_from_page(reserved) == "reserved"
    assert status_from_page(deleted) == "gone"
    assert status_from_page('<span class="pvap-reserved-title is-hidden">Reserviert • </span>') is None
    assert status_from_page('<span class="pvap-reserved-title" style="color: #797979;">Reserviert • </span>') == "reserved"


def test_shortlist_is_rechecked(monkeypatch):
    from fundus import scoring
    sid = db.save_search({"query": "sensor bar"})
    for i, p in enumerate((300, 340, 365, 420)):
        l = _listing(f"5{i}")
        with db.connect() as c:
            c.execute("UPDATE listings SET price = ? WHERE key = ?", (p, l.key))
        db.add_hit(sid, l.key, False)
        db.update_hit_ai(sid, l.key, True, "Bar", "gut")
    scoring.rescore_search(sid)
    keys = [h["key"] for h in runner.shortlist_to_check(sid)]
    assert "kleinanzeigen:50" in keys
    db.mark_listing_checked("kleinanzeigen:50")
    assert "kleinanzeigen:50" not in [h["key"] for h in runner.shortlist_to_check(sid)]


def test_reserved_markers():
    assert reserved_from_page('{"isReserved": true}')
    assert reserved_from_page('<div class="badge is-reserved">x</div>')
    assert not reserved_from_page('<span class="pvap-reserved-title is-hidden">Reserviert • </span>')
    assert not reserved_from_page("<p>Bitte nicht reservieren</p>")
