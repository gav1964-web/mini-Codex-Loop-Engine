"""External routing and all-of composition for parent integration checks."""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
from types import MappingProxyType
from typing import Any, Mapping

from .models import LeafExecutionResult, TaskGraph, TaskNode, TaskStatus
from .ports import IntegrationVerifier
from .integration_selectors import (
    IntegrationSelector,
    IntegrationSelectorExpression,
    IntegrationSelectorGroup,
)


@dataclass(frozen=True, slots=True)
class IntegrationPlan:
    verifier_names: tuple[str, ...]

    @classmethod
    def create(
        cls,
        verifier_names: list[str] | tuple[str, ...],
    ) -> IntegrationPlan:
        names = tuple(str(name).strip() for name in verifier_names)
        if not names or any(not name for name in names):
            raise ValueError("integration plan verifier names must be non-empty")
        if len(names) != len(set(names)):
            raise ValueError("integration plan verifier names must be unique")
        return cls(verifier_names=names)


@dataclass(frozen=True, slots=True)
class IntegrationRoute:
    name: str
    selector: IntegrationSelectorExpression
    plan: IntegrationPlan

    def __post_init__(self) -> None:
        if not isinstance(self.name, str):
            raise TypeError("integration selector route name must be a string")
        name = self.name.strip()
        if not name:
            raise ValueError("integration selector route name is required")
        if not isinstance(
            self.selector,
            (IntegrationSelector, IntegrationSelectorGroup),
        ):
            raise TypeError(
                "integration route selector must be a typed selector"
            )
        if not isinstance(self.plan, IntegrationPlan):
            raise TypeError("integration route plan must be IntegrationPlan")
        object.__setattr__(self, "name", name)


@dataclass(frozen=True, slots=True)
class IntegrationCompositionPolicy:
    routes: Mapping[str, IntegrationPlan]
    selector_routes: tuple[IntegrationRoute, ...] = ()
    default_plan: IntegrationPlan | None = None

    def __post_init__(self) -> None:
        normalized: dict[str, IntegrationPlan] = {}
        for node_id, plan in self.routes.items():
            key = node_id.strip()
            if not key:
                raise ValueError("integration route node id is required")
            if key in normalized:
                raise ValueError(f"duplicate integration route node id: {key}")
            if not isinstance(plan, IntegrationPlan):
                raise TypeError("integration routes must contain IntegrationPlan")
            normalized[key] = plan
        selector_routes = tuple(self.selector_routes)
        if any(not isinstance(route, IntegrationRoute) for route in selector_routes):
            raise TypeError("selector routes must contain IntegrationRoute")
        names = [route.name for route in selector_routes]
        if len(names) != len(set(names)):
            raise ValueError("integration selector route names must be unique")
        if self.default_plan is not None and not isinstance(
            self.default_plan,
            IntegrationPlan,
        ):
            raise TypeError("default integration plan must be IntegrationPlan")
        if not normalized and not selector_routes and self.default_plan is None:
            raise ValueError(
                "at least one integration route, selector route, or default plan "
                "is required"
            )
        object.__setattr__(self, "routes", MappingProxyType(normalized))
        object.__setattr__(self, "selector_routes", selector_routes)

    @classmethod
    def create(
        cls,
        *,
        routes: dict[str, IntegrationPlan] | None = None,
        selector_routes: list[IntegrationRoute] | tuple[IntegrationRoute, ...] = (),
        default_plan: IntegrationPlan | None = None,
    ) -> IntegrationCompositionPolicy:
        return cls(
            routes=routes or {},
            selector_routes=tuple(selector_routes),
            default_plan=default_plan,
        )

    def resolve(
        self,
        node: TaskNode,
    ) -> tuple[str, IntegrationPlan, IntegrationSelectorExpression | None] | None:
        exact = self.routes.get(node.id)
        if exact is not None:
            return node.id, exact, None
        for route in self.selector_routes:
            if route.selector.matches(node):
                return f"selector:{route.name}", route.plan, route.selector
        if self.default_plan is not None:
            return "default", self.default_plan, None
        return None


