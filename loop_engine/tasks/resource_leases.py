"""Contracts for externally coordinated resource leases."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Generic, Protocol, TypeVar

from .events import record_task_event
from .models import TaskGraph, TaskNode
from .parallel import ResourceClaim


@dataclass(frozen=True, slots=True)
class ResourceLease:
    lease_id: str
    owner_id: str
    claims: tuple[ResourceClaim, ...]


@dataclass(frozen=True, slots=True)
class ResourceLeaseAttempt:
    lease: ResourceLease | None
    conflicting_resources: tuple[str, ...] = ()

    @property
    def granted(self) -> bool:
        return self.lease is not None


class ResourceLeaseManager(Protocol):
    def acquire(
        self,
        *,
        owner_id: str,
        claims: tuple[ResourceClaim, ...],
    ) -> ResourceLeaseAttempt:
        ...

    def release(self, lease: ResourceLease) -> None:
        ...


T = TypeVar("T")


@dataclass(frozen=True, slots=True)
class LeasedOperationResult(Generic[T]):
    value: T | None = None
    lease: ResourceLease | None = None
    conflicting_resources: tuple[str, ...] = ()
    acquisition_error: str | None = None
    release_error: str | None = None

    @property
    def executed(self) -> bool:
        return self.value is not None


def run_leased_operation(
    *,
    manager: ResourceLeaseManager | None,
    owner_id: str,
    claims: tuple[ResourceClaim, ...],
    on_acquired: Callable[[ResourceLease], None],
    operation: Callable[[], T],
) -> LeasedOperationResult[T]:
    if manager is None or not claims:
        return LeasedOperationResult(value=operation())
    try:
        attempt = manager.acquire(owner_id=owner_id, claims=claims)
    except Exception as exc:
        return LeasedOperationResult(
            acquisition_error=f"resource_lease_error:{type(exc).__name__}:{exc}"
        )
    if not isinstance(attempt, ResourceLeaseAttempt):
        return LeasedOperationResult(
            acquisition_error="resource_lease_error:invalid_acquisition_contract"
        )
    if not attempt.granted or attempt.lease is None:
        return LeasedOperationResult(
            conflicting_resources=attempt.conflicting_resources
        )

    lease = attempt.lease
    value: T | None = None
    release_error: str | None = None
    try:
        on_acquired(lease)
        value = operation()
    finally:
        try:
            manager.release(lease)
        except Exception as exc:
            release_error = (
                f"resource_lease_release_error:{type(exc).__name__}:{exc}"
            )
    return LeasedOperationResult(
        value=value,
        lease=lease,
        release_error=release_error,
    )


def batch_claims(
    nodes: list[TaskNode],
    policy,
) -> tuple[ResourceClaim, ...]:
    by_resource: dict[str, ResourceClaim] = {}
    for node in nodes:
        for claim in policy.claims_for(node):
            existing = by_resource.get(claim.resource)
            if existing is None or claim.mode == "write":
                by_resource[claim.resource] = claim
    return tuple(by_resource[key] for key in sorted(by_resource))


def record_resource_lease(
    graph: TaskGraph,
    nodes: list[TaskNode],
    lease: ResourceLease,
) -> None:
    record_task_event(
        graph,
        "resource_lease_acquired",
        graph.root_id,
        {
            "lease_id": lease.lease_id,
            "nodes": [node.id for node in nodes],
            "resources": [claim.resource for claim in lease.claims],
        },
    )
