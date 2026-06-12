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
    ScriptedTaskDecomposer,
    TaskBudget,
    TaskGraph,
    TaskScheduler,
    TaskSchedulerPolicy,
    TaskStatus,
)


def _policy(
    *,
    workers: int = 2,
    safe: set[str] | None = None,
) -> TaskSchedulerPolicy:
    return TaskSchedulerPolicy.create(
        max_parallel_leaves=workers,
        parallel_safe_capabilities=safe or {"parallel.safe"},
    )


def _scheduler(children, executor, *, policy, capabilities=None):
    return TaskScheduler(
        decomposer=ScriptedTaskDecomposer({"root": children}),
        capability_resolver=InMemoryCapabilityResolver(
            capabilities or {"parallel.safe", "serial.only"}
        ),
        leaf_executor=FunctionLeafExecutor(executor),
        integration_verifier=FunctionIntegrationVerifier(),
        policy=policy,
    )


def test_independent_safe_leaves_execute_with_bounded_parallelism() -> None:
    lock = threading.Lock()
    active = 0
    maximum = 0

    def execute(node, graph):
        nonlocal active, maximum
        with lock:
            active += 1
            maximum = max(maximum, active)
        time.sleep(0.12)
        with lock:
            active -= 1
        return LeafExecutionResult(status="completed", summary=f"{node.id} done")

    children = [
        ChildTaskSpec(
            key=str(index),
            goal=f"Leaf {index}",
            required_capabilities=["parallel.safe"],
        )
        for index in range(4)
    ]

    result = _scheduler(
        children,
        execute,
        policy=_policy(workers=2),
    ).run(TaskGraph.create("Parallel work"))

    assert result.root.status == TaskStatus.COMPLETED
    assert maximum == 2
    assert result.leaf_executions == 4


def test_dependency_leaf_waits_while_independent_leaf_can_run() -> None:
    lock = threading.Lock()
    started: dict[str, float] = {}
    finished: dict[str, float] = {}

    def execute(node, graph):
        with lock:
            started[node.id] = time.perf_counter()
        time.sleep(0.08)
        with lock:
            finished[node.id] = time.perf_counter()
        return LeafExecutionResult(status="completed", summary=f"{node.id} done")

    children = [
        ChildTaskSpec(
            key="a",
            goal="A",
            required_capabilities=["parallel.safe"],
        ),
        ChildTaskSpec(
            key="b",
            goal="B",
            required_capabilities=["parallel.safe"],
            depends_on=["a"],
        ),
        ChildTaskSpec(
            key="c",
            goal="C",
            required_capabilities=["parallel.safe"],
        ),
    ]

    result = _scheduler(
        children,
        execute,
        policy=_policy(workers=3),
    ).run(TaskGraph.create("Dependency-aware work"))

    assert result.root.status == TaskStatus.COMPLETED
    assert started["root.b"] >= finished["root.a"]
    assert started["root.c"] < finished["root.a"]


def test_non_admitted_capabilities_remain_serial() -> None:
    lock = threading.Lock()
    active = 0
    maximum = 0

    def execute(node, graph):
        nonlocal active, maximum
        with lock:
            active += 1
            maximum = max(maximum, active)
        time.sleep(0.05)
        with lock:
            active -= 1
        return LeafExecutionResult(status="completed", summary="done")

    children = [
        ChildTaskSpec(
            key=str(index),
            goal=f"Serial {index}",
            required_capabilities=["serial.only"],
        )
        for index in range(3)
    ]

    result = _scheduler(
        children,
        execute,
        policy=_policy(workers=3),
    ).run(TaskGraph.create("Serial-only work"))

    assert result.root.status == TaskStatus.COMPLETED
    assert maximum == 1


def test_parallel_batch_reserves_leaf_budget_before_launch() -> None:
    executed: list[str] = []
    children = [
        ChildTaskSpec(
            key="a",
            goal="A",
            required_capabilities=["parallel.safe"],
        ),
        ChildTaskSpec(
            key="b",
            goal="B",
            required_capabilities=["parallel.safe"],
        ),
    ]

    result = _scheduler(
        children,
        lambda node, graph: (
            executed.append(node.id)
            or LeafExecutionResult(status="completed", summary="done")
        ),
        policy=_policy(workers=2),
    ).run(
        TaskGraph.create(
            "Budgeted parallel work",
            budget=TaskBudget(max_leaf_executions=1),
        )
    )

    assert executed == ["root.a"]
    assert result.nodes["root.b"].status == TaskStatus.BLOCKED
    assert result.nodes["root.b"].error == "leaf_execution_budget_exhausted"
    assert result.leaf_executions == 1


