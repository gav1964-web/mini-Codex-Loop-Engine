"""Cross-process resource leases backed by an atomic JSON registry."""

from __future__ import annotations

import os
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable
from uuid import uuid4

from loop_engine.tasks.parallel import ResourceClaim
from loop_engine.tasks.resource_leases import (
    ResourceLease,
    ResourceLeaseAttempt,
)

from .subprocesses import lookup_process_identity
from .directory_lock import DirectoryLock
from .resource_lease_registry import (
    LeaseRecord,
    LeaseRegistry,
    load_registry,
    save_registry,
)


@dataclass(frozen=True, slots=True)
class FileResourceLeasePolicy:
    acquire_timeout_seconds: float = 1.0
    poll_interval_seconds: float = 0.02
    stale_lock_seconds: float = 30.0
    lease_ttl_seconds: float = 30.0
    heartbeat_interval_seconds: float = 5.0

    def __post_init__(self) -> None:
        if (
            self.acquire_timeout_seconds <= 0
            or self.poll_interval_seconds <= 0
            or self.stale_lock_seconds <= 0
            or self.lease_ttl_seconds <= 0
            or self.heartbeat_interval_seconds <= 0
        ):
            raise ValueError("resource lease policy bounds must be positive")
        if self.heartbeat_interval_seconds >= self.lease_ttl_seconds:
            raise ValueError(
                "resource lease heartbeat interval must be less than TTL"
            )


