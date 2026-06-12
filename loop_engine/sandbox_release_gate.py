"""Strict release-gate contract for the canonical OS sandbox smoke."""

from __future__ import annotations

import json
import os
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from .adapters import BoundedSubprocessTool, SubprocessSpec
from .models import LoopDefinition, LoopState

REQUIRED_SANDBOX_CHECKS = (
    "completed",
    "backend",
    "data_write_blocked",
    "host_hidden",
    "network_blocked",
    "output_written",
    "read_only_data_unchanged",
    "output_materialized",
)


@dataclass(frozen=True, slots=True)
class SandboxReleaseGatePolicy:
    workspace_root: Path
    command: tuple[str, ...]
    report_path: Path
    timeout_seconds: float = 60.0
    max_output_bytes: int = 128 * 1024

    def __post_init__(self) -> None:
        root = Path(self.workspace_root).resolve()
        if not root.is_dir():
            raise ValueError("sandbox release gate workspace must exist")
        command = tuple(str(item) for item in self.command)
        if not command or not command[0].strip():
            raise ValueError("sandbox release gate command is required")
        report = Path(self.report_path)
        if not report.is_absolute():
            report = root / report
        report = report.resolve()
        try:
            report.relative_to(root)
        except ValueError as exc:
            raise ValueError("sandbox release report escapes workspace") from exc
        if self.timeout_seconds <= 0 or self.max_output_bytes <= 0:
            raise ValueError("sandbox release gate bounds must be positive")
        object.__setattr__(self, "workspace_root", root)
        object.__setattr__(self, "command", command)
        object.__setattr__(self, "report_path", report)

    @classmethod
    def create(
        cls,
        *,
        workspace_root: str | Path,
        command: tuple[str, ...],
        report_path: str | Path,
        timeout_seconds: float = 60.0,
        max_output_bytes: int = 128 * 1024,
    ) -> SandboxReleaseGatePolicy:
        return cls(
            workspace_root=Path(workspace_root),
            command=tuple(command),
            report_path=Path(report_path),
            timeout_seconds=timeout_seconds,
            max_output_bytes=max_output_bytes,
        )


@dataclass(frozen=True, slots=True)
class SandboxReleaseGateReport:
    schema_version: int
    status: str
    releasable: bool
    backend: str
    checks: dict[str, bool]
    smoke_exit_code: int | None
    timed_out: bool
    stdout_truncated: bool
    stderr_truncated: bool
    duration_seconds: float
    error: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class SandboxReleaseGate:
    """Run and strictly interpret the canonical real sandbox smoke."""

    def __init__(self, policy: SandboxReleaseGatePolicy) -> None:
        self.policy = policy

    def run(self, *, degraded_ok: bool = False) -> SandboxReleaseGateReport:
        started = time.perf_counter()
        try:
            process = BoundedSubprocessTool(
                self.policy.workspace_root,
                SubprocessSpec(
                    argv=self.policy.command,
                    cwd=".",
                    timeout_seconds=self.policy.timeout_seconds,
                    max_output_bytes=self.policy.max_output_bytes,
                ),
            )(
                {},
                LoopState(
                    run_id="sandbox-release-gate",
                    definition=LoopDefinition(
                        goal="Verify release sandbox backend"
                    ),
                ),
            )
            report = _interpret_process(process, degraded_ok=degraded_ok)
        except Exception as exc:
            report = SandboxReleaseGateReport(
                schema_version=1,
                status="failed",
                releasable=False,
                backend="wsl_bubblewrap",
                checks={},
                smoke_exit_code=None,
                timed_out=False,
                stdout_truncated=False,
                stderr_truncated=False,
                duration_seconds=time.perf_counter() - started,
                error=f"sandbox_release_smoke_launch_error:{type(exc).__name__}:{exc}",
            )
        _write_report(self.policy.report_path, report)
        return report


def _interpret_process(
    process: dict[str, Any],
    *,
    degraded_ok: bool,
) -> SandboxReleaseGateReport:
    common = {
        "schema_version": 1,
        "backend": "wsl_bubblewrap",
        "smoke_exit_code": process.get("exit_code"),
        "timed_out": bool(process.get("timed_out")),
        "stdout_truncated": bool(process.get("stdout_truncated")),
        "stderr_truncated": bool(process.get("stderr_truncated")),
        "duration_seconds": float(process.get("duration_seconds", 0.0)),
    }
    if common["timed_out"]:
        return _failed(common, "sandbox_release_smoke_timeout")
    if common["stdout_truncated"] or common["stderr_truncated"]:
        return _failed(common, "sandbox_release_smoke_output_truncated")
    try:
        payload = json.loads(str(process.get("stdout", "")))
    except json.JSONDecodeError as exc:
        return _failed(common, f"sandbox_release_smoke_invalid_json:{exc.msg}")
    if not isinstance(payload, dict):
        return _failed(common, "sandbox_release_smoke_non_object")

    if process.get("exit_code") == 2 and payload.get("status") == "blocked":
        error = str(payload.get("error") or "sandbox_backend_unavailable")
        if error != "wsl_bubblewrap_unavailable":
            return _failed(common, f"sandbox_release_unexpected_block:{error}")
        status = "degraded" if degraded_ok else "blocked"
        return SandboxReleaseGateReport(
            status=status,
            releasable=degraded_ok,
            checks={},
            error=error,
            **common,
        )

    checks = payload.get("checks")
    normalized_checks = (
        {
            name: checks.get(name) is True
            for name in REQUIRED_SANDBOX_CHECKS
        }
        if isinstance(checks, dict)
        else {}
    )
    if (
        process.get("exit_code") == 0
        and payload.get("status") == "completed"
        and len(normalized_checks) == len(REQUIRED_SANDBOX_CHECKS)
        and all(normalized_checks.values())
    ):
        return SandboxReleaseGateReport(
            status="passed",
            releasable=True,
            checks=normalized_checks,
            **common,
        )
    return _failed(
        common,
        str(payload.get("error") or "sandbox_release_smoke_checks_failed"),
        checks=normalized_checks,
    )


def _failed(
    common: dict[str, Any],
    error: str,
    *,
    checks: dict[str, bool] | None = None,
) -> SandboxReleaseGateReport:
    return SandboxReleaseGateReport(
        status="failed",
        releasable=False,
        checks=checks or {},
        error=error,
        **common,
    )


def _write_report(
    path: Path,
    report: SandboxReleaseGateReport,
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp")
    temporary.write_text(
        json.dumps(report.to_dict(), ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    os.replace(temporary, path)
