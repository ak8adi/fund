import pytest
from pathlib import Path
from datetime import datetime
from fundus.adapters.willhaben import WillhabenAdapter
from fundus.adapters.extract import extract_description_from_page
from fundus.config import get_config


@pytest.fixture
def willhaben_html():
    return (Path(__file__).parent / "fixtures" / "willhaben_macbook.html").read_text()


def test_willhaben_parse(willhaben_html):
    cfg = get_config()
    adapter = WillhabenAdapter(cfg)
    listings = adapter.parse(willhaben_html, "willhaben")

    assert len(listings) == 2, f"Expected 2 organic listings, got {len(listings)}"

    l1 = listings[0]
    assert l1.platform == "willhaben"
    assert l1.external_id == "100000001"
    assert "MacBook Pro 14 M2" in l1.title
    assert l1.price == 1250.0
    assert l1.currency == "EUR"
    assert l1.country == "AT"
    assert l1.url.startswith("https://www.willhaben.at/iad/")
    assert l1.image == "https://willhaben.static.at/100000001_thumb.jpg"
    assert l1.location == "1010 Wien"
    assert isinstance(l1.posted_at, datetime)
    assert l1.seller is not None

    l2 = listings[1]
    assert l2.external_id == "100000003"
    assert l2.price == 550.0
    assert l2.currency == "EUR"
    assert l2.country == "AT"


def test_willhaben_description_extraction(willhaben_html):
    desc = extract_description_from_page(willhaben_html, "willhaben")
    assert desc is not None
    assert "MacBook Pro 14 Zoll M2 Pro Chip" in desc


def test_seo_url_gets_iad_prefix():
    # willhaben liefert SEO_URL ohne /iad – ohne Präfix führt der Link auf 404 (und galt als «verkauft»)
    from fundus.adapters.willhaben import WillhabenAdapter
    from fundus.config import get_config
    w = WillhabenAdapter(get_config())
    d = {"attributes": {"attribute": [{"name": "SEO_URL", "values": ["kaufen-und-verkaufen/d/thule-1/"]}]}}
    assert w.build_url(d, "1") == "https://www.willhaben.at/iad/kaufen-und-verkaufen/d/thule-1/"
    assert w.build_url({"seoUrl": "/iad/kaufen-und-verkaufen/d/thule-1/"}, "1").count("/iad/") == 1
