"""Composite release gate for tests, wheel smoke, and OS sandbox evidence."""

from __future__ import annotations

import json
import os
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from .adapters import BoundedSubprocessTool, SubprocessSpec
from .models import LoopDefinition, LoopState
from .sandbox_release_gate import interpret_sandbox_process

RELEASE_GATE_SCHEMA_VERSION = 1
REQUIRED_RELEASE_STAGES = ("pytest", "wheel_smoke", "sandbox")


@dataclass(frozen=True, slots=True)
class ReleaseCommand:
    argv: tuple[str, ...]
    timeout_seconds: float
    max_output_bytes: int = 256 * 1024

    def __post_init__(self) -> None:
        command = tuple(str(item) for item in self.argv)
        if not command or not command[0].strip():
            raise ValueError("release gate command is required")
        if self.timeout_seconds <= 0 or self.max_output_bytes <= 0:
            raise ValueError("release gate command bounds must be positive")
        object.__setattr__(self, "argv", command)


@dataclass(frozen=True, slots=True)
class CompositeReleaseGatePolicy:
    workspace_root: Path
    pytest: ReleaseCommand
    wheel_smoke: ReleaseCommand
    sandbox: ReleaseCommand
    report_path: Path

    def __post_init__(self) -> None:
        root = Path(self.workspace_root).resolve()
        if not root.is_dir():
            raise ValueError("release gate workspace must exist")
        report = Path(self.report_path)
        if not report.is_absolute():
            report = root / report
        report = report.resolve()
        try:
            report.relative_to(root)
        except ValueError as exc:
            raise ValueError("release gate report escapes workspace") from exc
        for command in (self.pytest, self.wheel_smoke, self.sandbox):
            if not isinstance(command, ReleaseCommand):
                raise TypeError("release gate stages require ReleaseCommand")
        object.__setattr__(self, "workspace_root", root)
        object.__setattr__(self, "report_path", report)


@dataclass(frozen=True, slots=True)
class ReleaseStageReport:
    name: str
    status: str
    releasable: bool
    exit_code: int | None
    timed_out: bool
    stdout_truncated: bool
    stderr_truncated: bool
    duration_seconds: float
    error: str | None = None
    details: dict[str, Any] | None = None


@dataclass(frozen=True, slots=True)
class CompositeReleaseGateReport:
    schema_version: int
    status: str
    releasable: bool
    degraded: bool
    started_at: float
    finished_at: float
    stages: tuple[ReleaseStageReport, ...]
    error: str | None = None

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["stages"] = [asdict(stage) for stage in self.stages]
        return payload


class CompositeReleaseGate:
    """Run every release stage and derive one deterministic verdict."""

    def __init__(self, policy: CompositeReleaseGatePolicy) -> None:
        self.policy = policy

    def run(self, *, degraded_ok: bool = False) -> CompositeReleaseGateReport:
        started = time.time()
        stages = (
            self._run_command("pytest", self.policy.pytest),
            self._run_command("wheel_smoke", self.policy.wheel_smoke),
            self._run_sandbox(self.policy.sandbox, degraded_ok=degraded_ok),
        )
        degraded = any(stage.status == "degraded" for stage in stages)
        releasable = all(stage.releasable for stage in stages)
        status = (
            "degraded"
            if releasable and degraded
            else "passed"
            if releasable
            else "failed"
        )
        failed_names = [
            stage.name for stage in stages if not stage.releasable
        ]
        report = CompositeReleaseGateReport(
            schema_version=RELEASE_GATE_SCHEMA_VERSION,
            status=status,
            releasable=releasable,
            degraded=degraded,
            started_at=started,
            finished_at=time.time(),
            stages=stages,
            error=(
                None
                if releasable
                else f"release_stages_failed:{','.join(failed_names)}"
            ),
        )
        _write_report(self.policy.report_path, report)
        return report

    def _run_command(
        self,
        name: str,
        command: ReleaseCommand,
    ) -> ReleaseStageReport:
        process, launch_error = self._launch(name, command)
        if launch_error is not None:
            return _launch_failure(name, launch_error)
        assert process is not None
        return _interpret_command(name, process)

    def _run_sandbox(
        self,
        command: ReleaseCommand,
        *,
        degraded_ok: bool,
    ) -> ReleaseStageReport:
        process, launch_error = self._launch("sandbox", command)
        if launch_error is not None:
            return _launch_failure("sandbox", launch_error)
        assert process is not None
        report = interpret_sandbox_process(
            process,
            degraded_ok=degraded_ok,
        )
        return ReleaseStageReport(
            name="sandbox",
            status=report.status,
            releasable=report.releasable,
            exit_code=report.smoke_exit_code,
            timed_out=report.timed_out,
            stdout_truncated=report.stdout_truncated,
            stderr_truncated=report.stderr_truncated,
            duration_seconds=report.duration_seconds,
            error=report.error,
            details={
                "backend": report.backend,
                "checks": report.checks,
            },
        )

    def _launch(
        self,
        name: str,
        command: ReleaseCommand,
    ) -> tuple[dict[str, Any] | None, str | None]:
        try:
            process = BoundedSubprocessTool(
                self.policy.workspace_root,
                SubprocessSpec(
                    argv=command.argv,
                    cwd=".",
                    timeout_seconds=command.timeout_seconds,
                    max_output_bytes=command.max_output_bytes,
                ),
            )(
                {},
                LoopState(
                    run_id=f"release-gate-{name}",
                    definition=LoopDefinition(goal=f"Run release stage {name}"),
                ),
            )
            return process, None
        except Exception as exc:
            return None, f"release_stage_launch_error:{type(exc).__name__}:{exc}"


def _interpret_command(
    name: str,
    process: dict[str, Any],
) -> ReleaseStageReport:
    timed_out = bool(process.get("timed_out"))
    stdout_truncated = bool(process.get("stdout_truncated"))
    stderr_truncated = bool(process.get("stderr_truncated"))
    exit_code = process.get("exit_code")
    error = (
        "stage_timeout"
        if timed_out
        else "stage_output_truncated"
        if stdout_truncated or stderr_truncated
        else f"stage_exit_code:{exit_code}"
        if exit_code != 0
        else None
    )
    passed = error is None
    return ReleaseStageReport(
        name=name,
        status="passed" if passed else "failed",
        releasable=passed,
        exit_code=exit_code,
        timed_out=timed_out,
        stdout_truncated=stdout_truncated,
        stderr_truncated=stderr_truncated,
        duration_seconds=float(process.get("duration_seconds", 0.0)),
        error=error,
    )


def _launch_failure(name: str, error: str) -> ReleaseStageReport:
    return ReleaseStageReport(
        name=name,
        status="failed",
        releasable=False,
        exit_code=None,
        timed_out=False,
        stdout_truncated=False,
        stderr_truncated=False,
        duration_seconds=0.0,
        error=error,
    )


def _write_report(
    path: Path,
    report: CompositeReleaseGateReport,
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp")
    temporary.write_text(
        json.dumps(report.to_dict(), ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    os.replace(temporary, path)
