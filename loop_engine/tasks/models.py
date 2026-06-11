"""Stable contracts for persistent atomic task graphs."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from enum import StrEnum
from typing import Any
from uuid import uuid4


class TaskStatus(StrEnum):
    PENDING = "pending"
    READY = "ready"
    RUNNING = "running"
    WAITING = "waiting"
    COMPLETED = "completed"
    FAILED = "failed"
    BLOCKED = "blocked"


@dataclass(slots=True)
class TaskBudget:
    max_nodes: int = 32
    max_depth: int = 5
    max_leaf_executions: int = 16


@dataclass(slots=True)
class ChildTaskSpec:
    key: str
    goal: str
    success_criteria: list[str] = field(default_factory=list)
    required_capabilities: list[str] = field(default_factory=list)
    depends_on: list[str] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class AtomicityDecision:
    is_atomic: bool
    reason: str
    children: list[ChildTaskSpec] = field(default_factory=list)


@dataclass(slots=True)
class CapabilityResolution:
    available: list[str] = field(default_factory=list)
    missing: list[str] = field(default_factory=list)


@dataclass(slots=True)
class LeafExecutionResult:
    status: str
    summary: str
    evidence: dict[str, Any] = field(default_factory=dict)
    error: str | None = None


@dataclass(slots=True)
class TaskNode:
    id: str
    goal: str
    parent_id: str | None = None
    success_criteria: list[str] = field(default_factory=list)
    required_capabilities: list[str] = field(default_factory=list)
    dependencies: list[str] = field(default_factory=list)
    children: list[str] = field(default_factory=list)
    status: TaskStatus = TaskStatus.PENDING
    depth: int = 0
    attempts: int = 0
    result: LeafExecutionResult | None = None
    error: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class TaskEvent:
    sequence: int
    event_type: str
    node_id: str
    payload: dict[str, Any]
    timestamp: str


@dataclass(slots=True)
class TaskGraph:
    id: str
    root_id: str
    nodes: dict[str, TaskNode]
    budget: TaskBudget = field(default_factory=TaskBudget)
    leaf_executions: int = 0
    events: list[TaskEvent] = field(default_factory=list)
    stop_reason: str | None = None

    @classmethod
    def create(
        cls,
        goal: str,
        *,
        success_criteria: list[str] | None = None,
        required_capabilities: list[str] | None = None,
        budget: TaskBudget | None = None,
        graph_id: str | None = None,
    ) -> TaskGraph:
        normalized_goal = goal.strip()
        if not normalized_goal:
            raise ValueError("task graph goal is required")
        root_id = "root"
        root = TaskNode(
            id=root_id,
            goal=normalized_goal,
            success_criteria=list(success_criteria or []),
            required_capabilities=list(required_capabilities or []),
        )
        return cls(
            id=graph_id or uuid4().hex,
            root_id=root_id,
            nodes={root_id: root},
            budget=budget or TaskBudget(),
        )

    @property
    def root(self) -> TaskNode:
        return self.nodes[self.root_id]

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> TaskGraph:
        nodes: dict[str, TaskNode] = {}
        for node_id, raw_node in dict(value["nodes"]).items():
            node_data = dict(raw_node)
            node_data["status"] = TaskStatus(node_data.get("status", TaskStatus.PENDING))
            result = node_data.get("result")
            if result is not None:
                node_data["result"] = LeafExecutionResult(**dict(result))
            nodes[node_id] = TaskNode(**node_data)
        return cls(
            id=str(value["id"]),
            root_id=str(value["root_id"]),
            nodes=nodes,
            budget=TaskBudget(**dict(value.get("budget", {}))),
            leaf_executions=int(value.get("leaf_executions", 0)),
            events=[TaskEvent(**dict(item)) for item in value.get("events", [])],
            stop_reason=value.get("stop_reason"),
        )
