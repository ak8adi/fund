from fastapi.testclient import TestClient

from fundus import db
from fundus.adapters.ebay import EbayAdapter


def test_ebay_item_mapping():
    item = {
        "itemId": "v1|1234|0", "title": "Duotone Neo 10m", "itemWebUrl": "https://www.ebay.de/itm/1234",
        "price": {"value": "480.00", "currency": "EUR"}, "image": {"imageUrl": "https://i.ebayimg.com/1.jpg"},
        "seller": {"username": "surfshop", "feedbackPercentage": "99.8", "feedbackScore": 1520},
        "itemLocation": {"postalCode": "24*", "city": "Kiel", "country": "DE"},
        "itemCreationDate": "2026-10-10T10:00:00.000Z",
    }
    l = EbayAdapter._to_listing(item, "EBAY_DE")
    assert l.price == 480.0 and l.currency == "EUR"
    assert l.seller.rating_count == 1520 and l.seller.rating_pct == 99.8
    assert l.country == "DE" and l.source == "EBAY_DE"


def test_ebay_needs_keys():
    from fundus.config import get_config
    ok, hint = EbayAdapter(get_config()).configured()
    assert not ok and "eBay" in hint


def test_web_pages_render(monkeypatch):
    from fundus import runner
    from fundus.web import app as webapp

    monkeypatch.setattr(runner, "run_in_background", lambda *a, **k: True)
    with TestClient(webapp.app) as client:
        assert client.get("/").status_code == 200
        r = client.post("/searches", data={"query": "kite 9m", "max_price": "800", "platforms": ["tutti", "ebay"]},
                        follow_redirects=False)
        assert r.status_code == 303
        sid = int(r.headers["location"].rsplit("/", 1)[1])
        assert db.get_search(sid)["platforms"] == ["tutti", "ebay"]
        assert "kite 9m" in client.get(f"/search/{sid}").text
        page = client.get("/settings").text
        assert "Tutti" in page and "eBay DE" in page
        assert client.post("/platforms/tutti/toggle").status_code == 200
        assert db.platform_states()["tutti"]["enabled"] == 0


def test_requirements_and_price_sort(monkeypatch):
    from fundus import ai, runner
    from fundus.models import Listing
    from fundus.web import app as webapp

    monkeypatch.setattr(runner, "run_in_background", lambda *a, **k: True)
    started = []
    monkeypatch.setattr(ai, "start_background_evaluation", lambda sid: started.append(sid))
    with TestClient(webapp.app) as client:
        r = client.post("/searches", data={"query": "tisch", "requirements": "130 bis 150 cm lang",
                                           "require_shipping": "1"}, follow_redirects=False)
        sid = int(r.headers["location"].rsplit("/", 1)[1])
        s = db.get_search(sid)
        assert s["requirements"] == "130 bis 150 cm lang" and s["require_shipping"] == 1

        for i, price in enumerate((300, 100, 200)):
            l = Listing(platform="tutti", external_id=str(i), title=f"Tisch {i}", url=f"https://x/{i}", price=price,
                        currency="CHF")
            db.upsert_listing(l, "ok", [])
            db.add_hit(sid, l.key, False)
        db.update_hit_ai(sid, "tutti:0", True, "x", "gut", req="ja", req_note="Länge 140 cm")

        client.post(f"/search/{sid}/sort", data={"by": "price"})
        # Noch ungeprüfte Inserate stehen nach den geprüften, auch wenn sie billiger sind
        page = client.get(f"/search/{sid}").text
        assert page.index("Tisch 0") < page.index("Tisch 1") < page.index("Tisch 2")
        assert "✓ Länge 140 cm" in page

        for k in ("tutti:1", "tutti:2"):
            db.update_hit_ai(sid, k, True, "x", "gut")
        page = client.get(f"/search/{sid}").text
        assert page.index("Tisch 1") < page.index("Tisch 2") < page.index("Tisch 0")

        # Geänderte Anforderungen: nur passende und an der Anforderung gescheiterte Inserate neu prüfen,
        # Fehltreffer (Zubehör) bleiben ausgeblendet statt bei Preis-Sortierung zuoberst aufzutauchen
        acc = Listing(platform="tutti", external_id="9", title="Tisch Adapter", url="https://x/9", price=5,
                      currency="CHF")
        db.upsert_listing(acc, "ok", [])
        db.add_hit(sid, acc.key, False)
        db.update_hit_ai(sid, acc.key, False, "Nur Zubehör")
        db.update_hit_ai(sid, "tutti:1", False, f"{db.REQ_FAILED} (180 cm)", req="nein", req_note="180 cm")
        db.update_hit_ai(sid, "tutti:2", False, "Anderes Modell (eckig)", req="nein")
        client.post(f"/search/{sid}", data={"query": "tisch", "requirements": "nur rund"})
        by_key = {h["key"]: h for h in db.hits_for_search(sid)}
        assert by_key["tutti:9"]["ai_relevant"] == 0 and by_key["tutti:9"]["ai_reason"] == "Nur Zubehör"
        assert all(by_key[k]["ai_relevant"] is None for k in ("tutti:0", "tutti:1", "tutti:2"))
        assert by_key["tutti:0"]["ai_req"] is None and started == [sid]


