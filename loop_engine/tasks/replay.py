"""Deterministic decomposition replay and strategy comparison."""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import asdict, dataclass
from pathlib import Path
from time import perf_counter
from typing import Callable

from .models import (
    AtomicLeafSpec,
    AtomicityDecision,
    ChildTaskSpec,
    TaskGraph,
    TaskNode,
)
from .ports import TaskDecomposer
from .scheduler import TaskScheduler
from .strategy_metrics import (
    ReplayTaskCase,
    StrategyMetrics,
    StrategyUsage,
    StrategyUsageProvider,
    attach_strategy_usage,
    strategy_metrics,
)

DECOMPOSITION_TRACE_SCHEMA_VERSION = 1
STRATEGY_COMPARISON_SCHEMA_VERSION = 2
_SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")


@dataclass(frozen=True, slots=True)
class DecompositionTraceEntry:
    node_id: str
    context_sha256: str
    decision: dict


@dataclass(frozen=True, slots=True)
class DecompositionTrace:
    entries: tuple[DecompositionTraceEntry, ...]

    def save(self, path: str | Path) -> None:
        target = Path(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "schema_version": DECOMPOSITION_TRACE_SCHEMA_VERSION,
            "entries": [asdict(entry) for entry in self.entries],
        }
        temporary = target.with_name(f".{target.name}.tmp")
        temporary.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        temporary.replace(target)

    @classmethod
    def load(cls, path: str | Path) -> DecompositionTrace:
        payload = json.loads(Path(path).read_text(encoding="utf-8"))
        if payload.get("schema_version") != DECOMPOSITION_TRACE_SCHEMA_VERSION:
            raise ValueError("unsupported decomposition trace schema_version")
        rows = payload.get("entries")
        if not isinstance(rows, list):
            raise ValueError("decomposition trace entries must be an array")
        entries = tuple(
            DecompositionTraceEntry(
                node_id=str(row["node_id"]),
                context_sha256=str(row["context_sha256"]),
                decision=dict(row["decision"]),
            )
            for row in rows
        )
        if len({entry.node_id for entry in entries}) != len(entries):
            raise ValueError("decomposition trace node ids must be unique")
        if any(
            not entry.node_id.strip()
            or not _SHA256_PATTERN.fullmatch(entry.context_sha256)
            for entry in entries
        ):
            raise ValueError("decomposition trace identity fields are invalid")
        for entry in entries:
            _decision_from_dict(entry.decision)
        return cls(entries=entries)


class RecordingTaskDecomposer:
    def __init__(self, delegate: TaskDecomposer) -> None:
        self.delegate = delegate
        self._entries: list[DecompositionTraceEntry] = []

    def assess(self, node: TaskNode, graph: TaskGraph) -> AtomicityDecision:
        decision = self.delegate.assess(node, graph)
        if any(entry.node_id == node.id for entry in self._entries):
            raise ValueError(f"decomposition node assessed more than once: {node.id}")
        self._entries.append(
            DecompositionTraceEntry(
                node_id=node.id,
                context_sha256=decomposition_context_sha256(node, graph),
                decision=asdict(decision),
            )
        )
        return decision

    def trace(self) -> DecompositionTrace:
        return DecompositionTrace(entries=tuple(self._entries))


class RecordedTaskDecomposer:
    def __init__(self, trace: DecompositionTrace) -> None:
        self._entries = {entry.node_id: entry for entry in trace.entries}
        self._used: set[str] = set()

    def assess(self, node: TaskNode, graph: TaskGraph) -> AtomicityDecision:
        entry = self._entries.get(node.id)
        if entry is None:
            raise ValueError(f"decomposition trace has no node: {node.id}")
        if decomposition_context_sha256(node, graph) != entry.context_sha256:
            raise ValueError(f"decomposition replay context mismatch: {node.id}")
        if node.id in self._used:
            raise ValueError(f"decomposition trace node reused: {node.id}")
        self._used.add(node.id)
        return _decision_from_dict(entry.decision)

    def unused_node_ids(self) -> list[str]:
        return sorted(set(self._entries) - self._used)


