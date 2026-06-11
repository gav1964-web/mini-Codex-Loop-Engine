"""Stable contracts for the loop engine."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from enum import StrEnum
from typing import Any


class LoopStatus(StrEnum):
    READY = "ready"
    RUNNING = "running"
    COMPLETED = "completed"
    STOPPED = "stopped"
    FAILED = "failed"


class LoopPhase(StrEnum):
    READY = "ready"
    PLANNING = "planning"
    EXECUTING = "executing"
    VERIFYING = "verifying"
    JUDGING = "judging"
    TERMINAL = "terminal"


class Decision(StrEnum):
    CONTINUE = "continue"
    COMPLETE = "complete"
    STOP = "stop"
    REPLAN = "replan"


@dataclass(slots=True)
class LoopBudget:
    max_iterations: int = 6
    max_actions: int = 12
    timeout_seconds: float = 300.0
    max_repeated_observations: int = 2


@dataclass(slots=True)
class LoopDefinition:
    goal: str
    success_criteria: list[str] = field(default_factory=list)
    constraints: list[str] = field(default_factory=list)
    budget: LoopBudget = field(default_factory=LoopBudget)
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class Action:
    tool: str
    arguments: dict[str, Any] = field(default_factory=dict)
    reason: str = ""


@dataclass(slots=True)
class Plan:
    actions: list[Action] = field(default_factory=list)
    rationale: str = ""
    expected_evidence: list[str] = field(default_factory=list)


@dataclass(slots=True)
class ActionResult:
    action: Action
    status: str
    output: dict[str, Any] = field(default_factory=dict)
    error: str | None = None
    duration_seconds: float = 0.0


@dataclass(slots=True)
class VerificationResult:
    status: str
    passed: list[str] = field(default_factory=list)
    failed: list[str] = field(default_factory=list)
    evidence: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class Judgement:
    decision: Decision
    reason: str
    progress_signals: list[str] = field(default_factory=list)
    next_focus: str | None = None


@dataclass(slots=True)
class LoopEvent:
    sequence: int
    event_type: str
    iteration: int
    payload: dict[str, Any]
    timestamp: str


@dataclass(slots=True)
class LoopState:
    run_id: str
    definition: LoopDefinition
    status: LoopStatus = LoopStatus.READY
    phase: LoopPhase = LoopPhase.READY
    iteration: int = 0
    action_count: int = 0
    started_at: str | None = None
    finished_at: str | None = None
    current_focus: str = ""
    latest_plan: Plan | None = None
    next_action_index: int = 0
    iteration_results: list[ActionResult] = field(default_factory=list)
    action_results: list[ActionResult] = field(default_factory=list)
    latest_verification: VerificationResult | None = None
    latest_judgement: Judgement | None = None
    observation_signatures: list[str] = field(default_factory=list)
    stop_reason: str | None = None
    events: list[LoopEvent] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> LoopState:
        definition_data = dict(value["definition"])
        budget = LoopBudget(**dict(definition_data.pop("budget", {})))
        definition = LoopDefinition(budget=budget, **definition_data)

        def action_from_dict(data: dict[str, Any]) -> Action:
            return Action(**dict(data))

        def plan_from_dict(data: dict[str, Any] | None) -> Plan | None:
            if data is None:
                return None
            plan_data = dict(data)
            plan_data["actions"] = [action_from_dict(item) for item in plan_data.get("actions", [])]
            return Plan(**plan_data)

        def result_from_dict(data: dict[str, Any]) -> ActionResult:
            result_data = dict(data)
            result_data["action"] = action_from_dict(result_data["action"])
            return ActionResult(**result_data)

        verification_data = value.get("latest_verification")
        judgement_data = value.get("latest_judgement")
        if judgement_data is not None:
            judgement_data = dict(judgement_data)
            judgement_data["decision"] = Decision(judgement_data["decision"])

        loaded_status = LoopStatus(value.get("status", LoopStatus.READY))
        legacy_running = "phase" not in value and loaded_status == LoopStatus.RUNNING
        default_phase = (
            LoopPhase.TERMINAL
            if loaded_status in {LoopStatus.COMPLETED, LoopStatus.STOPPED, LoopStatus.FAILED}
            else LoopPhase.READY
        )
        return cls(
            run_id=str(value["run_id"]),
            definition=definition,
            status=loaded_status,
            phase=LoopPhase(value.get("phase", default_phase)),
            iteration=int(value.get("iteration", 0)),
            action_count=int(value.get("action_count", 0)),
            started_at=value.get("started_at"),
            finished_at=value.get("finished_at"),
            current_focus=str(value.get("current_focus", "")),
            latest_plan=None if legacy_running else plan_from_dict(value.get("latest_plan")),
            next_action_index=0 if legacy_running else int(value.get("next_action_index", 0)),
            iteration_results=(
                []
                if legacy_running
                else [result_from_dict(item) for item in value.get("iteration_results", [])]
            ),
            action_results=[result_from_dict(item) for item in value.get("action_results", [])],
            latest_verification=(
                VerificationResult(**dict(verification_data))
                if verification_data is not None
                else None
            ),
            latest_judgement=Judgement(**judgement_data) if judgement_data is not None else None,
            observation_signatures=list(value.get("observation_signatures", [])),
            stop_reason=value.get("stop_reason"),
            events=[LoopEvent(**dict(item)) for item in value.get("events", [])],
        )
