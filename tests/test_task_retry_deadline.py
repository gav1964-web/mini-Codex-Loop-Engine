from __future__ import annotations

import pytest

from loop_engine.tasks import (
    FunctionIntegrationVerifier,
    FunctionLeafExecutor,
    InMemoryCapabilityResolver,
    JsonTaskGraphStore,
    LeafExecutionResult,
    ScriptedTaskDecomposer,
    TaskGraph,
    TaskRetryPolicy,
    TaskScheduler,
    TaskStatus,
)


def _retryable() -> LeafExecutionResult:
    return LeafExecutionResult(
        status="failed",
        summary="temporary failure",
        error="transient_io",
        retryable=True,
        retry_code="transient_io",
        idempotency_key="operation-1",
    )


def _policy(**overrides) -> TaskRetryPolicy:
    values = {
        "max_attempts_per_leaf": 2,
        "retryable_codes": {"transient_io"},
        "idempotency_keys": {"root": "operation-1"},
    }
    values.update(overrides)
    return TaskRetryPolicy.create(**values)


class FakeRetryClock:
    def __init__(self, now: float = 1000.0) -> None:
        self.value = now

    def now(self) -> float:
        return self.value


def test_retry_deadline_rejects_delay_that_cannot_fit() -> None:
    graph = TaskScheduler(
        decomposer=ScriptedTaskDecomposer({}),
        capability_resolver=InMemoryCapabilityResolver(),
        leaf_executor=FunctionLeafExecutor(
            lambda node, graph: _retryable()
        ),
        integration_verifier=FunctionIntegrationVerifier(),
        retry_policy=_policy(
            backoff_seconds=(2.0,),
            max_retry_elapsed_seconds=1.0,
        ),
        retry_clock=FakeRetryClock(),
    ).run(TaskGraph.create("Deadline"))

    assert graph.root.status == TaskStatus.FAILED
    assert graph.root.retries == 0
    rejected = next(
        event
        for event in graph.events
        if event.event_type == "leaf_retry_rejected"
    )
    assert rejected.payload["reason"] == "retry_delay_exceeds_deadline"
    assert rejected.payload["remaining_seconds"] == 1.0


def test_retry_wait_crossing_deadline_is_terminal() -> None:
    clock = FakeRetryClock()

    class AdvancingWaiter:
        def wait(self, delay_seconds, *, node, graph):
            clock.value += 1.1
            return True

    graph = TaskScheduler(
        decomposer=ScriptedTaskDecomposer({}),
        capability_resolver=InMemoryCapabilityResolver(),
        leaf_executor=FunctionLeafExecutor(
            lambda node, graph: _retryable()
        ),
        integration_verifier=FunctionIntegrationVerifier(),
        retry_policy=_policy(
            backoff_seconds=(0.5,),
            max_retry_elapsed_seconds=1.0,
        ),
        retry_waiter=AdvancingWaiter(),
        retry_clock=clock,
    ).run(TaskGraph.create("Cross deadline"))

    assert graph.root.status == TaskStatus.FAILED
    assert graph.root.retry_started_at == 1000.0
    completed = next(
        event
        for event in graph.events
        if event.event_type == "leaf_retry_wait_completed"
    )
    assert completed.payload["deadline_rejection"] == (
        "retry_elapsed_budget_exhausted"
    )
    rejected = next(
        event
        for event in graph.events
        if event.event_type == "leaf_retry_rejected"
    )
    assert rejected.payload["reason"] == "retry_elapsed_budget_exhausted"


def test_retry_elapsed_budget_survives_multiple_attempts() -> None:
    clock = FakeRetryClock()
    calls = 0

    def execute(node, graph):
        nonlocal calls
        calls += 1
        if calls == 1:
            return _retryable()
        clock.value += 2.0
        return _retryable()

    graph = TaskScheduler(
        decomposer=ScriptedTaskDecomposer({}),
        capability_resolver=InMemoryCapabilityResolver(),
        leaf_executor=FunctionLeafExecutor(execute),
        integration_verifier=FunctionIntegrationVerifier(),
        retry_policy=_policy(
            max_attempts_per_leaf=3,
            max_retry_elapsed_seconds=1.0,
        ),
        retry_clock=clock,
    ).run(TaskGraph.create("Elapsed budget"))

    assert graph.root.attempts == 2
    assert graph.root.retries == 1
    rejected = next(
        event
        for event in graph.events
        if event.event_type == "leaf_retry_rejected"
    )
    assert rejected.payload["reason"] == "retry_elapsed_budget_exhausted"


