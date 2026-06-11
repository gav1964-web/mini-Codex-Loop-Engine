"""Dependency inversion ports for atomic task orchestration."""

from __future__ import annotations

from typing import Protocol

from .models import (
    AtomicityDecision,
    CapabilityResolution,
    LeafExecutionResult,
    TaskGraph,
    TaskNode,
)


class TaskDecomposer(Protocol):
    def assess(self, node: TaskNode, graph: TaskGraph) -> AtomicityDecision:
        ...


class CapabilityResolver(Protocol):
    def resolve(self, node: TaskNode, graph: TaskGraph) -> CapabilityResolution:
        ...


class CapabilityAcquirer(Protocol):
    def acquire(self, capability: str, node: TaskNode, graph: TaskGraph) -> bool:
        ...


class LeafExecutor(Protocol):
    def execute(self, node: TaskNode, graph: TaskGraph) -> LeafExecutionResult:
        ...


class IntegrationVerifier(Protocol):
    def verify(self, node: TaskNode, graph: TaskGraph) -> LeafExecutionResult:
        ...


class TaskGraphStore(Protocol):
    def save(self, graph: TaskGraph) -> None:
        ...
