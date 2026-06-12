from __future__ import annotations

import json
import sys

import pytest

from loop_engine.sandbox_release_gate import (
    REQUIRED_SANDBOX_CHECKS,
    SandboxReleaseGate,
    SandboxReleaseGatePolicy,
)


def _command(payload, *, exit_code=0):
    source = (
        "import json,sys;"
        f"print(json.dumps({payload!r}));"
        f"sys.exit({exit_code})"
    )
    return (sys.executable, "-c", source)


def _gate(tmp_path, command, *, timeout=2, output_limit=32 * 1024):
    policy = SandboxReleaseGatePolicy.create(
        workspace_root=tmp_path,
        command=command,
        report_path="gate/report.json",
        timeout_seconds=timeout,
        max_output_bytes=output_limit,
    )
    return SandboxReleaseGate(policy), policy.report_path


def _passed_payload():
    return {
        "status": "completed",
        "error": None,
        "checks": {name: True for name in REQUIRED_SANDBOX_CHECKS},
    }


def test_all_required_checks_pass_release_and_write_report(tmp_path) -> None:
    gate, report_path = _gate(tmp_path, _command(_passed_payload()))

    report = gate.run()

    assert report.status == "passed"
    assert report.releasable is True
    assert all(report.checks.values())
    assert json.loads(report_path.read_text(encoding="utf-8")) == report.to_dict()


def test_unavailable_backend_blocks_release_by_default(tmp_path) -> None:
    gate, _ = _gate(
        tmp_path,
        _command(
            {"status": "blocked", "error": "wsl_bubblewrap_unavailable"},
            exit_code=2,
        ),
    )

    report = gate.run()

    assert report.status == "blocked"
    assert report.releasable is False


def test_explicit_degraded_mode_is_releasable_but_never_passed(tmp_path) -> None:
    gate, _ = _gate(
        tmp_path,
        _command(
            {"status": "blocked", "error": "wsl_bubblewrap_unavailable"},
            exit_code=2,
        ),
    )

    report = gate.run(degraded_ok=True)

    assert report.status == "degraded"
    assert report.releasable is True
    assert report.checks == {}


def test_degraded_mode_does_not_allow_unexpected_block_reason(tmp_path) -> None:
    gate, _ = _gate(
        tmp_path,
        _command(
            {"status": "blocked", "error": "sandbox_policy_corrupt"},
            exit_code=2,
        ),
    )

    report = gate.run(degraded_ok=True)

    assert report.status == "failed"
    assert report.releasable is False
    assert report.error == (
        "sandbox_release_unexpected_block:sandbox_policy_corrupt"
    )


def test_missing_or_false_check_fails_closed(tmp_path) -> None:
    payload = _passed_payload()
    del payload["checks"]["network_blocked"]
    gate, _ = _gate(tmp_path, _command(payload))

    report = gate.run()

    assert report.status == "failed"
    assert report.releasable is False
    assert report.checks["network_blocked"] is False


def test_malformed_output_and_timeout_fail_closed(tmp_path) -> None:
    malformed, _ = _gate(
        tmp_path,
        (sys.executable, "-c", "print('not-json')"),
    )
    timeout, _ = _gate(
        tmp_path,
        (sys.executable, "-c", "import time; time.sleep(2)"),
        timeout=0.05,
    )

    malformed_report = malformed.run()
    timeout_report = timeout.run()

    assert malformed_report.status == "failed"
    assert "invalid_json" in (malformed_report.error or "")
    assert timeout_report.status == "failed"
    assert timeout_report.error == "sandbox_release_smoke_timeout"


def test_truncated_output_fails_closed_before_json_interpretation(tmp_path) -> None:
    gate, _ = _gate(
        tmp_path,
        (sys.executable, "-c", "print('x' * 10000)"),
        output_limit=64,
    )

    report = gate.run()

    assert report.status == "failed"
    assert report.stdout_truncated is True
    assert report.error == "sandbox_release_smoke_output_truncated"


def test_report_path_must_stay_inside_workspace(tmp_path) -> None:
    with pytest.raises(ValueError, match="escapes workspace"):
        SandboxReleaseGatePolicy.create(
            workspace_root=tmp_path,
            command=(sys.executable, "-V"),
            report_path=tmp_path.parent / "report.json",
        )


def test_direct_policy_construction_cannot_bypass_validation(tmp_path) -> None:
    with pytest.raises(ValueError, match="escapes workspace"):
        SandboxReleaseGatePolicy(
            workspace_root=tmp_path,
            command=(sys.executable, "-V"),
            report_path=tmp_path.parent / "report.json",
        )


def test_launch_failure_creates_failed_report(tmp_path) -> None:
    gate, report_path = _gate(
        tmp_path,
        ("definitely-missing-sandbox-smoke-command",),
    )

    report = gate.run()

    assert report.status == "failed"
    assert report.releasable is False
    assert "launch_error:FileNotFoundError" in (report.error or "")
    assert json.loads(report_path.read_text(encoding="utf-8")) == report.to_dict()
