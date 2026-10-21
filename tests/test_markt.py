import pytest
from pathlib import Path
from datetime import datetime, timezone
from fundus.adapters.markt import MarktAdapter
from fundus.adapters.extract import extract_description_from_page
from fundus.config import get_config


@pytest.fixture
def markt_html():
    return (Path(__file__).parent / "fixtures" / "markt_macbook.html").read_text()


def test_markt_parse(markt_html):
    cfg = get_config()
    adapter = MarktAdapter(cfg)
    listings = adapter.parse(markt_html, "markt")

    assert len(listings) == 2, f"Expected 2 organic listings, got {len(listings)}"

    l1 = listings[0]
    assert l1.platform == "markt"
    assert l1.external_id == "a1b2c3d4"
    assert "Apple MacBook Pro 15 Zoll" in l1.title
    assert l1.price == 450.0
    assert l1.currency == "EUR"
    assert l1.country == "DE"
    assert "markt.de" in l1.url
    assert l1.image == "https://images.markt.de/a1b2c3d4_thumb.jpg"
    assert l1.location == "10115 Berlin"
    assert isinstance(l1.posted_at, datetime)
    assert l1.seller is not None

    l2 = listings[1]
    assert l2.external_id == "e5f6g7h8"
    assert l2.price == 800.0
    assert l2.currency == "EUR"
    assert l2.country == "DE"


def test_markt_description_extraction(markt_html):
    desc = extract_description_from_page(markt_html, "markt")
    assert desc is not None
    assert "Apple MacBook Pro 15 Zoll" in desc
