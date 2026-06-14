"""Build a cross-case profile from independent confidence reports."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from loop_engine.benchmarks import (
    CrossCaseProfileAnalyzer,
    CrossCaseProfilePolicy,
    load_benchmark_confidence,
    write_cross_case_profile,
)

_ROLE_MAPPINGS = {
    "python-project-change": {
        "monolithic": "monolithic",
        "sequential_staged": "sequential",
        "parallel_staged": "parallel",
    },
    "python-project-audit": {
        "monolithic": "monolithic",
        "sequential_evidence": "sequential",
        "parallel_evidence": "parallel",
    },
    "resource-contention-recovery": {
        "monolithic": "monolithic",
        "sequential_recovery": "sequential",
        "parallel_recovery": "parallel",
    },
    "retryable-idempotent-side-effect": {
        "monolithic": "monolithic",
        "sequential_retry": "sequential",
        "parallel_retry": "parallel",
    },
}


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--change-confidence",
        type=Path,
        default=Path(
            "build/benchmarks/python-project-change/confidence.json"
        ),
    )
    parser.add_argument(
        "--audit-confidence",
        type=Path,
        default=Path(
            "build/benchmarks/python-project-audit/confidence.json"
        ),
    )
    parser.add_argument(
        "--recovery-confidence",
        type=Path,
        default=Path(
            "build/benchmarks/resource-contention-recovery/confidence.json"
        ),
    )
    parser.add_argument(
        "--retry-confidence",
        type=Path,
        default=Path(
            "build/benchmarks/retryable-idempotent-side-effect/"
            "confidence.json"
        ),
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=Path("build/benchmarks/cross_case_profile.json"),
    )
    parser.add_argument("--minimum-winner-share-bp", type=int, default=6700)
    return parser


def main() -> int:
    args = _parser().parse_args()
    reports = (
        load_benchmark_confidence(args.change_confidence),
        load_benchmark_confidence(args.audit_confidence),
        load_benchmark_confidence(args.recovery_confidence),
        load_benchmark_confidence(args.retry_confidence),
    )
    profile = CrossCaseProfileAnalyzer(
        CrossCaseProfilePolicy(
            role_mappings=_ROLE_MAPPINGS,
            minimum_cases=4,
            minimum_winner_share_basis_points=(
                args.minimum_winner_share_bp
            ),
        )
    ).analyze(reports)
    write_cross_case_profile(args.output, profile)
    print(json.dumps(profile.to_dict(), ensure_ascii=False, indent=2))
    return 0 if profile.status == "confident" else 1


if __name__ == "__main__":
    raise SystemExit(main())
