from fundus import db, runner
from fundus.adapters import Adapter
from fundus.models import Listing


class FakeAdapter(Adapter):
    name = "ricardo"
    label = "Ricardo"
    calls = 0
    inventory: list[Listing] = []

    def search(self, query, source, min_price, max_price):
        FakeAdapter.calls += 1
        return list(FakeAdapter.inventory)


def item(i, price=100, title=None):
    return Listing(platform="ricardo", external_id=str(i), title=title or f"Kite {i}", url=f"https://x/{i}",
                   price=price, image="img")


def test_baseline_then_only_new_items_notify(monkeypatch):
    sent = []
    monkeypatch.setattr(runner, "all_adapters", lambda cfg: [FakeAdapter(cfg)])
    monkeypatch.setattr(runner, "notify_hits", lambda cfg, s, hits, labels: sent.extend(hits) or [h["key"] for h in hits])

    sid = db.save_search({"query": "kite", "exclude": "kinder", "max_price": 500})
    FakeAdapter.inventory = [item(1), item(2), item(3, title="Kinder Kite"), item(4, price=900)]
    FakeAdapter.calls = 0

    r1 = runner.run("test")
    assert r1["new"] == 0 and not sent                      # erster Lauf = nur Bestand
    assert {h["external_id"] for h in db.hits_for_search(sid)} == {"1", "2"}   # Filter greifen

    FakeAdapter.inventory = [item(1), item(2), item(5)]
    r2 = runner.run("test")
    assert r2["new"] == 1
    assert [h["key"] for h in sent] == ["ricardo:5"]
    assert FakeAdapter.calls == 2                            # genau eine Anfrage pro Lauf


def test_disabled_platform_is_skipped(monkeypatch):
    monkeypatch.setattr(runner, "all_adapters", lambda cfg: [FakeAdapter(cfg)])
    db.save_search({"query": "sofa"})
    runner.available_sources({})
    db.set_platform_enabled("ricardo", False)
    FakeAdapter.calls = 0
    runner.run("test")
    assert FakeAdapter.calls == 0


def test_blocked_platform_pauses_for_next_runs(monkeypatch):
    from fundus.adapters import BlockedError

    class BlockingAdapter(FakeAdapter):
        def search(self, query, source, min_price, max_price):
            FakeAdapter.calls += 1
            raise BlockedError("HTTP 403")

    monkeypatch.setattr(runner, "all_adapters", lambda cfg: [BlockingAdapter(cfg)])
    db.save_search({"query": "sofa"})
    db.save_search({"query": "tisch"})
    FakeAdapter.calls = 0
    runner.run("test")
    assert FakeAdapter.calls == 1                            # nach Sperre im selben Lauf nicht weiter anklopfen
    runner.run("test")
    assert FakeAdapter.calls == 1                            # und im nächsten Lauf Pause
    assert db.platform_states()["ricardo"]["last_error"].startswith("Pausiert nach Sperre")


def test_gone_listing_reappears_and_ai_reset(monkeypatch):
    monkeypatch.setattr(runner, "all_adapters", lambda cfg: [FakeAdapter(cfg)])
    sid = db.save_search({"query": "kite"})
    FakeAdapter.inventory = [item(1)]
    runner.run("test")
    db.mark_listing_gone("ricardo:1")
    assert db.get_listing("ricardo:1")["gone_at"]
    db.update_hit_ai(sid, "ricardo:1", True, "passt")
    db.reset_hit_ai(sid, "ricardo:1")                        # Sets-Umschalten: zurücksetzen
    assert db.hits_for_search(sid)[0]["ai_relevant"] is None
    runner.run("test")                                       # wieder in den Suchergebnissen → verfügbar
    assert db.get_listing("ricardo:1")["gone_at"] is None


def test_prefilter_drops_titles_without_search_words(monkeypatch):
    monkeypatch.setattr(runner, "all_adapters", lambda cfg: [FakeAdapter(cfg)])
    sid = db.save_search({"query": "kite 9m"})
    FakeAdapter.inventory = [item(1, title="North Orbit Kite 9m"), item(2, title="Trapez Gr. M"), item(3, title="Kite 12m")]
    runner.run("test")
    assert {h["external_id"] for h in db.hits_for_search(sid)} == {"1"}


