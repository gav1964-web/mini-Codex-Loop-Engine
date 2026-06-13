from __future__ import annotations

import threading

import pytest

from loop_engine.adapters import (
    ProcessReaperPolicy,
    ProcessReaperService,
    ProcessRegistry,
    ProcessRetentionPolicy,
)


def _record(registry: ProcessRegistry, tmp_path, *, pid: int = 101):
    return registry.register(
        owner_run_id="service-test",
        pid=pid,
        process_identity=f"identity-{pid}",
        argv=("python", "-V"),
        cwd=str(tmp_path),
        timeout_seconds=60,
    )


def test_service_runs_immediate_bounded_cycles(tmp_path) -> None:
    registry = ProcessRegistry()
    record = _record(registry, tmp_path)
    calls: list[float] = []

    def reap(current, stale_after):
        calls.append(stale_after)
        if len(calls) == 1:
            return [
                current.finish(
                    record.record_id,
                    status="terminated",
                    exit_code=None,
                    reason="stale_heartbeat",
                )
            ]
        return []

    report = ProcessReaperService(
        registry,
        ProcessReaperPolicy(
            stale_after_seconds=30,
            interval_seconds=0.001,
            max_cycles=2,
        ),
        reaper=reap,
    ).run()

    assert report.status == "completed"
    assert report.stop_reason == "max_cycles"
    assert report.reaped_count == 1
    assert calls == [30, 30]
    assert report.cycles[0].terminated_count == 1
    assert report.cycles[0].reaped_record_ids == (record.record_id,)


def test_pre_requested_stop_runs_no_cycle() -> None:
    event = threading.Event()
    event.set()
    called = False

    def reap(registry, stale_after):
        nonlocal called
        called = True
        return []

    report = ProcessReaperService(
        ProcessRegistry(),
        ProcessReaperPolicy(
            stale_after_seconds=30,
            interval_seconds=1,
            max_cycles=3,
        ),
        reaper=reap,
    ).run(stop_event=event)

    assert report.stop_reason == "stop_requested"
    assert report.cycles == ()
    assert not called


def test_stop_request_interrupts_interval_wait() -> None:
    event = threading.Event()
    calls = 0

    def reap(registry, stale_after):
        nonlocal calls
        calls += 1
        event.set()
        return []

    report = ProcessReaperService(
        ProcessRegistry(),
        ProcessReaperPolicy(
            stale_after_seconds=30,
            interval_seconds=60,
            max_cycles=3,
        ),
        reaper=reap,
    ).run(stop_event=event)

    assert report.stop_reason == "stop_requested"
    assert len(report.cycles) == 1
    assert calls == 1


def test_reaper_error_is_structured_and_stops_service() -> None:
    def broken(registry, stale_after):
        raise OSError("identity lookup unavailable")

    report = ProcessReaperService(
        ProcessRegistry(),
        ProcessReaperPolicy(
            stale_after_seconds=30,
            interval_seconds=1,
            max_cycles=3,
        ),
        reaper=broken,
    ).run()

    assert report.status == "failed"
    assert report.stop_reason == "error"
    assert report.cycles == ()
    assert report.error == "OSError: identity lookup unavailable"


def test_invalid_reaper_output_fails_closed_and_service_can_restart() -> None:
    responses = iter([None, []])

    def invalid_then_valid(registry, stale_after):
        return next(responses)

    service = ProcessReaperService(
        ProcessRegistry(),
        ProcessReaperPolicy(
            stale_after_seconds=30,
            interval_seconds=1,
            max_cycles=1,
        ),
        reaper=invalid_then_valid,  # type: ignore[arg-type]
    )

    failed = service.run()
    completed = service.run()

    assert failed.status == "failed"
    assert failed.error == "TypeError: process reaper must return a list of ProcessRecord"
    assert completed.status == "completed"
    assert completed.stop_reason == "max_cycles"


def test_registry_rejects_concurrent_service_runs() -> None:
    entered = threading.Event()
    release = threading.Event()
    registry = ProcessRegistry()
    policy = ProcessReaperPolicy(
        stale_after_seconds=30,
        interval_seconds=1,
        max_cycles=1,
    )

    def blocking(registry, stale_after):
        entered.set()
        release.wait(timeout=2)
        return []

    first = ProcessReaperService(
        registry,
        policy,
        reaper=blocking,
    )
    second = ProcessReaperService(registry, policy, reaper=lambda current, age: [])
    thread = threading.Thread(target=first.run)
    thread.start()
    assert entered.wait(timeout=1)

    with pytest.raises(RuntimeError, match="already running"):
        second.run()

    release.set()
    thread.join(timeout=2)
    assert not thread.is_alive()


