"""Run the resource contention and recovery benchmark."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from loop_engine.benchmarks import run_resource_recovery_benchmark


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--output",
        type=Path,
        default=Path("build/resource_recovery_benchmark/report.json"),
    )
    parser.add_argument("--samples", type=int, default=3)
    args = parser.parse_args()
    report = run_resource_recovery_benchmark(
        args.output,
        sample_count=args.samples,
    )
    print(json.dumps(report.to_dict(), ensure_ascii=False, indent=2))
    return 0 if report.passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
