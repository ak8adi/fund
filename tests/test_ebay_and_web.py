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
