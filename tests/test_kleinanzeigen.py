import pytest
from pathlib import Path
from datetime import datetime, timezone
from fundus.adapters.kleinanzeigen import KleinanzeigenAdapter

@pytest.fixture
def kleinanzeigen_html():
    return (Path(__file__).parent / "fixtures" / "kleinanzeigen_macbook.html").read_text()

def test_kleinanzeigen_parse(kleinanzeigen_html):
    adapter = KleinanzeigenAdapter({"platforms": {"kleinanzeigen": {"url": "https://www.kleinanzeigen.de/s-{query}/k0"}}})
    listings = adapter.parse(kleinanzeigen_html, "kleinanzeigen")

    assert len(listings) > 0, "Should have found some listings"

    # We test that we extract listings correctly. The test file was downloaded fresh, so we just
    # test that things are extracted properly.

    l1 = listings[0]
    assert l1.platform == "kleinanzeigen"
    assert l1.external_id is not None
    assert l1.title is not None
    assert l1.url is not None
    assert l1.currency == "EUR"

    # Verify date parsing isn't just throwing errors
    # It might be `Heute`, `Gestern` or `DD.MM.YYYY`, so we should have a valid datetime
    assert isinstance(l1.posted_at, datetime)
    assert l1.posted_at.tzinfo == timezone.utc

    # Verify seller
    assert l1.seller is not None
    assert isinstance(l1.seller.is_business, bool)
