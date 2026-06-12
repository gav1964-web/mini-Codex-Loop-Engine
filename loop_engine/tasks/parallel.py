"""Bounded execution policy for independent task leaves."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from dataclasses import dataclass

from .models import LeafExecutionResult, TaskGraph, TaskNode, TaskStatus
from .ports import LeafExecutor


@dataclass(frozen=True, slots=True)
class TaskSchedulerPolicy:
    max_parallel_leaves: int = 1
    parallel_safe_capabilities: frozenset[str] = frozenset()

    @classmethod
    def create(
        cls,
        *,
        max_parallel_leaves: int = 1,
        parallel_safe_capabilities: set[str] | frozenset[str] = frozenset(),
    ) -> TaskSchedulerPolicy:
        if max_parallel_leaves <= 0:
            raise ValueError("max_parallel_leaves must be positive")
        normalized = frozenset(
            capability.strip() for capability in parallel_safe_capabilities
        )
        if "" in normalized:
            raise ValueError("parallel-safe capability names must be non-empty")
        if max_parallel_leaves > 1 and not normalized:
            raise ValueError(
                "parallel_safe_capabilities are required for parallel execution"
            )
        return cls(
            max_parallel_leaves=max_parallel_leaves,
            parallel_safe_capabilities=normalized,
        )

    def allows_parallel(self, node: TaskNode) -> bool:
        capabilities = set(node.required_capabilities)
        return (
            self.max_parallel_leaves > 1
            and bool(capabilities)
            and capabilities <= self.parallel_safe_capabilities
        )


def select_leaf_batch(
    candidates: list[TaskNode],
    policy: TaskSchedulerPolicy,
) -> list[TaskNode]:
    ready = [node for node in candidates if node.status == TaskStatus.READY]
    first = ready[0]
    if not policy.allows_parallel(first):
        return [first]
    return [
        node for node in ready if policy.allows_parallel(node)
    ][: policy.max_parallel_leaves]


def select_pending_candidate(
    candidates: list[TaskNode],
    policy: TaskSchedulerPolicy,
) -> TaskNode | None:
    if policy.max_parallel_leaves == 1:
        first = candidates[0]
        return first if first.status == TaskStatus.PENDING else None
    return next(
        (node for node in candidates if node.status == TaskStatus.PENDING),
        None,
    )


def execute_leaf_batch(
    executor: LeafExecutor,
    nodes: list[TaskNode],
    graph: TaskGraph,
) -> dict[str, LeafExecutionResult]:
    if not nodes:
        return {}
    if len(nodes) == 1:
        node = nodes[0]
        return {node.id: _execute_one(executor, node, graph)}

    try:
        snapshots = {
            node.id: (
                deepcopy(graph.nodes[node.id]),
                deepcopy(graph),
            )
            for node in nodes
        }
    except Exception as exc:
        return {
            node.id: LeafExecutionResult(
                status="failed",
                summary="parallel leaf snapshot failed",
                error=f"{type(exc).__name__}: {exc}",
            )
            for node in nodes
        }
    with ThreadPoolExecutor(
        max_workers=len(nodes),
        thread_name_prefix="task-leaf",
    ) as pool:
        futures = {
            node_id: pool.submit(
                _execute_one,
                executor,
                node_snapshot,
                graph_snapshot,
            )
            for node_id, (node_snapshot, graph_snapshot) in snapshots.items()
        }
        return {
            node_id: futures[node_id].result()
            for node_id in sorted(futures)
        }


def _execute_one(
    executor: LeafExecutor,
    node: TaskNode,
    graph: TaskGraph,
) -> LeafExecutionResult:
    try:
        return executor.execute(node, graph)
    except Exception as exc:
        return LeafExecutionResult(
            status="failed",
            summary="leaf executor raised an exception",
            error=f"{type(exc).__name__}: {exc}",
        )
