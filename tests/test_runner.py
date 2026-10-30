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
