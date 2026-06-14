"""Measured, provider-neutral evidence for decomposition strategies."""

from __future__ import annotations

import hashlib
import json
import statistics
from copy import deepcopy
from dataclasses import dataclass, field, replace
from typing import Protocol

from .models import TaskBudget, TaskGraph


@dataclass(frozen=True, slots=True)
class ReplayTaskCase:
    name: str
    goal: str
    success_criteria: tuple[str, ...] = ()
    required_capabilities: tuple[str, ...] = ()
    budget: TaskBudget = field(default_factory=TaskBudget)

    def create_graph(self, *, graph_id: str) -> TaskGraph:
        return TaskGraph.create(
            self.goal,
            success_criteria=list(self.success_criteria),
            required_capabilities=list(self.required_capabilities),
            budget=deepcopy(self.budget),
            graph_id=graph_id,
        )


@dataclass(frozen=True, slots=True)
class StrategyUsage:
    input_tokens: int
    output_tokens: int
    cost_microunits: int
    cost_basis: str

    def __post_init__(self) -> None:
        values = (self.input_tokens, self.output_tokens, self.cost_microunits)
        if any(
            not isinstance(value, int)
            or isinstance(value, bool)
            or value < 0
            for value in values
        ):
            raise ValueError(
                "strategy usage counters must be non-negative integers"
            )
        basis = self.cost_basis.strip()
        if not basis:
            raise ValueError("strategy usage cost_basis is required")
        object.__setattr__(self, "cost_basis", basis)


class StrategyUsageProvider(Protocol):
    def measure(
        self,
        *,
        strategy: str,
        case: ReplayTaskCase,
        graph: TaskGraph,
    ) -> StrategyUsage | None:
        ...


@dataclass(frozen=True, slots=True)
class StrategyMetrics:
    strategy: str
    case: str
    root_status: str
    node_count: int
    leaf_count: int
    max_depth: int
    dependency_edge_count: int
    leaf_executions: int
    event_count: int
    failed_count: int
    blocked_count: int
    topology_sha256: str
    outcome_sha256: str
    elapsed_ms: int = 0
    elapsed_sample_count: int | None = None
    elapsed_samples_ms: tuple[int, ...] = ()
    elapsed_min_ms: int | None = None
    elapsed_max_ms: int | None = None
    elapsed_mad_ms: int | None = None
    input_tokens: int | None = None
    output_tokens: int | None = None
    total_tokens: int | None = None
    cost_microunits: int | None = None
    cost_basis: str | None = None

    def __post_init__(self) -> None:
        _validate_counter("elapsed_ms", self.elapsed_ms, optional=False)
        samples = tuple(self.elapsed_samples_ms) or (self.elapsed_ms,)
        if len(samples) % 2 == 0:
            raise ValueError("strategy latency sample count must be odd")
        for sample in samples:
            _validate_counter("elapsed sample", sample, optional=False)
        median = int(statistics.median(samples))
        minimum = min(samples)
        maximum = max(samples)
        mad = int(
            statistics.median(
                abs(sample - median) for sample in samples
            )
        )
        expected = {
            "elapsed_ms": median,
            "elapsed_sample_count": len(samples),
            "elapsed_min_ms": minimum,
            "elapsed_max_ms": maximum,
            "elapsed_mad_ms": mad,
        }
        if self.elapsed_ms != median:
            raise ValueError("strategy elapsed_ms must equal sample median")
        for name in (
            "elapsed_sample_count",
            "elapsed_min_ms",
            "elapsed_max_ms",
            "elapsed_mad_ms",
        ):
            value = getattr(self, name)
            if value is not None and value != expected[name]:
                raise ValueError(f"strategy {name} does not match samples")
            object.__setattr__(self, name, expected[name])
        object.__setattr__(self, "elapsed_samples_ms", samples)
        for name in (
            "input_tokens",
            "output_tokens",
            "total_tokens",
            "cost_microunits",
        ):
            _validate_counter(name, getattr(self, name), optional=True)
        token_values = (
            self.input_tokens,
            self.output_tokens,
            self.total_tokens,
        )
        if any(value is None for value in token_values) and any(
            value is not None for value in token_values
        ):
            raise ValueError("strategy token metrics must be complete")
        if (
            self.total_tokens is not None
            and self.total_tokens != self.input_tokens + self.output_tokens
        ):
            raise ValueError("strategy total_tokens must equal input plus output")
        basis = self.cost_basis.strip() if self.cost_basis is not None else None
        if (self.cost_microunits is None) != (basis is None):
            raise ValueError("strategy cost and cost_basis must be provided together")
        object.__setattr__(self, "cost_basis", basis)


