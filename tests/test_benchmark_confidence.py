from __future__ import annotations

from dataclasses import replace
import json
import sys

import pytest

from loop_engine.benchmarks import (
    BenchmarkConfidenceAnalyzer,
    BenchmarkConfidencePolicy,
    JsonBenchmarkHistoryStore,
    run_consolidation_benchmark,
    run_project_audit_benchmark,
    write_benchmark_confidence,
)
from tools.benchmark_confidence import main as benchmark_confidence_main


@pytest.fixture(scope="module")
def benchmark_report(tmp_path_factory):
    return run_consolidation_benchmark(
        tmp_path_factory.mktemp("benchmark") / "report.json",
        sample_count=1,
        read_delay_seconds=0.04,
    )


def test_history_round_trip_is_immutable_and_newest_first(
    tmp_path,
    benchmark_report,
) -> None:
    store = JsonBenchmarkHistoryStore(tmp_path / "history")
    old = store.record(
        benchmark_report,
        run_id="old",
        recorded_at=1,
    )
    store.record(
        benchmark_report,
        run_id="new",
        recorded_at=2,
    )

    assert store.load("old") == old
    assert [entry.run_id for entry in store.list(limit=1)] == ["new"]
    with pytest.raises(FileExistsError, match="already exists"):
        store.record(benchmark_report, run_id="old")

    path = tmp_path / "history" / "old.json"
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["entry"]["passed"] = "yes"
    path.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(ValueError, match="must be boolean"):
        store.load("old")


def test_three_consistent_runs_produce_confident_consensus(
    tmp_path,
    benchmark_report,
) -> None:
    store = JsonBenchmarkHistoryStore(tmp_path / "history")
    entries = tuple(
        store.record(
            benchmark_report,
            run_id=f"run-{index}",
            recorded_at=float(index),
        )
        for index in range(3)
    )

    confidence = BenchmarkConfidenceAnalyzer().analyze(entries)

    assert confidence.status == "confident"
    assert confidence.consensus_winners == ("parallel_staged",)
    assert confidence.winner_share_basis_points == 10000
    assert confidence.passed_run_count == 3
    assert confidence.strategies[0].strategy == "parallel_staged"
    assert confidence.strategies[0].elapsed_mad_ms == 0


def test_confidence_reports_insufficient_and_failed_history(
    tmp_path,
    benchmark_report,
) -> None:
    store = JsonBenchmarkHistoryStore(tmp_path)
    first = store.record(benchmark_report, run_id="first", recorded_at=1)
    second = store.record(benchmark_report, run_id="second", recorded_at=2)

    insufficient = BenchmarkConfidenceAnalyzer().analyze((first, second))
    failed = BenchmarkConfidenceAnalyzer().analyze(
        (replace(first, passed=False), second, replace(second, run_id="third"))
    )

    assert insufficient.status == "insufficient_history"
    assert failed.status == "low_confidence"
    assert failed.reason == "one or more benchmark runs failed acceptance"


def test_incompatible_history_fails_closed(
    tmp_path,
    benchmark_report,
) -> None:
    store = JsonBenchmarkHistoryStore(tmp_path)
    first = store.record(benchmark_report, run_id="first", recorded_at=1)
    incompatible = replace(first, run_id="other", policy_sha256="f" * 64)

    with pytest.raises(ValueError, match="not comparable"):
        BenchmarkConfidenceAnalyzer(
            BenchmarkConfidencePolicy(minimum_runs=2)
        ).analyze((first, incompatible))


def test_different_benchmark_cases_cannot_share_confidence_history(
    tmp_path,
    benchmark_report,
) -> None:
    store = JsonBenchmarkHistoryStore(tmp_path)
    change = store.record(benchmark_report, run_id="change", recorded_at=1)
    audit_report = run_project_audit_benchmark(
        tmp_path / "reports" / "audit.json",
        sample_count=1,
        read_delay_seconds=0.02,
    )
    audit = store.record(audit_report, run_id="audit", recorded_at=2)

    with pytest.raises(ValueError, match="not comparable"):
        BenchmarkConfidenceAnalyzer(
            BenchmarkConfidencePolicy(minimum_runs=2)
        ).analyze((change, audit))


def test_confidence_report_writer_and_policy_validation(
    tmp_path,
    benchmark_report,
) -> None:
    store = JsonBenchmarkHistoryStore(tmp_path / "history")
    entry = store.record(benchmark_report, run_id="only")
    confidence = BenchmarkConfidenceAnalyzer().analyze((entry,))

    target = write_benchmark_confidence(
        tmp_path / "confidence.json",
        confidence,
    )

    assert json.loads(target.read_text(encoding="utf-8")) == confidence.to_dict()
    with pytest.raises(ValueError, match="window"):
        BenchmarkConfidencePolicy(history_window=0)
    with pytest.raises(ValueError, match="minimum runs"):
        BenchmarkConfidencePolicy(history_window=2, minimum_runs=3)
    with pytest.raises(ValueError, match="winner share"):
        BenchmarkConfidencePolicy(minimum_winner_share_basis_points=5000)
    with pytest.raises(ValueError, match="run_id"):
        store.record(benchmark_report, run_id="../escape")
    with pytest.raises(ValueError, match="limit"):
        store.list(limit=101)


def test_confidence_cli_analyzes_existing_history(
    tmp_path,
    benchmark_report,
    monkeypatch,
    capsys,
) -> None:
    history = tmp_path / "history"
    store = JsonBenchmarkHistoryStore(history)
    for index in range(3):
        store.record(
            benchmark_report,
            run_id=f"run-{index}",
            recorded_at=float(index),
        )
    target = tmp_path / "confidence.json"
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "benchmark_confidence",
            "--history-root",
            str(history),
            "--confidence-report",
            str(target),
        ],
    )

    exit_code = benchmark_confidence_main()
    output = json.loads(capsys.readouterr().out)

    assert exit_code == 0
    assert output["status"] == "confident"
    assert json.loads(target.read_text(encoding="utf-8")) == output
