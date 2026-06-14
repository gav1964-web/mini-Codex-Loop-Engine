from __future__ import annotations

import json

import pytest

from loop_engine.tasks import (
    FunctionIntegrationVerifier,
    FunctionLeafExecutor,
    InMemoryCapabilityResolver,
    JsonTaskGraphStore,
    LeafExecutionResult,
    ScriptedTaskDecomposer,
    TaskBudget,
    TaskGraph,
    TaskRetryPolicy,
    TaskScheduler,
    TaskStatus,
)


def _retryable(key: str = "operation-1", code: str = "transient_io"):
    return LeafExecutionResult(
        status="failed",
        summary="temporary failure",
        error=code,
        retryable=True,
        retry_code=code,
        idempotency_key=key,
    )


def _policy(**overrides) -> TaskRetryPolicy:
    values = {
        "max_attempts_per_leaf": 2,
        "retryable_codes": {"transient_io"},
        "idempotency_keys": {"root": "operation-1"},
    }
    values.update(overrides)
    return TaskRetryPolicy.create(**values)


def test_retryable_leaf_completes_on_second_attempt() -> None:
    calls = 0

    def execute(node, graph):
        nonlocal calls
        calls += 1
        if calls == 1:
            return _retryable()
        return LeafExecutionResult(status="completed", summary="done")

    result = TaskScheduler(
        decomposer=ScriptedTaskDecomposer({}),
        capability_resolver=InMemoryCapabilityResolver(),
        leaf_executor=FunctionLeafExecutor(execute),
        integration_verifier=FunctionIntegrationVerifier(),
        retry_policy=_policy(),
    ).run(TaskGraph.create("Retry", budget=TaskBudget(max_leaf_executions=3)))

    assert result.root.status == TaskStatus.COMPLETED
    assert result.root.attempts == 2
    assert result.root.retries == 1
    assert result.leaf_executions == 2
    assert result.root.error is None
    assert [
        event.event_type for event in result.events
    ].count("leaf_retry_scheduled") == 1


def test_retry_attempt_budget_exhaustion_is_terminal() -> None:
    result = TaskScheduler(
        decomposer=ScriptedTaskDecomposer({}),
        capability_resolver=InMemoryCapabilityResolver(),
        leaf_executor=FunctionLeafExecutor(
            lambda node, graph: _retryable()
        ),
        integration_verifier=FunctionIntegrationVerifier(),
        retry_policy=_policy(),
    ).run(TaskGraph.create("Retry twice", budget=TaskBudget(max_leaf_executions=3)))

    assert result.root.status == TaskStatus.FAILED
    assert result.root.attempts == 2
    rejected = [
        event
        for event in result.events
        if event.event_type == "leaf_retry_rejected"
    ]
    assert rejected[0].payload["reason"] == "retry_attempt_budget_exhausted"


@pytest.mark.parametrize(
    ("policy", "result", "reason"),
    [
        (None, _retryable(), "retry_policy_missing"),
        (_policy(), _retryable(key="wrong"), "retry_idempotency_key_mismatch"),
        (_policy(), _retryable(code="busy"), "retry_code_not_allowed"),
        (
            _policy(idempotency_keys={"other": "operation-1"}),
            _retryable(),
            "retry_node_not_authorized",
        ),
    ],
)
def test_retry_request_rejected_fail_closed(policy, result, reason) -> None:
    graph = TaskScheduler(
        decomposer=ScriptedTaskDecomposer({}),
        capability_resolver=InMemoryCapabilityResolver(),
        leaf_executor=FunctionLeafExecutor(lambda node, graph: result),
        integration_verifier=FunctionIntegrationVerifier(),
        retry_policy=policy,
    ).run(TaskGraph.create("Reject retry"))

    assert graph.root.status == TaskStatus.FAILED
    rejected = next(
        event
        for event in graph.events
        if event.event_type == "leaf_retry_rejected"
    )
    assert rejected.payload["reason"] == reason


def test_retry_contract_validation() -> None:
    with pytest.raises(ValueError, match="must be failed or blocked"):
        LeafExecutionResult(
            status="completed",
            summary="invalid",
            retryable=True,
            retry_code="transient_io",
            idempotency_key="key",
        )
    with pytest.raises(ValueError, match="requires retry code"):
        LeafExecutionResult(
            status="failed",
            summary="invalid",
            retryable=True,
        )
    with pytest.raises(ValueError, match="between 2 and 10"):
        _policy(max_attempts_per_leaf=1)
    with pytest.raises(ValueError, match="codes"):
        _policy(retryable_codes=set())
    with pytest.raises(ValueError, match="idempotency"):
        _policy(idempotency_keys={})
    with pytest.raises(ValueError, match="must be unique"):
        _policy(idempotency_keys={"root": "a", " root ": "b"})
    with pytest.raises(ValueError, match="backoff"):
        _policy(backoff_seconds=(1, 2))
    with pytest.raises(ValueError, match="between 0 and 3600"):
        _policy(backoff_seconds=(-1,))
    source = {"root": "operation-1"}
    policy = _policy(idempotency_keys=source)
    source["other"] = "operation-2"
    assert "other" not in policy.idempotency_keys
    with pytest.raises(TypeError):
        policy.idempotency_keys["other"] = "operation-2"  # type: ignore[index]


