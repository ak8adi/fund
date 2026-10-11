"""Automatische Läufe zu festen Zeiten mit Zufallsversatz."""

from __future__ import annotations

import logging

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger

from .runner import run

log = logging.getLogger(__name__)


def start_scheduler(cfg: dict) -> BackgroundScheduler | None:
    sched_cfg = cfg["schedule"]
    times = sched_cfg.get("times") or []
    if not times:
        log.info("Kein Zeitplan konfiguriert – nur manuelle Läufe")
        return None
    scheduler = BackgroundScheduler(timezone=sched_cfg.get("timezone", "Europe/Zurich"))
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
    scheduler.start()
    log.info("Zeitplan aktiv: %s (±%s min)", ", ".join(times), sched_cfg.get("jitter_minutes", 0))
    return scheduler


def next_runs(scheduler: BackgroundScheduler | None) -> list:
    if not scheduler:
        return []
    return sorted(j.next_run_time for j in scheduler.get_jobs() if j.next_run_time)