class FileResourceLeaseManager:
    """Coordinate resource claims between scheduler processes."""

    def __init__(
        self,
        storage_path: str | Path,
        *,
        policy: FileResourceLeasePolicy | None = None,
        clock: Callable[[], float] = time.time,
        sleep: Callable[[float], None] = time.sleep,
        pid: int | None = None,
        identity_lookup: Callable[[int], str | None] = lookup_process_identity,
    ) -> None:
        self.storage_path = Path(storage_path).resolve()
        self.policy = policy or FileResourceLeasePolicy()
        self.clock = clock
        self.sleep = sleep
        self.pid = pid or os.getpid()
        self.identity_lookup = identity_lookup
        identity = self.identity_lookup(self.pid)
        if not identity:
            raise RuntimeError("resource lease owner identity is unavailable")
        self.process_identity = identity
        self.lock_path = self.storage_path.with_name(
            f".{self.storage_path.name}.lock"
        )

    @property
    def heartbeat_interval_seconds(self) -> float:
        return self.policy.heartbeat_interval_seconds

    def acquire(
        self,
        *,
        owner_id: str,
        claims: tuple[ResourceClaim, ...],
    ) -> ResourceLeaseAttempt:
        owner = owner_id.strip()
        if not owner:
            raise ValueError("resource lease owner_id is required")
        normalized = _normalize_claims(claims)
        now = self.clock()
        if not normalized:
            return ResourceLeaseAttempt(
                lease=ResourceLease(
                    lease_id=uuid4().hex,
                    owner_id=owner,
                    claims=(),
                    expires_at=now + self.policy.lease_ttl_seconds,
                )
            )

        deadline = now + self.policy.acquire_timeout_seconds
        while True:
            with self._registry_lock(deadline):
                registry = self._load()
                live = self._live_records(list(registry.records))
                conflicts = _conflicting_resources(normalized, live)
                if not conflicts:
                    counters = dict(registry.fencing_counters)
                    fencing_tokens = _next_fencing_tokens(
                        normalized,
                        counters,
                    )
                    lease = ResourceLease(
                        lease_id=uuid4().hex,
                        owner_id=owner,
                        claims=normalized,
                        expires_at=self.clock() + self.policy.lease_ttl_seconds,
                        fencing_tokens=fencing_tokens,
                    )
                    live.append(
                        LeaseRecord(
                            lease_id=lease.lease_id,
                            owner_id=owner,
                            owner_pid=self.pid,
                            process_identity=self.process_identity,
                            claims=normalized,
                            acquired_at=self.clock(),
                            heartbeat_at=self.clock(),
                            expires_at=lease.expires_at,
                            fencing_tokens=fencing_tokens,
                        )
                    )
                    self._save(live, counters)
                    return ResourceLeaseAttempt(lease=lease)
                if live != list(registry.records):
                    self._save(live, dict(registry.fencing_counters))
            if self.clock() >= deadline:
                return ResourceLeaseAttempt(
                    lease=None,
                    conflicting_resources=conflicts,
                )
            self.sleep(
                min(
                    self.policy.poll_interval_seconds,
                    max(0.0, deadline - self.clock()),
                )
            )

    def renew(self, lease: ResourceLease) -> ResourceLease:
        self._validate_lease(lease)
        deadline = self.clock() + self.policy.acquire_timeout_seconds
        with self._registry_lock(deadline):
            registry = self._load()
            records = list(registry.records)
            now = self.clock()
            match = self._owned_record(
                records,
                lease,
                require_live_at=now,
            )
            renewed = ResourceLease(
                lease_id=lease.lease_id,
                owner_id=lease.owner_id,
                claims=lease.claims,
                expires_at=now + self.policy.lease_ttl_seconds,
                fencing_tokens=lease.fencing_tokens,
            )
            self._save(
                [
                    (
                        LeaseRecord(
                            lease_id=record.lease_id,
                            owner_id=record.owner_id,
                            owner_pid=record.owner_pid,
                            process_identity=record.process_identity,
                            claims=record.claims,
                            acquired_at=record.acquired_at,
                            heartbeat_at=now,
                            expires_at=renewed.expires_at,
                            fencing_tokens=record.fencing_tokens,
                        )
                        if record.lease_id == match.lease_id
                        else record
                    )
                    for record in records
                ],
                dict(registry.fencing_counters),
            )
            return renewed

    def release(self, lease: ResourceLease) -> None:
        self._validate_lease(lease)
        deadline = self.clock() + self.policy.acquire_timeout_seconds
        with self._registry_lock(deadline):
            registry = self._load()
            records = list(registry.records)
            try:
                self._owned_record(records, lease)
            except KeyError:
                return
            self._save(
                [
                    record
                    for record in records
                    if record.lease_id != lease.lease_id
                ],
                dict(registry.fencing_counters),
            )

    def _live_records(self, records: list[LeaseRecord]) -> list[LeaseRecord]:
        now = self.clock()
        return [
            record
            for record in records
            if record.expires_at > now
            if self.identity_lookup(record.owner_pid) == record.process_identity
        ]

    def _owned_record(
        self,
        records: list[LeaseRecord],
        lease: ResourceLease,
        *,
        require_live_at: float | None = None,
    ) -> LeaseRecord:
        match = next(
            (record for record in records if record.lease_id == lease.lease_id),
            None,
        )
        if match is None:
            raise KeyError(f"resource lease no longer exists: {lease.lease_id}")
        if (
            match.owner_id != lease.owner_id
            or match.owner_pid != self.pid
            or match.process_identity != self.process_identity
            or match.claims != lease.claims
            or dict(match.fencing_tokens) != dict(lease.fencing_tokens)
        ):
            raise ValueError("resource lease is not owned by this manager")
        if require_live_at is not None and match.expires_at <= require_live_at:
            raise RuntimeError("resource lease already expired")
        return match

    @staticmethod
    def _validate_lease(lease: ResourceLease) -> None:
        if not isinstance(lease, ResourceLease):
            raise TypeError("resource lease contract is required")

    def _load(self) -> LeaseRegistry:
        return load_registry(self.storage_path)

    def _save(
        self,
        records: list[LeaseRecord],
        fencing_counters: dict[str, int],
    ) -> None:
        save_registry(self.storage_path, records, fencing_counters)

    def _registry_lock(self, deadline: float) -> DirectoryLock:
        return DirectoryLock(
            self.lock_path,
            deadline=deadline,
            stale_after_seconds=self.policy.stale_lock_seconds,
            clock=self.clock,
            sleep=self.sleep,
            poll_interval_seconds=self.policy.poll_interval_seconds,
        )

def _normalize_claims(
    claims: tuple[ResourceClaim, ...],
) -> tuple[ResourceClaim, ...]:
    if any(not isinstance(claim, ResourceClaim) for claim in claims):
        raise TypeError("resource leases require ResourceClaim values")
    by_resource: dict[str, ResourceClaim] = {}
    for claim in claims:
        existing = by_resource.get(claim.resource)
        if existing is None or claim.mode == "write":
            by_resource[claim.resource] = claim
    return tuple(by_resource[key] for key in sorted(by_resource))


def _conflicting_resources(
    requested: tuple[ResourceClaim, ...],
    records: list[LeaseRecord],
) -> tuple[str, ...]:
    occupied: dict[str, set[str]] = {}
    for record in records:
        for claim in record.claims:
            occupied.setdefault(claim.resource, set()).add(claim.mode)
    return tuple(
        claim.resource
        for claim in requested
        if claim.resource in occupied
        and (claim.mode == "write" or "write" in occupied[claim.resource])
    )


def _next_fencing_tokens(
    claims: tuple[ResourceClaim, ...],
    counters: dict[str, int],
) -> dict[str, int]:
    tokens: dict[str, int] = {}
    for claim in claims:
        if claim.mode != "write":
            continue
        token = counters.get(claim.resource, 0) + 1
        counters[claim.resource] = token
        tokens[claim.resource] = token
    return tokens
