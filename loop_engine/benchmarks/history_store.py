"""Immutable JSON persistence for benchmark ranking snapshots."""

from __future__ import annotations

import hashlib
import json
import os
import re
import time
from dataclasses import asdict
from pathlib import Path
from typing import Any
from uuid import uuid4

from .history_models import (
    BENCHMARK_HISTORY_SCHEMA_VERSION,
    MAX_BENCHMARK_HISTORY_LIMIT,
    BenchmarkHistoryEntry,
    BenchmarkStrategySnapshot,
)
from .models import BenchmarkReport

_RUN_ID_PATTERN = re.compile(r"^[A-Za-z0-9_-]+$")


class JsonBenchmarkHistoryStore:
    def __init__(
        self,
        root: str | Path,
        *,
        workspace_root: str | Path | None = None,
    ) -> None:
        self.root = Path(root).resolve()
        if workspace_root is not None:
            workspace = Path(workspace_root).resolve()
            try:
                self.root.relative_to(workspace)
            except ValueError as exc:
                raise ValueError("benchmark history root escapes workspace") from exc

    def record(
        self,
        report: BenchmarkReport,
        *,
        run_id: str | None = None,
        recorded_at: float | None = None,
    ) -> BenchmarkHistoryEntry:
        if not isinstance(report, BenchmarkReport):
            raise TypeError("benchmark history requires BenchmarkReport")
        entry = _snapshot(
            report,
            run_id=_validated_run_id(run_id or uuid4().hex),
            recorded_at=time.time() if recorded_at is None else recorded_at,
        )
        target = self.root / f"{entry.run_id}.json"
        if target.exists():
            raise FileExistsError(
                f"benchmark history snapshot already exists: {entry.run_id}"
            )
        _write_json(
            target,
            {
                "schema_version": BENCHMARK_HISTORY_SCHEMA_VERSION,
                "entry": _entry_to_dict(entry),
            },
        )
        return entry

    def load(self, run_id: str) -> BenchmarkHistoryEntry:
        identity = _validated_run_id(run_id)
        payload = json.loads(
            (self.root / f"{identity}.json").read_text(encoding="utf-8")
        )
        version = payload.get("schema_version")
        if version != BENCHMARK_HISTORY_SCHEMA_VERSION:
            raise ValueError(
                f"unsupported benchmark history schema version: {version}"
            )
        entry = _entry_from_dict(dict(payload["entry"]))
        if entry.run_id != identity:
            raise ValueError("benchmark history identity does not match its path")
        return entry

    def list(self, *, limit: int = 20) -> tuple[BenchmarkHistoryEntry, ...]:
        if (
            not isinstance(limit, int)
            or isinstance(limit, bool)
            or limit <= 0
            or limit > MAX_BENCHMARK_HISTORY_LIMIT
        ):
            raise ValueError("benchmark history limit must be between 1 and 100")
        if not self.root.exists():
            return ()
        entries = [
            self.load(path.stem)
            for path in self.root.glob("*.json")
            if _RUN_ID_PATTERN.fullmatch(path.stem)
        ]
        entries.sort(
            key=lambda entry: (entry.recorded_at, entry.run_id),
            reverse=True,
        )
        return tuple(entries[:limit])


def _snapshot(
    report: BenchmarkReport,
    *,
    run_id: str,
    recorded_at: float,
) -> BenchmarkHistoryEntry:
    metrics = {run.strategy: run for run in report.comparison.runs}
    ranks = {entry.strategy: entry for entry in report.ranking.entries}
    if set(metrics) != set(ranks):
        raise ValueError("benchmark comparison and ranking strategies differ")
    policy_payload = report.ranking.to_dict()["policy"]
    return BenchmarkHistoryEntry(
        run_id=run_id,
        recorded_at=float(recorded_at),
        benchmark=report.benchmark,
        case=report.comparison.case,
        passed=report.passed,
        policy_sha256=_sha256(policy_payload),
        strategies=tuple(
            BenchmarkStrategySnapshot(
                strategy=name,
                rank=ranks[name].rank,
                eligible=ranks[name].eligible,
                elapsed_ms=metrics[name].elapsed_ms,
            )
            for name in sorted(metrics)
        ),
        winners=tuple(sorted(report.ranking.winners)),
    )


def _entry_to_dict(entry: BenchmarkHistoryEntry) -> dict[str, Any]:
    payload = asdict(entry)
    payload["strategies"] = [asdict(item) for item in entry.strategies]
    payload["winners"] = list(entry.winners)
    return payload


def _entry_from_dict(value: dict[str, Any]) -> BenchmarkHistoryEntry:
    if not isinstance(value.get("passed"), bool):
        raise ValueError("benchmark history passed must be boolean")
    return BenchmarkHistoryEntry(
        run_id=_validated_run_id(str(value["run_id"])),
        recorded_at=float(value["recorded_at"]),
        benchmark=str(value["benchmark"]),
        case=str(value["case"]),
        passed=value["passed"],
        policy_sha256=str(value["policy_sha256"]),
        strategies=tuple(
            BenchmarkStrategySnapshot(**dict(item))
            for item in value["strategies"]
        ),
        winners=tuple(str(item) for item in value["winners"]),
    )


def _validated_run_id(value: str) -> str:
    if not isinstance(value, str) or not _RUN_ID_PATTERN.fullmatch(value):
        raise ValueError(
            "run_id may contain only letters, digits, underscore, and hyphen"
        )
    return value


def _sha256(value: Any) -> str:
    encoded = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp")
    temporary.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    os.replace(temporary, path)
