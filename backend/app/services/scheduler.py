"""Scheduled scenario runs: once, every day, or on chosen days of the week, in local time.

A background thread checks for due schedules every SCHEDULER_INTERVAL_SECONDS. Each due
schedule is claimed in the database before it starts, so a run never starts twice. A run
that fell due while the server was not running is skipped, not started late, and the
schedule says so: a load test at an unexpected hour can hurt the system under test.

Times follow the schedule's IANA time zone (daylight saving included) when the server has
a time-zone database: Linux has one, Windows needs `pip install tzdata`. Without one, the
browser's UTC offset at the time the schedule was saved is used.
"""
from __future__ import annotations

import functools
import logging
import re
import threading
import zoneinfo
from datetime import datetime, time, timedelta, timezone, tzinfo
from typing import Any, Callable

from ..db import Database

log = logging.getLogger("perf.scheduler")
MISSED_AFTER = timedelta(minutes=10)
_ZONE_NAME = re.compile(r"^[A-Za-z0-9_+-]+(/[A-Za-z0-9_+-]+)*$")
_UTC_NAMES = {"UTC", "ETC/UTC", "Z", "GMT"}


@functools.lru_cache(maxsize=1)
def has_zone_database() -> bool:
    try:
        return bool(zoneinfo.available_timezones())
    except Exception:  # noqa: BLE001 - any failure means: no usable database
        return False


def zone_of(cfg: dict[str, Any]) -> tuple[tzinfo, str]:
    """The schedule's time zone, and how it applies: 'named' (follows daylight saving) or 'offset'."""
    name = cfg.get("timezone") or "UTC"
    if name.upper() in _UTC_NAMES:
        return timezone.utc, "named"
    if has_zone_database() and _ZONE_NAME.match(name):
        try:
            return zoneinfo.ZoneInfo(name), "named"
        except (zoneinfo.ZoneInfoNotFoundError, ValueError):
            pass
    return timezone(timedelta(minutes=cfg.get("utc_offset_minutes") or 0)), "offset"


def zone_problem(cfg: dict[str, Any]) -> str | None:
    name = cfg.get("timezone") or "UTC"
    if not _ZONE_NAME.match(name):
        return f"'{name}' is not a time zone name"
    if has_zone_database() and name.upper() not in _UTC_NAMES and zone_of(cfg)[1] != "named":
        return f"Unknown time zone '{name}'"
    return None


def iso(moment: datetime) -> str:
    """The database's format (UTC, seconds), so times compare as text."""
    return moment.astimezone(timezone.utc).isoformat(timespec="seconds")


def next_run(cfg: dict[str, Any], after: datetime) -> datetime | None:
    """The first run time strictly after `after`, in UTC; None when there is none (a past one-off)."""
    zone, _ = zone_of(cfg)
    if cfg["repeat"] == "once":
        at = datetime.fromisoformat(cfg["at"])
        at = (at.replace(tzinfo=zone) if at.tzinfo is None else at).astimezone(timezone.utc)
        return at if at > after else None
    hour, minute = (int(x) for x in cfg["time"].split(":"))
    today = after.astimezone(zone).date()
    for add in range(8):
        day = today + timedelta(days=add)
        if cfg["repeat"] == "weekly" and day.weekday() not in cfg["days"]:
            continue
        candidate = datetime.combine(day, time(hour, minute), tzinfo=zone).astimezone(timezone.utc)
        if candidate > after:
            return candidate
    return None


class Scheduler:
    """Starts due schedules with `start(scenario, who)`, which returns the new run ({"id": ...})."""

    def __init__(self, db: Database, start: Callable[[dict[str, Any], str], dict[str, Any]], interval: float):
        self.db, self.start_run, self.interval = db, start, interval
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        if self.interval > 0:
            self._thread = threading.Thread(target=self._loop, name="perf-scheduler", daemon=True)
            self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=5)

    def _loop(self) -> None:
        while not self._stop.wait(self.interval):
            try:
                self.tick()
            except Exception:  # noqa: BLE001 - keep the scheduler alive
                log.exception("scheduler check failed")

    def tick(self, now: datetime | None = None) -> list[str]:
        """Start every due schedule; return the ids of the runs started."""
        now = now or datetime.now(timezone.utc)
        started = []
        for schedule in self.db.due_schedules(iso(now)):
            run_id = self._fire(schedule, now)
            if run_id:
                started.append(run_id)
        return started

    def _fire(self, schedule: dict[str, Any], now: datetime) -> str | None:
        sid, cfg, due_at = schedule["id"], schedule["config"], schedule["next_run_at"]
        following = next_run(cfg, now) if cfg.get("enabled", True) else None
        if not self.db.claim_schedule(sid, due_at, iso(following) if following else None, iso(now)):
            return None                                  # another worker claimed it
        if now - datetime.fromisoformat(due_at) > MISSED_AFTER:
            log.warning("schedule %s: skipped the run due at %s (the server was not running)", sid, due_at)
            self.db.update_schedule(sid, last_error=f"Skipped the run due at {due_at} UTC: the server was not running then.")
            return None
        scenario = self.db.get_scenario(schedule["scenario_id"])
        if not scenario:
            self.db.update_schedule(sid, last_error="The scenario no longer exists.")
            return None
        try:
            run = self.start_run(scenario, f"schedule:{sid}")
        except Exception as exc:  # noqa: BLE001 - recorded on the schedule
            detail = getattr(exc, "detail", None) or str(exc)
            log.warning("schedule %s: not started: %s", sid, detail)
            self.db.update_schedule(sid, last_error=f"Not started: {detail}")
            return None
        self.db.update_schedule(sid, last_run_id=run["id"], last_error=None)
        log.info("schedule %s started run %s of scenario %s", sid, run["id"], scenario["id"])
        return run["id"]
