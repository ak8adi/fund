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
    # Ricardo, November 2026: Chip fehlt im Titel – die KI entscheidet; andere Grösse/Chip/Generation fliegt raus
    assert matches_query("MacBook Pro 16 M1", "MacBook Pro 16 inch 2021")
    assert matches_query("MacBook Pro 16 M1", "16” M1 MAX 32GB 1TB")
    assert not matches_query("MacBook Pro 16 M1", "MacBook Pro 14 M1 Pro 1TB ***in super Zustand inkl. OVP")
    assert not matches_query("MacBook Pro 16 M1", "Apple MacBook Pro 16 M2 Pro 2023")
    assert not matches_query("MacBook Pro 16 M1", "MacBook Pro 13\"")
    assert not matches_query("core sensor pro 4", "Core Sensor 3 Pro Bar")
    assert matches_query("MacBook Air 15 16GB", "MacBook Air 15 M3 16 GB 512")
    assert not matches_query("MacBook Air 15 16GB", "MacBook Air 15 M3 8GB 256GB")


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


def test_new_price_offer_from_json_ld():
    from fundus.newprice import extract_offer
    page = """<script type="application/ld+json">{"@context":"http://schema.org","@graph":[{"@type":"Product",
      "name":"Core Sensor 4 Pro Bar","offers":[{"@type":"Offer","price":"765.00","priceCurrency":"CHF",
      "itemCondition":"https://schema.org/NewCondition"},{"@type":"Offer","price":"749","priceCurrency":"CHF"}]}]}</script>"""
    assert extract_offer(page) == {"name": "Core Sensor 4 Pro Bar", "price": 749.0, "currency": "CHF", "condition": "",
                                   "available": None}
    meta = '<meta property="og:title" content="Trust Bar"><meta property="product:price:amount" content="549.90">' \
           '<meta property="product:price:currency" content="chf">'
    assert extract_offer(meta)["price"] == 549.9 and extract_offer(meta)["currency"] == "CHF"
    assert extract_offer("<html>kein Produkt</html>") is None


def test_large_image_variants():
    from fundus.notify import image_url
    assert image_url("https://c.tutti.ch/thumbnail/1.jpg") == "https://c.tutti.ch/big/1.jpg"
    assert image_url("https://img.kleinanzeigen.de/api/v1/prod-ads/images/7/x?rule=$_2.AUTO").endswith("rule=$_59.AUTO")
    assert "/t_1000x750/" in image_url("https://img.ricardostatic.ch/images/a/t_265x200/b")
    assert image_url("https://cache.willhaben.at/mmo/1_hoved.jpg") == "https://cache.willhaben.at/mmo/1.jpg"


def test_newprice_cheapest_matching_offer(monkeypatch):
    from fundus import ai, newprice
    offers = [
        {"shop": "a.ch", "name": "Thule Urban Glide 3", "price": 711, "currency": "CHF", "price_chf": 711.0, "url": "u1", "condition": ""},
        {"shop": "b.ch", "name": "Thule Urban Glide 3 Single", "price": 655.25, "currency": "CHF", "price_chf": 655.25, "url": "u2", "condition": ""},
        {"shop": "b.ch", "name": "Thule Urban Glide 3 Single", "price": 655.25, "currency": "CHF", "price_chf": 655.25, "url": "u3", "condition": ""},
        {"shop": "c.ch", "name": "Thule Regenverdeck", "price": 49, "currency": "CHF", "price_chf": 49.0, "url": "u4", "condition": ""},
    ]
    monkeypatch.setattr(newprice, "get_config", lambda: {"ai": {"gemini_api_key": "x"}})
    monkeypatch.setattr(newprice, "find_offers", lambda q, skip=None: offers if skip is None else [])
    monkeypatch.setattr(ai, "_gemini_json", lambda *a, **k: {"match": [0, 1, 2], "product": "Thule Urban Glide 3", "size": "XL"})
    r = newprice.lookup("thule urban glide 3")
    assert r["price"] == 655  # günstigstes passendes, nicht das Zubehör für 49
    assert r["note"] == "Thule Urban Glide 3 · günstigster lieferbar bei b.ch (von 2 Shops)"
    assert [o["url"] for o in r["offers"]] == ["u2", "u1"]


def test_large_image_variants_ebay_blocket():
    from fundus.notify import image_url
    assert image_url("https://i.ebayimg.com/images/g/x/s-l225.jpg").endswith("/s-l1600.jpg")
    assert image_url("https://i.ebayimg.com/images/g/x/s-l225.jpg", large=False).endswith("/s-l500.jpg")
    assert "/dynamic/960w/" in image_url("https://images.blocketcdn.se/dynamic/default/item/1/a")


def test_new_price_availability_and_product_pages():
    from fundus.newprice import extract_offer
    def ld(*products):
        import json
        return '<script type="application/ld+json">' + json.dumps({"@graph": list(products)}) + "</script>"
    bar = {"@type": "Product", "name": "Trust Bar", "offers": [
        {"price": "299", "priceCurrency": "CHF", "availability": "https://schema.org/OutOfStock", "url": "https://s.ch/a"},
        {"price": "349", "priceCurrency": "CHF", "availability": "https://schema.org/InStock", "url": "https://s.ch/b"}]}
    # Variante der Seite (URL) hat Vorrang, sonst günstigste lieferbare
    assert extract_offer(ld(bar), "https://s.ch/a")["available"] is False
    assert extract_offer(ld(bar), "https://s.ch/x")["price"] == 349.0
    # Kategorie-/Trefferseite mit vielen Produkten ist keine Produktseite
    many = [{"@type": "Product", "name": f"Bar {i}", "offers": {"price": str(100 + i), "priceCurrency": "CHF"}} for i in range(5)]
    assert extract_offer(ld(*many), "https://s.ch/kategorie") is None


def test_newprice_prefers_in_stock(monkeypatch):
    from fundus import ai, newprice
    offers = [
        {"name": "Trust Bar", "price": 299, "currency": "CHF", "condition": "", "available": False, "url": "https://a.ch/1",
         "shop": "a.ch", "price_chf": 299},
        {"name": "Trust Bar", "price": 349, "currency": "CHF", "condition": "", "available": True, "url": "https://b.ch/1",
         "shop": "b.ch", "price_chf": 349},
    ]
    monkeypatch.setattr(newprice, "get_config", lambda: {"ai": {"gemini_api_key": "x"}})
    monkeypatch.setattr(newprice, "find_offers", lambda q, skip=None, compare=False: [] if skip or compare else offers)
    monkeypatch.setattr(ai, "_gemini_json", lambda *a, **k: {"match": [0, 1], "product": "Trust Bar"})
    r = newprice.lookup("trust bar")
    assert r["price"] == 349 and "ausverkauft ab 299 CHF bei a.ch" in r["note"]
    assert [o["available"] for o in r["offers"]] == [True, False]


def test_newprice_drops_fake_shop_prices():
    from fundus.newprice import plausible
    offers = [{"price_chf": p} for p in (156, 541, 688, 764, 765)]
    assert [o["price_chf"] for o in plausible(offers, offers)] == [541, 688, 764, 765]
