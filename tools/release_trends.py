"""Archive a release report and emit bounded regression trends."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from loop_engine.release_gate import CompositeReleaseGateReport
from loop_engine.release_history import (
    JsonReleaseHistoryStore,
    ReleaseHistoryAnalyzer,
    ReleaseRegressionPolicy,
    write_release_trend,
)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--record-report",
        type=Path,
    )
    parser.add_argument(
        "--history-root",
        type=Path,
        default=Path("build/release_history/runs"),
    )
    parser.add_argument(
        "--trend-report",
        type=Path,
        default=Path("build/release_history/trend.json"),
    )
    parser.add_argument("--history-window", type=int, default=5)
    parser.add_argument("--duration-ratio", type=float, default=1.25)
    parser.add_argument("--duration-absolute-seconds", type=float, default=1.0)
    return parser


def main() -> int:
    args = _parser().parse_args()
    workspace = Path.cwd().resolve()
    trend_path = args.trend_report.resolve()
    try:
        trend_path.relative_to(workspace)
    except ValueError as exc:
        raise ValueError("release trend report escapes workspace") from exc
    store = JsonReleaseHistoryStore(
        args.history_root,
        workspace_root=workspace,
    )
    if args.record_report is not None:
        payload = json.loads(args.record_report.read_text(encoding="utf-8"))
        gate_report = CompositeReleaseGateReport.from_dict(payload)
        store.record(gate_report)
    policy = ReleaseRegressionPolicy(
        history_window=args.history_window,
        duration_ratio=args.duration_ratio,
        duration_absolute_seconds=args.duration_absolute_seconds,
    )
    entries = store.list(limit=policy.history_window + 1)
    trend = ReleaseHistoryAnalyzer(policy).analyze(entries)
    write_release_trend(trend_path, trend)
    print(json.dumps(trend.to_dict(), ensure_ascii=False, indent=2))
    return 1 if trend.status == "regressed" else 0


if __name__ == "__main__":
    raise SystemExit(main())
