from __future__ import annotations

import time

import pytest

from loop_engine.tasks import (
    FunctionIntegrationVerifier,
    FunctionLeafExecutor,
    InMemoryCapabilityResolver,
    LeafExecutionResult,
    ResourceClaim,
    ResourceLease,
    ResourceLeaseAttempt,
    ScriptedTaskDecomposer,
    TaskGraph,
    TaskRetryPolicy,
    TaskScheduler,
    TaskSchedulerPolicy,
    TaskStatus,
)


class OneShotContentionManager:
    heartbeat_interval_seconds = 1.0

    def __init__(self) -> None:
        self.acquire_calls = 0
        self.active = False

    def acquire(self, *, owner_id, claims):
        self.acquire_calls += 1
        if self.acquire_calls == 1:
            return ResourceLeaseAttempt(
                lease=None,
                conflicting_resources=("workspace:shared",),
            )
        self.active = True
        return ResourceLeaseAttempt(
            lease=ResourceLease(
                lease_id="lease-1",
                owner_id=owner_id,
                claims=claims,
                expires_at=time.time() + 60,
                fencing_tokens={"workspace:shared": 1},
            )
        )

    def renew(self, lease):
        return lease

    def release(self, lease):
        self.active = False


class ContendedManager:
    heartbeat_interval_seconds = 1.0

    def acquire(self, *, owner_id, claims):
        return ResourceLeaseAttempt(
            lease=None,
            conflicting_resources=("workspace:shared",),
        )

    def renew(self, lease):
        raise AssertionError("renew must not run")

    def release(self, lease):
        raise AssertionError("release must not run")


class ImmediateWaiter:
    def wait(self, delay_seconds, *, node, graph):
        return True


def _scheduler_policy() -> TaskSchedulerPolicy:
    return TaskSchedulerPolicy.create(
        parallel_safe_capabilities={"workspace.mutate"},
        mutation_capabilities={"workspace.mutate"},
        resource_claims={
            "root": [
                ResourceClaim.create("workspace:shared", mode="write")
            ]
        },
    )


def _retry_policy() -> TaskRetryPolicy:
    return TaskRetryPolicy.create(
        retryable_codes={"resource_lease_contention"},
        idempotency_keys={"root": "workspace-operation"},
        backoff_seconds=(0.2,),
    )


def test_scheduler_retries_lease_contention_after_external_backoff() -> None:
    manager = OneShotContentionManager()
    waits = []

    class RecordingWaiter:
        def wait(self, delay_seconds, *, node, graph):
            waits.append((delay_seconds, manager.active))
            return True

    graph = TaskScheduler(
        decomposer=ScriptedTaskDecomposer({}),
        capability_resolver=InMemoryCapabilityResolver(
            {"workspace.mutate"}
        ),
        leaf_executor=FunctionLeafExecutor(
            lambda node, graph: LeafExecutionResult(
                status="completed", summary="done"
            )
        ),
        integration_verifier=FunctionIntegrationVerifier(),
        policy=_scheduler_policy(),
        resource_lease_manager=manager,
        retry_policy=_retry_policy(),
        retry_waiter=RecordingWaiter(),
    ).run(
        TaskGraph.create(
            "contended retry",
            required_capabilities=["workspace.mutate"],
        )
    )

    assert graph.root.status == TaskStatus.COMPLETED
    assert graph.root.attempts == 1
    assert graph.root.retries == 1
    assert graph.leaf_executions == 1
    assert manager.acquire_calls == 2
    assert waits == [(0.2, False)]


def test_persistent_contention_exhausts_retry_without_execution() -> None:
    graph = TaskScheduler(
        decomposer=ScriptedTaskDecomposer({}),
        capability_resolver=InMemoryCapabilityResolver(
            {"workspace.mutate"}
        ),
        leaf_executor=FunctionLeafExecutor(
            lambda node, graph: pytest.fail("executor must not run")
        ),
        integration_verifier=FunctionIntegrationVerifier(),
        policy=_scheduler_policy(),
        resource_lease_manager=ContendedManager(),
        retry_policy=_retry_policy(),
        retry_waiter=ImmediateWaiter(),
    ).run(
        TaskGraph.create(
            "persistent contention",
            required_capabilities=["workspace.mutate"],
        )
    )

    assert graph.root.status == TaskStatus.BLOCKED
    assert graph.root.attempts == 0
    assert graph.root.retries == 1
    assert graph.leaf_executions == 0
    rejected = next(
        event
        for event in graph.events
        if event.event_type == "leaf_retry_rejected"
    )
    assert rejected.payload["reason"] == "retry_attempt_budget_exhausted"
