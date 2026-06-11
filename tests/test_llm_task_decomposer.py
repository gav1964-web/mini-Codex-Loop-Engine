from __future__ import annotations

import json

import pytest

from loop_engine.tasks import (
    DecompositionContractError,
    FunctionIntegrationVerifier,
    FunctionLeafExecutor,
    InMemoryCapabilityResolver,
    LeafExecutionResult,
    TaskGraph,
    TaskScheduler,
    TaskStatus,
    ValidatedLLMTaskDecomposer,
)


class SequenceClient:
    def __init__(self, responses):
        self.responses = list(responses)
        self.messages = []

    def complete_json(self, messages):
        self.messages.append(messages)
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response


def _atomic_payload(*, goal: str = "Inspect one file") -> dict:
    return {
        "decision": "atomic",
        "reason": "one bounded observable operation",
        "leaf": {
            "goal": goal,
            "success_criteria": ["The file content is returned as evidence"],
            "required_capabilities": ["filesystem.read"],
            "metadata": {"path": "target.py"},
        },
    }


def _completed_leaf(node, graph) -> LeafExecutionResult:
    return LeafExecutionResult(
        status="completed",
        summary=f"{node.id} completed",
        evidence={"goal": node.goal},
    )


def test_atomic_llm_decision_applies_validated_leaf_contract() -> None:
    client = SequenceClient([_atomic_payload()])
    graph = TaskGraph.create("Understand target.py")
    result = TaskScheduler(
        decomposer=ValidatedLLMTaskDecomposer(
            client,
            available_capabilities={"filesystem.read"},
        ),
        capability_resolver=InMemoryCapabilityResolver({"filesystem.read"}),
        leaf_executor=FunctionLeafExecutor(_completed_leaf),
        integration_verifier=FunctionIntegrationVerifier(),
    ).run(graph)

    assert result.root.status == TaskStatus.COMPLETED
    assert result.root.goal == "Inspect one file"
    assert result.root.success_criteria == [
        "The file content is returned as evidence"
    ]
    assert result.root.required_capabilities == ["filesystem.read"]
    assert result.root.metadata["path"] == "target.py"
    assert any(
        event.event_type == "atomic_leaf_contract_applied"
        for event in result.events
    )
    context = json.loads(client.messages[0][1]["content"])
    assert context["available_capabilities"] == ["filesystem.read"]


@pytest.mark.parametrize("wrapper", ["response", "atomic"])
def test_known_single_object_wrappers_are_contract_compatible(wrapper) -> None:
    graph = TaskGraph.create("Inspect")
    decision = ValidatedLLMTaskDecomposer(
        SequenceClient([{wrapper: _atomic_payload()}])
    ).assess(graph.root, graph)

    assert decision.is_atomic is True
    assert decision.leaf is not None


def test_llm_decomposition_executes_dependency_ordered_children() -> None:
    client = SequenceClient(
        [
            {
                "decision": "decompose",
                "reason": "inspection must precede repair",
                "children": [
                    {
                        "key": "inspect",
                        "goal": "Inspect target",
                        "success_criteria": [],
                        "required_capabilities": [],
                        "depends_on": [],
                        "metadata": {},
                    },
                    {
                        "key": "repair",
                        "goal": "Repair target",
                        "success_criteria": [],
                        "required_capabilities": [],
                        "depends_on": ["inspect"],
                        "metadata": {},
                    },
                ],
            },
            _atomic_payload(goal="Read target.py"),
            {
                **_atomic_payload(goal="Apply one bounded repair"),
                "leaf": {
                    "goal": "Apply one bounded repair",
                    "success_criteria": ["Verification passes"],
                    "required_capabilities": ["filesystem.patch"],
                    "metadata": {},
                },
            },
        ]
    )
    execution_order: list[str] = []

    def execute(node, graph):
        execution_order.append(node.id)
        return _completed_leaf(node, graph)

    result = TaskScheduler(
        decomposer=ValidatedLLMTaskDecomposer(client),
        capability_resolver=InMemoryCapabilityResolver(
            {"filesystem.read", "filesystem.patch"}
        ),
        leaf_executor=FunctionLeafExecutor(execute),
        integration_verifier=FunctionIntegrationVerifier(),
    ).run(TaskGraph.create("Repair target"))

    assert result.root.status == TaskStatus.COMPLETED
    assert execution_order == ["root.inspect", "root.repair"]
    assert result.nodes["root.repair"].dependencies == ["root.inspect"]


