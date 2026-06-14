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
    with pytest.raises(ValueError, match="must be failed"):
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
    assert payload["schema_version"] == 2


def test_task_graph_loader_accepts_legacy_schema_v1(tmp_path) -> None:
    graph = TaskGraph.create("Legacy", graph_id="legacy")
    store = JsonTaskGraphStore(tmp_path)
    store.save(graph)
    path = tmp_path / "legacy.json"
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["schema_version"] = 1
    path.write_text(json.dumps(payload), encoding="utf-8")

    loaded = store.load("legacy")

    assert loaded.id == "legacy"
