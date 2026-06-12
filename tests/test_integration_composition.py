from __future__ import annotations

import pytest

from loop_engine.tasks import (
    ChildTaskSpec,
    CompositeIntegrationVerifier,
    FunctionIntegrationVerifier,
    FunctionLeafExecutor,
    InMemoryCapabilityResolver,
    IntegrationCompositionPolicy,
    IntegrationPlan,
    LeafExecutionResult,
    ScriptedTaskDecomposer,
    TaskGraph,
    TaskScheduler,
    TaskStatus,
)


def _leaf(node, graph) -> LeafExecutionResult:
    return LeafExecutionResult(
        status="completed",
        summary=f"{node.id} completed",
        evidence={"node": node.id},
    )


def _decomposer() -> ScriptedTaskDecomposer:
    return ScriptedTaskDecomposer(
        {
            "root": [
                ChildTaskSpec(key="one", goal="One"),
                ChildTaskSpec(key="two", goal="Two"),
            ]
        }
    )


def _run(verifier, *, graph=None):
    return TaskScheduler(
        decomposer=_decomposer(),
        capability_resolver=InMemoryCapabilityResolver(),
        leaf_executor=FunctionLeafExecutor(_leaf),
        integration_verifier=verifier,
    ).run(graph or TaskGraph.create("Composite integration"))


def _result(status: str, name: str) -> LeafExecutionResult:
    return LeafExecutionResult(
        status=status,
        summary=f"{name} {status}",
        error=None if status == "completed" else f"{name}_error",
        evidence={"check": name},
    )


def test_route_runs_all_required_checks_in_policy_order() -> None:
    calls: list[str] = []

    def check(name):
        return FunctionIntegrationVerifier(
            lambda node, graph: (
                calls.append(name) or _result("completed", name)
            )
        )

    verifier = CompositeIntegrationVerifier(
        {"tests": check("tests"), "schema": check("schema")},
        IntegrationCompositionPolicy.create(
            routes={"root": IntegrationPlan.create(["schema", "tests"])}
        ),
    )

    result = _run(verifier)

    assert result.root.status == TaskStatus.COMPLETED
    assert calls == ["schema", "tests"]
    assert result.root.result.evidence["integration_plan"] == [
        "schema",
        "tests",
    ]
    assert list(result.root.result.evidence["integration_checks"]) == [
        "schema",
        "tests",
    ]


def test_failed_check_has_precedence_but_all_checks_still_run() -> None:
    calls: list[str] = []
    verifier = CompositeIntegrationVerifier(
        {
            "blocked": FunctionIntegrationVerifier(
                lambda node, graph: (
                    calls.append("blocked") or _result("blocked", "blocked")
                )
            ),
            "failed": FunctionIntegrationVerifier(
                lambda node, graph: (
                    calls.append("failed") or _result("failed", "failed")
                )
            ),
            "passed": FunctionIntegrationVerifier(
                lambda node, graph: (
                    calls.append("passed") or _result("completed", "passed")
                )
            ),
        },
        IntegrationCompositionPolicy.create(
            default_plan=IntegrationPlan.create(
                ["blocked", "failed", "passed"]
            )
        ),
    )

    result = _run(verifier)

    assert calls == ["blocked", "failed", "passed"]
    assert result.root.status == TaskStatus.FAILED
    assert result.root.error == "integration_check_failed:failed:failed_error"
    assert set(result.root.result.evidence["integration_checks"]) == {
        "blocked",
        "failed",
        "passed",
    }


def test_blocked_check_blocks_when_no_check_failed() -> None:
    verifier = CompositeIntegrationVerifier(
        {
            "passed": FunctionIntegrationVerifier(
                lambda node, graph: _result("completed", "passed")
            ),
            "waiting": FunctionIntegrationVerifier(
                lambda node, graph: _result("blocked", "waiting")
            ),
        },
        IntegrationCompositionPolicy.create(
            default_plan=IntegrationPlan.create(["passed", "waiting"])
        ),
    )

    result = _run(verifier)

    assert result.root.status == TaskStatus.BLOCKED
    assert result.root.error == (
        "integration_check_blocked:waiting:waiting_error"
    )


def test_exact_route_overrides_default_plan() -> None:
    verifier = CompositeIntegrationVerifier(
        {
            "exact": FunctionIntegrationVerifier(
                lambda node, graph: _result("completed", "exact")
            ),
            "default": FunctionIntegrationVerifier(
                lambda node, graph: _result("failed", "default")
            ),
        },
        IntegrationCompositionPolicy.create(
            routes={"root": IntegrationPlan.create(["exact"])},
            default_plan=IntegrationPlan.create(["default"]),
        ),
    )

    result = _run(verifier)

    assert result.root.status == TaskStatus.COMPLETED
    assert result.root.result.evidence["integration_route"] == "root"
    assert result.root.result.evidence["integration_plan"] == ["exact"]


