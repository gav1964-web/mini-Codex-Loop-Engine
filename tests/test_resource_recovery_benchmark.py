from __future__ import annotations

import json

from loop_engine.benchmarks import run_resource_recovery_benchmark


def test_resource_recovery_benchmark_exercises_interrupted_resume(
    tmp_path,
) -> None:
    output = tmp_path / "report.json"

    report = run_resource_recovery_benchmark(
        output,
        sample_count=1,
        operation_delay_seconds=0.02,
    )

    assert report.passed
    assert report.benchmark == "resource-contention-recovery"
    assert report.ranking.winners == ("parallel_recovery",)
    assert {run.strategy for run in report.comparison.runs} == {
        "monolithic",
        "parallel_recovery",
        "sequential_recovery",
    }
    payload = json.loads(output.read_text(encoding="utf-8"))
    assert payload["passed"] is True
    assert all(check["passed"] for check in payload["checks"])