def test_retention_runs_on_configured_cadence_with_explicit_limit() -> None:
    calls: list[tuple[float, int]] = []

    def prune(registry, retain_seconds, max_records):
        calls.append((retain_seconds, max_records))
        return 2

    report = ProcessReaperService(
        ProcessRegistry(),
        ProcessReaperPolicy(
            stale_after_seconds=30,
            interval_seconds=0.001,
            max_cycles=3,
            retention=ProcessRetentionPolicy(
                retain_seconds=3600,
                prune_every_cycles=2,
                max_pruned_per_cycle=7,
            ),
        ),
        reaper=lambda registry, age: [],
        pruner=prune,
    ).run()

    assert calls == [(3600, 7)]
    assert report.cycles[0].pruning_attempted is False
    assert report.cycles[1].pruning_attempted is True
    assert report.cycles[1].pruned_count == 2
    assert report.cycles[2].pruning_attempted is False


def test_pruning_error_preserves_completed_reaping_evidence(tmp_path) -> None:
    registry = ProcessRegistry()
    record = _record(registry, tmp_path)

    def reap(current, stale_after):
        return [
            current.finish(
                record.record_id,
                status="terminated",
                exit_code=None,
                reason="stale_heartbeat",
            )
        ]

    def broken_pruner(registry, retain_seconds, max_records):
        raise OSError("registry persistence unavailable")

    report = ProcessReaperService(
        registry,
        ProcessReaperPolicy(
            stale_after_seconds=30,
            interval_seconds=1,
            max_cycles=2,
            retention=ProcessRetentionPolicy(retain_seconds=60),
        ),
        reaper=reap,
        pruner=broken_pruner,
    ).run()

    assert report.status == "failed"
    assert report.stop_reason == "error"
    assert report.reaped_count == 1
    assert len(report.cycles) == 1
    assert report.cycles[0].reaped_record_ids == (record.record_id,)
    assert report.cycles[0].pruning_attempted is True
    assert report.cycles[0].pruning_error == (
        "OSError: registry persistence unavailable"
    )
    assert report.error == (
        "process_retention_error:OSError: registry persistence unavailable"
    )


@pytest.mark.parametrize("invalid_result", [-1, True, "1"])
def test_invalid_pruner_result_fails_closed(invalid_result) -> None:
    report = ProcessReaperService(
        ProcessRegistry(),
        ProcessReaperPolicy(
            stale_after_seconds=30,
            interval_seconds=1,
            max_cycles=1,
            retention=ProcessRetentionPolicy(retain_seconds=60),
        ),
        reaper=lambda registry, age: [],
        pruner=lambda registry, retain, limit: invalid_result,
    ).run()

    assert report.status == "failed"
    assert "non-negative integer" in (report.error or "")
    assert report.cycles[0].pruning_attempted is True


@pytest.mark.parametrize(
    ("kwargs", "message"),
    [
        (
            {"stale_after_seconds": 0, "interval_seconds": 1, "max_cycles": 1},
            "stale threshold",
        ),
        (
            {"stale_after_seconds": 1, "interval_seconds": 0, "max_cycles": 1},
            "interval",
        ),
        (
            {"stale_after_seconds": 1, "interval_seconds": 1, "max_cycles": 0},
            "max_cycles",
        ),
    ],
)
def test_policy_requires_positive_bounds(kwargs, message) -> None:
    with pytest.raises(ValueError, match=message):
        ProcessReaperPolicy(**kwargs)


@pytest.mark.parametrize(
    ("kwargs", "message"),
    [
        (
            {
                "retain_seconds": -1,
                "prune_every_cycles": 1,
                "max_pruned_per_cycle": 1,
            },
            "retention seconds",
        ),
        (
            {
                "retain_seconds": 1,
                "prune_every_cycles": 0,
                "max_pruned_per_cycle": 1,
            },
            "cadence",
        ),
        (
            {
                "retain_seconds": 1,
                "prune_every_cycles": True,
                "max_pruned_per_cycle": 1,
            },
            "cadence",
        ),
        (
            {
                "retain_seconds": 1,
                "prune_every_cycles": 1,
                "max_pruned_per_cycle": 0,
            },
            "limit",
        ),
    ],
)
def test_retention_policy_requires_bounded_values(kwargs, message) -> None:
    with pytest.raises(ValueError, match=message):
        ProcessRetentionPolicy(**kwargs)


def test_reaper_policy_requires_typed_retention() -> None:
    with pytest.raises(TypeError, match="ProcessRetentionPolicy"):
        ProcessReaperPolicy(
            stale_after_seconds=1,
            interval_seconds=1,
            max_cycles=1,
            retention="invalid",  # type: ignore[arg-type]
        )
