from __future__ import annotations

import threading

import pytest

from loop_engine.adapters import (
    ProcessReaperPolicy,
    ProcessReaperService,
    ProcessRegistry,
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