def test_exclude_words_match_word_starts_in_title_only():
    from fundus.matching import excluded
    assert not excluded(["dell"], "CORE Sensor 4 Pro Bar Modell 2024")   # «Modell» ist kein Dell
    assert excluded(["dell"], "Dell XPS 13")
    assert excluded(["defekt"], "Kite defekter Bladder")
    assert not excluded(["hp"], "Kiteshop Bar")
    assert excluded(["double"], "Thule Urban Glide 3 Double")
    assert not excluded([], "egal")


def test_ricardo_complete_list_marks_missing_as_gone(monkeypatch):
    """Liefert Ricardo die ganze Trefferliste, ist ein früheres Angebot, das darin fehlt, verkauft oder beendet."""
    sid = db.save_search({"query": "kite 9m"})

    class Complete(FakeAdapter):
        complete = True
    monkeypatch.setattr(runner, "all_adapters", lambda cfg: [Complete(cfg)])
    FakeAdapter.inventory = [item(1, title="North Kite 9m"), item(2, title="Core Kite 9m")]
    runner.run("test")
    FakeAdapter.inventory = [item(1, title="North Kite 9m")]
    runner.run("test")
    gone = {h["external_id"]: bool(h["gone_at"]) for h in db.hits_for_search(sid)}
    assert gone == {"1": False, "2": True}


def test_status_shows_progress_ring(monkeypatch):
    from fastapi.testclient import TestClient
    from fundus import ai
    from fundus.web import app as webapp
    with TestClient(webapp.app) as client:
        assert "Alle" in client.get("/status").text            # kein Scan: Knopf statt Ring
        runner._run_lock.acquire()
        try:
            runner._progress.clear()
            runner._progress.update(total=4, index=2, query="MacBook Pro 16 M1", sources=5, source=2, step="sucht",
                                    platform="Ricardo")
            p = runner.progress()
            assert round(p["share"], 2) == round((1 + 0.8 * 2 / 5) / 4, 2)
            html = client.get("/status").text
            assert 'class="ring"' in html and "2/4" in html and "Ricardo" in html and "MacBook Pro 16 M1" in html
        finally:
            runner._run_lock.release()
        monkeypatch.setattr(ai, "AI_RUNNING", True)
        monkeypatch.setattr(webapp.db, "get_ai_progress", lambda: (30, 120))
        html = client.get("/status").text
        assert "KI bewertet <b>25 %</b>" in html and 'class="ring"' in html


def test_deep_first_scan_and_paging_when_page_is_all_new():
    """Erster Abruf liest mehrere Seiten (Bestand); später wird nur nachgeblättert, wenn eine volle Seite nur
    Unbekanntes zeigt."""
    from fundus import runner
    from fundus.models import Listing

    class Paged:
        paged = True

        def __init__(self, pages):
            self.pages, self.calls = pages, []

        def search(self, query, source, lo, hi, page=1):
            self.calls.append(page)
            return [Listing(platform="kleinanzeigen", external_id=str(i), title="x", url=f"https://x/{i}", price=1)
                    for i in self.pages.get(page, [])]

    search = {"query": "x"}
    a = Paged({1: range(0, 25), 2: range(25, 50), 3: range(50, 60)})
    found, stock = runner._search_pages(a, search, "kleinanzeigen", True, set(), lambda: None)
    assert a.calls == [1, 2, 3] and len(found) == 60 and len(stock) == 35      # Seiten 2–3 sind Bestand

    known = {f"kleinanzeigen:{i}" for i in range(10, 60)}
    b = Paged({1: range(0, 25), 2: range(25, 50)})                              # Seite 1 enthält Bekanntes
    found, stock = runner._search_pages(b, search, "kleinanzeigen", False, known, lambda: None)
    assert b.calls == [1] and len(found) == 25 and not stock

    c = Paged({1: range(100, 125), 2: range(125, 135)})                         # volle Seite, alles unbekannt
    found, stock = runner._search_pages(c, search, "kleinanzeigen", False, known, lambda: None)
    assert c.calls == [1, 2] and len(found) == 35 and not stock                 # nachgeblättert, zählt als neu


def test_kleinanzeigen_url_with_price_and_page():
    from fundus.adapters.kleinanzeigen import KleinanzeigenAdapter
    from fundus.config import get_config
    a = KleinanzeigenAdapter(get_config())
    assert a.search_url("macbook pro") == "https://www.kleinanzeigen.de/s-macbook-pro/k0"
    assert a.search_url("macbook pro", 2) == "https://www.kleinanzeigen.de/s-seite:2/macbook-pro/k0"
    assert a.search_url("sessel", 2, 50, 500) == "https://www.kleinanzeigen.de/s-preis:48:585/seite:2/sessel/k0"
