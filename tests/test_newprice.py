"""Neupreis: Preisvergleiche, Auslaufmodell mit Nachfolger, Urteil «lohnt sich gebraucht?», MWST separat."""

from pathlib import Path

from fundus import delivery, newprice, views

FIX = Path(__file__).parent / "fixtures"


def test_parse_toppreise_search():
    rows = newprice.parse_toppreise((FIX / "toppreise_search.html").read_text())
    assert len(rows) == 3
    r = rows[0]
    assert r["name"] == "THULE Urban Glide 3 Babywanne, Beige" and r["currency"] == "CHF"
    assert r["price"] == 238.0 and r["shipping"] == 2.95 and r["count"] == 5 and r["available"] is True
    assert r["url"].startswith("https://www.toppreise.ch/preisvergleich/")


def test_parse_geizhals_search():
    rows = newprice.parse_geizhals((FIX / "geizhals_search.html").read_text())
    assert rows[0] == {"name": "Thule Urban Glide 3", "price": 692.0, "currency": "EUR", "condition": "",
                       "available": True, "count": 9, "shop": "geizhals.de",
                       "url": "https://geizhals.de/thule-urban-glide-3-2024-v183265.html"}
    assert newprice._num("4'017.95") == 4017.95 and newprice._num("1.692,00") == 1692.0


def _offer(shop, name, price, **kw):
    return {"shop": shop, "name": name, "price": price, "currency": "CHF", "price_chf": price, "url": f"https://{shop}/{name}",
            "condition": "", "available": True, **kw}


def _lookup(monkeypatch, offers, answer, query="macbook pro 16 m1", **kw):
    from fundus import ai
    monkeypatch.setattr(newprice, "get_config", lambda: {"ai": {"gemini_api_key": "x"}})
    monkeypatch.setattr(newprice, "compare_offers", lambda q: offers)
    monkeypatch.setattr(newprice, "find_offers", lambda q, skip=None, compare=False: [])
    monkeypatch.setattr(ai, "_gemini_json", lambda *a, **k: answer)
    return newprice.lookup(query, **kw)


def test_discontinued_product_gets_successor_not_a_wrong_new_price(monkeypatch):
    offers = [_offer("toppreise.ch", "MacBook Pro 16 CTO (Late 2021) M1 Pro 8 TB", 4017.95, count=1),
              _offer("toppreise.ch", "MacBook Pro 16 (Early 2026) M5 Pro", 2319.85, count=70),
              _offer("revendo.ch", "MacBook Pro 16 M1 Pro erneuert", 1004.0)]
    r = _lookup(monkeypatch, offers, {"match": [], "successor": [1], "refurb": [2], "discontinued": True,
                                      "successor_name": "MacBook Pro 16 M5 Pro", "product": "MacBook Pro 16 M1"})
    assert r["price"] is None and r["note"].startswith("Nicht mehr neu erhältlich")
    assert "Nachfolger MacBook Pro 16 M5 Pro neu ab 2320 CHF" in r["note"]
    assert [o["kind"] for o in r["offers"]] == ["erneuert", "nachfolger"]


def test_exclude_words_apply_to_shop_offers(monkeypatch):
    offers = [_offer("geizhals.de", "Thule Urban Glide 3 double schwarz", 500.0),
              _offer("toppreise.ch", "THULE Urban Glide 3 Single", 602.0, count=7)]
    r = _lookup(monkeypatch, offers, {"match": [0], "product": "Thule Urban Glide 3"}, exclude=["double"])
    assert r["price"] == 602 and len(r["offers"]) == 1     # Nr. 0 ist nach dem Ausschluss das Single


def test_verdict_levels():
    best = {"pay": 600}
    assert views.verdict(best, {"total": 1000, "shop": "x"}, None, None)["level"] == "klar"
    assert views.verdict(best, {"total": 800, "shop": "x"}, None, None)["level"] == "ja"
    assert views.verdict(best, {"total": 700, "shop": "x"}, None, None)["level"] == "knapp"
    v = views.verdict(best, {"total": 640, "shop": "x"}, {"total": 650, "shop": "revendo.ch"}, None)
    assert v["level"] == "nein" and "40 CHF mehr" in v["text"] and "revendo.ch" in v["note"]
    assert views.verdict(best, {"total": 590, "shop": "x"}, None, None)["level"] == "nein"
    v = views.verdict(best, None, None, {"total": 2400, "shop": "toppreise.ch"})
    assert v["level"] == "info" and v["against"] == "nachfolger" and "75 %" in v["text"] and "2400" in v["text"]
    assert views.verdict(None, {"total": 1000}, None, None) is None