def test_store_facts_requirement_not_met():
    from fundus import ai
    sid = db.save_search({"query": "tisch", "requirements": "130-150 cm"})
    from fundus.models import Listing
    l = Listing(platform="tutti", external_id="1", title="Tisch", url="https://x/1", price=100, currency="CHF")
    db.upsert_listing(l, "ok", [])
    db.add_hit(sid, l.key, False)
    ai._store_facts(db.get_search(sid), {"key": l.key, "relevant": True, "req": "nein", "req_note": "180 cm"})
    h = db.hits_for_search(sid)[0]
    assert h["ai_relevant"] == 0 and h["ai_req"] == "nein" and h["ai_req_note"] == "180 cm"


def test_store_facts_set_estimate():
    from fundus import ai
    from fundus.models import Listing
    sid = db.save_search({"query": "trust bar"})
    l = Listing(platform="tutti", external_id="9", title="Pack EVO 9m 11m + Trust Bar", url="https://x/9", price=1400,
                currency="CHF")
    db.upsert_listing(l, "ok", [])
    db.add_hit(sid, l.key, False)
    ai._store_facts(db.get_search(sid), {"key": l.key, "relevant": True, "set_items": "2 Kites, Trust Bar",
                                         "single_price": 250, "single_price_estimated": True})
    h = db.hits_for_search(sid)[0]
    assert h["single_price"] == 250 and h["single_estimated"] == 1 and h["ai_set_items"] == "2 Kites, Trust Bar"


def test_scam_note_hides_hit(monkeypatch):
    from fundus import ai
    from fundus.models import Listing
    from fundus.web import app as webapp

    assert ai.flags_scam("versand möglich. kostet 24 euro.\n\nbetrugsmasche")
    assert ai.flags_scam("Fake-Inserat") and not ai.flags_scam("kein Betrug, alles ok") and not ai.flags_scam("")

    sid = db.save_search({"query": "thule urban glide 3"})
    l = Listing(platform="kleinanzeigen", external_id="1", title="Thule Urban Glide 3", url="https://x/1", price=430,
                currency="EUR")
    db.upsert_listing(l, "ok", [])
    db.add_hit(sid, l.key, False)
    db.update_hit_ai(sid, l.key, True, "Gesuchter Wagen", "sehr gut")

    # Die KI übersieht den Vermerk (wie beim Lite-Modell passiert) – der Vermerk gilt trotzdem
    monkeypatch.setattr(ai, "_gemini_json", lambda *a, **k: [{"key": l.key, "relevant": True, "reason": "Gesuchter Wagen",
                                                               "shipping": "ja", "shipping_cost": 24}])
    monkeypatch.setattr(ai, "get_config", lambda: {"ai": {"gemini_api_key": "x"}})
    with TestClient(webapp.app) as client:
        client.post(f"/api/listing/{l.key}/update_chat", data={"search_id": sid, "seller_chat": "betrugsmasche"})
    h = db.hits_for_search(sid)[0]
    assert h["ai_relevant"] == 0 and h["ai_reason"].startswith(db.SCAM)

    # Die KI erkennt Betrug selbst
    l2 = Listing(platform="tutti", external_id="2", title="Thule Urban Glide 3", url="https://x/2", price=300,
                 currency="CHF")
    db.upsert_listing(l2, "ok", [])
    db.add_hit(sid, l2.key, False)
    ai._store_facts(db.get_search(sid), {"key": l2.key, "relevant": True, "scam": True, "reason": "Nur Vorauskasse"})
    h2 = next(h for h in db.hits_for_search(sid) if h["key"] == l2.key)
    assert h2["ai_relevant"] == 0 and h2["ai_reason"] == f"{db.SCAM} – Nur Vorauskasse"


def test_search_edit_applies_exclude_words(monkeypatch):
    from fundus import ai, runner
    from fundus.models import Listing
    from fundus.web import app as webapp

    monkeypatch.setattr(runner, "run_in_background", lambda *a, **k: True)
    started = []
    monkeypatch.setattr(ai, "start_background_evaluation", lambda sid: started.append(sid))
    assert webapp._num("1'200.-") == 1200 and webapp._num("CHF 80") == 80 and webapp._num("abc") is None
    sid = db.save_search({"query": "sensor bar", "exclude": "dell"})
    for i, title in enumerate(("Sensor Bar Modell 2024", "Sensor Bar Dell Edition")):
        l = Listing(platform="tutti", external_id=str(i), title=title, url=f"https://x/{i}", price=100, currency="CHF")
        db.upsert_listing(l, "ok", [])
        db.add_hit(sid, l.key, False)
        db.update_hit_ai(sid, l.key, i == 0, "x" if i == 0 else "Ausgeschlossener Begriff")
    with TestClient(webapp.app) as client:
        # Ausschlusswort entfernt → zuvor ausgeschlossener Treffer wird neu geprüft
        r = client.post(f"/search/{sid}", data={"query": "sensor bar", "exclude": "", "min_price": "abc",
                                                "allow_sets": "on"})
        assert r.status_code == 200
    by_key = {h["key"]: h for h in db.hits_for_search(sid)}
    assert by_key["tutti:1"]["ai_relevant"] is None and by_key["tutti:0"]["ai_relevant"] == 1
    assert started == [sid]


