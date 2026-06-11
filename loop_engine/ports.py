"""Dependency inversion ports for loop components."""

from __future__ import annotations

from typing import Protocol

from .models import Action, ActionResult, Judgement, LoopState, Plan, VerificationResult


class Planner(Protocol):
    def plan(self, state: LoopState) -> Plan:
        ...


class ActionExecutor(Protocol):
    def execute(self, action: Action, state: LoopState) -> ActionResult:
        ...


class Verifier(Protocol):
    def verify(self, state: LoopState, results: list[ActionResult]) -> VerificationResult:
        ...


class Judge(Protocol):
    def judge(self, state: LoopState, verification: VerificationResult) -> Judgement:
        ...


class CheckpointStore(Protocol):
    def save(self, state: LoopState) -> None:
        ...
