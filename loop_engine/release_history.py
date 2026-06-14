"""Persistent release snapshots and bounded regression trend analysis."""

from __future__ import annotations

import json
import os
import re
import statistics
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any
from uuid import uuid4

from .release_gate import CompositeReleaseGateReport

RELEASE_HISTORY_SCHEMA_VERSION = 1
RELEASE_TREND_SCHEMA_VERSION = 1
MAX_RELEASE_HISTORY_LIMIT = 100
_RELEASE_ID_PATTERN = re.compile(r"^[A-Za-z0-9_-]+$")
_STATUS_RANK = {"failed": 0, "degraded": 1, "passed": 2}


@dataclass(frozen=True, slots=True)
class ReleaseHistoryEntry:
    release_id: str
    recorded_at: float
    report: CompositeReleaseGateReport


@dataclass(frozen=True, slots=True)
class ReleaseRegressionPolicy:
    history_window: int = 5
    duration_ratio: float = 1.25
    duration_absolute_seconds: float = 1.0

    def __post_init__(self) -> None:
        if (
            not isinstance(self.history_window, int)
            or isinstance(self.history_window, bool)
            or self.history_window <= 0
            or self.history_window > MAX_RELEASE_HISTORY_LIMIT - 1
        ):
            raise ValueError("release history window must be between 1 and 99")
        if self.duration_ratio <= 1:
            raise ValueError("release duration ratio must be greater than 1")
        if self.duration_absolute_seconds < 0:
            raise ValueError("release duration absolute threshold must not be negative")


@dataclass(frozen=True, slots=True)
class ReleaseStageTrend:
    name: str
    current_seconds: float
    baseline_median_seconds: float
    delta_seconds: float
    ratio: float | None
    regressed: bool


@dataclass(frozen=True, slots=True)
class ReleaseTrendReport:
    schema_version: int
    status: str
    current_release_id: str
    baseline_release_ids: tuple[str, ...]
    current_gate_status: str
    previous_gate_status: str | None
    regressions: tuple[str, ...]
    stage_trends: tuple[ReleaseStageTrend, ...]

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["baseline_release_ids"] = list(self.baseline_release_ids)
        payload["regressions"] = list(self.regressions)
        payload["stage_trends"] = [asdict(item) for item in self.stage_trends]
        return payload


class JsonReleaseHistoryStore:
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
                raise ValueError("release history root escapes workspace") from exc

    def record(
        self,
        report: CompositeReleaseGateReport,
        *,
        release_id: str | None = None,
    ) -> ReleaseHistoryEntry:
        if not isinstance(report, CompositeReleaseGateReport):
            raise TypeError("release history requires CompositeReleaseGateReport")
        entry = ReleaseHistoryEntry(
            release_id=_release_id(release_id or uuid4().hex),
            recorded_at=report.finished_at,
            report=report,
        )
        target = self.root / f"{entry.release_id}.json"
        if target.exists():
            raise FileExistsError(
                f"release history snapshot already exists: {entry.release_id}"
            )
        _write_json(
            target,
            {
                "schema_version": RELEASE_HISTORY_SCHEMA_VERSION,
                "entry": {
                    "release_id": entry.release_id,
                    "recorded_at": entry.recorded_at,
                    "report": entry.report.to_dict(),
                },
            },
        )
        return entry

    def load(self, release_id: str) -> ReleaseHistoryEntry:
        release = _release_id(release_id)
        payload = json.loads(
            (self.root / f"{release}.json").read_text(encoding="utf-8")
        )
        version = payload.get("schema_version")
        if version != RELEASE_HISTORY_SCHEMA_VERSION:
            raise ValueError(f"unsupported release history schema version: {version}")
        raw = dict(payload["entry"])
        if raw.get("release_id") != release:
            raise ValueError("release history identity does not match its path")
        return ReleaseHistoryEntry(
            release_id=release,
            recorded_at=float(raw["recorded_at"]),
            report=CompositeReleaseGateReport.from_dict(dict(raw["report"])),
        )

    def list(self, *, limit: int = 20) -> tuple[ReleaseHistoryEntry, ...]:
        if (
            not isinstance(limit, int)
            or isinstance(limit, bool)
            or limit <= 0
            or limit > MAX_RELEASE_HISTORY_LIMIT
        ):
            raise ValueError("release history limit must be between 1 and 100")
        if not self.root.exists():
            return ()
        entries = [
            self.load(path.stem)
            for path in self.root.glob("*.json")
            if _RELEASE_ID_PATTERN.fullmatch(path.stem)
        ]
        entries.sort(
            key=lambda entry: (entry.recorded_at, entry.release_id),
            reverse=True,
        )
        return tuple(entries[:limit])