def test_metadata_cannot_override_route_or_plan() -> None:
    graph = TaskGraph.create("Composite integration")
    graph.root.metadata = {
        "integration_route": "unsafe",
        "integration_plan": ["unsafe"],
    }
    verifier = CompositeIntegrationVerifier(
        {
            "safe": FunctionIntegrationVerifier(
                lambda node, task_graph: _result("completed", "safe")
            ),
            "unsafe": FunctionIntegrationVerifier(
                lambda node, task_graph: _result("failed", "unsafe")
            ),
        },
        IntegrationCompositionPolicy.create(
            routes={"root": IntegrationPlan.create(["safe"])}
        ),
    )

    result = _run(verifier, graph=graph)

    assert result.root.status == TaskStatus.COMPLETED
    assert result.root.result.evidence["integration_plan"] == ["safe"]


def test_each_verifier_receives_an_independent_snapshot() -> None:
    observed: list[str] = []

    def mutate(node, graph):
        node.goal = "mutated"
        graph.stop_reason = "mutated"
        return _result("completed", "mutate")

    def observe(node, graph):
        observed.extend([node.goal, str(graph.stop_reason)])
        return _result("completed", "observe")

    verifier = CompositeIntegrationVerifier(
        {
            "mutate": FunctionIntegrationVerifier(mutate),
            "observe": FunctionIntegrationVerifier(observe),
        },
        IntegrationCompositionPolicy.create(
            default_plan=IntegrationPlan.create(["mutate", "observe"])
        ),
    )

    result = _run(verifier)

    assert result.root.status == TaskStatus.COMPLETED
    assert observed == ["Composite integration", "None"]
    assert result.root.goal == "Composite integration"


def test_verifier_exception_becomes_failed_check() -> None:
    def broken(node, graph):
        raise RuntimeError("check unavailable")

    verifier = CompositeIntegrationVerifier(
        {"broken": FunctionIntegrationVerifier(broken)},
        IntegrationCompositionPolicy.create(
            default_plan=IntegrationPlan.create(["broken"])
        ),
    )

    result = _run(verifier)

    assert result.root.status == TaskStatus.FAILED
    assert result.root.error == (
        "integration_check_failed:broken:RuntimeError: check unavailable"
    )


def test_missing_route_blocks_parent() -> None:
    verifier = CompositeIntegrationVerifier(
        {
            "check": FunctionIntegrationVerifier(
                lambda node, graph: _result("completed", "check")
            )
        },
        IntegrationCompositionPolicy.create(
            routes={"another.parent": IntegrationPlan.create(["check"])}
        ),
    )

    result = _run(verifier)

    assert result.root.status == TaskStatus.BLOCKED
    assert result.root.error == "integration_route_missing:root"


def test_policy_rejects_unknown_or_duplicate_checks() -> None:
    try:
        IntegrationPlan.create(["same", "same"])
    except ValueError as exc:
        assert "must be unique" in str(exc)
    else:
        raise AssertionError("duplicate checks must be rejected")

    try:
        CompositeIntegrationVerifier(
            {
                "known": FunctionIntegrationVerifier(
                    lambda node, graph: _result("completed", "known")
                )
            },
            IntegrationCompositionPolicy.create(
                default_plan=IntegrationPlan.create(["missing"])
            ),
        )
    except ValueError as exc:
        assert "unknown integration verifiers" in str(exc)
    else:
        raise AssertionError("unknown checks must be rejected")


def test_unknown_check_status_fails_closed() -> None:
    verifier = CompositeIntegrationVerifier(
        {
            "invalid": FunctionIntegrationVerifier(
                lambda node, graph: LeafExecutionResult(
                    status="maybe",
                    summary="ambiguous",
                )
            )
        },
        IntegrationCompositionPolicy.create(
            default_plan=IntegrationPlan.create(["invalid"])
        ),
    )

    result = _run(verifier)

    assert result.root.status == TaskStatus.FAILED
    assert result.root.error == (
        "integration_check_failed:invalid:"
        "integration_verifier_invalid_status:maybe"
    )


def test_invalid_check_result_fails_closed() -> None:
    verifier = CompositeIntegrationVerifier(
        {
            "invalid": FunctionIntegrationVerifier(
                lambda node, graph: None  # type: ignore[arg-type]
            )
        },
        IntegrationCompositionPolicy.create(
            default_plan=IntegrationPlan.create(["invalid"])
        ),
    )

    result = _run(verifier)

    assert result.root.status == TaskStatus.FAILED
    assert result.root.error == (
        "integration_check_failed:invalid:"
        "integration_verifier_invalid_result:NoneType"
    )


def test_policy_routes_are_immutable_after_creation() -> None:
    source = {"root": IntegrationPlan.create(["contract"])}
    policy = IntegrationCompositionPolicy.create(routes=source)
    source["other"] = IntegrationPlan.create(["contract"])

    assert "other" not in policy.routes
    with pytest.raises(TypeError):
        policy.routes["other"] = source["other"]  # type: ignore[index]
