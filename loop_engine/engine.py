"""Universal deterministic loop orchestrator."""

from __future__ import annotations

from dataclasses import asdict
from datetime import datetime, timezone
from time import perf_counter
from uuid import uuid4

from .events import record_event, utc_now
from .models import (
    ActionResult,
    Decision,
    LoopDefinition,
    LoopPhase,
    LoopState,
    LoopStatus,
)
from .policies import budget_stop_reason, observation_signature, stagnation_stop_reason
from .ports import ActionExecutor, CheckpointStore, Judge, Planner, Verifier


class LoopEngine:
    def __init__(
        self,
        *,
        planner: Planner,
        executor: ActionExecutor,
        verifier: Verifier,
        judge: Judge,
        checkpoint_store: CheckpointStore | None = None,
    ) -> None:
        self.planner = planner
        self.executor = executor
        self.verifier = verifier
        self.judge = judge
        self.checkpoint_store = checkpoint_store

    def create_state(self, definition: LoopDefinition, *, run_id: str | None = None) -> LoopState:
        goal = definition.goal.strip()
        if not goal:
            raise ValueError("loop goal is required")
        definition.goal = goal
        return LoopState(
            run_id=run_id or uuid4().hex,
            definition=definition,
            current_focus=goal,
        )

    def run(self, definition: LoopDefinition, *, run_id: str | None = None) -> LoopState:
        return self.resume(self.create_state(definition, run_id=run_id))

    def resume(self, state: LoopState) -> LoopState:
        if state.status in {LoopStatus.COMPLETED, LoopStatus.STOPPED, LoopStatus.FAILED}:
            return state
        is_new_run = state.started_at is None
        if is_new_run:
            state.started_at = utc_now()
        state.status = LoopStatus.RUNNING
        record_event(
            state,
            "loop_started" if is_new_run else "loop_resumed",
            {"goal": state.definition.goal, "phase": state.phase},
        )
        self._checkpoint(state)

        while state.status == LoopStatus.RUNNING:
            reason = budget_stop_reason(
                state,
                now=datetime.now(timezone.utc),
                check_iteration=state.phase == LoopPhase.READY,
            )
            if reason:
                self._stop(state, reason)
                break

            if state.phase == LoopPhase.READY:
                state.iteration += 1
                state.phase = LoopPhase.PLANNING
                state.latest_plan = None
                state.next_action_index = 0
                state.iteration_results = []
                record_event(state, "iteration_started", {"focus": state.current_focus})
                self._checkpoint(state)

            if state.phase == LoopPhase.PLANNING:
                try:
                    plan = self.planner.plan(state)
                except Exception as exc:
                    self._fail(state, f"planner_error:{type(exc).__name__}:{exc}")
                    break
                state.latest_plan = plan
                state.next_action_index = 0
                state.iteration_results = []
                record_event(state, "plan_created", asdict(plan))
                if not plan.actions:
                    self._stop(state, "planner_returned_no_actions")
                    break
                state.phase = LoopPhase.EXECUTING
                self._checkpoint(state)

            if state.phase == LoopPhase.EXECUTING:
                if state.latest_plan is None:
                    self._fail(state, "invalid_checkpoint:executing_without_plan")
                    break
                actions = state.latest_plan.actions
                if state.next_action_index < 0 or state.next_action_index > len(actions):
                    self._fail(state, "invalid_checkpoint:action_index_out_of_range")
                    break
                while state.next_action_index < len(actions):
                    reason = budget_stop_reason(
                        state,
                        now=datetime.now(timezone.utc),
                        check_iteration=False,
                    )
                    if reason:
                        self._stop(state, reason)
                        break
                    action = actions[state.next_action_index]
                    started = perf_counter()
                    try:
                        result = self.executor.execute(action, state)
                    except Exception as exc:
                        result = ActionResult(
                            action=action,
                            status="error",
                            error=f"{type(exc).__name__}: {exc}",
                        )
                    result.duration_seconds = max(result.duration_seconds, perf_counter() - started)
                    state.action_count += 1
                    state.action_results.append(result)
                    state.iteration_results.append(result)
                    state.next_action_index += 1
                    record_event(state, "action_finished", asdict(result))
                    self._checkpoint(state)
                if state.status != LoopStatus.RUNNING:
                    break
                state.phase = LoopPhase.VERIFYING
                self._checkpoint(state)

            if state.phase == LoopPhase.VERIFYING:
                try:
                    verification = self.verifier.verify(state, state.iteration_results)
                except Exception as exc:
                    self._fail(state, f"verifier_error:{type(exc).__name__}:{exc}")
                    break
                state.latest_verification = verification
                signature = observation_signature(verification)
                record_event(state, "verification_finished", asdict(verification))

                stagnation = stagnation_stop_reason(state, signature)
                state.observation_signatures.append(signature)
                if stagnation:
                    self._stop(state, stagnation)
                    break
                state.phase = LoopPhase.JUDGING
                self._checkpoint(state)

            if state.phase == LoopPhase.JUDGING:
                if state.latest_verification is None:
                    self._fail(state, "invalid_checkpoint:judging_without_verification")
                    break
                try:
                    judgement = self.judge.judge(state, state.latest_verification)
                except Exception as exc:
                    self._fail(state, f"judge_error:{type(exc).__name__}:{exc}")
                    break
                state.latest_judgement = judgement
                record_event(state, "judgement_created", asdict(judgement))

                if judgement.decision == Decision.COMPLETE:
                    state.status = LoopStatus.COMPLETED
                    state.phase = LoopPhase.TERMINAL
                    state.stop_reason = judgement.reason
                    state.finished_at = utc_now()
                    record_event(state, "loop_completed", {"reason": judgement.reason})
                elif judgement.decision == Decision.STOP:
                    self._stop(state, judgement.reason or "judge_requested_stop")
                else:
                    state.current_focus = judgement.next_focus or state.current_focus
                    state.phase = LoopPhase.READY
                    record_event(
                        state,
                        "loop_continues",
                        {
                            "decision": judgement.decision,
                            "focus": state.current_focus,
                            "progress_signals": judgement.progress_signals,
                        },
                    )
                self._checkpoint(state)

        return state

    def _checkpoint(self, state: LoopState) -> None:
        if self.checkpoint_store is not None:
            self.checkpoint_store.save(state)

    def _stop(self, state: LoopState, reason: str) -> None:
        state.status = LoopStatus.STOPPED
        state.phase = LoopPhase.TERMINAL
        state.stop_reason = reason
        state.finished_at = utc_now()
        record_event(state, "loop_stopped", {"reason": reason})
        self._checkpoint(state)

    def _fail(self, state: LoopState, reason: str) -> None:
        state.status = LoopStatus.FAILED
        state.phase = LoopPhase.TERMINAL
        state.stop_reason = reason
        state.finished_at = utc_now()
        record_event(state, "loop_failed", {"reason": reason})
        self._checkpoint(state)
