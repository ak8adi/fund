from datetime import datetime, timedelta, timezone

from fundus.models import Listing, Seller
from fundus.trust import assess, reference_price

FX = {"CHF": 1.0, "EUR": 0.94}


def L(price=100, **kw):
    seller = kw.pop("seller", Seller())
    return Listing(platform="ebay", external_id="1", title=kw.pop("title", "Kite"), url="u", price=price,
                   image=kw.pop("image", "img"), seller=seller, **kw)


def test_good_seller_is_ok():
    r = assess(L(seller=Seller(rating_pct=99.6, rating_count=420)), 100, FX)
    assert r.level == "ok"
    assert "420 Bewertungen" in r.reasons[0]


def test_cheap_new_account_is_warn():
    s = Seller(member_since=datetime.now(timezone.utc) - timedelta(days=4), rating_count=0)
    r = assess(L(price=20, seller=s), 300, FX)
    assert r.level == "warn"
    assert any("Median" in x for x in r.reasons)


def test_no_data_is_neutral():
    r = assess(L(), None, FX)
    assert r.level == "neutral"


def test_whatsapp_in_title_flags():
    r = assess(L(title="iPhone 15 Pro – nur WhatsApp"), None, FX)
    assert r.level == "warn"


def test_reference_price_needs_enough_data():
    assert reference_price([L(price=p) for p in (10, 20)], FX) is None
    assert reference_price([L(price=p) for p in (100, 110, 120, 130, 140)], FX) == 120
