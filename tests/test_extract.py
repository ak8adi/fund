import json

from fundus.adapters.extract import extract_listings, parse_price
from fundus.adapters.scraped import RicardoAdapter, TuttiAdapter
from fundus.config import get_config


def page_with(next_data: dict, extra: str = "") -> str:
    return (
        "<html><head><script>var x = 1;</script></head><body>"
        f'<script id="__NEXT_DATA__" type="application/json">{json.dumps(next_data)}</script>{extra}</body></html>'
    )


def test_parse_price_variants():
    assert parse_price(120) == (120.0, None)
    assert parse_price("CHF 1'200.50") == (1200.5, "CHF")
    assert parse_price("85.–") == (85.0, None)
    assert parse_price("Gratis") == (None, None)
    assert parse_price({"value": "49.90", "currency": "EUR"}) == (49.9, "EUR")
    assert parse_price({"amount": 300}) == (300.0, None)


def test_tutti_like_structure():
    data = {
        "props": {"pageProps": {"dehydratedState": {"queries": [{"state": {"data": {"listings": {"edges": [
            {"node": {
                "listingID": "71234567",
                "title": "Kite North Orbit 9m",
                "formattedPrice": "650.–",
                "timestamp": "2026-10-10T08:15:00+02:00",
                "postcodeInformation": {"postcode": "8400", "locationName": "Winterthur", "canton": {"shortName": "ZH"}},
                "thumbnail": {"normalRendition": {"src": "https://c.tutti.ch/images/1.jpg"}},
                "sellerInfo": {"alias": "kitefan"},
                "seoInformation": {"deSlug": "zuerich/sport/kite-north-orbit-9m"},
            }},
            {"node": {"listingID": "71234568", "title": "Trapez Gr. M", "formattedPrice": "Gratis",
                      "seoInformation": {"deSlug": "zuerich/sport/trapez"}}},
        ]}}}}]}}},
        "categories": [{"id": "1", "name": "Sport", "url": "/de/c/sport"}],
    }
    listings = TuttiAdapter(get_config()).parse(page_with(data))
    assert {l.external_id for l in listings} == {"71234567", "71234568"}
    kite = next(l for l in listings if l.external_id == "71234567")
    assert kite.price == 650.0
    assert kite.url == "https://www.tutti.ch/de/vi/zuerich/sport/kite-north-orbit-9m/71234567"
    assert kite.location == "8400 Winterthur ZH"
    assert kite.image == "https://c.tutti.ch/images/1.jpg"
    assert kite.seller.name == "kitefan"
    assert kite.posted_at is not None


def test_tutti_title_in_localization():
    """Stand Okt. 2026: Tutti/Anibis liefern den Titel nur noch unter localization.title."""
    data = {
        "props": {"pageProps": {"dehydratedState": {"queries": [{"state": {"data": {"listings": {"edges": [
            {"node": {
                "listingID": "82625814",
                "localization": {"title": "Kinderwagen Hartan", "body": "Gut erhalten", "translationStatus": "ORIGINAL"},
                "formattedPrice": "1'200.-",
                "timestamp": "2026-10-30T09:07:26+01:00",
                "postcodeInformation": {"postcode": "8400", "locationName": "Winterthur", "canton": {"shortName": "ZH"}},
                "sellerInfo": {"alias": "Janet"},
                "seoInformation": {"deSlug": "zuerich/baby-kind/kinderwagen/kinderwagen-hartan"},
            }},
        ]}}}}]}}},
    }
    listings = TuttiAdapter(get_config()).parse(page_with(data))
    assert len(listings) == 1
    l = listings[0]
    assert l.title == "Kinderwagen Hartan"
    assert l.price == 1200.0
    assert l.url == "https://www.tutti.ch/de/vi/zuerich/baby-kind/kinderwagen/kinderwagen-hartan/82625814"


