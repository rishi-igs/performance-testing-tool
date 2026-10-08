"""Scheduled runs of a scenario: once, every day, or on chosen days of the week."""
from __future__ import annotations

import re
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field, model_validator

_TIME = re.compile(r"^([01]\d|2[0-3]):[0-5]\d$")


class ScheduleIn(BaseModel):
    scenario_id: str = Field(min_length=1, max_length=40)
    repeat: Literal["once", "daily", "weekly"] = "once"
    at: str | None = None                   # once: local date and time, 2026-10-09T09:00
    time: str | None = None                 # daily and weekly: local time of day, 09:00
    days: list[int] = []                    # weekly: 0 = Monday ... 6 = Sunday
    timezone: str = Field("UTC", max_length=64)                  # IANA name, as the browser reports it
    utc_offset_minutes: int = Field(0, ge=-14 * 60, le=14 * 60)   # used if the server has no time-zone database
    enabled: bool = True
    note: str = Field("", max_length=120)

    @model_validator(mode="after")
    def _check(self) -> "ScheduleIn":
        if self.repeat == "once":
            if not self.at:
                raise ValueError("choose the date and time of the run")
            try:
                datetime.fromisoformat(self.at)
            except ValueError as exc:
                raise ValueError("'at' must be a date and time such as 2026-10-09T09:00") from exc
        elif not self.time or not _TIME.match(self.time):
            raise ValueError("choose the time of day as HH:MM")
        if any(not 0 <= d <= 6 for d in self.days):
            raise ValueError("days run from 0 (Monday) to 6 (Sunday)")
        if self.repeat == "weekly" and not self.days:
            raise ValueError("choose at least one day of the week")
        self.days = sorted(set(self.days))
        return self
