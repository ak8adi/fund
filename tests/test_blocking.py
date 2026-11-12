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


class _Resp:
    def __init__(self, status, text="<html>ok</html>"):
        self.status_code, self.text, self.headers = status, text, {}


def test_transient_error_is_retried_once(monkeypatch):
    """Tutti 12.11.: einmal HTTP 502 – ein Neuversuch nach kurzer Pause; Sperren (429) werden nie wiederholt."""
    import pytest
    slept, answers = [], []
    monkeypatch.setattr(base.time, "sleep", slept.append)
    monkeypatch.setattr(base, "_get", lambda url, imp, h: answers.pop(0))
    answers[:] = [_Resp(502), _Resp(200)]
    assert base.fetch_html("https://www.tutti.ch/de/q/suche?query=tisch") == "<html>ok</html>"
    assert slept == [base.RETRY_WAIT]
    answers[:] = [_Resp(502), _Resp(502)]
    with pytest.raises(base.AdapterError, match="HTTP 502"):
        base.fetch_html("https://www.tutti.ch/de/q/suche?query=tisch")
    answers[:] = [_Resp(429), _Resp(200)]
    with pytest.raises(base.BlockedError):
        base.fetch_html("https://www.tutti.ch/de/q/suche?query=tisch")
    assert len(answers) == 1                                             # 429: kein zweiter Versuch


def test_single_failed_query_is_not_a_platform_error():
    """1 von 13 Abfragen gescheitert → Plattform liefert, Hinweis «teilweise»; alle gescheitert → Fehler."""
    db.ensure_platforms(["tutti"])
    db.record_platform_result("tutti", 231, "HTTP 502 für https://www.tutti.ch/…", fails=1, tries=13)
    st = db.platform_states()["tutti"]
    assert st["last_error"] is None and st["last_partial"].startswith("1 von 13 Abfragen fehlgeschlagen: HTTP 502")
    db.record_platform_result("tutti", 0, "HTTP 502 für https://www.tutti.ch/…", fails=13, tries=13)
    st = db.platform_states()["tutti"]
    assert st["last_error"].startswith("HTTP 502") and st["last_partial"] is None
    db.record_platform_result("tutti", 200, None, fails=0, tries=13)
    assert db.platform_states()["tutti"]["last_error"] is None
