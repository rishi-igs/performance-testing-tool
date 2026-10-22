"""Turn a traffic profile into standard JMeter Thread Groups.

A profile is a list of steps: at a time, change to a number of users over a ramp (0 = at once).
The target number of running users is therefore a line that goes up, holds and comes down.
Users are numbered 1..peak; user k runs whenever the target is at least k, so when the load
drops, the users added last stop first. Every stretch a user runs starts in one step and ends in
a later one (or at the end of the test):

* users that start in the same step and stop in the same step form one Thread Group: its start
  delay is the first start and its ramp-up spreads the others the way the step's ramp does;
* users that stop during a ramp-down each stop at a different time, but a Thread Group stops all
  its users at once, so they are split into up to MAX_SPLIT groups that stop one after another.

Only the Thread Group's own scheduler is used (start delay, ramp-up, duration), so the plan needs
no JMeter plugins. The same steps drive quick tests and scenario schedules.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Sequence

from ..models.test_config import TrafficStep

MAX_SPLIT = 10              # thread groups per ramp-down
MAX_THREAD_GROUPS = 300


class ProfileError(ValueError):
    """The profile cannot be turned into a plan; the message is shown to the user."""


@dataclass
class Cohort:
    users: int
    delay: int              # seconds after the test starts
    ramp: int
    duration: int | None    # seconds after its own start; None = run its iterations
    loops: int | None
    label: str


@dataclass
class _Segment:
    step: int               # index of the step that makes this change
    start: float
    end: float
    before: int
    after: int

    def spacing(self) -> float:
        return (self.end - self.start) / abs(self.after - self.before)


def _segments(steps: Sequence[TrafficStep]) -> list[_Segment]:
    out, level = [], 0
    for index, step in enumerate(steps):
        if step.users != level:
            out.append(_Segment(index, step.time_seconds, step.time_seconds + step.ramp_seconds, level, step.users))
        level = step.users
    return out


def scaled(steps: Sequence[TrafficStep], users: int) -> list[TrafficStep]:
    """The profile's shape with its peak at `users` (a scenario group follows it with its own users)."""
    peak = max(s.users for s in steps)
    out, level = [], 0
    for s in steps:
        target = round(s.users * users / peak) if peak else 0
        if target != level:
            out.append(TrafficStep(time_seconds=s.time_seconds, users=target, ramp_seconds=s.ramp_seconds))
            level = target
    return out


def curve(steps: Sequence[TrafficStep], duration: int) -> list[dict[str, float]]:
    """The planned number of users: the corners of the line, from 0 to `duration` seconds."""
    points = [{"t": 0, "users": 0}]
    for seg in _segments(steps):
        points.append({"t": seg.start, "users": seg.before})
        points.append({"t": seg.end, "users": seg.after})
    points.append({"t": duration, "users": points[-1]["users"]})
    out: list[dict[str, float]] = []
    for p in points:                 # drop repeated corners
        if out and out[-1]["t"] == p["t"] and out[-1]["users"] == p["users"]:
            continue
        out.append(p)
    return out


def users_at(steps: Sequence[TrafficStep], t: float) -> float:
    level = 0.0
    for seg in _segments(steps):
        if t < seg.start:
            break
        if t >= seg.end:
            level = seg.after
        else:
            level = seg.before + (seg.after - seg.before) * (t - seg.start) / (seg.end - seg.start)
    return level


def cohorts(steps: Sequence[TrafficStep], duration: int,
            label: Callable[[int, int, int, int], str]) -> list[Cohort]:
    """Thread Groups that together follow the profile.

    `label(step_index, users_after_step, part, parts)` names each one.
    """
    segments = _segments(steps)
    peak = max((s.after for s in segments), default=0)
    runs: dict[tuple[int, int | None], list[int]] = {}
    for k in range(1, peak + 1):
        started = None
        for n, seg in enumerate(segments):
            if seg.before < k <= seg.after:
                started = n
            elif seg.after < k <= seg.before and started is not None:
                runs.setdefault((started, n), []).append(k)
                started = None
        if started is not None:
            runs.setdefault((started, None), []).append(k)

    out: list[Cohort] = []
    for (up, down), users in runs.items():
        for chunk in _contiguous(users):
            out += _cohorts_for(chunk, segments[up], segments[down] if down is not None else None, duration, label)
    if len(out) > MAX_THREAD_GROUPS:
        raise ProfileError(f"The profile needs {len(out)} thread groups (at most {MAX_THREAD_GROUPS}): "
                           "use fewer ramp-downs, or make them instant.")
    return sorted(out, key=lambda c: (c.delay, c.label))


def _contiguous(values: list[int]) -> list[list[int]]:
    out: list[list[int]] = []
    for v in values:
        if out and out[-1][-1] == v - 1:
            out[-1].append(v)
        else:
            out.append([v])
    return out


def _cohorts_for(users: list[int], up: _Segment, down: _Segment | None, duration: int,
                 label: Callable[[int, int, int, int], str]) -> list[Cohort]:
    up_spacing = up.spacing()

    def start(k: int) -> float:
        return up.start + (k - up.before - 1) * up_spacing

    if down is None or down.end == down.start:
        parts = [users]
        stops = [float(duration if down is None else down.start)]
    else:
        n = min(len(users), MAX_SPLIT)
        size, extra = divmod(len(users), n)
        parts, at = [], 0
        for i in range(n):
            take = size + (1 if i < extra else 0)
            parts.append(users[at:at + take])
            at += take
        down_spacing = down.spacing()
        # The highest user stops first: user k stops (before - k) spacings into the ramp-down.
        stops = [sum(down.start + (down.before - k) * down_spacing for k in part) / len(part) for part in parts]
    out = []
    for i, (part, stop) in enumerate(zip(parts, stops)):
        delay = round(start(part[0]))
        out.append(Cohort(users=len(part), delay=delay, ramp=round(up_spacing * len(part)),
                          duration=max(1, round(stop) - delay), loops=None,
                          label=label(up.step, up.after, i + 1, len(parts))))
    return out
