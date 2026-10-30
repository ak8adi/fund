from fastapi.testclient import TestClient

from fundus import db
from fundus.adapters.ebay import EbayAdapter


def test_ebay_item_mapping():
    item = {
        "itemId": "v1|1234|0", "title": "Duotone Neo 10m", "itemWebUrl": "https://www.ebay.de/itm/1234",
        "price": {"value": "480.00", "currency": "EUR"}, "image": {"imageUrl": "https://i.ebayimg.com/1.jpg"},
        "seller": {"username": "surfshop", "feedbackPercentage": "99.8", "feedbackScore": 1520},
        "itemLocation": {"postalCode": "24*", "city": "Kiel", "country": "DE"},
        "itemCreationDate": "2026-10-10T10:00:00.000Z",
    }
    l = EbayAdapter._to_listing(item, "EBAY_DE")
    assert l.price == 480.0 and l.currency == "EUR"
    assert l.seller.rating_count == 1520 and l.seller.rating_pct == 99.8
    assert l.country == "DE" and l.source == "EBAY_DE"


def test_ebay_needs_keys():
    from fundus.config import get_config
    ok, hint = EbayAdapter(get_config()).configured()
    assert not ok and "eBay" in hint


def test_web_pages_render(monkeypatch):
    from fundus import runner
    from fundus.web import app as webapp

    monkeypatch.setattr(runner, "run_in_background", lambda *a, **k: True)
    with TestClient(webapp.app) as client:
        assert client.get("/").status_code == 200
        r = client.post("/searches", data={"query": "kite 9m", "max_price": "800", "platforms": ["tutti", "ebay"]},
                        follow_redirects=False)
        assert r.status_code == 303
        sid = int(r.headers["location"].rsplit("/", 1)[1])
        assert db.get_search(sid)["platforms"] == ["tutti", "ebay"]
        assert "kite 9m" in client.get(f"/search/{sid}").text
        page = client.get("/settings").text
        assert "Tutti" in page and "eBay DE" in page
        assert client.post("/platforms/tutti/toggle").status_code == 200
        assert db.platform_states()["tutti"]["enabled"] == 0


def test_requirements_and_price_sort(monkeypatch):
    from fundus import ai, runner
    from fundus.models import Listing
    from fundus.web import app as webapp

    monkeypatch.setattr(runner, "run_in_background", lambda *a, **k: True)
    started = []
    monkeypatch.setattr(ai, "start_background_evaluation", lambda sid: started.append(sid))
    with TestClient(webapp.app) as client:
        r = client.post("/searches", data={"query": "tisch", "requirements": "130 bis 150 cm lang",
                                           "require_shipping": "1"}, follow_redirects=False)
        sid = int(r.headers["location"].rsplit("/", 1)[1])
        s = db.get_search(sid)
        assert s["requirements"] == "130 bis 150 cm lang" and s["require_shipping"] == 1

        for i, price in enumerate((300, 100, 200)):
            l = Listing(platform="tutti", external_id=str(i), title=f"Tisch {i}", url=f"https://x/{i}", price=price,
                        currency="CHF")
            db.upsert_listing(l, "ok", [])
            db.add_hit(sid, l.key, False)
        db.update_hit_ai(sid, "tutti:0", True, "x", "gut", req="ja", req_note="Länge 140 cm")

        client.post(f"/search/{sid}/sort", data={"by": "price"})
        page = client.get(f"/search/{sid}").text
        assert page.index("Tisch 1") < page.index("Tisch 2") < page.index("Tisch 0")
        assert "✓ Länge 140 cm" in page

        # Geänderte Anforderungen: Bewertungen verworfen, KI startet neu
        client.post(f"/search/{sid}", data={"query": "tisch", "requirements": "nur rund"})
        assert db.hits_for_search(sid)[0]["ai_relevant"] is None and started == [sid]


def test_store_facts_requirement_not_met():
    from fundus import ai
    sid = db.save_search({"query": "tisch", "requirements": "130-150 cm"})
    from fundus.models import Listing
    l = Listing(platform="tutti", external_id="1", title="Tisch", url="https://x/1", price=100, currency="CHF")
    db.upsert_listing(l, "ok", [])
    db.add_hit(sid, l.key, False)
    ai._store_facts(db.get_search(sid), {"key": l.key, "relevant": True, "req": "nein", "req_note": "180 cm"})
    h = db.hits_for_search(sid)[0]
    assert h["ai_relevant"] == 0 and h["ai_req"] == "nein" and h["ai_req_note"] == "180 cm"
