"""Run the real multi-process lease contention benchmark."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from loop_engine.benchmarks import run_multiprocess_contention_benchmark


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--output",
        type=Path,
        default=Path("build/multiprocess_contention/report.json"),
    )
    args = parser.parse_args()
    report = run_multiprocess_contention_benchmark(args.output)
    print(json.dumps(report.to_dict(), ensure_ascii=False, indent=2))
    return 0 if report.passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
