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
