"""Sperren: Pause nach Retry-After bzw. 1 h, 2 h, 4 h … 24 h; Ricardo nur über erlaubte Suchseiten."""

from datetime import datetime, timedelta, timezone

from fundus import db, runner
from fundus.adapters import base
from fundus.adapters.scraped import RicardoAdapter
from fundus.config import get_config


def _hours_left(until: datetime) -> float:
    return (until - datetime.now(timezone.utc)) / timedelta(hours=1)


def test_block_pause_escalates_and_resets():
    db.ensure_platforms(["ricardo"])
    assert round(_hours_left(db.mark_platform_blocked("ricardo"))) == 1
    assert round(_hours_left(db.mark_platform_blocked("ricardo"))) == 2
    assert round(_hours_left(db.mark_platform_blocked("ricardo", retry_after=10 * 3600))) == 10  # Server weiss es besser
    assert "ricardo" in runner._paused_sources(["ricardo"])
    db.record_platform_result("ricardo", 9, None)  # liefert wieder
    assert round(_hours_left(db.mark_platform_blocked("ricardo"))) == 1
    for _ in range(10):
        until = db.mark_platform_blocked("ricardo")
    assert round(_hours_left(until)) == 24


def test_retry_after_header():
    assert base._retry_after("120") == 120
    assert base._retry_after(None) is None and base._retry_after("bald") is None


def test_ricardo_search_url_without_parameters():
    # robots.txt: «Allow: /*/s/», «Disallow: /*/s/*?»
    url = RicardoAdapter(get_config()).search_url("duotone trust bar")
    assert url == "https://www.ricardo.ch/de/s/duotone%20trust%20bar/" and "?" not in url


def test_host_gap(monkeypatch):
    slept = []
    monkeypatch.setattr(base.time, "sleep", slept.append)
    monkeypatch.setattr(base, "_last_request", {})
    base._wait_for_host("https://www.ricardo.ch/de/s/a/")
    base._wait_for_host("https://www.ricardo.ch/de/s/b/")
    base._wait_for_host("https://www.tutti.ch/de/q/suche?query=a")  # andere Hosts: keine Wartezeit
    assert len(slept) == 1 and 59 < slept[0] <= 60
