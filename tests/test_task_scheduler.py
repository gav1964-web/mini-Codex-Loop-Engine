from __future__ import annotations

import json

import pytest

from loop_engine.tasks import (
    AtomicLeafSpec,
    AtomicityDecision,
    ChildTaskSpec,
    FunctionCapabilityAcquirer,
    FunctionIntegrationVerifier,
    FunctionLeafExecutor,
    InMemoryCapabilityResolver,
    JsonTaskGraphStore,
    LeafExecutionResult,
    ScriptedTaskDecomposer,
    TaskBudget,
    TaskGraph,
    TaskScheduler,
    TaskStatus,
)


def test_scheduler_decomposes_to_atomic_leaves_in_dependency_order(tmp_path) -> None:
    graph = TaskGraph.create("Build feature", graph_id="ordered")
    decomposer = ScriptedTaskDecomposer(
        {
            "root": [
                ChildTaskSpec(
                    key="inspect",
                    goal="Inspect target",
                    required_capabilities=["read"],
                ),
                ChildTaskSpec(
                    key="repair",
                    goal="Repair target",
                    required_capabilities=["edit"],
                    depends_on=["inspect"],
                ),
            ]
        }
    )
    execution_order: list[str] = []

    def execute(node, task_graph):
        execution_order.append(node.id)
        return LeafExecutionResult(
            status="completed",
            summary=f"{node.id} done",
            evidence={"node": node.id},
        )

    store = JsonTaskGraphStore(tmp_path)
    result = TaskScheduler(
        decomposer=decomposer,
        capability_resolver=InMemoryCapabilityResolver({"read", "edit"}),
        leaf_executor=FunctionLeafExecutor(execute),
        integration_verifier=FunctionIntegrationVerifier(),
        store=store,
    ).run(graph)

    assert result.root.status == TaskStatus.COMPLETED
    assert execution_order == ["root.inspect", "root.repair"]
    assert result.nodes["root.repair"].dependencies == ["root.inspect"]
    assert result.root.result.evidence["children"]["root.inspect"]["node"] == "root.inspect"
    checkpoint = json.loads((tmp_path / "ordered.json").read_text(encoding="utf-8"))
    assert checkpoint["graph"]["nodes"]["root"]["status"] == "completed"


def test_missing_capability_is_acquired_before_leaf_execution() -> None:
    graph = TaskGraph.create(
        "Use generated capability",
        required_capabilities=["generated_tool"],
    )
    resolver = InMemoryCapabilityResolver()
    acquisition_requests: list[str] = []

    def acquire(capability, node, task_graph):
        acquisition_requests.append(capability)
        resolver.register(capability)
        return True

    result = TaskScheduler(
        decomposer=ScriptedTaskDecomposer({}),
        capability_resolver=resolver,
        capability_acquirer=FunctionCapabilityAcquirer(acquire),
        leaf_executor=FunctionLeafExecutor(
            lambda node, task_graph: LeafExecutionResult(
                status="completed",
                summary="capability used",
            )
        ),
        integration_verifier=FunctionIntegrationVerifier(),
    ).run(graph)

    assert result.root.status == TaskStatus.COMPLETED
    assert acquisition_requests == ["generated_tool"]
    assert any(
        event.event_type == "capability_acquisition_requested"
        for event in result.events
    )


def test_missing_capability_blocks_leaf_and_parent() -> None:
    graph = TaskGraph.create("Parent")
    decomposer = ScriptedTaskDecomposer(
        {
            "root": [
                ChildTaskSpec(
                    key="leaf",
                    goal="Unavailable work",
                    required_capabilities=["missing"],
                )
            ]
        }
    )

    result = TaskScheduler(
        decomposer=decomposer,
        capability_resolver=InMemoryCapabilityResolver(),
        leaf_executor=FunctionLeafExecutor(
            lambda node, task_graph: LeafExecutionResult(
                status="completed",
                summary="must not execute",
            )
        ),
        integration_verifier=FunctionIntegrationVerifier(),
    ).run(graph)

    assert result.nodes["root.leaf"].status == TaskStatus.BLOCKED
    assert result.nodes["root.leaf"].attempts == 0
    assert result.root.status == TaskStatus.BLOCKED
    assert result.root.error == "child_failed_or_blocked"


def test_failed_leaf_blocks_dependent_sibling() -> None:
    graph = TaskGraph.create("Parent")
    decomposer = ScriptedTaskDecomposer(
        {
            "root": [
                ChildTaskSpec(key="first", goal="Fail"),
                ChildTaskSpec(key="second", goal="Must wait", depends_on=["first"]),
            ]
        }
    )
    executed: list[str] = []

    def execute(node, task_graph):
        executed.append(node.id)
        return LeafExecutionResult(
            status="failed",
            summary="failure",
            error="expected failure",
        )

    result = TaskScheduler(
        decomposer=decomposer,
        capability_resolver=InMemoryCapabilityResolver(),
        leaf_executor=FunctionLeafExecutor(execute),
        integration_verifier=FunctionIntegrationVerifier(),
    ).run(graph)

    assert executed == ["root.first"]
    assert result.nodes["root.second"].status == TaskStatus.BLOCKED
    assert result.nodes["root.second"].error == "dependency_failed_or_blocked"
    assert result.root.status == TaskStatus.BLOCKED


def test_depth_budget_blocks_non_atomic_node() -> None:
    graph = TaskGraph.create(
        "Cannot expand",
        budget=TaskBudget(max_depth=0),
    )
    result = TaskScheduler(
        decomposer=ScriptedTaskDecomposer(
            {"root": [ChildTaskSpec(key="child", goal="Too deep")]}
        ),
        capability_resolver=InMemoryCapabilityResolver(),
        leaf_executor=FunctionLeafExecutor(
            lambda node, task_graph: LeafExecutionResult(
                status="completed",
                summary="unused",
            )
        ),
        integration_verifier=FunctionIntegrationVerifier(),
    ).run(graph)

    assert result.root.status == TaskStatus.BLOCKED
    assert result.root.error == "task_depth_budget_exhausted"