def test_import_vat_is_shown_but_not_in_total():
    cfg = {"home_country": "CH", "addresses": ["CH", "DE"], "forwarder": "Paket4you", "forwarder_address": ""}
    fx = {"EUR": 0.94, "CHF": 1.0}
    p = delivery.plan({"price": 1000, "currency": "EUR", "platform": "kleinanzeigen", "ai_shipping": "ja"}, fx, cfg)
    goods = dict(p.parts)["Preis"] + dict(p.parts)["Versand"]
    assert p.import_vat == round(goods * 0.081, 2)
    assert p.total == round(sum(v for _, v in p.parts), 2)          # MWST nicht eingerechnet
    cheap = delivery.plan({"price": 100, "currency": "EUR", "platform": "kleinanzeigen", "ai_shipping": "ja"}, fx, cfg)
    assert cheap.import_vat is None                                  # unter der Wertfreigrenze von 150 CHF
    home = delivery.plan({"price": 1000, "currency": "CHF", "platform": "tutti", "ai_shipping": "ja"}, fx, cfg)
    assert home.import_vat is None


def test_named_size_sets_price_but_all_shops_stay_listed(monkeypatch):
    offers = [_offer("duotonesports.com", "Rebel SLS 2026", 1300.0),            # «ab»-Preis, kleinste Grösse
              _offer("geizhals.de", "Duotone Rebel SLS 2025 Kite 8qm", 1436.0),
              _offer("kitesailing.ch", "Duotone Kite Rebel SLS 2026", 1665.0)]
    r = _lookup(monkeypatch, offers, {"match": [0, 1, 2], "product": "Rebel SLS 8"},
                query="duotone rebel sls 8")
    assert r["price"] == 1436 and len(r["offers"]) == 3
    assert [o["approx"] for o in sorted(r["offers"], key=lambda o: o["price"])] == [True, False, True]
    options = [{**o, "country": "DE", "total": o["price"]} for o in sorted(r["offers"], key=lambda o: o["price"])]
    assert newprice.best_new(options)["shop"] == "geizhals.de"                   # nicht der tiefere ab-Preis


def test_manual_shop_is_remembered():
    from fundus import db
    sid = db.save_search({"query": "rebel"})
    assert not newprice.add_shop(sid, "kein link")
    assert newprice.add_shop(sid, "https://www.you-love-it.eu/rebel-sls")
    assert newprice.extra_shops(sid) == ["https://www.you-love-it.eu/rebel-sls"]


def test_special_versions_are_not_the_new_price(monkeypatch):
    offers = [_offer("surfer.shop", "Core Sensor 3 Pro Foil Control Bar", 564.0),
              _offer("surfer.shop", "Core Sensor 3 Pro Control Bar", 686.0)]
    r = _lookup(monkeypatch, offers, {"match": [0], "product": "Core Sensor 3 Pro"})
    assert r["price"] == 686 and len(r["offers"]) == 1        # die Foil-Bar kommt gar nicht erst zur KI


def test_model_number_is_not_a_size(monkeypatch):
    offers = [_offer("surfer.shop", "Core Sensor 3 Pro Control Bar", 683.0),
              _offer("kite-shop.ch", "Core SENSOR 3 Pro Bar (52cm/18-24m)", 799.0)]
    r = _lookup(monkeypatch, offers, {"match": [0, 1], "exact_size": [1], "product": "Core Sensor 3 Pro"},
                query="core sensor pro 3")
    assert r["price"] == 683 and not any(o["approx"] for o in r["offers"])


def test_all_prices_marked_when_size_is_only_known_elsewhere(monkeypatch):
    offers = [_offer("duotonesports.com", "Rebel SLS 2026", 1316.0), _offer("windsport.de", "Duotone Kite Rebel SLS 2026", 1337.0),
              _offer("surfpirates.de", "DUOTONE Rebel SLS 2026 DEMO 8.0m", 1236.0)]
    r = _lookup(monkeypatch, offers, {"match": [0, 1], "refurb": [2], "product": "Rebel SLS 8"}, query="duotone rebel sls 8")
    assert r["price"] == 1316 and "ab-Preis" in r["note"]
    assert all(o["approx"] for o in r["offers"] if o["kind"] == "neu")