@dataclass(frozen=True, slots=True)
class StrategyComparison:
    case: str
    runs: tuple[StrategyMetrics, ...]
    topology_groups: dict[str, tuple[str, ...]]
    outcome_groups: dict[str, tuple[str, ...]]

    @property
    def topology_diverged(self) -> bool:
        return len(self.topology_groups) > 1

    @property
    def outcome_diverged(self) -> bool:
        return len(self.outcome_groups) > 1

    def to_dict(self) -> dict:
        return {
            "schema_version": STRATEGY_COMPARISON_SCHEMA_VERSION,
            "case": self.case,
            "topology_diverged": self.topology_diverged,
            "outcome_diverged": self.outcome_diverged,
            "topology_groups": self.topology_groups,
            "outcome_groups": self.outcome_groups,
            "runs": [asdict(run) for run in self.runs],
        }

    def save(self, path: str | Path) -> None:
        target = Path(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        temporary = target.with_name(f".{target.name}.tmp")
        temporary.write_text(
            json.dumps(self.to_dict(), ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        temporary.replace(target)


class DecompositionStrategyRunner:
    def __init__(
        self,
        scheduler_factory: Callable[[TaskDecomposer], TaskScheduler],
        *,
        usage_provider: StrategyUsageProvider | None = None,
        clock: Callable[[], float] = perf_counter,
    ) -> None:
        self.scheduler_factory = scheduler_factory
        self.usage_provider = usage_provider
        self.clock = clock

    def compare(
        self,
        case: ReplayTaskCase,
        strategies: dict[str, Callable[[], TaskDecomposer]],
    ) -> StrategyComparison:
        if not strategies:
            raise ValueError("at least one decomposition strategy is required")
        runs: list[StrategyMetrics] = []
        for name in sorted(strategies):
            strategy_name = name.strip()
            if not strategy_name:
                raise ValueError("strategy names must be non-empty")
            graph = case.create_graph(
                graph_id=_stable_graph_id(case.name, strategy_name)
            )
            started = self.clock()
            result = self.scheduler_factory(strategies[name]()).run(graph)
            elapsed_seconds = self.clock() - started
            if elapsed_seconds < 0:
                raise ValueError("strategy measurement clock moved backwards")
            elapsed_ms = round(elapsed_seconds * 1000)
            metrics = strategy_metrics(
                strategy_name,
                case.name,
                result,
                elapsed_ms=elapsed_ms,
            )
            usage = (
                self.usage_provider.measure(
                    strategy=strategy_name,
                    case=case,
                    graph=result,
                )
                if self.usage_provider is not None
                else None
            )
            if usage is not None and not isinstance(usage, StrategyUsage):
                raise TypeError("strategy usage provider must return StrategyUsage")
            runs.append(attach_strategy_usage(metrics, usage))
        return StrategyComparison(
            case=case.name,
            runs=tuple(runs),
            topology_groups=_group_runs(runs, "topology_sha256"),
            outcome_groups=_group_runs(runs, "outcome_sha256"),
        )


def decomposition_context_sha256(node: TaskNode, graph: TaskGraph) -> str:
    payload = {
        "node": {
            "id": node.id,
            "parent_id": node.parent_id,
            "goal": node.goal,
            "success_criteria": node.success_criteria,
            "required_capabilities": node.required_capabilities,
            "dependencies": node.dependencies,
            "depth": node.depth,
            "metadata": node.metadata,
        },
        "ancestors": _ancestor_context(node, graph),
        "budget": asdict(graph.budget),
        "node_count": len(graph.nodes),
        "leaf_executions": graph.leaf_executions,
    }
    return _sha256(payload)


def _decision_from_dict(value: dict) -> AtomicityDecision:
    if set(value) != {"is_atomic", "reason", "children", "leaf"}:
        raise ValueError("decomposition trace decision fields are invalid")
    if not isinstance(value["is_atomic"], bool):
        raise ValueError("decomposition trace is_atomic must be boolean")
    if not isinstance(value["reason"], str):
        raise ValueError("decomposition trace reason must be a string")
    if not isinstance(value["children"], list):
        raise ValueError("decomposition trace children must be an array")
    leaf = value.get("leaf")
    decision = AtomicityDecision(
        is_atomic=value["is_atomic"],
        reason=str(value["reason"]),
        children=[
            ChildTaskSpec(**dict(child)) for child in value.get("children", [])
        ],
        leaf=AtomicLeafSpec(**dict(leaf)) if leaf is not None else None,
    )
    if decision.is_atomic and decision.children:
        raise ValueError("atomic replay decision cannot have children")
    if not decision.is_atomic and (not decision.children or decision.leaf is not None):
        raise ValueError("non-atomic replay decision requires only children")
    return decision


def _ancestor_context(node: TaskNode, graph: TaskGraph) -> list[dict]:
    ancestors: list[dict] = []
    parent_id = node.parent_id
    while parent_id is not None:
        parent = graph.nodes[parent_id]
        ancestors.append({"id": parent.id, "goal": parent.goal})
        parent_id = parent.parent_id
    ancestors.reverse()
    return ancestors


def _group_runs(
    runs: list[StrategyMetrics],
    field_name: str,
) -> dict[str, tuple[str, ...]]:
    groups: dict[str, list[str]] = {}
    for run in runs:
        groups.setdefault(str(getattr(run, field_name)), []).append(run.strategy)
    return {
        fingerprint: tuple(sorted(names))
        for fingerprint, names in sorted(groups.items())
    }


def _stable_graph_id(case: str, strategy: str) -> str:
    digest = hashlib.sha256(f"{case}\0{strategy}".encode("utf-8")).hexdigest()
    return f"replay-{digest[:16]}"


def _sha256(value) -> str:
    encoded = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()
