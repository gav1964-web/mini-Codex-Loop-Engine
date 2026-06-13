from __future__ import annotations

import json
import multiprocessing
import os
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
    ScriptedTaskDecomposer,
    TaskGraph,
    TaskScheduler,
    TaskSchedulerPolicy,
    TaskStatus,
)


def _manager(path, *, pid=100, identities=None, timeout=0.02):
    if identities is None:
        identities = {pid: f"process-{pid}"}
    return FileResourceLeaseManager(
        path,
        policy=FileResourceLeasePolicy(
            acquire_timeout_seconds=timeout,
            poll_interval_seconds=0.001,
            stale_lock_seconds=1,
        ),
        pid=pid,
        identity_lookup=identities.get,
    )


def _hold_lease_in_process(path, ready, release, result_queue) -> None:
    manager = FileResourceLeaseManager(
        path,
        policy=FileResourceLeasePolicy(
            acquire_timeout_seconds=1,
            poll_interval_seconds=0.005,
        ),
    )
    claim = ResourceClaim.create("workspace:shared", mode="write")
    attempt = manager.acquire(owner_id="child", claims=(claim,))
    result_queue.put(attempt.granted)
    ready.set()
    release.wait(timeout=5)
    if attempt.lease is not None:
        manager.release(attempt.lease)


def test_separate_processes_enforce_write_exclusion(tmp_path) -> None:
    context = multiprocessing.get_context("spawn")
    ready = context.Event()
    release = context.Event()
    result_queue = context.Queue()
    path = tmp_path / "leases.json"
    process = context.Process(
        target=_hold_lease_in_process,
        args=(path, ready, release, result_queue),
    )
    process.start()
    try:
        assert ready.wait(timeout=5)
        assert result_queue.get(timeout=1) is True
        parent = FileResourceLeaseManager(
            path,
            policy=FileResourceLeasePolicy(
                acquire_timeout_seconds=0.05,
                poll_interval_seconds=0.005,
            ),
        )
        claim = ResourceClaim.create("workspace:shared", mode="write")
        assert not parent.acquire(owner_id="parent", claims=(claim,)).granted
    finally:
        release.set()
        process.join(timeout=5)
        if process.is_alive():
            process.terminate()
            process.join(timeout=2)
    assert process.exitcode == 0


def test_independent_manager_instances_share_write_exclusion(tmp_path) -> None:
    path = tmp_path / "leases.json"
    identities = {100: "process-100", 200: "process-200"}
    first = _manager(path, pid=100, identities=identities)
    second = _manager(path, pid=200, identities=identities)
    claim = ResourceClaim.create("workspace:shared", mode="write")

    held = first.acquire(owner_id="first", claims=(claim,))
    denied = second.acquire(owner_id="second", claims=(claim,))

    assert held.granted
    assert not denied.granted
    assert denied.conflicting_resources == ("workspace:shared",)

    first.release(held.lease)  # type: ignore[arg-type]
    acquired = second.acquire(owner_id="second", claims=(claim,))
    assert acquired.granted


def test_shared_reads_coexist_and_writer_waits(tmp_path) -> None:
    path = tmp_path / "leases.json"
    identities = {100: "process-100", 200: "process-200", 300: "process-300"}
    readers = [
        _manager(path, pid=pid, identities=identities)
        for pid in (100, 200)
    ]
    writer = _manager(path, pid=300, identities=identities)
    read = ResourceClaim.create("workspace:shared", mode="read")
    write = ResourceClaim.create("workspace:shared", mode="write")

    leases = [
        manager.acquire(owner_id=f"reader-{index}", claims=(read,))
        for index, manager in enumerate(readers)
    ]

    assert all(attempt.granted for attempt in leases)
    assert not writer.acquire(owner_id="writer", claims=(write,)).granted


def test_acquire_is_atomic_for_multiple_resources(tmp_path) -> None:
    path = tmp_path / "leases.json"
    identities = {100: "process-100", 200: "process-200"}
    first = _manager(path, pid=100, identities=identities)
    second = _manager(path, pid=200, identities=identities)
    occupied = ResourceClaim.create("workspace:b", mode="write")
    first.acquire(owner_id="first", claims=(occupied,))

    denied = second.acquire(
        owner_id="second",
        claims=(
            ResourceClaim.create("workspace:a", mode="write"),
            occupied,
        ),
    )
    payload = json.loads(path.read_text(encoding="utf-8"))

    assert not denied.granted
    assert {
        claim["resource"]
        for row in payload["leases"]
        for claim in row["claims"]
    } == {"workspace:b"}