def strategy_metrics(
    strategy: str,
    case: str,
    graph: TaskGraph,
    *,
    elapsed_ms: int = 0,
    elapsed_samples_ms: tuple[int, ...] | None = None,
    usage: StrategyUsage | None = None,
) -> StrategyMetrics:
    samples = tuple(elapsed_samples_ms or (elapsed_ms,))
    if len(samples) % 2 == 0:
        raise ValueError("strategy latency sample count must be odd")
    for sample in samples:
        _validate_counter("elapsed sample", sample, optional=False)
    median_elapsed_ms = int(statistics.median(samples))
    if (
        not isinstance(median_elapsed_ms, int)
        or isinstance(median_elapsed_ms, bool)
        or median_elapsed_ms < 0
    ):
        raise ValueError("strategy elapsed_ms must be a non-negative integer")
    if usage is not None and not isinstance(usage, StrategyUsage):
        raise TypeError("strategy usage must be StrategyUsage")
    leaves = [node for node in graph.nodes.values() if not node.children]
    topology = [
        {
            "id": node.id,
            "parent_id": node.parent_id,
            "goal": node.goal,
            "criteria": node.success_criteria,
            "capabilities": node.required_capabilities,
            "dependencies": node.dependencies,
            "children": node.children,
            "depth": node.depth,
        }
        for node in sorted(graph.nodes.values(), key=lambda item: item.id)
    ]
    outcomes = [
        {
            "id": node.id,
            "status": str(node.status),
            "attempts": node.attempts,
            "error": node.error,
            "summary": node.result.summary if node.result is not None else None,
        }
        for node in sorted(graph.nodes.values(), key=lambda item: item.id)
    ]
    return StrategyMetrics(
        strategy=strategy,
        case=case,
        root_status=str(graph.root.status),
        node_count=len(graph.nodes),
        leaf_count=len(leaves),
        max_depth=max(node.depth for node in graph.nodes.values()),
        dependency_edge_count=sum(
            len(node.dependencies) for node in graph.nodes.values()
        ),
        leaf_executions=graph.leaf_executions,
        event_count=len(graph.events),
        failed_count=sum(
            str(node.status) == "failed" for node in graph.nodes.values()
        ),
        blocked_count=sum(
            str(node.status) == "blocked" for node in graph.nodes.values()
        ),
        topology_sha256=_sha256(topology),
        outcome_sha256=_sha256(outcomes),
        elapsed_ms=median_elapsed_ms,
        elapsed_samples_ms=samples,
        input_tokens=usage.input_tokens if usage is not None else None,
        output_tokens=usage.output_tokens if usage is not None else None,
        total_tokens=(
            usage.input_tokens + usage.output_tokens
            if usage is not None
            else None
        ),
        cost_microunits=usage.cost_microunits if usage is not None else None,
        cost_basis=usage.cost_basis if usage is not None else None,
    )


def attach_strategy_usage(
    metrics: StrategyMetrics,
    usage: StrategyUsage | None,
) -> StrategyMetrics:
    if not isinstance(metrics, StrategyMetrics):
        raise TypeError("strategy metrics contract is required")
    if usage is None:
        return metrics
    if not isinstance(usage, StrategyUsage):
        raise TypeError("strategy usage must be StrategyUsage")
    return replace(
        metrics,
        input_tokens=usage.input_tokens,
        output_tokens=usage.output_tokens,
        total_tokens=usage.input_tokens + usage.output_tokens,
        cost_microunits=usage.cost_microunits,
        cost_basis=usage.cost_basis,
    )


def _sha256(value) -> str:
    encoded = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _validate_counter(
    name: str,
    value: int | None,
    *,
    optional: bool,
) -> None:
    if value is None and optional:
        return
    if (
        not isinstance(value, int)
        or isinstance(value, bool)
        or value < 0
    ):
        raise ValueError(f"strategy {name} must be a non-negative integer")
