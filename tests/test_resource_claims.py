from __future__ import annotations

import threading
import time

import pytest

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


def _run(children, policy, execute):
    return TaskScheduler(
        decomposer=ScriptedTaskDecomposer({"root": children}),
        capability_resolver=InMemoryCapabilityResolver(
            {"workspace.mutate", "workspace.read"}
        ),
        leaf_executor=FunctionLeafExecutor(execute),
        integration_verifier=FunctionIntegrationVerifier(),
        policy=policy,
    ).run(TaskGraph.create("Resource claims"))


def _tracking_executor():
    lock = threading.Lock()
    active: set[str] = set()
    overlaps: set[frozenset[str]] = set()
    maximum = [0]

    def execute(node, graph):
        with lock:
            for other in active:
                overlaps.add(frozenset({node.id, other}))
            active.add(node.id)
            maximum[0] = max(maximum[0], len(active))
        time.sleep(0.08)
        with lock:
            active.remove(node.id)
        return LeafExecutionResult(status="completed", summary="done")

    return execute, overlaps, maximum


def _mutation_policy(claims) -> TaskSchedulerPolicy:
    return TaskSchedulerPolicy.create(
        max_parallel_leaves=3,
        parallel_safe_capabilities={"workspace.mutate"},
        mutation_capabilities={"workspace.mutate"},
        resource_claims=claims,
    )


def test_different_workspace_writes_run_in_parallel() -> None:
    execute, overlaps, maximum = _tracking_executor()
    children = [
        ChildTaskSpec(
            key=key,
            goal=key,
            required_capabilities=["workspace.mutate"],
        )
        for key in ["a", "b"]
    ]
    result = _run(
        children,
        _mutation_policy(
            {
                "root.a": [ResourceClaim.create("workspace:a", mode="write")],
                "root.b": [ResourceClaim.create("workspace:b", mode="write")],
            }
        ),
        execute,
    )

    assert result.root.status == TaskStatus.COMPLETED
    assert maximum[0] == 2
    assert frozenset({"root.a", "root.b"}) in overlaps


def test_conflicting_write_is_skipped_but_later_independent_leaf_runs() -> None:
    execute, overlaps, maximum = _tracking_executor()
    children = [
        ChildTaskSpec(
            key=key,
            goal=key,
            required_capabilities=["workspace.mutate"],
        )
        for key in ["a", "b", "c"]
    ]
    result = _run(
        children,
        _mutation_policy(
            {
                "root.a": [ResourceClaim.create("workspace:shared", mode="write")],
                "root.b": [ResourceClaim.create("workspace:shared", mode="write")],
                "root.c": [ResourceClaim.create("workspace:other", mode="write")],
            }
        ),
        execute,
    )

    assert result.root.status == TaskStatus.COMPLETED
    assert maximum[0] == 2
    assert frozenset({"root.a", "root.c"}) in overlaps
    assert frozenset({"root.a", "root.b"}) not in overlaps


def test_mutation_without_external_write_claim_remains_serial() -> None:
    execute, overlaps, maximum = _tracking_executor()
    children = [
        ChildTaskSpec(
            key=key,
            goal=key,
            required_capabilities=["workspace.mutate"],
        )
        for key in ["a", "b"]
    ]

    result = _run(children, _mutation_policy({}), execute)

    assert result.root.status == TaskStatus.COMPLETED
    assert maximum[0] == 1
    assert not overlaps


def test_task_metadata_cannot_override_external_claims() -> None:
    execute, _, maximum = _tracking_executor()
    children = [
        ChildTaskSpec(
            key=key,
            goal=key,
            required_capabilities=["workspace.mutate"],
            metadata={"resource_claims": [f"workspace:{key}"]},
        )
        for key in ["a", "b"]
    ]
    policy = _mutation_policy(
        {
            "root.a": [ResourceClaim.create("workspace:shared", mode="write")],
            "root.b": [ResourceClaim.create("workspace:shared", mode="write")],
        }
    )

    result = _run(children, policy, execute)

    assert result.root.status == TaskStatus.COMPLETED
    assert maximum[0] == 1


