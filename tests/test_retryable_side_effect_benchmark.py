from __future__ import annotations

import json

from loop_engine.benchmarks import run_retryable_side_effect_benchmark


def test_retryable_side_effect_benchmark_is_bounded_and_idempotent(
    tmp_path,
) -> None:
    output = tmp_path / "report.json"

    report = run_retryable_side_effect_benchmark(
        output,
        sample_count=1,
        operation_delay_seconds=0.02,
    )

    assert report.passed
    assert report.benchmark == "retryable-idempotent-side-effect"
    assert report.ranking.winners == ("parallel_retry",)
    assert {run.strategy for run in report.comparison.runs} == {
        "monolithic",
        "parallel_retry",
        "sequential_retry",
    }
    payload = json.loads(output.read_text(encoding="utf-8"))
    assert all(check["passed"] for check in payload["checks"])
