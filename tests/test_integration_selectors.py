from __future__ import annotations

import pytest

from loop_engine.tasks import (
    CompositeIntegrationVerifier,
    FunctionIntegrationVerifier,
    IntegrationCompositionPolicy,
    IntegrationPlan,
    IntegrationRoute,
    IntegrationSelector,
    IntegrationSelectorGroup,
    LeafExecutionResult,
    TaskGraph,
    TaskNode,
    TaskStatus,
)


def _result(name: str, *, status: str = "completed") -> LeafExecutionResult:
    return LeafExecutionResult(
        status=status,
        summary=f"{name} {status}",
        error=None if status == "completed" else f"{name}_error",
    )


def _completed_parent(
    *,
    node_id: str = "root.feature",
    depth: int = 1,
    capabilities: list[str] | None = None,
    metadata: dict | None = None,
) -> tuple[TaskNode, TaskGraph]:
    root = TaskNode(
        id="root",
        goal="Root",
        status=TaskStatus.WAITING,
        children=[node_id],
    )
    parent = TaskNode(
        id=node_id,
        goal="Feature parent",
        parent_id="root",
        depth=depth,
        status=TaskStatus.WAITING,
        children=[f"{node_id}.leaf"],
        required_capabilities=list(capabilities or []),
        metadata=dict(metadata or {}),
    )
    leaf = TaskNode(
        id=f"{node_id}.leaf",
        goal="Leaf",
        parent_id=node_id,
        depth=depth + 1,
        status=TaskStatus.COMPLETED,
        result=_result("leaf"),
    )
    graph = TaskGraph(
        id="selector-graph",
        root_id="root",
        nodes={"root": root, node_id: parent, leaf.id: leaf},
    )
    return parent, graph


def _verifier(policy: IntegrationCompositionPolicy):
    checks = {
        name: FunctionIntegrationVerifier(
            lambda node, graph, selected=name: _result(selected)
        )
        for name in {"exact", "prefix", "depth", "capability", "default"}
    }
    return CompositeIntegrationVerifier(checks, policy)


def test_exact_route_has_priority_over_selector_routes() -> None:
    parent, graph = _completed_parent()
    policy = IntegrationCompositionPolicy.create(
        routes={"root.feature": IntegrationPlan.create(["exact"])},
        selector_routes=[
            IntegrationRoute(
                "feature-prefix",
                IntegrationSelector.node_id_prefix("root."),
                IntegrationPlan.create(["prefix"]),
            )
        ],
    )

    result = _verifier(policy).verify(parent, graph)

    assert result.status == "completed"
    assert result.evidence["integration_route"] == "root.feature"
    assert result.evidence["integration_plan"] == ["exact"]


def test_first_matching_selector_route_wins() -> None:
    parent, graph = _completed_parent(depth=1, capabilities=["integration.deep"])
    policy = IntegrationCompositionPolicy.create(
        selector_routes=[
            IntegrationRoute(
                "depth-one",
                IntegrationSelector.depth(1),
                IntegrationPlan.create(["depth"]),
            ),
            IntegrationRoute(
                "deep-capability",
                IntegrationSelector.required_capability("integration.deep"),
                IntegrationPlan.create(["capability"]),
            ),
        ],
    )

    result = _verifier(policy).verify(parent, graph)

    assert result.evidence["integration_route"] == "selector:depth-one"
    assert result.evidence["integration_selector"] == {
        "kind": "depth",
        "value": 1,
    }
    assert result.evidence["integration_plan"] == ["depth"]


@pytest.mark.parametrize(
    ("selector", "kwargs", "expected"),
    [
        (
            IntegrationSelector.node_id_prefix("root.feature"),
            {"node_id": "root.feature.api"},
            True,
        ),
        (IntegrationSelector.depth(2), {"depth": 2}, True),
        (
            IntegrationSelector.required_capability("integration.security"),
            {"capabilities": ["integration.security"]},
            True,
        ),
    ],
)
def test_selector_types_match_structural_node_fields(
    selector,
    kwargs,
    expected,
) -> None:
    parent, _ = _completed_parent(**kwargs)

    assert selector.matches(parent) is expected


def test_default_plan_is_used_after_selector_miss() -> None:
    parent, graph = _completed_parent()
    policy = IntegrationCompositionPolicy.create(
        selector_routes=[
            IntegrationRoute(
                "deep-only",
                IntegrationSelector.depth(5),
                IntegrationPlan.create(["depth"]),
            )
        ],
        default_plan=IntegrationPlan.create(["default"]),
    )

    result = _verifier(policy).verify(parent, graph)

    assert result.evidence["integration_route"] == "default"
    assert result.evidence["integration_plan"] == ["default"]


def test_all_group_requires_every_structural_selector() -> None:
    selector = IntegrationSelectorGroup.all_of(
        [
            IntegrationSelector.node_id_prefix("root.feature"),
            IntegrationSelector.depth(1),
            IntegrationSelector.required_capability("integration.deep"),
        ]
    )

    matching, _ = _completed_parent(
        node_id="root.feature.api",
        depth=1,
        capabilities=["integration.deep"],
    )
    missing_capability, _ = _completed_parent(
        node_id="root.feature.api",
        depth=1,
    )

    assert selector.matches(matching) is True
    assert selector.matches(missing_capability) is False