def test_shared_reads_are_compatible_but_read_write_conflicts() -> None:
    read_claim = ResourceClaim.create("workspace:shared", mode="read")
    write_claim = ResourceClaim.create("workspace:shared", mode="write")
    base = {
        "max_parallel_leaves": 2,
        "parallel_safe_capabilities": {"workspace.read"},
    }
    readers, _, readers_maximum = _tracking_executor()
    reader_children = [
        ChildTaskSpec(
            key=key,
            goal=key,
            required_capabilities=["workspace.read"],
        )
        for key in ["a", "b"]
    ]
    read_result = _run(
        reader_children,
        TaskSchedulerPolicy.create(
            **base,
            resource_claims={"root.a": [read_claim], "root.b": [read_claim]},
        ),
        readers,
    )

    mixed, _, mixed_maximum = _tracking_executor()
    mixed_result = _run(
        reader_children,
        TaskSchedulerPolicy.create(
            **base,
            resource_claims={"root.a": [read_claim], "root.b": [write_claim]},
        ),
        mixed,
    )

    assert read_result.root.status == TaskStatus.COMPLETED
    assert mixed_result.root.status == TaskStatus.COMPLETED
    assert readers_maximum[0] == 2
    assert mixed_maximum[0] == 1


def test_resource_claim_policy_is_immutable_and_copies_input() -> None:
    source = {
        "root.a": [ResourceClaim.create("workspace:a", mode="write")]
    }
    policy = _mutation_policy(source)
    source["root.b"] = [ResourceClaim.create("workspace:b", mode="write")]

    assert "root.b" not in policy.resource_claims
    with pytest.raises(TypeError):
        policy.resource_claims["root.b"] = tuple(source["root.b"])  # type: ignore[index]


def test_resource_claim_contract_validation() -> None:
    with pytest.raises(ValueError, match="read or write"):
        ResourceClaim.create("workspace:a", mode="exclusive")
    with pytest.raises(ValueError, match="must be parallel-safe"):
        TaskSchedulerPolicy.create(
            max_parallel_leaves=2,
            parallel_safe_capabilities={"workspace.read"},
            mutation_capabilities={"workspace.mutate"},
        )
    with pytest.raises(ValueError, match="must be unique"):
        _mutation_policy(
            {
                "root.a": [
                    ResourceClaim.create("workspace:a", mode="read"),
                    ResourceClaim.create("workspace:a", mode="write"),
                ]
            }
        )
    with pytest.raises(ValueError, match="duplicate resource claim node id"):
        _mutation_policy(
            {
                "root.a": [
                    ResourceClaim.create("workspace:a", mode="write")
                ],
                " root.a ": [
                    ResourceClaim.create("workspace:b", mode="write")
                ],
            }
        )


def test_direct_construction_cannot_bypass_contract_validation() -> None:
    with pytest.raises(ValueError, match="name is required"):
        ResourceClaim(resource=" ", mode="write")
    with pytest.raises(ValueError, match="read or write"):
        ResourceClaim(resource="workspace:a", mode="exclusive")
    with pytest.raises(ValueError, match="must be parallel-safe"):
        TaskSchedulerPolicy(
            max_parallel_leaves=2,
            parallel_safe_capabilities=frozenset({"workspace.read"}),
            mutation_capabilities=frozenset({"workspace.mutate"}),
        )


def test_workspace_claim_canonicalizes_equivalent_paths(tmp_path) -> None:
    nested = tmp_path / "nested"
    nested.mkdir()

    direct = ResourceClaim.workspace(nested, mode="write")
    equivalent = ResourceClaim.workspace(
        nested / ".." / "nested",
        mode="write",
    )

    assert direct == equivalent
