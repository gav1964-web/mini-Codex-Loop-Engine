"""Deterministic adapters for tests, demos, and replay."""

from __future__ import annotations

from collections.abc import Callable

from ..models import ActionResult, Decision, Judgement, LoopState, Plan, VerificationResult


class FunctionPlanner:
    def __init__(self, callback: Callable[[LoopState], Plan]) -> None:
        self.callback = callback

    def plan(self, state: LoopState) -> Plan:
        return self.callback(state)


class FunctionVerifier:
    def __init__(
        self,
        callback: Callable[[LoopState, list[ActionResult]], VerificationResult],
    ) -> None:
        self.callback = callback

    def verify(self, state: LoopState, results: list[ActionResult]) -> VerificationResult:
        return self.callback(state, results)


class CriteriaJudge:
    """Complete only when the verifier explicitly reports passed."""

    def judge(self, state: LoopState, verification: VerificationResult) -> Judgement:
        if verification.status == "passed" and not verification.failed:
            return Judgement(
                decision=Decision.COMPLETE,
                reason="success criteria satisfied",
                progress_signals=list(verification.passed),
            )
        if verification.status == "blocked":
            return Judgement(decision=Decision.STOP, reason="verification reported an external block")
        return Judgement(
            decision=Decision.REPLAN,
            reason="success criteria not yet satisfied",
            progress_signals=list(verification.passed),
            next_focus=verification.failed[0] if verification.failed else state.current_focus,
        )
