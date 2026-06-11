"""A small deterministic loop proving the engine end to end."""

from __future__ import annotations

from typing import Any

from .adapters import CriteriaJudge, FunctionPlanner, FunctionVerifier, ToolRegistryExecutor
from .checkpoint import JsonCheckpointStore
from .engine import LoopEngine
from .models import Action, LoopBudget, LoopDefinition, LoopState, Plan, VerificationResult


def build_counter_demo(checkpoint_root: str | None = None) -> tuple[LoopEngine, LoopDefinition]:
    executor = ToolRegistryExecutor()

    def increment(arguments: dict[str, Any], state: LoopState) -> dict[str, Any]:
        current = 0
        if state.action_results:
            current = int(state.action_results[-1].output.get("value", 0))
        return {"value": current + int(arguments.get("amount", 1))}

    executor.register("increment", increment)

    def plan(state: LoopState) -> Plan:
        return Plan(
            actions=[Action(tool="increment", arguments={"amount": 1}, reason="move toward target")],
            rationale="increment once and verify",
            expected_evidence=["counter value"],
        )

    def verify(state: LoopState, results) -> VerificationResult:
        value = int(results[-1].output.get("value", 0)) if results else 0
        target = int(state.definition.metadata.get("target", 3))
        return VerificationResult(
            status="passed" if value >= target else "failed",
            passed=[f"value={value}"] if value >= target else [],
            failed=[] if value >= target else [f"value {value} is below target {target}"],
            evidence={"value": value, "target": target},
        )

    store = JsonCheckpointStore(checkpoint_root) if checkpoint_root else None
    engine = LoopEngine(
        planner=FunctionPlanner(plan),
        executor=executor,
        verifier=FunctionVerifier(verify),
        judge=CriteriaJudge(),
        checkpoint_store=store,
    )
    definition = LoopDefinition(
        goal="Reach the requested counter value",
        success_criteria=["counter value is at least target"],
        constraints=["one increment per iteration"],
        budget=LoopBudget(max_iterations=6, max_actions=6, timeout_seconds=30),
        metadata={"target": 3},
    )
    return engine, definition
