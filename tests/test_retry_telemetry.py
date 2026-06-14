from __future__ import annotations

from loop_engine.tasks import (
    TaskGraph,
    aggregate_retry_telemetry,
)
from loop_engine.tasks.events import record_task_event


def test_retry_telemetry_aggregates_only_safe_fields() -> None:
    graph = TaskGraph.create("Telemetry")
    record_task_event(
        graph,
        "leaf_retry_scheduled",
        "root",
        {
            "retry_code": "resource_lease_contention",
            "delay_seconds": 0.2,
            "jitter_seconds": 0.03,
            "idempotency_key": "secret-operation-key",
        },
    )
    record_task_event(
        graph,
        "leaf_retry_wait_started",
        "root",
        {"delay_seconds": 0.2},
    )
    record_task_event(
        graph,
        "leaf_retry_wait_completed",
        "root",
        {"completed": True},
    )
    record_task_event(
        graph,
        "leaf_retry_rejected",
        "root",
        {
            "retry_code": "transient_io",
            "reason": "retry_attempt_budget_exhausted",
            "idempotency_key": "another-secret",
        },
    )

    payload = aggregate_retry_telemetry(graph).to_dict()

    assert payload["scheduled"] == 1
    assert payload["rejected"] == 1
    assert payload["total_delay_seconds"] == 0.2
    assert payload["total_jitter_seconds"] == 0.03
    assert payload["codes"] == {
        "resource_lease_contention": 1,
        "transient_io": 1,
    }
    assert payload["rejection_reasons"] == {
        "retry_attempt_budget_exhausted": 1
    }
    assert "secret" not in repr(payload)


def test_retry_telemetry_ignores_invalid_numeric_payloads() -> None:
    graph = TaskGraph.create("Invalid telemetry")
    record_task_event(
        graph,
        "leaf_retry_scheduled",
        "root",
        {
            "delay_seconds": float("nan"),
            "jitter_seconds": float("inf"),
        },
    )
    record_task_event(
        graph,
        "leaf_retry_wait_completed",
        "root",
        {"completed": False},
    )

    telemetry = aggregate_retry_telemetry(graph)

    assert telemetry.total_delay_seconds == 0
    assert telemetry.total_jitter_seconds == 0
    assert telemetry.waits_cancelled == 1
