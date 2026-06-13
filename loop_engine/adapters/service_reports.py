"""Versioned atomic persistence for bounded service-run reports."""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import Any, Mapping, Protocol

SERVICE_RUN_REPORT_SCHEMA_VERSION = 1
MAX_SERVICE_REPORT_LIST_LIMIT = 100
_IDENTIFIER_PATTERN = re.compile(r"^[A-Za-z0-9_-]+$")


@dataclass(frozen=True, slots=True)
class ServiceRunReport:
    run_id: str
    service: str
    status: str
    stop_reason: str
    started_at: float
    finished_at: float
    metrics: Mapping[str, int | float]
    details: Mapping[str, Any]
    error: str | None = None

    def __post_init__(self) -> None:
        run_id = _identifier(self.run_id, "service run_id")
        service = _identifier(self.service, "service name")
        if self.status not in {"completed", "failed"}:
            raise ValueError("service report status must be completed or failed")
        if not isinstance(self.stop_reason, str) or not self.stop_reason.strip():
            raise ValueError("service report stop_reason is required")
        if self.finished_at < self.started_at:
            raise ValueError("service report timestamps are out of order")
        metrics = dict(self.metrics)
        if any(
            not isinstance(name, str)
            or not name.strip()
            or not isinstance(value, (int, float))
            or isinstance(value, bool)
            for name, value in metrics.items()
        ):
            raise TypeError("service report metrics must be named numbers")
        details = _json_snapshot(self.details, "service report details")
        object.__setattr__(self, "run_id", run_id)
        object.__setattr__(self, "service", service)
        object.__setattr__(self, "stop_reason", self.stop_reason.strip())
        object.__setattr__(self, "metrics", MappingProxyType(metrics))
        object.__setattr__(self, "details", _freeze_json(details))

    @property
    def duration_seconds(self) -> float:
        return self.finished_at - self.started_at

    def to_dict(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "service": self.service,
            "status": self.status,
            "stop_reason": self.stop_reason,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "duration_seconds": self.duration_seconds,
            "metrics": dict(self.metrics),
            "details": _thaw_json(self.details),
            "error": self.error,
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> ServiceRunReport:
        return cls(
            run_id=str(value["run_id"]),
            service=str(value["service"]),
            status=str(value["status"]),
            stop_reason=str(value["stop_reason"]),
            started_at=float(value["started_at"]),
            finished_at=float(value["finished_at"]),
            metrics=dict(value.get("metrics", {})),
            details=dict(value.get("details", {})),
            error=value.get("error"),
        )


class ServiceRunReportSink(Protocol):
    def save(self, report: ServiceRunReport) -> Any:
        ...


class JsonServiceRunReportStore:
    def __init__(self, root: str | Path) -> None:
        self.root = Path(root).resolve()

    def save(self, report: ServiceRunReport) -> Path:
        if not isinstance(report, ServiceRunReport):
            raise TypeError("service report store requires ServiceRunReport")
        target = self._target(report.service, report.run_id)
        target.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "schema_version": SERVICE_RUN_REPORT_SCHEMA_VERSION,
            "report": report.to_dict(),
        }
        temporary = target.with_name(f".{target.name}.tmp")
        temporary.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        os.replace(temporary, target)
        return target

    def load(self, service: str, run_id: str) -> ServiceRunReport:
        payload = json.loads(
            self._target(service, run_id).read_text(encoding="utf-8")
        )
        version = payload.get("schema_version")
        if version != SERVICE_RUN_REPORT_SCHEMA_VERSION:
            raise ValueError(
                f"unsupported service report schema version: {version}"
            )
        report = ServiceRunReport.from_dict(dict(payload["report"]))
        if report.service != service or report.run_id != run_id:
            raise ValueError("service report identity does not match its path")
        return report

    def list(
        self,
        service: str,
        *,
        limit: int = 20,
    ) -> tuple[ServiceRunReport, ...]:
        service_name = _identifier(service, "service name")
        if (
            not isinstance(limit, int)
            or isinstance(limit, bool)
            or limit <= 0
            or limit > MAX_SERVICE_REPORT_LIST_LIMIT
        ):
            raise ValueError(
                "service report list limit must be between 1 and "
                f"{MAX_SERVICE_REPORT_LIST_LIMIT}"
            )
        directory = self.root / service_name
        if not directory.exists():
            return ()
        reports = [
            self.load(service_name, path.stem)
            for path in directory.glob("*.json")
            if _IDENTIFIER_PATTERN.fullmatch(path.stem)
        ]
        reports.sort(
            key=lambda report: (report.started_at, report.run_id),
            reverse=True,
        )
        return tuple(reports[:limit])

    def _target(self, service: str, run_id: str) -> Path:
        service_name = _identifier(service, "service name")
        report_id = _identifier(run_id, "service run_id")
        return self.root / service_name / f"{report_id}.json"


def _identifier(value: str, label: str) -> str:
    if not isinstance(value, str) or not _IDENTIFIER_PATTERN.fullmatch(value):
        raise ValueError(
            f"{label} may contain only letters, digits, underscore, and hyphen"
        )
    return value


def _json_snapshot(value: Mapping[str, Any], label: str) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise TypeError(f"{label} must be a mapping")
    try:
        return json.loads(json.dumps(dict(value), ensure_ascii=False))
    except (TypeError, ValueError) as exc:
        raise TypeError(f"{label} must be JSON serializable") from exc


def _freeze_json(value: Any) -> Any:
    if isinstance(value, dict):
        return MappingProxyType(
            {key: _freeze_json(item) for key, item in value.items()}
        )
    if isinstance(value, list):
        return tuple(_freeze_json(item) for item in value)
    return value


def _thaw_json(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {key: _thaw_json(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return [_thaw_json(item) for item in value]
    return value
