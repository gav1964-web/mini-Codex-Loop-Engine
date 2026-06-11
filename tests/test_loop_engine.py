from __future__ import annotations

import json

from loop_engine import (
    Action,
    Decision,
    Judgement,
    LoopBudget,
    LoopDefinition,
    LoopEngine,
    LoopStatus,
    Plan,
    VerificationResult,
)
from loop_engine.adapters import FunctionPlanner, FunctionVerifier, ToolRegistryExecutor
from loop_engine.demo import build_counter_demo


def test_counter_loop_completes_and_records_events(tmp_path) -> None:
    engine, definition = build_counter_demo(tmp_path)
    state = engine.run(definition, run_id="counter")

    assert state.status == LoopStatus.COMPLETED
    assert state.iteration == 3
    assert state.action_count == 3
    assert state.latest_verification.evidence["value"] == 3
    assert state.events[-1].event_type == "loop_completed"

    checkpoint = json.loads((tmp_path / "counter.json").read_text(encoding="utf-8"))
    assert checkpoint["schema_version"] == 1
    assert checkpoint["state"]["status"] == "completed"


def test_repeated_observation_stops_stagnant_loop() -> None:
    executor = ToolRegistryExecutor()
    executor.register("noop", lambda arguments, state: {"value": "unchanged"})
    planner = FunctionPlanner(lambda state: Plan(actions=[Action(tool="noop")]))
    verifier = FunctionVerifier(
        lambda state, results: VerificationResult(
            status="failed", failed=["same failure"], evidence={"signature": "same"}
        )
    )

    class ContinueJudge:
        def judge(self, state, verification):
            return Judgement(decision=Decision.CONTINUE, reason="try again")

    state = LoopEngine(
        planner=planner,
        executor=executor,
        verifier=verifier,
        judge=ContinueJudge(),
    ).run(
        LoopDefinition(
            goal="Demonstrate stagnation detection",
            budget=LoopBudget(max_iterations=10, max_repeated_observations=2),
        )
    )

    assert state.status == LoopStatus.STOPPED
    assert state.iteration == 2
    assert state.stop_reason == "repeated_observation_stagnation"


def test_action_budget_stops_before_extra_action() -> None:
    executor = ToolRegistryExecutor()
    executor.register("noop", lambda arguments, state: {"ok": True})
    planner = FunctionPlanner(lambda state: Plan(actions=[Action(tool="noop"), Action(tool="noop")]))
    verifier = FunctionVerifier(
        lambda state, results: VerificationResult(status="failed", failed=["not done"])
    )

    class ContinueJudge:
        def judge(self, state, verification):
            return Judgement(decision=Decision.CONTINUE, reason="continue")

    state = LoopEngine(
        planner=planner,
        executor=executor,
        verifier=verifier,
        judge=ContinueJudge(),
    ).run(
        LoopDefinition(
            goal="Respect action budget",
            budget=LoopBudget(max_iterations=5, max_actions=1),
        )
    )

    assert state.status == LoopStatus.STOPPED
    assert state.action_count == 1
    assert state.stop_reason == "action_budget_exhausted"


def test_single_iteration_budget_allows_first_iteration_action() -> None:
    executor = ToolRegistryExecutor()
    executor.register("finish", lambda arguments, state: {"done": True})
    planner = FunctionPlanner(lambda state: Plan(actions=[Action(tool="finish")]))
    verifier = FunctionVerifier(
        lambda state, results: VerificationResult(status="passed", passed=["done"])
    )

    class CompleteJudge:
        def judge(self, state, verification):
            return Judgement(decision=Decision.COMPLETE, reason="done")

    state = LoopEngine(
        planner=planner,
        executor=executor,
        verifier=verifier,
        judge=CompleteJudge(),
    ).run(
        LoopDefinition(
            goal="Complete exactly one iteration",
            budget=LoopBudget(max_iterations=1, max_actions=1),
        )
    )

    assert state.status == LoopStatus.COMPLETED
    assert state.iteration == 1
    assert state.action_count == 1


def test_unknown_tool_becomes_structured_action_error() -> None:
    executor = ToolRegistryExecutor()
    planner = FunctionPlanner(lambda state: Plan(actions=[Action(tool="missing")]))
    verifier = FunctionVerifier(
        lambda state, results: VerificationResult(
            status="blocked", failed=[results[0].error or "unknown"]
        )
    )

    class StopJudge:
        def judge(self, state, verification):
            return Judgement(decision=Decision.STOP, reason="blocked")

    state = LoopEngine(
        planner=planner,
        executor=executor,
        verifier=verifier,
        judge=StopJudge(),
    ).run(LoopDefinition(goal="Handle missing tool"))

    assert state.status == LoopStatus.STOPPED
    assert state.action_results[0].status == "error"
    assert "unknown tool" in state.action_results[0].error
