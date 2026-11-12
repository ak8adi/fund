"""Ungenaue Suchbegriffe: Umlaute, Alternativ-Begriffe (erster Baustein des Suchprofils)."""

from fastapi.testclient import TestClient

from fundus import db, runner
from fundus.matching import clean_alts, matches_query, matches_search
from tests.test_runner import FakeAdapter, item

# echte Titel aus der Suche «Ikea poäng Schaukelstuhl» (Nov. 2026), von der KI als passend beurteilt
POÄNG = ["Schaukelstuhl Poäng", "Poäng Schaukelstuhl mit Hocker, neuwertig", "Fauteuil à bascule IKEA POÄNG",
         "IKEA Poäng Schaukelstuhl", "Ikea Poang Schaukelstuhl"]


def test_umlauts_are_folded():
    assert matches_query("poang schaukelstuhl", "IKEA POÄNG Schaukelstuhl")
    assert matches_query("poäng schaukelstuhl", "Ikea Poang Schaukelstuhl")
    assert not matches_query("poang schaukelstuhl", "IKEA POÄNG Kindersessel")


def test_alternative_term_counts_in_title():
    vague = {"query": "ikea schaukelstuhl"}
    assert not matches_search(vague, "Schaukelstuhl Poäng")           # bisher verworfen
    with_alt = {**vague, "alt_queries": ["Poäng Schaukelstuhl"]}
    assert all(matches_search(with_alt, t) for t in POÄNG[:2] + POÄNG[3:])
    assert not matches_search(with_alt, "IKEA Poäng Kindersessel")    # klar Falsches bleibt draussen
    assert not matches_search(with_alt, "Ikea Sessel")


def test_clean_alts_limits_and_drops_redundant():
    assert clean_alts("ikea schaukelstuhl", ["Poäng Schaukelstuhl", "IKEA Schaukelstuhl", "poang  schaukelstuhl",
                                             "Poäng Rocking Chair"]) == ["Poäng Schaukelstuhl", "Poäng Rocking Chair"]


def test_runner_queries_alternatives_without_flooding(monkeypatch):
    """Alternative wird je Quelle einmal (erste Seite) abgefragt; ihr erster Abruf ist Bestand, nicht «neu»."""
    asked, sent = [], []

    class ByQuery(FakeAdapter):
        def search(self, query, source, min_price, max_price, page=1):
            asked.append(query)
            if query == "ikea schaukelstuhl":
                return [item(1, title="IKEA Schaukelstuhl")]
            return [item(2, title="Schaukelstuhl Poäng")] + ([item(3, title="Poäng Schaukelstuhl neu")] if len(asked) > 2 else [])

    monkeypatch.setattr(runner, "all_adapters", lambda cfg: [ByQuery(cfg)])
    monkeypatch.setattr(runner, "notify_hits", lambda cfg, s, hits, labels: sent.extend(hits) or [h["key"] for h in hits])
    sid = db.save_search({"query": "ikea schaukelstuhl"})
    db.set_alt_queries(sid, ["Poäng Schaukelstuhl"])
    runner.run("test")
    assert asked == ["ikea schaukelstuhl", "Poäng Schaukelstuhl"]
    assert {h["external_id"] for h in db.hits_for_search(sid)} == {"1", "2"}
    runner.run("test")
    assert [h["key"] for h in sent] == ["ricardo:3"]                   # nur wirklich Neues wird gemeldet


def test_suggestion_endpoint_saves_nothing(monkeypatch):
    from fundus import ai
    from fundus.web import app as webapp
    monkeypatch.setattr(ai, "suggest_alternatives", lambda q: ["Poäng Schaukelstuhl"])
    sid = db.save_search({"query": "ikea schaukelstuhl"})
    with TestClient(webapp.app) as client:
        assert client.get("/api/alternatives", params={"q": "ikea schaukelstuhl"}).json() == {"alt": ["Poäng Schaukelstuhl"]}
    assert db.get_search(sid)["alt_queries"] == []                     # aktiv erst nach Speichern


def test_edit_form_alternatives(monkeypatch):
    from fundus.web import app as webapp
    monkeypatch.setattr(webapp.runner, "run_in_background", lambda *a, **k: True)
    monkeypatch.setattr(webapp.newprice, "refresh_in_background", lambda *a, **k: None)
    sid = db.save_search({"query": "ikea schaukelstuhl"})
    with TestClient(webapp.app) as client:
        client.post(f"/search/{sid}", data={"query": "ikea schaukelstuhl", "alt_queries": "Poäng Schaukelstuhl, IKEA Schaukelstuhl"})
        assert db.get_search(sid)["alt_queries"] == ["Poäng Schaukelstuhl"]
        assert "Poäng Schaukelstuhl" in client.get(f"/search/{sid}").text
        client.post(f"/search/{sid}", data={"query": "ikea sessel", "alt_queries": ""})
        assert db.get_search(sid)["alt_queries"] == []