def test_retryable_result_persists_without_losing_contract(tmp_path) -> None:
    graph = TaskGraph.create("Persist retry", graph_id="retry-contract")
    graph.root.status = TaskStatus.READY
    graph.root.result = _retryable()
    store = JsonTaskGraphStore(tmp_path)

    store.save(graph)
    loaded = store.load("retry-contract")

    assert loaded.root.result.retryable is True
    assert loaded.root.result.retry_code == "transient_io"
    assert loaded.root.result.idempotency_key == "operation-1"
    payload = json.loads(
        (tmp_path / "retry-contract.json").read_text(encoding="utf-8")
    )
    assert payload["schema_version"] == 3
    assert payload["graph"]["nodes"]["root"]["retries"] == 0


@pytest.mark.parametrize("schema_version", [1, 2])
def test_task_graph_loader_accepts_legacy_schemas(
    tmp_path, schema_version
) -> None:
    graph = TaskGraph.create("Legacy", graph_id="legacy")
    store = JsonTaskGraphStore(tmp_path)
    store.save(graph)
    path = tmp_path / "legacy.json"
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["schema_version"] = schema_version
    path.write_text(json.dumps(payload), encoding="utf-8")

    loaded = store.load("legacy")

    assert loaded.id == "legacy"
    assert loaded.root.retries == 0


def test_retry_backoff_uses_injected_waiter() -> None:
    waits = []
    calls = 0

    class RecordingWaiter:
        def wait(self, delay_seconds, *, node, graph):
            waits.append((delay_seconds, node.id, node.status))
            return True

    def execute(node, graph):
        nonlocal calls
        calls += 1
        if calls == 1:
            return _retryable()
        return LeafExecutionResult(status="completed", summary="done")

    graph = TaskScheduler(
        decomposer=ScriptedTaskDecomposer({}),
        capability_resolver=InMemoryCapabilityResolver(),
        leaf_executor=FunctionLeafExecutor(execute),
        integration_verifier=FunctionIntegrationVerifier(),
        retry_policy=_policy(backoff_seconds=(0.25,)),
        retry_waiter=RecordingWaiter(),
    ).run(TaskGraph.create("Backoff"))

    assert graph.root.status == TaskStatus.COMPLETED
    assert waits == [(0.25, "root", TaskStatus.RUNNING)]
    event_types = [event.event_type for event in graph.events]
    assert event_types.index("leaf_retry_wait_started") < event_types.index(
        "leaf_retry_scheduled"
    )


@pytest.mark.parametrize("waiter", [None, False])
def test_delayed_retry_requires_successful_external_waiter(waiter) -> None:
    class CancellingWaiter:
        def wait(self, delay_seconds, *, node, graph):
            return False

    graph = TaskScheduler(
        decomposer=ScriptedTaskDecomposer({}),
        capability_resolver=InMemoryCapabilityResolver(),
        leaf_executor=FunctionLeafExecutor(
            lambda node, graph: _retryable()
        ),
        integration_verifier=FunctionIntegrationVerifier(),
        retry_policy=_policy(backoff_seconds=(0.1,)),
        retry_waiter=CancellingWaiter() if waiter is False else None,
    ).run(TaskGraph.create("Cancelled backoff"))

    assert graph.root.status == TaskStatus.FAILED
    assert graph.root.attempts == 1
    rejected = next(
        event
        for event in graph.events
        if event.event_type == "leaf_retry_rejected"
    )
    assert rejected.payload["reason"] == "retry_wait_cancelled"


def test_retry_waiter_failure_is_structured_terminal_failure() -> None:
    class FailingWaiter:
        def wait(self, delay_seconds, *, node, graph):
            raise RuntimeError("clock unavailable")

    graph = TaskScheduler(
        decomposer=ScriptedTaskDecomposer({}),
        capability_resolver=InMemoryCapabilityResolver(),
        leaf_executor=FunctionLeafExecutor(
            lambda node, graph: _retryable()
        ),
        integration_verifier=FunctionIntegrationVerifier(),
        retry_policy=_policy(backoff_seconds=(0.1,)),
        retry_waiter=FailingWaiter(),
    ).run(TaskGraph.create("Broken waiter"))

    completed = next(
        event
        for event in graph.events
        if event.event_type == "leaf_retry_wait_completed"
    )
    assert completed.payload["completed"] is False
    assert completed.payload["error"] == "RuntimeError:clock unavailable"
