import pytest
from pathlib import Path
from datetime import datetime, timezone
from fundus.adapters.markt import MarktAdapter, parse_price_de
from fundus.adapters.extract import extract_description_from_page
from fundus.config import get_config


@pytest.fixture
def markt_html():
    return (Path(__file__).parent / "fixtures" / "markt_macbook.html").read_text()


def test_markt_parse():
    page = (Path(__file__).parent / "fixtures" / "markt_search.html").read_text()
    listings = MarktAdapter(get_config()).parse(page, "markt", "macbook")

    # Partner-Anzeige und Inserat ohne Suchwort fallen weg
    assert [l.external_id for l in listings] == ["a1b2c3d4"]
    l1 = listings[0]
    assert l1.platform == "markt"
    assert l1.title == "Apple MacBook Pro 15 Zoll"
    assert l1.price == 1450.5
    assert l1.currency == "EUR"
    assert l1.country == "DE"
    assert l1.url == "https://www.markt.de/apple-macbook-pro-15/a/a1b2c3d4/"
    assert l1.image == "https://imagecache.markt.de/abc/260x340/a1b2c3d4/image"
    assert l1.location == "10115 Berlin"
    assert l1.posted_at == datetime(2026, 10, 28, tzinfo=timezone.utc)


def test_markt_no_results_ignores_filler():
    page = "<p>Leider wurden keine Anzeigen gefunden.</p><h2>Die neuesten Anzeigen</h2>" + (
        Path(__file__).parent / "fixtures" / "markt_search.html").read_text()
    assert MarktAdapter(get_config()).parse(page, "markt", "macbook") == []


def test_parse_price_de():
    assert parse_price_de("1.180,00 €") == 1180.0
    assert parse_price_de("500 €") == 500.0
    assert parse_price_de("Zu verschenken") is None


def test_markt_description_extraction(markt_html):
    desc = extract_description_from_page(markt_html, "markt")
    assert desc is not None
    assert "Apple MacBook Pro 15 Zoll" in desc
