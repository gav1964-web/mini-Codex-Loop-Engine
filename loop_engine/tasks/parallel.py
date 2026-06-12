"""Bounded execution policy for independent task leaves."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from dataclasses import dataclass, field
import os
from pathlib import Path
from types import MappingProxyType
from typing import Iterable, Mapping

from .models import LeafExecutionResult, TaskGraph, TaskNode, TaskStatus
from .ports import LeafExecutor


@dataclass(frozen=True, slots=True)
class ResourceClaim:
    resource: str
    mode: str

    def __post_init__(self) -> None:
        normalized_resource = self.resource.strip()
        normalized_mode = self.mode.strip().lower()
        if not normalized_resource:
            raise ValueError("resource claim name is required")
        if normalized_mode not in {"read", "write"}:
            raise ValueError("resource claim mode must be read or write")
        object.__setattr__(self, "resource", normalized_resource)
        object.__setattr__(self, "mode", normalized_mode)

    @classmethod
    def create(cls, resource: str, *, mode: str) -> ResourceClaim:
        return cls(resource=resource, mode=mode)

    @classmethod
    def workspace(cls, path: str | Path, *, mode: str) -> ResourceClaim:
        normalized = os.path.normcase(str(Path(path).resolve()))
        return cls(resource=f"workspace:{normalized}", mode=mode)


@dataclass(frozen=True, slots=True)
class TaskSchedulerPolicy:
    max_parallel_leaves: int = 1
    parallel_safe_capabilities: frozenset[str] = frozenset()
    mutation_capabilities: frozenset[str] = frozenset()
    resource_claims: Mapping[str, tuple[ResourceClaim, ...]] = field(
        default_factory=lambda: MappingProxyType({})
    )

    def __post_init__(self) -> None:
        if self.max_parallel_leaves <= 0:
            raise ValueError("max_parallel_leaves must be positive")
        safe = frozenset(
            capability.strip() for capability in self.parallel_safe_capabilities
        )
        if "" in safe:
            raise ValueError("parallel-safe capability names must be non-empty")
        if self.max_parallel_leaves > 1 and not safe:
            raise ValueError(
                "parallel_safe_capabilities are required for parallel execution"
            )
        mutations = frozenset(
            capability.strip() for capability in self.mutation_capabilities
        )
        if "" in mutations:
            raise ValueError("mutation capability names must be non-empty")
        if not mutations <= safe:
            raise ValueError(
                "mutation_capabilities must be parallel-safe capabilities"
            )
        claims = _normalize_claims(self.resource_claims)
        object.__setattr__(self, "parallel_safe_capabilities", safe)
        object.__setattr__(self, "mutation_capabilities", mutations)
        object.__setattr__(self, "resource_claims", MappingProxyType(claims))

    @classmethod
    def create(
        cls,
        *,
        max_parallel_leaves: int = 1,
        parallel_safe_capabilities: set[str] | frozenset[str] = frozenset(),
        mutation_capabilities: set[str] | frozenset[str] = frozenset(),
        resource_claims: Mapping[
            str,
            Iterable[ResourceClaim],
        ] | None = None,
    ) -> TaskSchedulerPolicy:
        return cls(
            max_parallel_leaves=max_parallel_leaves,
            parallel_safe_capabilities=frozenset(parallel_safe_capabilities),
            mutation_capabilities=frozenset(mutation_capabilities),
            resource_claims=resource_claims or {},
        )

    def allows_parallel(self, node: TaskNode) -> bool:
        capabilities = set(node.required_capabilities)
        admitted = (
            self.max_parallel_leaves > 1
            and bool(capabilities)
            and capabilities <= self.parallel_safe_capabilities
        )
        if not admitted:
            return False
        if capabilities & self.mutation_capabilities:
            return any(
                claim.mode == "write"
                for claim in self.claims_for(node)
            )
        return True

    def claims_for(self, node: TaskNode) -> tuple[ResourceClaim, ...]:
        return self.resource_claims.get(node.id, ())


def select_leaf_batch(
    candidates: list[TaskNode],
    policy: TaskSchedulerPolicy,
) -> list[TaskNode]:
    ready = [node for node in candidates if node.status == TaskStatus.READY]
    first = ready[0]
    if not policy.allows_parallel(first):
        return [first]
    selected = [first]
    selected_claims = list(policy.claims_for(first))
    for node in ready[1:]:
        if len(selected) == policy.max_parallel_leaves:
            break
        claims = policy.claims_for(node)
        if policy.allows_parallel(node) and not _claims_conflict(
            selected_claims,
            claims,
        ):
            selected.append(node)
            selected_claims.extend(claims)
    return selected


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


def _normalize_claims(
    claims: Mapping[str, Iterable[ResourceClaim]],
) -> dict[str, tuple[ResourceClaim, ...]]:
    normalized: dict[str, tuple[ResourceClaim, ...]] = {}
    for node_id, node_claims in claims.items():
        key = node_id.strip()
        if not key:
            raise ValueError("resource claim node id is required")
        if key in normalized:
            raise ValueError(f"duplicate resource claim node id: {key}")
        values = tuple(node_claims)
        if any(not isinstance(claim, ResourceClaim) for claim in values):
            raise TypeError("resource claims must contain ResourceClaim values")
        resources = [claim.resource for claim in values]
        if len(resources) != len(set(resources)):
            raise ValueError(f"resource claims must be unique for node: {key}")
        normalized[key] = values
    return normalized


def _claims_conflict(
    left: Iterable[ResourceClaim],
    right: Iterable[ResourceClaim],
) -> bool:
    left_by_resource = {claim.resource: claim.mode for claim in left}
    return any(
        claim.resource in left_by_resource
        and (claim.mode == "write" or left_by_resource[claim.resource] == "write")
        for claim in right
    )
