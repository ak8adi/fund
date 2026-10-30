from datetime import datetime
from zoneinfo import ZoneInfo

from fundus.scheduler import missed_run

TZ = ZoneInfo("Europe/Zurich")
TIMES = ["07:40", "18:20"]


def at(day, hour, minute=0):
    return datetime(2026, 10, day, hour, minute, tzinfo=TZ)


def test_missed_morning_run_is_caught_up():
    assert missed_run(at(28, 8), TIMES, at(30, 13))


def test_no_catch_up_when_last_run_after_slot():
    assert not missed_run(at(30, 7, 45), TIMES, at(30, 13))


def test_evening_slot_from_yesterday_counts():
    assert missed_run(at(29, 8), TIMES, at(30, 6))


def test_no_schedule_no_catch_up():
    assert not missed_run(None, [], at(30, 13))
