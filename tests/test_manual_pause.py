"""Manuell gestartete Suchen lässt der Zeitplan einige Stunden aus; Kleinanzeigen ohne Treffer ist kein Fehler."""

from datetime import datetime, timedelta, timezone

from fundus import db, runner
from fundus.adapters.kleinanzeigen import KleinanzeigenAdapter
from fundus.config import get_config


def test_recently_manual():
    now = datetime(2026, 11, 2, 12, tzinfo=timezone.utc)
    s = {"manual_run_at": (now - timedelta(hours=2)).isoformat()}
    assert runner.recently_manual(s, 3, now)
    assert not runner.recently_manual(s, 1, now)
    assert not runner.recently_manual(s, 0, now)
    assert not runner.recently_manual({"manual_run_at": None}, 3, now)


def test_schedule_skips_manual_search(monkeypatch):
    monkeypatch.setattr(runner, "available_sources", lambda cfg: [])
    monkeypatch.setattr(runner.newprice, "is_stale", lambda s: False)
    a = db.save_search({"query": "trust bar"})
    b = db.save_search({"query": "sensor pro"})
    runner.run("manuell", [a])
    assert db.get_search(a)["manual_run_at"] and not db.get_search(b)["manual_run_at"]

    started = []
    monkeypatch.setattr(db, "mark_search_run", lambda sid, manual=False: started.append(sid))
    runner.run("zeitplan")
    assert started == [b]  # a wurde eben von Hand gesucht
    assert runner.run("zeitplan", [a])["skipped_manual"] == 1


def test_kleinanzeigen_no_results_is_not_an_error(monkeypatch):
    page = '<div id="srchrslt-content"><p>Es wurden leider keine Ergebnisse für «sessel micasa» gefunden.</p></div>'
    monkeypatch.setattr("fundus.adapters.kleinanzeigen.fetch_html", lambda url, fetcher: page)
    assert KleinanzeigenAdapter(get_config()).search("sessel micasa", "kleinanzeigen", None, None) == []
