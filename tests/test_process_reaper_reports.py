from __future__ import annotations

from loop_engine.adapters import (
    JsonServiceRunReportStore,
    ProcessReaperPolicy,
    ProcessReaperService,
    ProcessRegistry,
)


def test_service_persists_operational_report(tmp_path) -> None:
    store = JsonServiceRunReportStore(tmp_path / "runs")
    report = ProcessReaperService(
        ProcessRegistry(),
        ProcessReaperPolicy(
            stale_after_seconds=30,
            interval_seconds=0.001,
            max_cycles=2,
        ),
        reaper=lambda registry, age: [],
        report_store=store,
    ).run()

    persisted = store.load("process_reaper", report.run_id)

    assert persisted.status == "completed"
    assert persisted.stop_reason == "max_cycles"
    assert dict(persisted.metrics) == {
        "cycle_count": 2,
        "reaped_count": 0,
        "terminated_count": 0,
        "lost_count": 0,
        "pruned_count": 0,
    }
    assert len(persisted.details["cycles"]) == 2


def test_failed_service_run_is_persisted(tmp_path) -> None:
    store = JsonServiceRunReportStore(tmp_path / "runs")

    def broken(registry, stale_after):
        raise OSError("identity lookup unavailable")

    report = ProcessReaperService(
        ProcessRegistry(),
        ProcessReaperPolicy(
            stale_after_seconds=30,
            interval_seconds=1,
            max_cycles=1,
        ),
        reaper=broken,
        report_store=store,
    ).run()

    persisted = store.load("process_reaper", report.run_id)

    assert persisted.status == "failed"
    assert persisted.error == "OSError: identity lookup unavailable"


def test_report_persistence_failure_fails_service_explicitly() -> None:
    class BrokenStore:
        def save(self, report):
            raise OSError("report volume unavailable")

    report = ProcessReaperService(
        ProcessRegistry(),
        ProcessReaperPolicy(
            stale_after_seconds=30,
            interval_seconds=1,
            max_cycles=1,
        ),
        reaper=lambda registry, age: [],
        report_store=BrokenStore(),  # type: ignore[arg-type]
    ).run()

    assert report.status == "failed"
    assert report.stop_reason == "error"
    assert report.error == (
        "service_report_persistence_error:"
        "OSError: report volume unavailable"
    )
    assert len(report.cycles) == 1
