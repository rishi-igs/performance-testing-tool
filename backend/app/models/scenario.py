"""A scenario: which scripts run with how many users, on what schedule, judged by which SLAs.

The equivalent of a LoadRunner Controller scenario. Each group runs one script with its own
number of virtual users and schedule (or the scenario's schedule): start after a delay, ramp
up, hold, ramp down. Rendezvous points make a share of a group's users wait for each other
before a business function. A goal-oriented scenario holds a target throughput instead.
"""
from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from .script import RuntimeSettings

MAX_DAY = 7 * 86_400


class Schedule(BaseModel):
    model_config = ConfigDict(extra="forbid")

    start_delay_seconds: int = Field(0, ge=0, le=MAX_DAY)
    ramp_up_seconds: int = Field(0, ge=0, le=MAX_DAY, description="users start evenly over this time")
    duration_seconds: int = Field(60, ge=1, le=MAX_DAY, description="time at full load, after the ramp-up")
    ramp_down_seconds: int = Field(0, ge=0, le=MAX_DAY, description="users stop evenly over this time")
    iterations: int | None = Field(None, ge=1, le=1_000_000,
                                   description="instead of a duration: each user runs this many iterations")

    @model_validator(mode="after")
    def _check(self) -> "Schedule":
        if self.iterations and self.ramp_down_seconds:
            raise ValueError("a ramp-down needs a duration, not a number of iterations")
        return self

    def total_seconds(self) -> int:
        return self.start_delay_seconds + self.ramp_up_seconds + self.duration_seconds + self.ramp_down_seconds


class Group(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=60, pattern=r"^[A-Za-z0-9][A-Za-z0-9 _.-]*$")
    script_id: str = Field(min_length=1, max_length=40)
    users: int = Field(1, ge=1)
    percent: float | None = Field(None, gt=0, le=100, description="share of total_users (percentage mode)")
    schedule: Schedule | None = None
    settings: RuntimeSettings | None = Field(None, description="override the script's run-time settings")
    enabled: bool = True

    @field_validator("name")
    @classmethod
    def _strip(cls, v: str) -> str:
        return v.strip()


class Rendezvous(BaseModel):
    """Users of a group wait for each other before a business function starts (lr_rendezvous)."""

    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=60)
    group: str
    transaction: str = Field(min_length=1, max_length=80, description="business function name")
    percent: float = Field(100, gt=0, le=100, description="share of the group's users to wait for")
    timeout_seconds: int = Field(30, ge=0, le=3600, description="0 = wait without a limit")


class SlaRule(BaseModel):
    model_config = ConfigDict(extra="forbid")

    transaction: str = Field("*", min_length=1, max_length=80, description="business function, or * for each one")
    metric: Literal["p90", "p95", "p99", "avg", "max", "error_rate", "tps_min"]
    limit: float = Field(gt=0)


class Goal(BaseModel):
    """Goal-oriented scenario: hold a throughput with up to max_users users."""

    model_config = ConfigDict(extra="forbid")

    type: Literal["transactions_per_second", "hits_per_second"]
    target: float = Field(gt=0, le=100_000)
    max_users: int = Field(ge=1)


class ScenarioConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=100)
    mode: Literal["manual", "goal"] = "manual"
    distribution: Literal["users", "percent"] = "users"
    total_users: int | None = Field(None, ge=1, description="percentage mode: users shared out by percent")
    schedule: Schedule = Field(default_factory=Schedule)
    groups: list[Group] = Field(min_length=1, max_length=50)
    rendezvous: list[Rendezvous] = Field(default_factory=list, max_length=100)
    sla: list[SlaRule] = Field(default_factory=list, max_length=200)
    goal: Goal | None = None
    bandwidth_kbps: int | None = Field(None, ge=8, le=10_000_000, description="network speed per user; empty = unlimited")
    generators: list[str] = Field(default_factory=list, max_length=100)
    monitors: list[str] = Field(default_factory=list, max_length=100)

    @model_validator(mode="after")
    def _check(self) -> "ScenarioConfig":
        names = [g.name.lower() for g in self.groups]
        if len(set(names)) != len(names):
            raise ValueError("group names must be unique")
        active = [g for g in self.groups if g.enabled]
        if not active:
            raise ValueError("at least one group must be enabled")
        if self.distribution == "percent":
            if not self.total_users:
                raise ValueError("percentage mode needs total_users")
            if any(g.percent is None for g in active):
                raise ValueError("percentage mode needs a percent for every enabled group")
            if abs(sum(g.percent for g in active) - 100) > 0.01:
                raise ValueError("group percentages must add up to 100")
            for g in active:
                g.users = max(1, round(self.total_users * g.percent / 100))
        if self.mode == "goal":
            if self.goal is None:
                raise ValueError("a goal-oriented scenario needs a goal")
            if any(g.schedule is not None for g in active):
                raise ValueError("a goal-oriented scenario uses one schedule for all groups")
            if self.schedule.ramp_down_seconds or self.schedule.iterations:
                raise ValueError("a goal-oriented scenario runs for a duration without ramp-down")
        for r in self.rendezvous:
            if r.group.lower() not in names:
                raise ValueError(f"rendezvous {r.name}: there is no group {r.group}")
        return self

    def active_groups(self) -> list[Group]:
        return [g for g in self.groups if g.enabled]

    def schedule_for(self, group: Group) -> Schedule:
        return group.schedule or self.schedule

    def total_users_planned(self) -> int:
        if self.mode == "goal" and self.goal:
            return self.goal.max_users
        return sum(g.users for g in self.active_groups())

    def total_seconds(self) -> int:
        return max(self.schedule_for(g).total_seconds() for g in self.active_groups())