class ReleaseHistoryAnalyzer:
    def __init__(self, policy: ReleaseRegressionPolicy | None = None) -> None:
        self.policy = policy or ReleaseRegressionPolicy()

    def analyze(
        self,
        entries: tuple[ReleaseHistoryEntry, ...],
    ) -> ReleaseTrendReport:
        if not entries:
            raise ValueError("release history analysis requires at least one entry")
        ordered = tuple(
            sorted(
                entries,
                key=lambda entry: (entry.recorded_at, entry.release_id),
                reverse=True,
            )
        )
        current = ordered[0]
        baseline = ordered[1 : self.policy.history_window + 1]
        if not baseline:
            return ReleaseTrendReport(
                schema_version=RELEASE_TREND_SCHEMA_VERSION,
                status="insufficient_history",
                current_release_id=current.release_id,
                baseline_release_ids=(),
                current_gate_status=current.report.status,
                previous_gate_status=None,
                regressions=(),
                stage_trends=(),
            )

        regressions: list[str] = []
        previous = baseline[0]
        current_rank = _status_rank(current.report.status)
        previous_rank = _status_rank(previous.report.status)
        if current_rank < previous_rank:
            regressions.append(
                f"gate_status:{previous.report.status}->{current.report.status}"
            )

        stage_trends: list[ReleaseStageTrend] = []
        current_stages = {stage.name: stage for stage in current.report.stages}
        previous_stages = {stage.name: stage for stage in previous.report.stages}
        for name, current_stage in current_stages.items():
            previous_stage = previous_stages[name]
            if _status_rank(current_stage.status) < _status_rank(previous_stage.status):
                regressions.append(
                    f"stage_status:{name}:"
                    f"{previous_stage.status}->{current_stage.status}"
                )
            samples = [
                next(stage for stage in entry.report.stages if stage.name == name)
                .duration_seconds
                for entry in baseline
            ]
            median = float(statistics.median(samples))
            delta = current_stage.duration_seconds - median
            ratio = (
                current_stage.duration_seconds / median
                if median > 0
                else None
            )
            regressed = (
                delta >= self.policy.duration_absolute_seconds
                and ratio is not None
                and ratio >= self.policy.duration_ratio
            )
            if regressed:
                regressions.append(f"stage_duration:{name}")
            stage_trends.append(
                ReleaseStageTrend(
                    name=name,
                    current_seconds=current_stage.duration_seconds,
                    baseline_median_seconds=median,
                    delta_seconds=delta,
                    ratio=ratio,
                    regressed=regressed,
                )
            )

        status = "regressed" if regressions else (
            "improved" if current_rank > previous_rank else "stable"
        )
        return ReleaseTrendReport(
            schema_version=RELEASE_TREND_SCHEMA_VERSION,
            status=status,
            current_release_id=current.release_id,
            baseline_release_ids=tuple(entry.release_id for entry in baseline),
            current_gate_status=current.report.status,
            previous_gate_status=previous.report.status,
            regressions=tuple(regressions),
            stage_trends=tuple(stage_trends),
        )


def write_release_trend(path: str | Path, report: ReleaseTrendReport) -> Path:
    if not isinstance(report, ReleaseTrendReport):
        raise TypeError("release trend writer requires ReleaseTrendReport")
    target = Path(path).resolve()
    _write_json(target, report.to_dict())
    return target


def _release_id(value: str) -> str:
    if not isinstance(value, str) or not _RELEASE_ID_PATTERN.fullmatch(value):
        raise ValueError(
            "release_id may contain only letters, digits, underscore, and hyphen"
        )
    return value


def _status_rank(status: str) -> int:
    try:
        return _STATUS_RANK[status]
    except KeyError as exc:
        raise ValueError(f"unsupported release status: {status}") from exc


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp")
    temporary.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    os.replace(temporary, path)
