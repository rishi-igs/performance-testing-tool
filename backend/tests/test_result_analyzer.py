from pathlib import Path

from app.models.test_config import Thresholds
from app.services.result_analyzer import Sample, parse_jtl, percentile, summarize, timeline

HEADER = "timeStamp,elapsed,label,responseCode,responseMessage,threadName,dataType,success,failureMessage,bytes,sentBytes,grpThreads,allThreads,URL,Latency,IdleTime,Connect\n"


def s(ts, elapsed=100, code="200", ok=True, users=10, failure="", msg="OK"):
    return Sample(ts=ts, elapsed=elapsed, latency=elapsed, code=code, message=msg, success=ok, failure=failure, users=users)


def test_percentile_nearest_rank():
    values = list(range(1, 101))
    assert percentile(values, 50) == 50
    assert percentile(values, 95) == 95
    assert percentile(values, 99) == 99
    assert percentile([], 95) == 0
    assert percentile([7], 99) == 7


def test_summary_basics():
    samples = [s(1000 + i * 100, elapsed=100 + i) for i in range(100)]
    out = summarize(samples, Thresholds())
    assert out["total_requests"] == 100 and out["failed_requests"] == 0
    assert out["response_time_ms"]["p95"] == 194
    assert out["status_codes"] == {"200": 100}
    assert out["verdict"] == "pass" and out["warnings"] == []
    assert out["throughput_rps"] > 0


def test_error_rate_warning_names_the_load_level():
    good = [s(i * 1000, users=50) for i in range(10)]
    bad = [s(10_000 + i * 100, ok=(i % 2 == 0), code="200" if i % 2 == 0 else "500", users=350) for i in range(40)]
    out = summarize(good + bad, Thresholds(max_error_rate_percent=5))
    msg = next(w for w in out["warnings"] if w["code"] == "error_rate")["message"]
    assert "350 users" in msg and "limit" in msg
    assert out["verdict"] == "fail"
    assert any(w["code"] == "http_5xx" for w in out["warnings"])


def test_p95_threshold_and_disabled_threshold():
    samples = [s(i * 100, elapsed=2000) for i in range(20)]
    assert any(w["code"] == "p95_latency" for w in summarize(samples, Thresholds(max_p95_ms=1000))["warnings"])
    assert summarize(samples, Thresholds(max_p95_ms=None))["verdict"] == "pass"


def test_connection_and_assertion_failures_are_counted_separately():
    samples = [
        s(1000, ok=False, code="Non HTTP response code: java.net.ConnectException", msg="Connection refused"),
        s(1100, ok=False, code="404", failure="Test failed: code expected to equal /200"),
        s(1200),
    ]
    out = summarize(samples, Thresholds(max_error_rate_percent=100))
    assert out["connection_failures"] == 1 and out["assertion_failures"] == 1 and out["failed_requests"] == 2


def test_sudden_degradation_detected():
    fast = [s(i * 1000, elapsed=50) for i in range(20)]
    slow = [s(20_000 + i * 200, elapsed=900, users=80) for i in range(25)]
    out = summarize(fast + slow, Thresholds(max_p95_ms=None))
    assert any(w["code"] == "degradation" for w in out["warnings"])


def test_timeline_buckets():
    samples = [s(i * 1000) for i in range(10)]
    points = timeline(samples, bucket_seconds=5)
    assert [p["t"] for p in points] == [0, 5] and points[0]["requests"] == 5


def test_parse_jtl_tolerates_missing_file_and_partial_last_line(tmp_path: Path):
    assert parse_jtl(tmp_path / "missing.jtl") == []
    f = tmp_path / "r.jtl"
    f.write_text(HEADER + "1000,120,x,200,OK,t,text,true,,10,0,5,5,u,100,0,1\n1100,13")
    out = parse_jtl(f)
    assert len(out) == 1 and out[0].elapsed == 120


def test_empty_results_have_no_data_verdict():
    assert summarize([], Thresholds())["verdict"] == "no_data"