def test_dead_process_lease_is_reclaimed(tmp_path) -> None:
    path = tmp_path / "leases.json"
    identities = {100: "process-100", 200: "process-200"}
    first = _manager(path, pid=100, identities=identities)
    claim = ResourceClaim.create("workspace:shared", mode="write")
    first.acquire(owner_id="first", claims=(claim,))
    identities.pop(100)

    second = _manager(path, pid=200, identities=identities)
    acquired = second.acquire(owner_id="second", claims=(claim,))

    assert acquired.granted
    payload = json.loads(path.read_text(encoding="utf-8"))
    assert [row["owner_pid"] for row in payload["leases"]] == [200]


def test_scheduler_blocks_without_consuming_execution_budget_on_contention(
    tmp_path,
) -> None:
    path = tmp_path / "leases.json"
    identities = {100: "process-100", 200: "process-200"}
    holder = _manager(path, pid=100, identities=identities)
    contender = _manager(path, pid=200, identities=identities)
    claim = ResourceClaim.create("workspace:shared", mode="write")
    holder.acquire(owner_id="other-run", claims=(claim,))
    executed: list[str] = []
    policy = TaskSchedulerPolicy.create(
        max_parallel_leaves=2,
        parallel_safe_capabilities={"workspace.mutate"},
        mutation_capabilities={"workspace.mutate"},
        resource_claims={"root.work": [claim]},
    )
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
                executed.append(node.id)
                or LeafExecutionResult(status="completed", summary="done")
            )
        ),
        integration_verifier=FunctionIntegrationVerifier(),
        policy=policy,
        resource_lease_manager=contender,
    )

    result = scheduler.run(TaskGraph.create("contended"))

    assert result.root.status == TaskStatus.BLOCKED
    assert result.nodes["root.work"].error == (
        "resource_lease_unavailable:workspace:shared"
    )
    assert result.leaf_executions == 0
    assert result.nodes["root.work"].attempts == 0
    assert not executed


def test_scheduler_releases_lease_after_executor_failure(tmp_path) -> None:
    path = tmp_path / "leases.json"
    identities = {100: "process-100", 200: "process-200"}
    manager = _manager(path, pid=100, identities=identities)
    claim = ResourceClaim.create("workspace:shared", mode="write")
    policy = TaskSchedulerPolicy.create(
        max_parallel_leaves=2,
        parallel_safe_capabilities={"workspace.mutate"},
        mutation_capabilities={"workspace.mutate"},
        resource_claims={"root.work": [claim]},
    )
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
            lambda node, graph: (_ for _ in ()).throw(RuntimeError("boom"))
        ),
        integration_verifier=FunctionIntegrationVerifier(),
        policy=policy,
        resource_lease_manager=manager,
    )

    result = scheduler.run(TaskGraph.create("failure"))
    next_manager = _manager(path, pid=200, identities=identities)

    assert result.nodes["root.work"].status == TaskStatus.FAILED
    assert next_manager.acquire(owner_id="next", claims=(claim,)).granted


def test_policy_and_registry_contracts_fail_closed(tmp_path) -> None:
    with pytest.raises(ValueError, match="bounds must be positive"):
        FileResourceLeasePolicy(acquire_timeout_seconds=0)
    with pytest.raises(RuntimeError, match="identity is unavailable"):
        _manager(tmp_path / "leases.json", identities={})

    path = tmp_path / "leases.json"
    path.write_text(
        json.dumps({"schema_version": 999, "leases": []}),
        encoding="utf-8",
    )
    manager = _manager(path)
    with pytest.raises(ValueError, match="schema_version"):
        manager.acquire(
            owner_id="owner",
            claims=(ResourceClaim.create("workspace:a", mode="read"),),
        )


def test_stale_lock_without_timestamp_is_reclaimed(tmp_path) -> None:
    path = tmp_path / "leases.json"
    manager = _manager(path)
    manager.lock_path.mkdir(parents=True)
    old = time.time() - 10
    os.utime(manager.lock_path, (old, old))

    acquired = manager.acquire(
        owner_id="owner",
        claims=(ResourceClaim.create("workspace:a", mode="read"),),
    )

    assert acquired.granted


def test_malformed_lease_manager_result_blocks_scheduler(tmp_path) -> None:
    class MalformedManager:
        def acquire(self, **kwargs):
            return {"granted": True}

        def release(self, lease):
            raise AssertionError("release must not run")

    claim = ResourceClaim.create("workspace:shared", mode="write")
    policy = TaskSchedulerPolicy.create(
        max_parallel_leaves=2,
        parallel_safe_capabilities={"workspace.mutate"},
        mutation_capabilities={"workspace.mutate"},
        resource_claims={"root.work": [claim]},
    )
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
            lambda node, graph: LeafExecutionResult(
                status="completed",
                summary="must not run",
            )
        ),
        integration_verifier=FunctionIntegrationVerifier(),
        policy=policy,
        resource_lease_manager=MalformedManager(),
    )

    result = scheduler.run(TaskGraph.create("malformed lease"))

    assert result.nodes["root.work"].status == TaskStatus.BLOCKED
    assert result.nodes["root.work"].error == (
        "resource_lease_error:invalid_acquisition_contract"
    )
