from __future__ import annotations

import json
import sys

import pytest

from loop_engine.release_gate import (
    CompositeReleaseGate,
    CompositeReleaseGatePolicy,
    CompositeReleaseGateReport,
    ReleaseCommand,
)
from loop_engine.sandbox_release_gate import REQUIRED_SANDBOX_CHECKS


def _command(
    *,
    exit_code=0,
    stdout="ok",
    marker=None,
    sleep_seconds=0,
):
    source = ["import pathlib,sys,time"]
    if sleep_seconds:
        source.append(f"time.sleep({sleep_seconds!r})")
    if marker is not None:
        source.append(f"pathlib.Path({str(marker)!r}).write_text('ran')")
    source.append(f"print({stdout!r})")
    source.append(f"sys.exit({exit_code})")
    return (sys.executable, "-c", ";".join(source))


def _sandbox_command(*, available=True, error=None):
    if available:
        payload = {
            "status": "completed",
            "error": None,
            "checks": {name: True for name in REQUIRED_SANDBOX_CHECKS},
        }
        exit_code = 0
    else:
        payload = {
            "status": "blocked",
            "error": error or "wsl_bubblewrap_unavailable",
        }
        exit_code = 2
    return _command(
        exit_code=exit_code,
        stdout=json.dumps(payload),
    )


def _policy(
    tmp_path,
    *,
    pytest_command=None,
    wheel_command=None,
    sandbox_command=None,
    output_limit=32 * 1024,
):
    return CompositeReleaseGatePolicy(
        workspace_root=tmp_path,
        pytest=ReleaseCommand(
            argv=pytest_command or _command(),
            timeout_seconds=1,
            max_output_bytes=output_limit,
        ),
        wheel_smoke=ReleaseCommand(
            argv=wheel_command or _command(),
            timeout_seconds=1,
            max_output_bytes=output_limit,
        ),
        sandbox=ReleaseCommand(
            argv=sandbox_command or _sandbox_command(),
            timeout_seconds=1,
            max_output_bytes=output_limit,
        ),
        report_path="release/report.json",
    )


def test_all_stages_pass_and_write_atomic_report(tmp_path) -> None:
    policy = _policy(tmp_path)

    report = CompositeReleaseGate(policy).run()

    assert report.status == "passed"
    assert report.releasable is True
    assert report.degraded is False
    assert [stage.name for stage in report.stages] == [
        "pytest",
        "wheel_smoke",
        "sandbox",
    ]
    assert json.loads(policy.report_path.read_text(encoding="utf-8")) == (
        report.to_dict()
    )
    assert CompositeReleaseGateReport.from_dict(report.to_dict()) == report


def test_failure_does_not_short_circuit_later_stages(tmp_path) -> None:
    wheel_marker = tmp_path / "wheel-ran"
    sandbox_marker = tmp_path / "sandbox-ran"
    sandbox_payload = json.dumps(
        {
            "status": "completed",
            "error": None,
            "checks": {name: True for name in REQUIRED_SANDBOX_CHECKS},
        }
    )
    policy = _policy(
        tmp_path,
        pytest_command=_command(exit_code=1),
        wheel_command=_command(marker=wheel_marker),
        sandbox_command=_command(
            stdout=sandbox_payload,
            marker=sandbox_marker,
        ),
    )

    report = CompositeReleaseGate(policy).run()

    assert report.status == "failed"
    assert report.error == "release_stages_failed:pytest"
    assert wheel_marker.is_file()
    assert sandbox_marker.is_file()
    assert [stage.status for stage in report.stages] == [
        "failed",
        "passed",
        "passed",
    ]


def test_multiple_failures_are_reported_in_stage_order(tmp_path) -> None:
    report = CompositeReleaseGate(
        _policy(
            tmp_path,
            pytest_command=_command(exit_code=3),
            wheel_command=_command(exit_code=4),
        )
    ).run()

    assert report.error == "release_stages_failed:pytest,wheel_smoke"


