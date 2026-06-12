"""Run the strict real-sandbox release gate."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from loop_engine.sandbox_release_gate import (
    SandboxReleaseGate,
    SandboxReleaseGatePolicy,
)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser()
    parser.add_argument("--distribution", default="Ubuntu-22.04")
    parser.add_argument("--timeout", type=float, default=60.0)
    parser.add_argument(
        "--work-root",
        type=Path,
        default=Path("build/sandbox_release_gate/smoke"),
    )
    parser.add_argument(
        "--report",
        type=Path,
        default=Path("build/sandbox_release_gate/report.json"),
    )
    parser.add_argument("--degraded-ok", action="store_true")
    return parser


def main() -> int:
    args = _parser().parse_args()
    workspace = Path.cwd().resolve()
    command = (
        sys.executable,
        "-m",
        "examples.plugin_sandbox_smoke",
        "--distribution",
        args.distribution,
        "--work-root",
        str(args.work_root),
    )
    report = SandboxReleaseGate(
        SandboxReleaseGatePolicy.create(
            workspace_root=workspace,
            command=command,
            report_path=args.report,
            timeout_seconds=args.timeout,
        )
    ).run(degraded_ok=args.degraded_ok)
    print(json.dumps(report.to_dict(), ensure_ascii=False, indent=2))
    return 0 if report.releasable else 1


if __name__ == "__main__":
    raise SystemExit(main())