def test_every_route_answers(monkeypatch):
    """Rundgang durch alle Seiten und Formulare: keine Route darf mit einem Serverfehler antworten."""
    from fundus import ai, newprice, notify, runner
    from fundus.models import Listing
    from fundus.web import app as webapp

    monkeypatch.setattr(runner, "run_in_background", lambda *a, **k: True)
    monkeypatch.setattr(newprice, "refresh_in_background", lambda sid: None)
    monkeypatch.setattr(ai, "start_background_evaluation", lambda sid: None)
    monkeypatch.setattr(ai, "generate_seller_question", lambda key, sid: None)
    monkeypatch.setattr(ai, "reevaluate_single_listing", lambda key, sid: False)
    monkeypatch.setattr(notify, "_publish", lambda cfg, payload: True)
    monkeypatch.setattr(webapp, "send_test", lambda cfg: True)
    with TestClient(webapp.app) as client:
        r = client.post("/searches", data={"query": "core sensor 4", "new_price": "700", "max_price": "600",
                                           "exclude": "dell", "requirements": "nur 52 cm"}, follow_redirects=False)
        sid = int(r.headers["location"].rsplit("/", 1)[1])
        l = Listing(platform="tutti", external_id="1", title="Core Sensor 4 Pro Bar", url="https://x/1", price=400,
                    currency="CHF")
        db.upsert_listing(l, "ok", [])
        db.add_hit(sid, l.key, True)
        db.update_hit_ai(sid, l.key, True, "passt", "gut", req="ja", req_note="52 cm")
        db.update_listing_description(l.key, "Bar in gutem Zustand")
        key = l.key

        gets = ["/", f"/search/{sid}", f"/search/{sid}/results", "/status", "/settings", "/settings?saved=ntfy",
                f"/api/listing/{key}/description"]
        for url in gets:
            assert client.get(url).status_code == 200, url
        posts = [
            (f"/search/{sid}/sort", {"by": "price"}), (f"/search/{sid}/sort", {"by": "score"}),
            (f"/search/{sid}", {"query": "core sensor 4", "new_price": "", "exclude": "", "requirements": ""}),
            (f"/search/{sid}/newprice", {}), (f"/search/{sid}/run", {}), (f"/search/{sid}/ai", {}),
            (f"/api/listing/{key}/generate_question", {"search_id": sid}),
            (f"/api/listing/{key}/update_chat", {"search_id": sid, "seller_chat": "inkl. Versand"}),
            (f"/api/listing/{key}/edit_description", {"description": "Leinen neu"}),
            (f"/search/{sid}/hide", {"key": key}), (f"/search/{sid}/restore", {"key": key}),
            ("/run", {}), ("/platforms/tutti/toggle", {}), ("/platforms/tutti/toggle", {}),
            ("/settings/ntfy", {"topic": "t", "server": "https://ntfy.sh"}), ("/ntfy/test", {}),
            ("/settings/ebay", {"app_id": "", "cert_id": ""}), ("/settings/ai", {"gemini_api_key": ""}),
            ("/settings/schedule", {"times": "07:40, 18:20", "jitter_minutes": "25"}),
            ("/settings/schedule", {"times": "25:99", "jitter_minutes": "x"}),
            ("/settings/notify", {"weekly_time": "08:00", "weekly_day": "mon", "on_new": "on", f"new_{sid}": "on"}),
            ("/settings/notify", {"weekly_time": "nope"}), ("/ntfy/weekly", {}),
            (f"/search/{sid}/toggle", {}), (f"/search/{sid}/delete", {}), ("/search/999/toggle", {}),
        ]
        for url, data in posts:
            resp = client.post(url, data=data)
            assert resp.status_code < 500, (url, resp.status_code)
        assert client.get(f"/search/{sid}", follow_redirects=False).status_code == 303  # gelöscht → Startseite


def test_gemini_retries_after_timeout(monkeypatch):
    import httpx
    from fundus import ai
    calls = []

    class Resp:
        def raise_for_status(self):
            pass

        def json(self):
            return {"candidates": [{"content": {"parts": [{"text": '{"ok": true}'}]}}]}

    def post(url, **kw):
        calls.append(url)
        if len(calls) == 1:
            raise httpx.ReadTimeout("The read operation timed out")
        return Resp()

    monkeypatch.setattr(ai.httpx, "post", post)
    monkeypatch.setattr(ai.time, "sleep", lambda s: None)
    assert ai._gemini_json("p", "s", "k") == {"ok": True}
    assert len(calls) == 2 and calls[0] == calls[1]  # gleiches Modell nochmals, nicht gleich aufgegeben
