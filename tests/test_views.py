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
        assert "Pareto-Front" in client.get(f"/search/{sid}/entwuerfe").text
