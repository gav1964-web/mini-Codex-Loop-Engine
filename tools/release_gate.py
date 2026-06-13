"""Run the complete mini-Codex Loop Engine release gate."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from loop_engine.release_gate import (
    CompositeReleaseGate,
    CompositeReleaseGatePolicy,
    ReleaseCommand,
)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser()
    parser.add_argument("--distribution", default="Ubuntu-22.04")
    parser.add_argument("--pytest-timeout", type=float, default=180.0)
    parser.add_argument("--wheel-timeout", type=float, default=240.0)
    parser.add_argument("--sandbox-timeout", type=float, default=60.0)
    parser.add_argument(
        "--report",
        type=Path,
        default=Path("build/release_gate/report.json"),
    )
    parser.add_argument("--degraded-ok", action="store_true")
    return parser


def main() -> int:
    args = _parser().parse_args()
    workspace = Path.cwd().resolve()
    python = sys.executable
    policy = CompositeReleaseGatePolicy(
        workspace_root=workspace,
        pytest=ReleaseCommand(
            argv=(python, "-m", "pytest"),
            timeout_seconds=args.pytest_timeout,
        ),
        wheel_smoke=ReleaseCommand(
            argv=(
                python,
                "-m",
                "tools.wheel_release_smoke",
                "--wheel-dir",
                "build/release_gate/dist",
                "--timeout",
                str(args.wheel_timeout),
            ),
            timeout_seconds=args.wheel_timeout,
        ),
        sandbox=ReleaseCommand(
            argv=(
                python,
                "-m",
                "examples.plugin_sandbox_smoke",
                "--distribution",
                args.distribution,
                "--work-root",
                "build/release_gate/sandbox",
            ),
            timeout_seconds=args.sandbox_timeout,
        ),
        report_path=args.report,
    )
    report = CompositeReleaseGate(policy).run(
        degraded_ok=args.degraded_ok
    )
    print(json.dumps(report.to_dict(), ensure_ascii=False, indent=2))
    return 0 if report.releasable else 1


if __name__ == "__main__":
    raise SystemExit(main())