def test_task_graph_store_recovers_interrupted_leaf_as_ready(tmp_path) -> None:
    graph = TaskGraph.create("Recover", graph_id="recovery")
    graph.root.status = TaskStatus.RUNNING
    graph.root.attempts = 1
    store = JsonTaskGraphStore(tmp_path)
    store.save(graph)

    loaded = store.load("recovery")

    assert loaded.root.status == TaskStatus.READY
    assert loaded.root.error == "recovered_after_interrupted_leaf_execution"
    assert loaded.root.attempts == 1


def test_integration_verifier_can_fail_completed_children() -> None:
    graph = TaskGraph.create("Integrate")
    decomposer = ScriptedTaskDecomposer(
        {"root": [ChildTaskSpec(key="leaf", goal="Complete leaf")]}
    )
    result = TaskScheduler(
        decomposer=decomposer,
        capability_resolver=InMemoryCapabilityResolver(),
        leaf_executor=FunctionLeafExecutor(
            lambda node, task_graph: LeafExecutionResult(
                status="completed",
                summary="leaf done",
            )
        ),
        integration_verifier=FunctionIntegrationVerifier(
            lambda node, task_graph: LeafExecutionResult(
                status="failed",
                summary="integration check failed",
                error="parent criteria not satisfied",
            )
        ),
    ).run(graph)

    assert result.nodes["root.leaf"].status == TaskStatus.COMPLETED
    assert result.root.status == TaskStatus.FAILED
    assert result.root.error == "parent criteria not satisfied"


def test_decomposer_exception_becomes_persisted_task_failure() -> None:
    graph = TaskGraph.create("Fail decomposition")

    class BrokenDecomposer:
        def assess(self, node, task_graph):
            raise RuntimeError("cannot decompose")

    result = TaskScheduler(
        decomposer=BrokenDecomposer(),
        capability_resolver=InMemoryCapabilityResolver(),
        leaf_executor=FunctionLeafExecutor(
            lambda node, task_graph: LeafExecutionResult(
                status="completed",
                summary="unused",
            )
        ),
        integration_verifier=FunctionIntegrationVerifier(),
    ).run(graph)

    assert result.root.status == TaskStatus.FAILED
    assert result.root.error == "decomposer_error:RuntimeError:cannot decompose"


def test_integration_exception_becomes_parent_failure() -> None:
    graph = TaskGraph.create("Integrate")

    def broken_integration(node, task_graph):
        raise RuntimeError("integration unavailable")

    result = TaskScheduler(
        decomposer=ScriptedTaskDecomposer(
            {"root": [ChildTaskSpec(key="leaf", goal="Complete leaf")]}
        ),
        capability_resolver=InMemoryCapabilityResolver(),
        leaf_executor=FunctionLeafExecutor(
            lambda node, task_graph: LeafExecutionResult(
                status="completed",
                summary="leaf done",
            )
        ),
        integration_verifier=FunctionIntegrationVerifier(broken_integration),
    ).run(graph)

    assert result.root.status == TaskStatus.FAILED
    assert result.root.error == "RuntimeError: integration unavailable"


def test_cyclic_decomposition_is_rejected_before_children_are_added() -> None:
    graph = TaskGraph.create("Reject cycle")
    result = TaskScheduler(
        decomposer=ScriptedTaskDecomposer(
            {
                "root": [
                    ChildTaskSpec(key="one", goal="One", depends_on=["two"]),
                    ChildTaskSpec(key="two", goal="Two", depends_on=["one"]),
                ]
            }
        ),
        capability_resolver=InMemoryCapabilityResolver(),
        leaf_executor=FunctionLeafExecutor(
            lambda node, task_graph: LeafExecutionResult(
                status="completed",
                summary="unused",
            )
        ),
        integration_verifier=FunctionIntegrationVerifier(),
    ).run(graph)

    assert result.root.status == TaskStatus.FAILED
    assert result.root.error == (
        "decomposition_contract_error:child task dependencies contain a cycle"
    )
    assert list(result.nodes) == ["root"]
    assert result.root.children == []


@pytest.mark.parametrize(
    ("decision", "expected_error"),
    [
        (
            AtomicityDecision(
                is_atomic=True,
                reason="contradictory",
                children=[ChildTaskSpec(key="child", goal="Child")],
            ),
            "atomic_task_has_children",
        ),
        (
            AtomicityDecision(
                is_atomic=False,
                reason="contradictory",
                leaf=AtomicLeafSpec(
                    goal="Leaf",
                    success_criteria=["Done"],
                    required_capabilities=["read"],
                ),
            ),
            "non_atomic_task_has_leaf_contract",
        ),
    ],
)
def test_scheduler_rejects_contradictory_atomicity_contracts(
    decision,
    expected_error,
) -> None:
    class ContradictoryDecomposer:
        def assess(self, node, task_graph):
            return decision

    result = TaskScheduler(
        decomposer=ContradictoryDecomposer(),
        capability_resolver=InMemoryCapabilityResolver({"read"}),
        leaf_executor=FunctionLeafExecutor(
            lambda node, task_graph: LeafExecutionResult(
                status="completed",
                summary="must not execute",
            )
        ),
        integration_verifier=FunctionIntegrationVerifier(),
    ).run(TaskGraph.create("Reject contradiction"))

    assert result.root.status == TaskStatus.FAILED
    assert result.root.error == expected_error
    assert result.root.attempts == 0
