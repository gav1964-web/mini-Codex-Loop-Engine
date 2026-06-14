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
    write_benchmark_confidence,
)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser()
    parser.add_argument("--run", action="store_true")
    parser.add_argument("--samples", type=int, default=3)
    parser.add_argument(
        "--benchmark-report",
        type=Path,
        default=Path("build/consolidation_benchmark/report.json"),
    )
    parser.add_argument(
        "--history-root",
        type=Path,
        default=Path("build/consolidation_benchmark/history"),
    )
    parser.add_argument(
        "--confidence-report",
        type=Path,
        default=Path("build/consolidation_benchmark/confidence.json"),
    )
    parser.add_argument("--history-window", type=int, default=7)
    parser.add_argument("--minimum-runs", type=int, default=3)
    parser.add_argument("--minimum-winner-share-bp", type=int, default=6700)
    return parser


def main() -> int:
    args = _parser().parse_args()
    workspace = Path.cwd().resolve()
    store = JsonBenchmarkHistoryStore(
        args.history_root,
        workspace_root=workspace,
    )
    benchmark_passed = True
    if args.run:
        benchmark = run_consolidation_benchmark(
            args.benchmark_report,
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
    write_benchmark_confidence(args.confidence_report, confidence)
    print(json.dumps(confidence.to_dict(), ensure_ascii=False, indent=2))
    return 0 if benchmark_passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
