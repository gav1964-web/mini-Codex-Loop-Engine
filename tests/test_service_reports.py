from __future__ import annotations

import json

import pytest

from loop_engine.adapters import JsonServiceRunReportStore, ServiceRunReport


def _report(
    run_id: str,
    *,
    started_at: float = 1.0,
    status: str = "completed",
) -> ServiceRunReport:
    return ServiceRunReport(
        run_id=run_id,
        service="test_service",
        status=status,
        stop_reason="max_cycles" if status == "completed" else "error",
        started_at=started_at,
        finished_at=started_at + 2,
        metrics={"cycle_count": 2, "processed_count": 3},
        details={"cycles": [{"cycle": 1}]},
        error=None if status == "completed" else "test_error",
    )


def test_store_round_trip_uses_versioned_atomic_envelope(tmp_path) -> None:
    store = JsonServiceRunReportStore(tmp_path / "runs")
    report = _report("run-1")

    path = store.save(report)
    loaded = store.load("test_service", "run-1")
    payload = json.loads(path.read_text(encoding="utf-8"))

    assert loaded == report
    assert payload["schema_version"] == 1
    assert payload["report"]["duration_seconds"] == 2
    assert not path.with_name(f".{path.name}.tmp").exists()


def test_store_lists_newest_runs_with_explicit_limit(tmp_path) -> None:
    store = JsonServiceRunReportStore(tmp_path)
    for run_id, started_at in [("old", 1), ("new", 3), ("middle", 2)]:
        store.save(_report(run_id, started_at=started_at))

    reports = store.list("test_service", limit=2)

    assert [report.run_id for report in reports] == ["new", "middle"]
    assert store.list("missing") == ()


def test_report_copies_json_details() -> None:
    details = {"items": ["original"]}
    report = ServiceRunReport(
        run_id="copy",
        service="test_service",
        status="completed",
        stop_reason="done",
        started_at=1,
        finished_at=2,
        metrics={},
        details=details,
    )
    details["items"].append("mutated")

    assert report.to_dict()["details"] == {"items": ["original"]}
    with pytest.raises(TypeError):
        report.details["new"] = "value"  # type: ignore[index]


@pytest.mark.parametrize("value", ["../escape", "with space", ""])
def test_report_identifiers_reject_path_traversal(value) -> None:
    with pytest.raises(ValueError, match="may contain"):
        ServiceRunReport(
            run_id=value,
            service="test_service",
            status="completed",
            stop_reason="done",
            started_at=1,
            finished_at=2,
            metrics={},
            details={},
        )


def test_store_rejects_unknown_schema_and_invalid_list_limit(tmp_path) -> None:
    store = JsonServiceRunReportStore(tmp_path)
    path = store.save(_report("schema"))
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["schema_version"] = 999
    path.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(ValueError, match="schema version"):
        store.load("test_service", "schema")
    with pytest.raises(ValueError, match="limit"):
        store.list("test_service", limit=0)
    with pytest.raises(ValueError, match="limit"):
        store.list("test_service", limit=101)


def test_report_contract_rejects_invalid_metrics_or_timestamps() -> None:
    with pytest.raises(TypeError, match="named numbers"):
        ServiceRunReport(
            run_id="run",
            service="test",
            status="completed",
            stop_reason="done",
            started_at=1,
            finished_at=2,
            metrics={"secret": "value"},  # type: ignore[dict-item]
            details={},
        )
    with pytest.raises(ValueError, match="out of order"):
        ServiceRunReport(
            run_id="run",
            service="test",
            status="completed",
            stop_reason="done",
            started_at=2,
            finished_at=1,
            metrics={},
            details={},
        )
