from app.services.comparison import compare_summaries


def summary(error_rate, throughput, p50, p95, p99):
    return {
        "error_rate_percent": error_rate,
        "throughput_rps": throughput,
        "response_time_ms": {"p50": p50, "p95": p95, "p99": p99},
    }


def test_comparison_reports_latency_and_error_regressions():
    result = compare_summaries(
        summary(2, 8, 120, 250, 400),
        summary(0, 10, 100, 200, 300),
    )
    assert result["status"] == "regressed"
    assert set(result["regressions"]) == {
        "error_rate_percent", "throughput_rps", "p50_ms", "p95_ms", "p99_ms",
    }
    assert result["metrics"]["p95_ms"]["percent"] == 25.0


def test_comparison_marks_improvements_as_stable():
    result = compare_summaries(
        summary(0, 12, 80, 150, 250),
        summary(1, 10, 100, 200, 300),
    )
    assert result["status"] == "stable_or_improved"
    assert result["regressions"] == []
