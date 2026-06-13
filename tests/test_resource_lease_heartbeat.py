from __future__ import annotations

import time

import pytest

from loop_engine.adapters import (
    FileResourceLeaseManager,
    FileResourceLeasePolicy,
)
from loop_engine.tasks import (
    ChildTaskSpec,
    FunctionIntegrationVerifier,
    FunctionLeafExecutor,
    InMemoryCapabilityResolver,
    LeafExecutionResult,
    ResourceClaim,
    ResourceLease,
    ResourceLeaseAttempt,
    ScriptedTaskDecomposer,
    TaskGraph,
    TaskScheduler,
    TaskSchedulerPolicy,
    TaskStatus,
)
from loop_engine.tasks.resource_leases import run_leased_operation


def _clocked_managers(path):
    now = [100.0]
    identities = {100: "process-100", 200: "process-200"}
    policy = FileResourceLeasePolicy(
        acquire_timeout_seconds=0.02,
        poll_interval_seconds=0.001,
        stale_lock_seconds=1,
        lease_ttl_seconds=2,
        heartbeat_interval_seconds=0.5,
    )

    def create(pid):
        return FileResourceLeaseManager(
            path,
            policy=policy,
            clock=lambda: now[0],
            sleep=lambda seconds: now.__setitem__(0, now[0] + seconds),
            pid=pid,
            identity_lookup=identities.get,
        )

    return now, create(100), create(200)


def test_expired_lease_is_reclaimed_while_owner_process_is_alive(
    tmp_path,
) -> None:
    now, first, second = _clocked_managers(tmp_path / "leases.json")
    claim = ResourceClaim.create("workspace:shared", mode="write")
    held = first.acquire(owner_id="first", claims=(claim,))
    assert held.lease is not None
    now[0] += 3

    with pytest.raises(RuntimeError, match="already expired"):
        first.renew(held.lease)
    acquired = second.acquire(owner_id="second", claims=(claim,))

    assert held.granted
    assert acquired.granted


def test_renew_extends_lease_and_prevents_reclaim(tmp_path) -> None:
    now, owner, contender = _clocked_managers(tmp_path / "leases.json")
    claim = ResourceClaim.create("workspace:shared", mode="write")
    attempt = owner.acquire(owner_id="owner", claims=(claim,))
    assert attempt.lease is not None
    original_expiry = attempt.lease.expires_at
    now[0] += 1.5

    renewed = owner.renew(attempt.lease)
    now[0] = original_expiry + 0.5

    assert renewed.expires_at > original_expiry
    assert not contender.acquire(owner_id="contender", claims=(claim,)).granted


def test_operation_heartbeat_keeps_short_ttl_lease_alive(tmp_path) -> None:
    path = tmp_path / "leases.json"
    policy = FileResourceLeasePolicy(
        acquire_timeout_seconds=0.2,
        poll_interval_seconds=0.005,
        stale_lock_seconds=1,
        lease_ttl_seconds=0.12,
        heartbeat_interval_seconds=0.04,
    )
    owner = FileResourceLeaseManager(path, policy=policy)
    claim = ResourceClaim.create("workspace:shared", mode="write")
    started_at = time.time()

    def operate():
        time.sleep(0.35)
        return "done"

    result = run_leased_operation(
        manager=owner,
        owner_id="owner",
        claims=(claim,),
        on_acquired=lambda lease: None,
        operation=operate,
    )

    assert result.lease is not None
    assert result.lease.expires_at < time.time()
    assert result.lease.expires_at > started_at
    assert result.value == "done"
    assert result.heartbeat_error is None


def test_scheduler_fails_result_when_heartbeat_renewal_fails() -> None:
    class FailingHeartbeatManager:
        heartbeat_interval_seconds = 0.01

        def acquire(self, **kwargs):
            return ResourceLeaseAttempt(
                lease=ResourceLease(
                    lease_id="lease",
                    owner_id=kwargs["owner_id"],
                    claims=kwargs["claims"],
                    expires_at=time.time() + 1,
                )
            )

        def renew(self, lease):
            raise RuntimeError("renew failed")

        def release(self, lease):
            return None

    claim = ResourceClaim.create("workspace:shared", mode="write")
    scheduler = TaskScheduler(
        decomposer=ScriptedTaskDecomposer(
            {
                "root": [
                    ChildTaskSpec(
                        key="work",
                        goal="mutate",
                        required_capabilities=["workspace.mutate"],
                    )
                ]
            }
        ),
        capability_resolver=InMemoryCapabilityResolver({"workspace.mutate"}),
        leaf_executor=FunctionLeafExecutor(
            lambda node, graph: (
                time.sleep(0.04)
                or LeafExecutionResult(status="completed", summary="done")
            )
        ),
        integration_verifier=FunctionIntegrationVerifier(),
        policy=TaskSchedulerPolicy.create(
            max_parallel_leaves=2,
            parallel_safe_capabilities={"workspace.mutate"},
            mutation_capabilities={"workspace.mutate"},
            resource_claims={"root.work": [claim]},
        ),
        resource_lease_manager=FailingHeartbeatManager(),
    )

    result = scheduler.run(TaskGraph.create("heartbeat failure"))

    assert result.nodes["root.work"].status == TaskStatus.FAILED
    assert result.nodes["root.work"].error == (
        "resource_lease_heartbeat_error:RuntimeError:renew failed"
    )


def test_invalid_heartbeat_contract_blocks_before_operation_and_releases() -> None:
    class InvalidHeartbeatManager:
        heartbeat_interval_seconds = 0

        def __init__(self):
            self.released = False

        def acquire(self, **kwargs):
            return ResourceLeaseAttempt(
                lease=ResourceLease(
                    lease_id="lease",
                    owner_id=kwargs["owner_id"],
                    claims=kwargs["claims"],
                    expires_at=time.time() + 1,
                )
            )

        def renew(self, lease):
            raise AssertionError("renew must not run")

        def release(self, lease):
            self.released = True

    manager = InvalidHeartbeatManager()
    claim = ResourceClaim.create("workspace:shared", mode="write")
    executed = []

    result = run_leased_operation(
        manager=manager,
        owner_id="owner",
        claims=(claim,),
        on_acquired=lambda lease: None,
        operation=lambda: executed.append(True),
    )

    assert not result.executed
    assert result.heartbeat_error == (
        "resource_lease_heartbeat_error:ValueError:"
        "resource lease heartbeat interval must be positive"
    )
    assert manager.released is True
    assert not executed
