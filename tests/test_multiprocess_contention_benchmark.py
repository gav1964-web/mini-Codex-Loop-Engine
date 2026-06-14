from __future__ import annotations

import json

from loop_engine.benchmarks import run_multiprocess_contention_benchmark


def test_multiprocess_contention_benchmark(tmp_path) -> None:
    output = tmp_path / "report.json"

    report = run_multiprocess_contention_benchmark(
        output,
        operation_delay_seconds=0.08,
        timeout_seconds=8,
    )

    assert report.passed
    assert len(report.workers) == 2
    assert sum(
        worker.telemetry["scheduled"] for worker in report.workers
    ) >= 1
    payload = json.loads(output.read_text(encoding="utf-8"))
    assert payload["benchmark"] == "multiprocess-lease-contention"
    assert all(check["passed"] for check in payload["checks"])