def test_persisted_retry_deadline_survives_resume(tmp_path) -> None:
    clock = FakeRetryClock()
    graph = TaskGraph.create("Resume deadline", graph_id="deadline-resume")
    graph.root.status = TaskStatus.READY
    graph.root.retries = 1
    graph.root.retry_started_at = 1000.0
    store = JsonTaskGraphStore(tmp_path)
    store.save(graph)
    clock.value = 1002.0

    resumed = TaskScheduler(
        decomposer=ScriptedTaskDecomposer({}),
        capability_resolver=InMemoryCapabilityResolver(),
        leaf_executor=FunctionLeafExecutor(
            lambda node, graph: _retryable()
        ),
        integration_verifier=FunctionIntegrationVerifier(),
        retry_policy=_policy(
            max_attempts_per_leaf=3,
            max_retry_elapsed_seconds=1.0,
        ),
        retry_clock=clock,
    ).run(store.load("deadline-resume"))

    assert resumed.root.retry_started_at == 1000.0
    assert resumed.root.status == TaskStatus.FAILED
    rejected = next(
        event
        for event in resumed.events
        if event.event_type == "leaf_retry_rejected"
    )
    assert rejected.payload["reason"] == "retry_elapsed_budget_exhausted"


def test_retry_jitter_is_deterministic_and_seeded() -> None:
    policy = _policy(
        backoff_seconds=(1.0,),
        max_jitter_seconds=0.5,
        jitter_seed="scheduler-a",
    )
    node = TaskGraph.create("Jitter", graph_id="graph-a").root
    first = policy.decide(
        node, _retryable(), graph_id="graph-a", now=1000.0
    )
    repeated = policy.decide(
        node, _retryable(), graph_id="graph-a", now=1000.0
    )
    other = _policy(
        backoff_seconds=(1.0,),
        max_jitter_seconds=0.5,
        jitter_seed="scheduler-b",
    ).decide(
        node, _retryable(), graph_id="graph-a", now=1000.0
    )

    assert first == repeated
    assert first.base_delay_seconds == 1.0
    assert 0 < first.jitter_seconds < 0.5
    assert first.delay_seconds == 1.0 + first.jitter_seconds
    assert other.jitter_seconds != first.jitter_seconds


def test_retry_deadline_and_jitter_contract_validation() -> None:
    with pytest.raises(ValueError, match="elapsed deadline"):
        _policy(max_retry_elapsed_seconds=0)
    with pytest.raises(ValueError, match="jitter must"):
        _policy(max_jitter_seconds=-1, jitter_seed="scheduler")
    with pytest.raises(ValueError, match="requires a non-empty seed"):
        _policy(max_jitter_seconds=0.1)
    with pytest.raises(ValueError, match="requires positive jitter"):
        _policy(jitter_seed="scheduler")
    with pytest.raises(ValueError, match="backoff"):
        _policy(backoff_seconds=(float("nan"),))
    with pytest.raises(ValueError, match="elapsed deadline"):
        _policy(max_retry_elapsed_seconds=float("inf"))


def test_retry_clock_failure_rejects_retry() -> None:
    class FailingClock:
        def now(self):
            raise RuntimeError("clock failed")

    graph = TaskScheduler(
        decomposer=ScriptedTaskDecomposer({}),
        capability_resolver=InMemoryCapabilityResolver(),
        leaf_executor=FunctionLeafExecutor(
            lambda node, graph: _retryable()
        ),
        integration_verifier=FunctionIntegrationVerifier(),
        retry_policy=_policy(),
        retry_clock=FailingClock(),
    ).run(TaskGraph.create("Clock failure"))

    rejected = next(
        event
        for event in graph.events
        if event.event_type == "leaf_retry_rejected"
    )
    assert rejected.payload["reason"] == "retry_clock_error"


def test_non_finite_retry_clock_is_rejected() -> None:
    clock = FakeRetryClock(float("nan"))
    graph = TaskScheduler(
        decomposer=ScriptedTaskDecomposer({}),
        capability_resolver=InMemoryCapabilityResolver(),
        leaf_executor=FunctionLeafExecutor(
            lambda node, graph: _retryable()
        ),
        integration_verifier=FunctionIntegrationVerifier(),
        retry_policy=_policy(),
        retry_clock=clock,
    ).run(TaskGraph.create("Invalid clock"))

    rejected = next(
        event
        for event in graph.events
        if event.event_type == "leaf_retry_rejected"
    )
    assert rejected.payload["reason"] == "retry_clock_invalid"
