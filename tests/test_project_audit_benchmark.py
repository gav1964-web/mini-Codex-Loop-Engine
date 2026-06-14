from __future__ import annotations

import json

from loop_engine.benchmarks import run_project_audit_benchmark


def test_project_audit_benchmark_exercises_read_only_architecture(
    tmp_path,
) -> None:
    output = tmp_path / "report.json"

    report = run_project_audit_benchmark(
        output,
        sample_count=1,
        read_delay_seconds=0.02,
    )

    assert report.passed
    assert report.benchmark == "python-project-audit"
    assert report.ranking.winners == ("parallel_evidence",)
    assert {run.strategy for run in report.comparison.runs} == {
        "monolithic",
        "parallel_evidence",
        "sequential_evidence",
    }
    payload = json.loads(output.read_text(encoding="utf-8"))
    assert payload["benchmark"] == "python-project-audit"
    assert payload["passed"] is True
