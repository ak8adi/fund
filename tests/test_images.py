"""Bildanalyse pro Suche und günstigster Preis in der Suchübersicht."""

from fastapi.testclient import TestClient

from fundus import ai, db
from fundus.models import Listing


def _hit(sid, ext, price, currency="CHF", title="Duotone Trust Bar", image="https://img/x.jpg"):
    l = Listing(platform="ricardo", external_id=ext, title=title, url=f"https://x/{ext}", price=price,
                currency=currency, image=image)
    db.upsert_listing(l, "ok", [])
    db.add_hit(sid, l.key, False)
    return l


def test_image_analysis_updates_verdict(monkeypatch):
    sid = db.save_search({"query": "duotone trust bar", "requirements": "ab Jahrgang 2022"})
    a = _hit(sid, "1", 300, title="Duotone Trust Bar Nr. A")
    b = _hit(sid, "2", 250, title="Duotone Trust Bar Nr. B")
    c = _hit(sid, "3", 200, title="Duotone Trust Bar Leinen")
    db.update_hit_ai(sid, a.key, True, "Trust Bar", "gut", req="unklar", req_note="Jahrgang fehlt")
    db.update_hit_ai(sid, b.key, True, "Trust Bar", "gut", req="unklar")
    db.update_hit_ai(sid, c.key, False, "Nur Zubehör")  # Fehltreffer: Fotos nicht nötig

    answers = {a.key: {"finding": "Jahrgang 2023 (Logo)", "relevant": True, "year": 2023, "req": "ja",
                       "req_note": "2023", "reason": "Trust Bar 2023, gepflegt"},
               b.key: {"finding": "Jahrgang 2019", "relevant": True, "year": 2019, "req": "nein",
                       "req_note": "2019"}}
    calls = []

    def fake_gemini(prompt, instr, key, models=(), images=None, timeout=60):
        calls.append(images)
        return answers[a.key if "Nr. A" in prompt else b.key]

    monkeypatch.setattr(ai, "_gemini_json", fake_gemini)
    monkeypatch.setattr(ai, "_fetch_images", lambda h, ad: [("image/jpeg", b"x")])
    monkeypatch.setattr(ai, "get_config", lambda: {"ai": {"gemini_api_key": "x"}})

    db.set_image_analysis(sid, "Bestimme den Jahrgang", True)
    assert db.image_progress(sid, "Bestimme den Jahrgang") == (0, 2)
    assert ai.analyze_images(sid) == 2 and len(calls) == 2
    assert ai.analyze_images(sid) == 0  # alles geprüft

    hits = {h["key"]: h for h in db.hits_for_search(sid, include_hidden=True)}
    assert hits[a.key]["ai_relevant"] == 1 and hits[a.key]["ai_year"] == 2023 and hits[a.key]["ai_req"] == "ja"
    assert hits[a.key]["img_note"] == "Jahrgang 2023 (Logo)"
    assert hits[b.key]["ai_relevant"] == 0 and hits[b.key]["ai_reason"].startswith(db.REQ_FAILED)
    assert db.image_progress(sid, "Bestimme den Jahrgang") == (2, 2)

    # Neuer Text-Befund (z.B. neues Angebot oder Neubewertung) → Fotos werden wieder fällig
    db.update_hit_ai(sid, a.key, True, "Trust Bar", "gut")
    assert [h["key"] for h in db.hits_for_image_check(sid, "Bestimme den Jahrgang")] == [a.key]


def test_image_routes(monkeypatch):
    from fundus.web import app as webapp
    started = []
    monkeypatch.setattr(ai, "start_background_evaluation", lambda sid: started.append(sid))
    sid = db.save_search({"query": "duotone trust bar"})
    with TestClient(webapp.app) as client:
        client.post(f"/search/{sid}/images", data={"image_prompt": "Jahrgang bestimmen"})
        s = db.get_search(sid)
        assert s["image_prompt"] == "Jahrgang bestimmen" and s["image_auto"] == 1 and started == [sid]
        client.post(f"/search/{sid}/images/off")
        s = db.get_search(sid)
        assert s["image_prompt"] == "Jahrgang bestimmen" and s["image_auto"] == 0


def test_overview_shows_cheapest_relevant_offer():
    from fundus.web import app as webapp
    sid = db.save_search({"query": "duotone trust bar"})
    best_score = _hit(sid, "1", 400)
    cheap_eur = _hit(sid, "2", 200, currency="EUR")   # ≈ 188 CHF zum Testkurs
    accessory = _hit(sid, "3", 20, title="Duotone Trust Bar Leinen")
    db.update_hit_ai(sid, best_score.key, True, "Bar")
    db.update_hit_ai(sid, cheap_eur.key, True, "Bar")
    db.update_hit_ai(sid, accessory.key, False, "Zubehör")
    db.set_scores(sid, 450, [(90, "S", "", 410, "", best_score.key), (60, "B", "", 230, "", cheap_eur.key),
                             (None, None, None, None, None, accessory.key)])
    from fundus import views
    assert views.build(sid)["head"]["used"]["key"] == cheap_eur.key
    with TestClient(webapp.app) as client:
        page = client.get("/").text
    row = page.split('class="ov-strip')[1].split('class="ov-ana"')[0]
    assert "gebraucht ab" in row and "200 CHF" in row and "Ricardo" in row


def test_price_sort_uses_final_price():
    from fundus.web import app as webapp
    sid = db.save_search({"query": "thule urban glide 3"})
    db.set_search_sort(sid, "price")
    cheap_far = _hit(sid, "1", 545)     # günstig, aber teure Lieferung
    near = _hit(sid, "2", 600)          # teurer, aber fast ohne Lieferkosten
    unknown = _hit(sid, "3", 580)       # ohne Lieferplan: Inseratpreis zählt
    for l in (cheap_far, near, unknown):
        db.update_hit_ai(sid, l.key, True, "Thule")
    db.set_scores(sid, 650, [(60, "B", "", 695, "", cheap_far.key), (70, "A", "", 621, "", near.key),
                             (65, "A", "", None, "", unknown.key)])
    hits = db.hits_for_search(sid)
    webapp._sort_hits(hits, db.get_search(sid))
    assert [h["key"] for h in hits] == [unknown.key, near.key, cheap_far.key]
    from fundus import views
    assert views.build(sid)["head"]["used"]["key"] == unknown.key
