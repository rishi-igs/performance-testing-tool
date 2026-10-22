"""Traffic profiles: steps of users over time become standard Thread Groups (no JMeter needed)."""
import math

import pytest
from pydantic import ValidationError

from app.models.test_config import LoadTestConfig, TrafficStep
from app.services import load_profile


def steps(*rows):
    return [TrafficStep(time_seconds=t, users=u, ramp_seconds=r) for t, u, r in rows]


def running(cohorts, t):
    """Users running at time t, as JMeter starts and stops them (threads start evenly over the ramp-up)."""
    total = 0
    for c in cohorts:
        end = c.delay + c.duration
        for i in range(c.users):
            start = c.delay + (i * c.ramp / c.users if c.ramp else 0)
            total += start <= t < end
    return total


def label(step, users, part, parts):
    return f"step {step + 1} ({part}/{parts})"


@pytest.mark.parametrize("profile, duration, tolerance", [
    (steps((0, 10, 0), (30, 50, 0), (60, 100, 0)), 90, 0),                       # step load
    (steps((0, 5, 0), (10, 500, 0), (30, 5, 0)), 60, 0),                         # spike and back
    (steps((0, 20, 20), (40, 0, 0), (50, 20, 10), (70, 5, 10)), 90, 2),          # waves with a pause
    (steps((0, 1, 0), (1, 200, 59)), 60, 4),                                     # gradual increase (breakpoint)
    (steps((0, 100, 30), (90, 0, 30)), 120, 10),                                 # ramp up, hold, ramp down
])
def test_thread_groups_follow_the_planned_users(profile, duration, tolerance):
    cohorts = load_profile.cohorts(profile, duration, label)
    for t in range(duration):
        planned = load_profile.users_at(profile, t + 0.5)
        assert abs(running(cohorts, t + 0.5) - planned) <= max(tolerance, 1), f"at {t}s"
    assert all(c.delay >= 0 and c.duration >= 1 and c.users >= 1 for c in cohorts)


def test_a_spike_starts_a_burst_that_stops_at_once():
    cohorts = load_profile.cohorts(steps((0, 5, 0), (10, 500, 0), (30, 5, 0)), 60, label)
    assert [(c.users, c.delay, c.ramp, c.duration) for c in cohorts] == [(5, 0, 0, 60), (495, 10, 0, 20)]


def test_ramp_downs_are_split_and_the_last_users_added_stop_first():
    cohorts = load_profile.cohorts(steps((0, 40, 40), (60, 0, 20)), 90, label)
    assert len(cohorts) == 10 and sum(c.users for c in cohorts) == 40
    stops = [c.delay + c.duration for c in cohorts]
    assert stops == sorted(stops, reverse=True)          # users started later stop earlier
    assert min(stops) >= 60 and max(stops) <= 80


def test_curve_and_scaling():
    profile = steps((0, 10, 0), (20, 50, 10), (60, 0, 0))
    assert load_profile.curve(profile, 90) == [{"t": 0, "users": 0}, {"t": 0, "users": 10}, {"t": 20, "users": 10},
                                               {"t": 30, "users": 50}, {"t": 60, "users": 50}, {"t": 60, "users": 0},
                                               {"t": 90, "users": 0}]
    half = load_profile.scaled(profile, 25)
    assert [(s.time_seconds, s.users, s.ramp_seconds) for s in half] == [(0, 5, 0), (20, 25, 10), (60, 0, 0)]
    assert load_profile.scaled(steps((0, 5, 0), (10, 500, 0), (30, 5, 0)), 1) == steps((10, 1, 0), (30, 0, 0))


def test_too_many_ramp_downs_are_refused():
    many = steps(*[(i * 20, 30 if i % 2 == 0 else 1, 10) for i in range(80)])
    with pytest.raises(load_profile.ProfileError, match="thread groups"):
        load_profile.cohorts(many, 80 * 20 + 20, label)


def cfg(**kw):
    return LoadTestConfig(**{"name": "t", "target_url": "http://example.com/x", **kw})


def test_profiles_accept_units_and_set_the_peak():
    c = cfg(test_type="spike", duration_seconds="2m",
            profile=[{"time": "0s", "users": 5}, {"time": "10s", "users": 500}, {"time": "30s", "users": 5}])
    assert c.users == 500 and c.duration_seconds == 120
    assert [(s.time_seconds, s.users) for s in c.profile] == [(0, 5), (10, 500), (30, 5)]
    wave = cfg(duration_seconds=300, profile=[{"time": 0, "users": 50, "ramp": "1m"}, {"time": "2m", "users": 0},
                                              {"time": "150s", "users": 50, "ramp": "30s"}])
    assert wave.profile[0].ramp_seconds == 60 and wave.users == 50


@pytest.mark.parametrize("profile, message", [
    ([{"time": 0, "users": 10, "ramp": 30}, {"time": 20, "users": 0}], "before the ramp of step 1 ends"),
    ([{"time": 0, "users": 10}, {"time": 50, "users": 20, "ramp": 20}], "after the test ends"),
    ([{"time": 0, "users": 0}, {"time": 5, "users": 20}], "does not change"),
    ([{"time": 0, "users": 10}, {"time": "soon", "users": 20}], "not a duration"),
])
def test_invalid_profiles_are_explained(profile, message):
    with pytest.raises(ValidationError, match=message):
        cfg(duration_seconds=60, profile=profile)


def test_the_example_configs_are_valid():
    import yaml
    from pathlib import Path
    folder = Path(__file__).resolve().parents[2] / "config"
    for path in sorted(folder.glob("*.yaml")):
        config = LoadTestConfig(**yaml.safe_load(path.read_text(encoding="utf-8")))
        if config.profile:
            assert load_profile.cohorts(config.profile, config.duration_seconds, label), path.name


def test_stop_condition_needs_a_limit():
    with pytest.raises(ValidationError, match="needs error_rate_percent"):
        cfg(stop_when={"for_seconds": 5})
    assert math.isclose(cfg(stop_when={"error_rate_percent": 5}).stop_when.error_rate_percent, 5)
