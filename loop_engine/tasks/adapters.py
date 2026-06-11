"""Deterministic adapters for task graph experiments."""

from __future__ import annotations

from collections.abc import Callable

from ..engine import LoopEngine
from ..models import LoopDefinition, LoopStatus
from .models import (
    AtomicityDecision,
    CapabilityResolution,
    ChildTaskSpec,
    LeafExecutionResult,
    TaskGraph,
    TaskNode,
)


class ScriptedTaskDecomposer:
    """Decompose nodes by id or goal; unspecified nodes are atomic leaves."""

    def __init__(
        self,
        decompositions: dict[str, list[ChildTaskSpec]],
    ) -> None:
        self.decompositions = decompositions

    def assess(self, node: TaskNode, graph: TaskGraph) -> AtomicityDecision:
        children = self.decompositions.get(node.id)
        if children is None:
            children = self.decompositions.get(node.goal)
        if children is None:
            return AtomicityDecision(
                is_atomic=True,
                reason="no further scripted decomposition",
            )
        return AtomicityDecision(
            is_atomic=False,
            reason="scripted decomposition is available",
            children=list(children),
        )


class InMemoryCapabilityResolver:
    def __init__(self, available: set[str] | None = None) -> None:
        self.available = set(available or set())

    def resolve(self, node: TaskNode, graph: TaskGraph) -> CapabilityResolution:
        required = set(node.required_capabilities)
        return CapabilityResolution(
            available=sorted(required & self.available),
            missing=sorted(required - self.available),
        )

    def register(self, capability: str) -> None:
        normalized = capability.strip()
        if not normalized:
            raise ValueError("capability name is required")
        self.available.add(normalized)


class FunctionCapabilityAcquirer:
    def __init__(
        self,
        callback: Callable[[str, TaskNode, TaskGraph], bool],
    ) -> None:
        self.callback = callback

    def acquire(self, capability: str, node: TaskNode, graph: TaskGraph) -> bool:
        return bool(self.callback(capability, node, graph))


class FunctionLeafExecutor:
    def __init__(
        self,
        callback: Callable[[TaskNode, TaskGraph], LeafExecutionResult],
    ) -> None:
        self.callback = callback

    def execute(self, node: TaskNode, graph: TaskGraph) -> LeafExecutionResult:
        return self.callback(node, graph)


class FunctionIntegrationVerifier:
    def __init__(
        self,
        callback: Callable[[TaskNode, TaskGraph], LeafExecutionResult] | None = None,
    ) -> None:
        self.callback = callback

    def verify(self, node: TaskNode, graph: TaskGraph) -> LeafExecutionResult:
        if self.callback is not None:
            return self.callback(node, graph)
        return LeafExecutionResult(
            status="completed",
            summary="all child tasks completed",
            evidence={
                "children": {
                    child_id: graph.nodes[child_id].result.evidence
                    if graph.nodes[child_id].result is not None
                    else {}
                    for child_id in node.children
                }
            },
        )


class LoopEngineLeafExecutor:
    """Run one atomic task through a task-specific LoopEngine factory."""

    def __init__(
        self,
        factory: Callable[
            [TaskNode, TaskGraph],
            tuple[LoopEngine, LoopDefinition],
        ],
    ) -> None:
        self.factory = factory

    def execute(self, node: TaskNode, graph: TaskGraph) -> LeafExecutionResult:
        engine, definition = self.factory(node, graph)
        run_id = f"{graph.id}-{node.id}".replace(".", "-")
        state = engine.run(definition, run_id=run_id)
        evidence = {
            "loop_run_id": state.run_id,
            "loop_status": state.status,
            "iterations": state.iteration,
            "actions": state.action_count,
            "verification": (
                state.latest_verification.evidence
                if state.latest_verification is not None
                else {}
            ),
            "stop_reason": state.stop_reason,
        }
        if state.status == LoopStatus.COMPLETED:
            return LeafExecutionResult(
                status="completed",
                summary=state.stop_reason or "leaf loop completed",
                evidence=evidence,
            )
        if state.status == LoopStatus.STOPPED:
            return LeafExecutionResult(
                status="blocked",
                summary=state.stop_reason or "leaf loop stopped",
                evidence=evidence,
                error=state.stop_reason,
            )
        return LeafExecutionResult(
            status="failed",
            summary=state.stop_reason or "leaf loop failed",
            evidence=evidence,
            error=state.stop_reason,
        )
