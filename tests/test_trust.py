from datetime import datetime, timedelta, timezone

from fundus.models import Listing, Seller
from fundus.trust import assess


def L(price=100, **kw):
    seller = kw.pop("seller", Seller())
    return Listing(platform="ebay", external_id="1", title=kw.pop("title", "Kite"), url="u", price=price,
                   image=kw.pop("image", "img"), seller=seller, **kw)


def test_good_seller_is_ok():
    r = assess(L(seller=Seller(rating_pct=99.6, rating_count=420)))
    assert r.level == "ok"
    assert "420 Bewertungen" in r.reasons[0]


def test_new_account_is_warn():
    s = Seller(member_since=datetime.now(timezone.utc) - timedelta(days=4), rating_count=0)
    r = assess(L(price=20, seller=s))
    assert r.level == "warn"
    assert any("Tage alt" in x for x in r.reasons)


def test_no_data_is_neutral():
    r = assess(L())
    assert r.level == "neutral"


def test_whatsapp_in_title_flags():
    r = assess(L(title="iPhone 15 Pro – nur WhatsApp"))
    assert r.level == "warn"
