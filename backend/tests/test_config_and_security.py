import pytest
from pydantic import ValidationError

from app.models.test_config import LoadTestConfig, mask_headers
from app.services.security import TargetNotAllowed, api_key_ok, check_target
from tests.conftest import make_settings


def cfg(**kw):
    return LoadTestConfig(**{"name": "t", "target_url": "http://example.com/x", **kw})


def test_defaults_are_a_simple_get_load_test():
    c = cfg()
    assert c.method == "GET" and c.expected_status_codes == [200] and c.users == 10


@pytest.mark.parametrize("url", ["ftp://example.com", "example.com", "http://", "http://user:pw@example.com/"])
def test_rejects_bad_urls(url):
    with pytest.raises(ValidationError):
        cfg(target_url=url)


def test_rejects_ramp_longer_than_duration():
    with pytest.raises(ValidationError):
        cfg(duration_seconds=10, ramp_up_seconds=20)


def test_accepts_valid_increasing_step_profile():
    c = cfg(users=100, duration_seconds=90, profile=[
        {"time_seconds": 0, "users": 10},
        {"time_seconds": 30, "users": 50},
        {"time_seconds": 60, "users": 100},
    ])
    assert c.profile[-1].users == c.users


@pytest.mark.parametrize("overrides", [
    {"profile": [{"time_seconds": 5, "users": 10}, {"time_seconds": 10, "users": 20}], "users": 20},
    {"profile": [{"time_seconds": 0, "users": 10}, {"time_seconds": 10, "users": 20}], "users": 20, "duration_seconds": 10},
    {"profile": [{"time_seconds": 0, "users": 10}, {"time_seconds": 10, "users": 20}], "users": 20, "ramp_up_seconds": 2},
    {"profile": [{"time_seconds": 0, "users": 10}, {"time_seconds": 10, "users": 20}, {"time_seconds": 9, "users": 30}], "users": 30},
    {"profile": [{"time_seconds": 0, "users": 10}, {"time_seconds": 10, "users": 10}], "users": 10},
    {"profile": [{"time_seconds": 0, "users": 10}, {"time_seconds": 10, "users": 20}], "users": 30},
    {"profile": [{"time_seconds": 0, "users": 10}]},
])
def test_rejects_invalid_step_profiles(overrides):
    with pytest.raises(ValidationError):
        cfg(**overrides)


def test_rejects_header_injection():
    with pytest.raises(ValidationError):
        cfg(headers={"X-A": "ok\r\nInjected: 1"})


def test_rejects_unknown_fields():
    with pytest.raises(ValidationError):
        cfg(surprise=True)


def test_masks_secret_headers():
    masked = mask_headers({"Authorization": "Bearer abc", "X-Trace": "1", "cookie": "s=1"})
    assert masked == {"Authorization": "********", "X-Trace": "1", "cookie": "********"}
    assert "abc" not in str(cfg(headers={"Authorization": "Bearer abc"}).masked())


def test_link_local_always_blocked(tmp_path):
    s = make_settings(tmp_path, allow_private_targets=True)
    with pytest.raises(TargetNotAllowed):
        check_target("http://169.254.169.254/latest/meta-data", s)


def test_private_blocked_unless_allowed(tmp_path):
    with pytest.raises(TargetNotAllowed):
        check_target("http://127.0.0.1:9000/", make_settings(tmp_path, allow_private_targets=False))
    check_target("http://127.0.0.1:9000/", make_settings(tmp_path, allow_private_targets=True))
    check_target("http://127.0.0.1:9000/", make_settings(tmp_path, allow_private_targets=False, allowed_hosts=("127.0.0.1",)))


def test_api_key_check(tmp_path):
    s = make_settings(tmp_path, api_key="secret")
    assert api_key_ok("secret", s) and not api_key_ok("nope", s) and not api_key_ok(None, s)
    assert api_key_ok(None, make_settings(tmp_path))