def test_exact_unavailable_sandbox_can_be_explicitly_degraded(tmp_path) -> None:
    gate = CompositeReleaseGate(
        _policy(tmp_path, sandbox_command=_sandbox_command(available=False))
    )

    blocked = gate.run()
    degraded = gate.run(degraded_ok=True)

    assert blocked.status == "failed"
    assert blocked.releasable is False
    assert degraded.status == "degraded"
    assert degraded.releasable is True
    assert degraded.degraded is True
    assert degraded.stages[-1].status == "degraded"


def test_unexpected_sandbox_block_remains_failed_in_degraded_mode(
    tmp_path,
) -> None:
    report = CompositeReleaseGate(
        _policy(
            tmp_path,
            sandbox_command=_sandbox_command(
                available=False,
                error="sandbox_policy_corrupt",
            ),
        )
    ).run(degraded_ok=True)

    assert report.status == "failed"
    assert report.stages[-1].error == (
        "sandbox_release_unexpected_block:sandbox_policy_corrupt"
    )


def test_timeout_and_output_truncation_fail_closed(tmp_path) -> None:
    timeout_policy = CompositeReleaseGatePolicy(
        workspace_root=tmp_path,
        pytest=ReleaseCommand(
            argv=_command(sleep_seconds=1),
            timeout_seconds=0.05,
        ),
        wheel_smoke=ReleaseCommand(
            argv=_command(),
            timeout_seconds=1,
        ),
        sandbox=ReleaseCommand(
            argv=_sandbox_command(),
            timeout_seconds=1,
        ),
        report_path="timeout.json",
    )
    truncated_policy = _policy(
        tmp_path,
        wheel_command=_command(stdout="x" * 10000),
        output_limit=64,
    )

    timeout = CompositeReleaseGate(timeout_policy).run()
    truncated = CompositeReleaseGate(truncated_policy).run()

    assert timeout.stages[0].error == "stage_timeout"
    assert truncated.stages[1].error == "stage_output_truncated"
    assert timeout.releasable is False
    assert truncated.releasable is False


def test_malformed_sandbox_output_fails_strict_interpretation(tmp_path) -> None:
    report = CompositeReleaseGate(
        _policy(
            tmp_path,
            sandbox_command=_command(stdout="not-json"),
        )
    ).run()

    assert report.status == "failed"
    assert "invalid_json" in (report.stages[-1].error or "")


def test_launch_failure_is_structured_and_other_stages_still_run(
    tmp_path,
) -> None:
    marker = tmp_path / "wheel-ran"
    report = CompositeReleaseGate(
        _policy(
            tmp_path,
            pytest_command=("definitely-missing-release-command",),
            wheel_command=_command(marker=marker),
        )
    ).run()

    assert "launch_error:FileNotFoundError" in (
        report.stages[0].error or ""
    )
    assert marker.is_file()


def test_policy_contracts_reject_invalid_bounds_and_external_report(
    tmp_path,
) -> None:
    with pytest.raises(ValueError, match="bounds must be positive"):
        ReleaseCommand(argv=_command(), timeout_seconds=0)
    with pytest.raises(ValueError, match="escapes workspace"):
        CompositeReleaseGatePolicy(
            workspace_root=tmp_path,
            pytest=ReleaseCommand(argv=_command(), timeout_seconds=1),
            wheel_smoke=ReleaseCommand(argv=_command(), timeout_seconds=1),
            sandbox=ReleaseCommand(argv=_sandbox_command(), timeout_seconds=1),
            report_path=tmp_path.parent / "report.json",
        )


def test_report_loader_rejects_unknown_schema_or_stage_shape(tmp_path) -> None:
    payload = CompositeReleaseGate(_policy(tmp_path)).run().to_dict()
    payload["schema_version"] = 999
    with pytest.raises(ValueError, match="schema version"):
        CompositeReleaseGateReport.from_dict(payload)

    payload = CompositeReleaseGate(_policy(tmp_path)).run().to_dict()
    payload["stages"] = payload["stages"][:-1]
    with pytest.raises(ValueError, match="stages"):
        CompositeReleaseGateReport.from_dict(payload)
