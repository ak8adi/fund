"""Automatische Läufe zu festen Zeiten mit Zufallsversatz."""

from __future__ import annotations

import logging

from datetime import datetime, timedelta, timezone

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.date import DateTrigger

from . import db
from .config import get_config
from .notify import send_weekly
from .runner import run

log = logging.getLogger(__name__)


def start_scheduler(cfg: dict) -> BackgroundScheduler:
    scheduler = BackgroundScheduler(timezone=cfg["schedule"].get("timezone", "Europe/Zurich"))
    scheduler.start()
    apply_schedule(scheduler, cfg)
    _catch_up(scheduler, cfg)
    return scheduler


def missed_run(last_started: datetime | None, times: list, now: datetime) -> bool:
    """War seit dem letzten Lauf ein geplanter Zeitpunkt, den niemand ausgeführt hat (Server war aus)?"""
    if not times:
        return False
    if last_started is None:
        return True
    for day in (now.date(), now.date() - timedelta(days=1)):
        for t in times:
            hour, minute = (int(x) for x in str(t).split(":"))
            slot = datetime.combine(day, datetime.min.time(), now.tzinfo).replace(hour=hour, minute=minute)
            if last_started < slot <= now:
                return True
    return False


def _catch_up(scheduler: BackgroundScheduler, cfg: dict) -> None:
    """Verpasste Läufe nachholen: der Zeitplan lebt nur im Speicher, bei ausgeschaltetem Server fällt er aus."""
    runs = db.last_runs(1)
    last = datetime.fromisoformat(runs[0]["started_at"]) if runs else None
    now = datetime.now(timezone.utc).astimezone(scheduler.timezone)
    if missed_run(last, cfg["schedule"].get("times") or [], now):
        when = now + timedelta(minutes=2)
        scheduler.add_job(run, DateTrigger(when), kwargs={"trigger": "nachgeholt"}, id="catch-up", max_instances=1)
        log.info("Geplanter Lauf wurde verpasst – wird um %s nachgeholt", when.strftime("%H:%M"))


def apply_schedule(scheduler: BackgroundScheduler, cfg: dict) -> None:
    """Jobs gemäss cfg['schedule'] neu setzen (auch nach Änderung in den Einstellungen)."""
    sched_cfg = cfg["schedule"]
    times = sched_cfg.get("times") or []
    for job in scheduler.get_jobs():
        job.remove()
    _add_weekly(scheduler, cfg)
    jitter = int(sched_cfg.get("jitter_minutes", 0)) * 60 or None
    for t in times:
        hour, minute = (int(x) for x in str(t).split(":"))
        scheduler.add_job(
            run,
            CronTrigger(hour=hour, minute=minute, jitter=jitter, timezone=scheduler.timezone),
            kwargs={"trigger": "zeitplan"},
            id=f"run-{hour:02d}{minute:02d}",
            misfire_grace_time=3 * 3600,   # Mac war im Ruhezustand → beim Aufwachen nachholen
            coalesce=True,
            max_instances=1,
        )
    if times:
        log.info("Zeitplan aktiv: %s (±%s min)", ", ".join(times), sched_cfg.get("jitter_minutes", 0))
    else:
        log.info("Kein Zeitplan konfiguriert – nur manuelle Läufe")


def weekly_job() -> None:
    send_weekly(get_config(), db.weekly_summary())


def _add_weekly(scheduler: BackgroundScheduler, cfg: dict) -> None:
    n = cfg["ntfy"]
    if not n.get("weekly", True):
        return
    hour, minute = (int(x) for x in str(n.get("weekly_time", "08:00")).split(":"))
    scheduler.add_job(
        weekly_job,
        CronTrigger(day_of_week=n.get("weekly_day", "mon"), hour=hour, minute=minute, timezone=scheduler.timezone),
        id="weekly", misfire_grace_time=6 * 3600, coalesce=True, max_instances=1,
    )


def next_runs(scheduler: BackgroundScheduler | None) -> list:
    if not scheduler:
        return []
    return sorted(j.next_run_time for j in scheduler.get_jobs() if j.next_run_time and j.id != "weekly")