def test_parallel_results_are_applied_in_stable_node_order() -> None:
    def execute(node, graph):
        time.sleep(0.08 if node.id.endswith("a") else 0.01)
        return LeafExecutionResult(status="completed", summary=f"{node.id} done")

    result = _scheduler(
        [
            ChildTaskSpec(
                key="a",
                goal="Slow",
                required_capabilities=["parallel.safe"],
            ),
            ChildTaskSpec(
                key="b",
                goal="Fast",
                required_capabilities=["parallel.safe"],
            ),
        ],
        execute,
        policy=_policy(workers=2),
    ).run(TaskGraph.create("Stable events"))

    completions = [
        event.node_id
        for event in result.events
        if event.event_type == "leaf_completed"
    ]
    assert completions == ["root.a", "root.b"]
    assert [event.sequence for event in result.events] == list(
        range(1, len(result.events) + 1)
    )


def test_parallel_workers_receive_graph_snapshots() -> None:
    def execute(node, graph):
        node.goal = "worker mutation"
        graph.stop_reason = "worker mutation"
        return LeafExecutionResult(status="completed", summary="done")

    result = _scheduler(
        [
            ChildTaskSpec(
                key="a",
                goal="Original A",
                required_capabilities=["parallel.safe"],
            ),
            ChildTaskSpec(
                key="b",
                goal="Original B",
                required_capabilities=["parallel.safe"],
            ),
        ],
        execute,
        policy=_policy(workers=2),
    ).run(TaskGraph.create("Snapshot isolation"))

    assert result.nodes["root.a"].goal == "Original A"
    assert result.nodes["root.b"].goal == "Original B"
    assert result.stop_reason == "all child tasks completed"


def test_parallel_policy_requires_explicit_safe_capabilities() -> None:
    with pytest.raises(ValueError, match="parallel_safe_capabilities"):
        TaskSchedulerPolicy.create(max_parallel_leaves=2)


def test_default_policy_preserves_assess_then_execute_order() -> None:
    result = _scheduler(
        [
            ChildTaskSpec(key="a", goal="A"),
            ChildTaskSpec(key="b", goal="B"),
        ],
        lambda node, graph: LeafExecutionResult(
            status="completed",
            summary="done",
        ),
        policy=TaskSchedulerPolicy(),
    ).run(TaskGraph.create("Sequential compatibility"))

    ordered = [
        (event.event_type, event.node_id)
        for event in result.events
        if event.event_type in {"atomicity_assessed", "leaf_execution_started"}
        and event.node_id != "root"
    ]
    assert ordered == [
        ("atomicity_assessed", "root.a"),
        ("leaf_execution_started", "root.a"),
        ("atomicity_assessed", "root.b"),
        ("leaf_execution_started", "root.b"),
    ]


def test_parallel_snapshot_failure_becomes_leaf_failure() -> None:
    def execute(node, graph):
        evidence = {"lock": threading.Lock()} if node.id == "root.seed" else {}
        return LeafExecutionResult(
            status="completed",
            summary="done",
            evidence=evidence,
        )

    result = _scheduler(
        [
            ChildTaskSpec(
                key="seed",
                goal="Seed",
                required_capabilities=["serial.only"],
            ),
            ChildTaskSpec(
                key="a",
                goal="A",
                required_capabilities=["parallel.safe"],
                depends_on=["seed"],
            ),
            ChildTaskSpec(
                key="b",
                goal="B",
                required_capabilities=["parallel.safe"],
                depends_on=["seed"],
            ),
        ],
        execute,
        policy=_policy(workers=2),
    ).run(TaskGraph.create("Snapshot failure"))

    assert result.nodes["root.a"].status == TaskStatus.FAILED
    assert result.nodes["root.b"].status == TaskStatus.FAILED
    assert result.nodes["root.a"].error.startswith("TypeError:")
