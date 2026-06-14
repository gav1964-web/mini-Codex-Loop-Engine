from __future__ import annotations

import json

from loop_engine.benchmarks import run_consolidation_benchmark


def test_consolidation_benchmark_exercises_real_architecture(tmp_path) -> None:
    output = tmp_path / "report.json"

    report = run_consolidation_benchmark(
        output,
        sample_count=1,
        read_delay_seconds=0.02,
    )

    assert report.passed
    assert report.ranking.winners == ("parallel_staged",)
    assert {run.strategy for run in report.comparison.runs} == {
        "monolithic",
        "parallel_staged",
        "sequential_staged",
    }
    assert all(run.root_status == "completed" for run in report.comparison.runs)
    payload = json.loads(output.read_text(encoding="utf-8"))
    assert payload["schema_version"] == 1
    assert payload["benchmark"] == "python-project-change"
    assert payload["passed"] is True
