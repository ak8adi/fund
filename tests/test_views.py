"""Entwürfe: K.-o.-Regeln und Pareto-Front."""

from fastapi.testclient import TestClient

from fundus import db, views
from fundus.models import Listing


def _o(pay, value, ko=None):
    return {"pay": pay, "value": value, "ko": ko, "front": False}


def test_pareto_front():
    cheap, mid, dominated, best, lure = _o(100, 150), _o(200, 300), _o(250, 280), _o(400, 420), _o(20, 900, "unrealistisch billig")
    offers = [cheap, mid, dominated, best, lure]
    views.pareto(offers)
    assert [o["front"] for o in offers] == [True, True, False, True, False]


def test_ko_reasons():
    assert views.ko_reason({}, 30, 100) == "unrealistisch billig"
    assert views.ko_reason({"ai_req": "nein"}, 90, 100) == "Anforderung nicht erfüllt"
    assert views.ko_reason({"reserved": 1}, 90, 100) == "reserviert"
    assert views.ko_reason({}, 90, 100) is None


def test_drafts_page_renders():
    from fundus.web import app as webapp
    sid = db.save_search({"query": "trust bar"})
    for i, p in enumerate((200, 250, 300, 350)):
        l = Listing(platform="tutti", external_id=str(i), title=f"Trust Bar {i}", url=f"https://x/{i}", price=p)
        db.upsert_listing(l, "ok", [])
        db.add_hit(sid, l.key, False)
        db.update_hit_ai(sid, l.key, True, "Bar", "gut")
    from fundus import scoring
    scoring.rescore_search(sid)
    d = views.build(sid)
    assert d["counts"]["open"] == 4 and d["counts"]["front"] >= 1
    with TestClient(webapp.app) as client:
        page = client.get(f"/search/{sid}").text
        assert "Beste Angebote" in page and "Alle Angebote als Liste" in page and "Gebraucht ab" in page
        assert "Preis-Wert-Karte" in client.get(f"/search/{sid}/analytics").text
        assert client.get(f"/search/{sid}/neu-kaufen").status_code == 200
        assert "Was suchst du?" in client.get("/neue-suche").text


def test_auction_is_not_a_lure():
    """Ricardo-Auktion mit tiefem aktuellem Gebot (MacBook «mehr Power für dich», 257 CHF bei 17 Geboten)."""
    from fundus.trust import bidding, listing_price
    auction = {"auction": 1, "bid_price": 257, "price": 257, "bids": 17}
    assert bidding(auction) and listing_price(auction) == 257
    assert views.ko_reason(auction, 260, 988) is None
    with_buy_now = {"auction": 1, "bid_price": 257, "price": 900, "bids": 17}   # sicher zu haben für 900
    assert not bidding(with_buy_now) and listing_price(with_buy_now) == 900
    assert views.ko_reason({"price": 257}, 260, 988) == "unrealistisch billig"


def test_search_order_by_drag():
    from fastapi.testclient import TestClient
    from fundus.web import app as webapp
    a, b, c = (db.save_search({"query": q}) for q in ("alpha", "beta", "gamma"))
    assert [s["id"] for s in db.list_searches()] == [c, b, a]       # neue Suchen oben
    with TestClient(webapp.app) as client:
        assert client.post("/searches/order", json={"ids": [a, c, b, 999]}).json() == {"ok": True}
        assert [s["id"] for s in db.list_searches()] == [a, c, b]
        page = client.get("/").text
    assert page.index(f'data-id="{a}"') < page.index(f'data-id="{c}"') < page.index(f'data-id="{b}"')
    d = db.save_search({"query": "delta"})
    assert db.list_searches()[0]["id"] == d                           # neue Suche wieder ganz oben


def test_groups_by_drag():
    """Übersicht: Suche in eine Gruppe ziehen, herausziehen; eine Gruppe mit nur einer Suche ist trotzdem sichtbar."""
    from fastapi.testclient import TestClient
    from fundus.web import app as webapp
    a, b = (db.save_search({"query": q}) for q in ("alpha", "beta"))
    with TestClient(webapp.app) as client:
        client.post("/searches/order", json={"groups": {str(a): "Wahl", str(b): ""}})
        page = client.get("/").text
        assert 'data-group="Wahl"' in page
        assert page.index('data-group="Wahl"') < page.index(f'data-id="{a}"')
        client.post("/searches/order", json={"ids": [b, a], "groups": {str(a): ""}})
        assert 'data-group=' not in client.get("/").text
    assert db.get_search(a)["group_name"] is None


def test_groups_and_gemini_export():
    """Entweder-oder-Gruppe: Mitglieder stehen beieinander, gemeinsame Bestenliste, ein Text für Gemini."""
    from fundus import scoring
    ids = []
    for q, group in (("sensor bar drei", "Kitebar"), ("anderes", None), ("sensor bar vier", "kitebar")):
        sid = db.save_search({"query": q, "group_name": group})
        ids.append(sid)
        for i, p in enumerate((200, 250, 300, 350)):
            l = Listing(platform="tutti", external_id=f"{sid}-{i}", title=f"{q} {i}", url=f"https://x/{sid}/{i}", price=p)
            db.upsert_listing(l, "ok", [])
            db.add_hit(sid, l.key, False)
            db.update_hit_ai(sid, l.key, True, "passt", "gut", "ja")
        scoring.rescore_search(sid)
    rows = [(s, views.build(s["id"])) for s in db.list_searches()]
    blocks = views.grouped(rows)
    group = next(b for b in blocks if b["name"])
    assert sorted(group["ids"]) == sorted([ids[0], ids[2]]) and group["best"]       # Gross-/Kleinschreibung egal
    assert {o["search"] for o in group["best"]} <= {"sensor bar drei", "sensor bar vier"}
    assert len(blocks) == 2
    text = views.gemini_prompt([ids[0], ids[2]])
    assert "entweder–oder" in text and "=== sensor bar drei ===" in text and "https://x/" in text and "Endpreis" in text
    with TestClient(__import__("fundus.web.app", fromlist=["app"]).app) as client:
        page = client.get("/").text
        assert "ov-grp" in page and "für Gemini kopieren" in page
        assert client.get(f"/api/gemini?ids={ids[1]}").text.startswith("Analysiere diese Inserate")


def test_overview_is_cached_until_data_changes():
    sid = db.save_search({"query": "x"})
    a = views.build(sid)
    assert views.build(sid) is a                    # gleicher Datenstand: aus dem Zwischenspeicher
    db.kv_set("irgendwas", "1")
    assert views.build(sid) is not a                # nach einem Schreibvorgang neu gerechnet