def test_nested_any_group_matches_either_admitted_branch() -> None:
    selector = IntegrationSelectorGroup.all_of(
        [
            IntegrationSelector.depth(1),
            IntegrationSelectorGroup.any_of(
                [
                    IntegrationSelector.node_id_prefix("root.api"),
                    IntegrationSelector.required_capability("integration.deep"),
                ]
            ),
        ]
    )

    prefix_node, _ = _completed_parent(node_id="root.api.users")
    capability_node, _ = _completed_parent(
        node_id="root.worker",
        capabilities=["integration.deep"],
    )
    miss, _ = _completed_parent(node_id="root.worker")

    assert selector.matches(prefix_node) is True
    assert selector.matches(capability_node) is True
    assert selector.matches(miss) is False


def test_compound_selector_route_preserves_first_match_and_evidence() -> None:
    parent, graph = _completed_parent(capabilities=["integration.deep"])
    compound = IntegrationSelectorGroup.all_of(
        [
            IntegrationSelector.depth(1),
            IntegrationSelector.required_capability("integration.deep"),
        ]
    )
    policy = IntegrationCompositionPolicy.create(
        selector_routes=[
            IntegrationRoute(
                "compound",
                compound,
                IntegrationPlan.create(["capability"]),
            ),
            IntegrationRoute(
                "fallback-prefix",
                IntegrationSelector.node_id_prefix("root."),
                IntegrationPlan.create(["prefix"]),
            ),
        ]
    )

    result = _verifier(policy).verify(parent, graph)

    assert result.evidence["integration_route"] == "selector:compound"
    assert result.evidence["integration_plan"] == ["capability"]
    assert result.evidence["integration_selector"] == {
        "operator": "all",
        "selectors": [
            {"kind": "depth", "value": 1},
            {"kind": "required_capability", "value": "integration.deep"},
        ],
    }


def test_metadata_cannot_create_or_override_selector_match() -> None:
    parent, graph = _completed_parent(
        metadata={
            "depth": 9,
            "required_capabilities": ["integration.security"],
            "integration_selector": "security",
        }
    )
    policy = IntegrationCompositionPolicy.create(
        selector_routes=[
            IntegrationRoute(
                "security",
                IntegrationSelector.required_capability("integration.security"),
                IntegrationPlan.create(["capability"]),
            )
        ],
        default_plan=IntegrationPlan.create(["default"]),
    )

    result = _verifier(policy).verify(parent, graph)

    assert result.evidence["integration_route"] == "default"
    assert result.evidence["integration_plan"] == ["default"]


def test_selector_routes_are_immutable_and_copy_input() -> None:
    source = [
        IntegrationRoute(
            "prefix",
            IntegrationSelector.node_id_prefix("root."),
            IntegrationPlan.create(["prefix"]),
        )
    ]
    policy = IntegrationCompositionPolicy.create(selector_routes=source)
    source.clear()

    assert len(policy.selector_routes) == 1
    with pytest.raises(AttributeError):
        policy.selector_routes.append(source)  # type: ignore[attr-defined]


def test_selector_group_copies_input_and_is_immutable() -> None:
    source = [IntegrationSelector.depth(1)]
    selector = IntegrationSelectorGroup.any_of(source)
    source.clear()

    assert selector.selectors == (IntegrationSelector.depth(1),)
    with pytest.raises(AttributeError):
        selector.selectors.append(  # type: ignore[attr-defined]
            IntegrationSelector.depth(2)
        )


def test_selector_and_route_validation_fail_closed() -> None:
    with pytest.raises(ValueError, match="unsupported"):
        IntegrationSelector("metadata", "kind")
    with pytest.raises(ValueError, match="non-negative"):
        IntegrationSelector.depth(-1)
    with pytest.raises(ValueError, match="route names must be unique"):
        route = IntegrationRoute(
            "same",
            IntegrationSelector.depth(1),
            IntegrationPlan.create(["depth"]),
        )
        IntegrationCompositionPolicy.create(selector_routes=[route, route])
    with pytest.raises(TypeError, match="typed selector"):
        IntegrationRoute(
            "invalid",
            "depth:1",  # type: ignore[arg-type]
            IntegrationPlan.create(["depth"]),
        )
    with pytest.raises(TypeError, match="kind must be a string"):
        IntegrationSelector(1, "value")  # type: ignore[arg-type]


def test_selector_group_validation_and_bounds_fail_closed() -> None:
    with pytest.raises(ValueError, match="unsupported"):
        IntegrationSelectorGroup("not", (IntegrationSelector.depth(1),))
    with pytest.raises(ValueError, match="non-empty"):
        IntegrationSelectorGroup.all_of([])
    with pytest.raises(TypeError, match="typed selectors"):
        IntegrationSelectorGroup.any_of(["depth:1"])  # type: ignore[list-item]

    selector = IntegrationSelector.depth(1)
    with pytest.raises(ValueError, match="maximum depth"):
        for _ in range(5):
            selector = IntegrationSelectorGroup.all_of([selector])

    with pytest.raises(ValueError, match="maximum node count"):
        IntegrationSelectorGroup.any_of(
            [IntegrationSelector.depth(index) for index in range(16)]
        )


def test_unknown_verifier_in_selector_plan_is_rejected() -> None:
    policy = IntegrationCompositionPolicy.create(
        selector_routes=[
            IntegrationRoute(
                "unknown",
                IntegrationSelector.depth(1),
                IntegrationPlan.create(["missing"]),
            )
        ]
    )

    with pytest.raises(ValueError, match="unknown integration verifiers"):
        _verifier(policy)
