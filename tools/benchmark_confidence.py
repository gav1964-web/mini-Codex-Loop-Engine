"""Run or analyze consolidation benchmark history."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from loop_engine.benchmarks import (
    BenchmarkConfidenceAnalyzer,
    BenchmarkConfidencePolicy,
    JsonBenchmarkHistoryStore,
    run_consolidation_benchmark,
    run_project_audit_benchmark,
    run_resource_recovery_benchmark,
    run_retryable_side_effect_benchmark,
    write_benchmark_confidence,
)

_RUNNERS = {
    "python-project-change": run_consolidation_benchmark,
    "python-project-audit": run_project_audit_benchmark,
    "resource-contention-recovery": run_resource_recovery_benchmark,
    "retryable-idempotent-side-effect": (
        run_retryable_side_effect_benchmark
    ),
}


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser()
    parser.add_argument("--run", action="store_true")
    parser.add_argument(
        "--case",
        choices=sorted(_RUNNERS),
        default="python-project-change",
    )
    parser.add_argument("--samples", type=int, default=3)
    parser.add_argument(
        "--benchmark-report",
        type=Path,
    )
    parser.add_argument(
        "--history-root",
        type=Path,
    )
    parser.add_argument(
        "--confidence-report",
        type=Path,
    )
    parser.add_argument("--history-window", type=int, default=7)
    parser.add_argument("--minimum-runs", type=int, default=3)
    parser.add_argument("--minimum-winner-share-bp", type=int, default=6700)
    return parser


def main() -> int:
    args = _parser().parse_args()
    workspace = Path.cwd().resolve()
    artifact_root = Path("build/benchmarks") / args.case
    benchmark_report = args.benchmark_report or artifact_root / "report.json"
    history_root = args.history_root or artifact_root / "history"
    confidence_report = (
        args.confidence_report or artifact_root / "confidence.json"
    )
    store = JsonBenchmarkHistoryStore(
        history_root,
        workspace_root=workspace,
    )
    benchmark_passed = True
    if args.run:
        benchmark = _RUNNERS[args.case](
            benchmark_report,
            sample_count=args.samples,
        )
        benchmark_passed = benchmark.passed
        store.record(benchmark)
    policy = BenchmarkConfidencePolicy(
        history_window=args.history_window,
        minimum_runs=args.minimum_runs,
        minimum_winner_share_basis_points=args.minimum_winner_share_bp,
    )
    confidence = BenchmarkConfidenceAnalyzer(policy).analyze(
        store.list(limit=policy.history_window)
    )
    write_benchmark_confidence(confidence_report, confidence)
    print(json.dumps(confidence.to_dict(), ensure_ascii=False, indent=2))
    return 0 if benchmark_passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