def test_ricardo_like_structure_ignores_navigation():
    data = {"props": {"pageProps": {"initialState": {"srp": {"results": [
        {"id": 1290000001, "title": "MacBook Air M2 16GB", "buyNowPrice": 790, "bidPrice": None,
         "image": "https://img.ricardo.ch/a.jpg", "sellerId": 55},
        {"id": 1290000002, "title": "Eames Plastic Chair", "bidPrice": 120.0},
    ]}, "menu": [{"id": 7, "name": "Elektronik"}]}}}}
    listings = RicardoAdapter(get_config()).parse(page_with(data))
    assert len(listings) == 2
    mac = next(l for l in listings if l.title.startswith("MacBook"))
    assert mac.url == "https://www.ricardo.ch/de/a/1290000001/"
    assert mac.price == 790.0


def test_ld_json_itemlist():
    ld = {"@context": "https://schema.org", "@type": "ItemList", "itemListElement": [
        {"@type": "ListItem", "position": 1, "item": {
            "@type": "Product", "name": "Sofa Vitra", "url": "https://www.anibis.ch/de/vi/sofa/55512345",
            "image": ["https://img/x.jpg"], "offers": {"price": "900", "priceCurrency": "CHF"}}},
    ]}
    page = f'<script type="application/ld+json">{json.dumps(ld)}</script>'
    listings = extract_listings(page, "anibis", "https://www.anibis.ch", url_pattern=r"/vi/")
    assert len(listings) == 1
    assert listings[0].external_id == "55512345"
    assert listings[0].price == 900.0
    assert listings[0].image == "https://img/x.jpg"


def test_ricardo_next_app_router_flight_data():
    # Next.js App Router: Daten in self.__next_f.push([1, "id:JSON\n"]); eine Zeile über zwei Chunks verteilt
    row = "5:" + json.dumps({"state": {"articles": [{
        "id": "1332672514", "title": "Eames Fiberglass Chair", "buyNowPrice": 450,
        "creationDate": "2026-10-17T10:20:00Z", "image": "https://img.ricardostatic.ch/x.jpg"}]}})
    half = len(row) // 2
    chunks = [row[:half], row[half:] + "\n"]
    page = "".join(f"<script>self.__next_f.push({json.dumps([1, c])})</script>" for c in chunks)
    items = RicardoAdapter(get_config()).parse(page)
    assert len(items) == 1
    assert items[0].url == "https://www.ricardo.ch/de/a/1332672514/"
    assert items[0].price == 450


def test_matches_query():
    from fundus.matching import matches_query
    assert matches_query("core sensor pro 4", "Core Sensor4 Pro Bar")              # Zahl am Wort klebt
    assert matches_query("thule urban glide 3", "Urban Glide 3 Kinderwagen")       # Marke fehlt: erlaubt
    assert not matches_query("thule urban glide 3", "Thule Urban Glide 2")         # Modellnummer muss passen
    assert not matches_query("core sensor pro 3", "APPLE MacBook Pro Touch Bar 2019 Core i7")
    assert not matches_query("duotone trust bar", "Trust Company - The Lonely Position (CD)")
    assert not matches_query("kinderwagen", "Sandalen")


def test_redirect_to_expired_notice_counts_as_gone(monkeypatch):
    """Kleinanzeigen leitet gelöschte Inserate mit 200 auf eine Kategorieseite um statt 404."""
    import pytest
    from fundus.adapters import base

    class Resp:
        status_code = 200
        text = '<div id="srchrslt-adexpired">Die gewünschte Anzeige ist nicht mehr verfügbar.</div>' + "x" * 40000

    monkeypatch.setattr(base, "CURL_AVAILABLE", True)
    monkeypatch.setattr(base.curl_requests, "get", lambda *a, **k: Resp())
    with pytest.raises(base.GoneError):
        base.fetch_html("https://www.kleinanzeigen.de/s-anzeige/x/1-230-1")


def test_normal_search_page_is_not_gone():
    from pathlib import Path
    from fundus.adapters.base import _check_gone

    _check_gone((Path(__file__).parent / "fixtures" / "kleinanzeigen_macbook.html").read_text(), "u")