def test_invalid_response_is_repaired_once() -> None:
    client = SequenceClient(
        [
            {"decision": "atomic", "reason": "missing leaf"},
            _atomic_payload(),
        ]
    )
    decision = ValidatedLLMTaskDecomposer(client).assess(
        TaskGraph.create("Inspect").root,
        TaskGraph.create("Inspect"),
    )

    assert decision.is_atomic is True
    assert decision.leaf is not None
    assert len(client.messages) == 2
    repair_request = json.loads(client.messages[1][1]["content"])
    assert "missing decomposition fields" in repair_request["validation_error"]
    assert repair_request["limits"]["repair_attempts_remaining"] == 0


def test_repair_prompt_requires_reason_and_complete_selected_shape() -> None:
    client = SequenceClient(
        [
            {
                "decision": "decompose",
                "children": [],
            },
            _atomic_payload(),
        ]
    )
    graph = TaskGraph.create("Inspect")

    decision = ValidatedLLMTaskDecomposer(client).assess(graph.root, graph)

    assert decision.is_atomic is True
    repair_request = json.loads(client.messages[1][1]["content"])
    assert repair_request["required_shape"]["atomic"]["reason"].startswith(
        "required"
    )
    assert "reason" in repair_request["required_shape"]["decompose"]
    assert "including reason" in client.messages[1][0]["content"]


def test_repeated_invalid_response_fails_without_graph_mutation() -> None:
    client = SequenceClient(
        [
            {
                "decision": "decompose",
                "reason": "bad dependencies",
                "children": [
                    {
                        "key": "one",
                        "goal": "One",
                        "success_criteria": [],
                        "required_capabilities": [],
                        "depends_on": ["two"],
                        "metadata": {},
                    },
                    {
                        "key": "two",
                        "goal": "Two",
                        "success_criteria": [],
                        "required_capabilities": [],
                        "depends_on": ["one"],
                        "metadata": {},
                    },
                ],
            },
            {"decision": "atomic", "reason": "still missing leaf"},
        ]
    )
    graph = TaskGraph.create("Reject invalid decomposition")
    result = TaskScheduler(
        decomposer=ValidatedLLMTaskDecomposer(client),
        capability_resolver=InMemoryCapabilityResolver(),
        leaf_executor=FunctionLeafExecutor(_completed_leaf),
        integration_verifier=FunctionIntegrationVerifier(),
    ).run(graph)

    assert result.root.status == TaskStatus.FAILED
    assert result.root.error.startswith(
        "decomposer_error:DecompositionContractError:"
    )
    assert list(result.nodes) == ["root"]
    assert result.root.children == []


def test_transport_error_is_not_repaired() -> None:
    client = SequenceClient([RuntimeError("gateway unavailable")])
    graph = TaskGraph.create("Inspect")

    with pytest.raises(RuntimeError, match="gateway unavailable"):
        ValidatedLLMTaskDecomposer(client).assess(graph.root, graph)

    assert len(client.messages) == 1


def test_contract_repair_can_be_disabled() -> None:
    client = SequenceClient([{"decision": "atomic"}])
    graph = TaskGraph.create("Inspect")

    with pytest.raises(ValueError, match="missing decomposition fields"):
        ValidatedLLMTaskDecomposer(
            client,
            contract_repair_attempts=0,
        ).assess(graph.root, graph)

    assert len(client.messages) == 1


def test_atomic_contract_rejects_empty_criteria_and_capabilities() -> None:
    payload = _atomic_payload()
    payload["leaf"]["success_criteria"] = []
    payload["leaf"]["required_capabilities"] = []
    graph = TaskGraph.create("Inspect")

    with pytest.raises(DecompositionContractError, match="must be non-empty"):
        ValidatedLLMTaskDecomposer(
            SequenceClient([payload, payload]),
        ).assess(graph.root, graph)


def test_context_limit_fails_before_calling_llm() -> None:
    client = SequenceClient([_atomic_payload()])
    graph = TaskGraph.create("x" * 500)

    with pytest.raises(ValueError, match="context exceeds"):
        ValidatedLLMTaskDecomposer(
            client,
            max_context_chars=100,
            contract_repair_attempts=0,
        ).assess(graph.root, graph)

    assert client.messages == []
