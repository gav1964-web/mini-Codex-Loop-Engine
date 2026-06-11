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
    iteration: int = 0
    action_count: int = 0
    started_at: str | None = None
    finished_at: str | None = None
    current_focus: str = ""
    latest_plan: Plan | None = None
    action_results: list[ActionResult] = field(default_factory=list)
    latest_verification: VerificationResult | None = None
    latest_judgement: Judgement | None = None
    observation_signatures: list[str] = field(default_factory=list)
    stop_reason: str | None = None
    events: list[LoopEvent] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)
