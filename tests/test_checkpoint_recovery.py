from __future__ import annotations

from loop_engine import (
    Action,
    ActionResult,
    Decision,
    Judgement,
    LoopBudget,
    LoopDefinition,
    LoopEngine,
    LoopPhase,
    LoopState,
    LoopStatus,
    Plan,
    VerificationResult,
)
from loop_engine.checkpoint import JsonCheckpointStore
from loop_engine.cli import main
from loop_engine.events import utc_now


class FailingPlanner:
    def plan(self, state):
        raise AssertionError("planner must not run during this recovery phase")


class FailingExecutor:
    def execute(self, action, state):
        raise AssertionError("completed actions must not run again")


class CompleteJudge:
    def judge(self, state, verification):
        return Judgement(decision=Decision.COMPLETE, reason="recovered")


def _definition() -> LoopDefinition:
    return LoopDefinition(
        goal="Recover a durable loop",
        budget=LoopBudget(max_iterations=2, max_actions=3, timeout_seconds=30),
    )


def test_resume_from_verifying_does_not_repeat_completed_action(tmp_path) -> None:
    action = Action(tool="write_once")
    result = ActionResult(action=action, status="ok", output={"written": True})
    state = LoopState(
        run_id="verify-recovery",
        definition=_definition(),
        status=LoopStatus.RUNNING,
        phase=LoopPhase.VERIFYING,
        iteration=1,
        action_count=1,
        started_at=utc_now(),
        current_focus="verify completed write",
        latest_plan=Plan(actions=[action]),
        next_action_index=1,
        iteration_results=[result],
        action_results=[result],
    )
    store = JsonCheckpointStore(tmp_path)
    store.save(state)
    loaded = store.load(state.run_id)
    calls = {"verifier": 0}

    class Verifier:
        def verify(self, resumed_state, results):
            calls["verifier"] += 1
            assert len(results) == 1
            assert results[0].output["written"] is True
            return VerificationResult(status="passed", passed=["write exists"])

    recovered = LoopEngine(
        planner=FailingPlanner(),
        executor=FailingExecutor(),
        verifier=Verifier(),
        judge=CompleteJudge(),
        checkpoint_store=store,
    ).resume(loaded)

    assert recovered.status == LoopStatus.COMPLETED
    assert recovered.phase == LoopPhase.TERMINAL
    assert recovered.action_count == 1
    assert calls["verifier"] == 1
    assert any(event.event_type == "loop_resumed" for event in recovered.events)


def test_resume_from_executing_runs_only_remaining_actions(tmp_path) -> None:
    first = Action(tool="first")
    second = Action(tool="second")
    first_result = ActionResult(action=first, status="ok", output={"step": 1})
    state = LoopState(
        run_id="action-recovery",
        definition=_definition(),
        status=LoopStatus.RUNNING,
        phase=LoopPhase.EXECUTING,
        iteration=1,
        action_count=1,
        started_at=utc_now(),
        current_focus="finish remaining action",
        latest_plan=Plan(actions=[first, second]),
        next_action_index=1,
        iteration_results=[first_result],
        action_results=[first_result],
    )
    store = JsonCheckpointStore(tmp_path)
    store.save(state)
    executed: list[str] = []

    class Executor:
        def execute(self, action, resumed_state):
            executed.append(action.tool)
            return ActionResult(action=action, status="ok", output={"step": 2})

    class Verifier:
        def verify(self, resumed_state, results):
            assert [result.output["step"] for result in results] == [1, 2]
            return VerificationResult(status="passed", passed=["both steps"])

    recovered = LoopEngine(
        planner=FailingPlanner(),
        executor=Executor(),
        verifier=Verifier(),
        judge=CompleteJudge(),
        checkpoint_store=store,
    ).resume(store.load(state.run_id))

    assert recovered.status == LoopStatus.COMPLETED
    assert recovered.action_count == 2
    assert executed == ["second"]


def test_resume_from_judging_does_not_repeat_verification(tmp_path) -> None:
    verification = VerificationResult(status="passed", passed=["evidence retained"])
    state = LoopState(
        run_id="judge-recovery",
        definition=_definition(),
        status=LoopStatus.RUNNING,
        phase=LoopPhase.JUDGING,
        iteration=1,
        started_at=utc_now(),
        current_focus="judge retained evidence",
        latest_verification=verification,
        observation_signatures=["retained-signature"],
    )
    store = JsonCheckpointStore(tmp_path)
    store.save(state)

    class FailingVerifier:
        def verify(self, resumed_state, results):
            raise AssertionError("verification must not run again")

    recovered = LoopEngine(
        planner=FailingPlanner(),
        executor=FailingExecutor(),
        verifier=FailingVerifier(),
        judge=CompleteJudge(),
        checkpoint_store=store,
    ).resume(store.load(state.run_id))

    assert recovered.status == LoopStatus.COMPLETED
    assert recovered.latest_verification.passed == ["evidence retained"]


def test_legacy_running_checkpoint_loads_at_safe_iteration_boundary(tmp_path) -> None:
    state = LoopState(
        run_id="legacy",
        definition=_definition(),
        status=LoopStatus.RUNNING,
        iteration=1,
        started_at=utc_now(),
        current_focus="legacy state",
        latest_plan=Plan(actions=[Action(tool="possibly_started")]),
    )
    target = tmp_path / "legacy.json"
    import json

    legacy = state.to_dict()
    legacy.pop("phase")
    legacy.pop("next_action_index")
    legacy.pop("iteration_results")
    target.write_text(json.dumps(legacy, default=str), encoding="utf-8")

    loaded = JsonCheckpointStore(tmp_path).load("legacy")

    assert loaded.phase == LoopPhase.READY
    assert loaded.latest_plan is None
    assert loaded.iteration == 1


def test_cli_resumes_demo_from_checkpoint(tmp_path, capsys) -> None:
    action = Action(tool="increment", arguments={"amount": 1})
    result = ActionResult(action=action, status="ok", output={"value": 1})
    state = LoopState(
        run_id="cli-recovery",
        definition=LoopDefinition(
            goal="Reach the requested counter value",
            budget=LoopBudget(max_iterations=3, max_actions=3),
            metadata={"target": 1},
        ),
        status=LoopStatus.RUNNING,
        phase=LoopPhase.VERIFYING,
        iteration=1,
        action_count=1,
        started_at=utc_now(),
        current_focus="verify counter",
        latest_plan=Plan(actions=[action]),
        next_action_index=1,
        iteration_results=[result],
        action_results=[result],
    )
    JsonCheckpointStore(tmp_path).save(state)

    exit_code = main(
        [
            "demo",
            "--checkpoints",
            str(tmp_path),
            "--resume",
            state.run_id,
        ]
    )

    import json

    output = json.loads(capsys.readouterr().out)
    assert exit_code == 0
    assert output["status"] == "completed"
    assert output["action_count"] == 1


def test_checkpoint_run_id_rejects_path_traversal(tmp_path) -> None:
    store = JsonCheckpointStore(tmp_path)

    import pytest

    with pytest.raises(ValueError, match="run_id"):
        store.load("../outside")