class CompositeIntegrationVerifier:
    """Route a parent to an ordered all-of set of independent verifiers."""

    def __init__(
        self,
        verifiers: dict[str, IntegrationVerifier],
        policy: IntegrationCompositionPolicy,
    ) -> None:
        normalized: dict[str, IntegrationVerifier] = {}
        for name, verifier in verifiers.items():
            key = name.strip()
            if not key:
                raise ValueError("integration verifier name is required")
            if key in normalized:
                raise ValueError(f"duplicate integration verifier name: {key}")
            normalized[key] = verifier
        if not normalized:
            raise ValueError("integration verifier registry must be non-empty")
        referenced = {
            name
            for plan in [
                *policy.routes.values(),
                *(route.plan for route in policy.selector_routes),
                policy.default_plan,
            ]
            if plan is not None
            for name in plan.verifier_names
        }
        unknown = sorted(referenced - set(normalized))
        if unknown:
            raise ValueError(f"unknown integration verifiers: {unknown}")
        self.verifiers = MappingProxyType(normalized)
        self.policy = policy

    def verify(self, node: TaskNode, graph: TaskGraph) -> LeafExecutionResult:
        child_error = _validate_children(node, graph)
        if child_error is not None:
            return LeafExecutionResult(
                status="blocked",
                summary="composite integration is not ready",
                error=child_error,
                evidence=_child_evidence(node, graph),
            )
        resolution = self.policy.resolve(node)
        if resolution is None:
            return LeafExecutionResult(
                status="blocked",
                summary="composite integration route is not configured",
                error=f"integration_route_missing:{node.id}",
                evidence=_child_evidence(node, graph),
            )
        route_name, plan, selector = resolution

        checks: dict[str, dict[str, Any]] = {}
        results: list[tuple[str, LeafExecutionResult]] = []
        for name in plan.verifier_names:
            try:
                result = self.verifiers[name].verify(
                    deepcopy(node),
                    deepcopy(graph),
                )
            except Exception as exc:
                result = LeafExecutionResult(
                    status="failed",
                    summary="integration verifier raised an exception",
                    error=f"{type(exc).__name__}: {exc}",
                )
            if not isinstance(result, LeafExecutionResult):
                result = LeafExecutionResult(
                    status="failed",
                    summary="integration verifier returned an invalid result",
                    error=f"integration_verifier_invalid_result:{type(result).__name__}",
                )
            elif result.status not in {"completed", "blocked", "failed"}:
                result = LeafExecutionResult(
                    status="failed",
                    summary="integration verifier returned an invalid status",
                    error=f"integration_verifier_invalid_status:{result.status}",
                    evidence={"original_evidence": result.evidence},
                )
            results.append((name, result))
            checks[name] = {
                "status": result.status,
                "summary": result.summary,
                "error": result.error,
                "evidence": result.evidence,
            }

        evidence = {
            **_child_evidence(node, graph),
            "integration_route": route_name,
            "integration_selector": (
                selector.to_dict() if selector is not None else None
            ),
            "integration_plan": list(plan.verifier_names),
            "integration_checks": checks,
        }
        failed = next(
            ((name, result) for name, result in results if result.status == "failed"),
            None,
        )
        if failed is not None:
            name, result = failed
            return LeafExecutionResult(
                status="failed",
                summary="one or more integration checks failed",
                error=f"integration_check_failed:{name}:{result.error or result.summary}",
                evidence=evidence,
            )
        blocked = next(
            ((name, result) for name, result in results if result.status == "blocked"),
            None,
        )
        if blocked is not None:
            name, result = blocked
            return LeafExecutionResult(
                status="blocked",
                summary="one or more integration checks are blocked",
                error=f"integration_check_blocked:{name}:{result.error or result.summary}",
                evidence=evidence,
            )
        return LeafExecutionResult(
            status="completed",
            summary="all integration checks passed",
            evidence=evidence,
        )


def _validate_children(node: TaskNode, graph: TaskGraph) -> str | None:
    if not node.children:
        return "integration_parent_has_no_children"
    missing = [child_id for child_id in node.children if child_id not in graph.nodes]
    if missing:
        return f"integration_children_missing:{','.join(sorted(missing))}"
    incomplete = [
        child_id
        for child_id in node.children
        if graph.nodes[child_id].status != TaskStatus.COMPLETED
    ]
    if incomplete:
        return f"integration_children_incomplete:{','.join(sorted(incomplete))}"
    return None


def _child_evidence(node: TaskNode, graph: TaskGraph) -> dict[str, Any]:
    return {
        "children": {
            child_id: (
                graph.nodes[child_id].result.evidence
                if child_id in graph.nodes
                and graph.nodes[child_id].result is not None
                else {}
            )
            for child_id in node.children
        }
    }
