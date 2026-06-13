"""Cross-process resource leases backed by an atomic JSON registry."""

from __future__ import annotations

import json
import os
import shutil
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Callable
from uuid import uuid4

from loop_engine.tasks.parallel import ResourceClaim
from loop_engine.tasks.resource_leases import (
    ResourceLease,
    ResourceLeaseAttempt,
)

from .subprocesses import lookup_process_identity

RESOURCE_LEASE_SCHEMA_VERSION = 2


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


@dataclass(frozen=True, slots=True)
class _LeaseRecord:
    lease_id: str
    owner_id: str
    owner_pid: int
    process_identity: str
    claims: tuple[ResourceClaim, ...]
    acquired_at: float
    heartbeat_at: float
    expires_at: float


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
                records = self._load()
                live = self._live_records(records)
                conflicts = _conflicting_resources(normalized, live)
                if not conflicts:
                    lease = ResourceLease(
                        lease_id=uuid4().hex,
                        owner_id=owner,
                        claims=normalized,
                        expires_at=self.clock() + self.policy.lease_ttl_seconds,
                    )
                    live.append(
                        _LeaseRecord(
                            lease_id=lease.lease_id,
                            owner_id=owner,
                            owner_pid=self.pid,
                            process_identity=self.process_identity,
                            claims=normalized,
                            acquired_at=self.clock(),
                            heartbeat_at=self.clock(),
                            expires_at=lease.expires_at,
                        )
                    )
                    self._save(live)
                    return ResourceLeaseAttempt(lease=lease)
                if live != records:
                    self._save(live)
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
            records = self._load()
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
            )
            self._save(
                [
                    (
                        _LeaseRecord(
                            lease_id=record.lease_id,
                            owner_id=record.owner_id,
                            owner_pid=record.owner_pid,
                            process_identity=record.process_identity,
                            claims=record.claims,
                            acquired_at=record.acquired_at,
                            heartbeat_at=now,
                            expires_at=renewed.expires_at,
                        )
                        if record.lease_id == match.lease_id
                        else record
                    )
                    for record in records
                ]
            )
            return renewed

    def release(self, lease: ResourceLease) -> None:
        self._validate_lease(lease)
        deadline = self.clock() + self.policy.acquire_timeout_seconds
        with self._registry_lock(deadline):
            records = self._load()
            try:
                self._owned_record(records, lease)
            except KeyError:
                return
            self._save(
                [
                    record
                    for record in records
                    if record.lease_id != lease.lease_id
                ]
            )

    def _live_records(self, records: list[_LeaseRecord]) -> list[_LeaseRecord]:
        now = self.clock()
        return [
            record
            for record in records
            if record.expires_at > now
            if self.identity_lookup(record.owner_pid) == record.process_identity
        ]

    def _owned_record(
        self,
        records: list[_LeaseRecord],
        lease: ResourceLease,
        *,
        require_live_at: float | None = None,
    ) -> _LeaseRecord:
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
        ):
            raise ValueError("resource lease is not owned by this manager")
        if require_live_at is not None and match.expires_at <= require_live_at:
            raise RuntimeError("resource lease already expired")
        return match

    @staticmethod
    def _validate_lease(lease: ResourceLease) -> None:
        if not isinstance(lease, ResourceLease):
            raise TypeError("resource lease contract is required")

    def _load(self) -> list[_LeaseRecord]:
        if not self.storage_path.exists():
            return []
        payload = json.loads(self.storage_path.read_text(encoding="utf-8"))
        if payload.get("schema_version") != RESOURCE_LEASE_SCHEMA_VERSION:
            raise ValueError("unsupported resource lease schema_version")
        rows = payload.get("leases")
        if not isinstance(rows, list):
            raise ValueError("resource lease rows must be an array")
        records: list[_LeaseRecord] = []
        seen: set[str] = set()
        for row in rows:
            values = dict(row)
            raw_claims = values.pop("claims")
            record = _LeaseRecord(
                claims=tuple(ResourceClaim(**dict(claim)) for claim in raw_claims),
                **values,
            )
            if record.lease_id in seen:
                raise ValueError("duplicate resource lease_id")
            seen.add(record.lease_id)
            records.append(record)
        return records

    def _save(self, records: list[_LeaseRecord]) -> None:
        self.storage_path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "schema_version": RESOURCE_LEASE_SCHEMA_VERSION,
            "leases": [
                {
                    **asdict(record),
                    "claims": [asdict(claim) for claim in record.claims],
                }
                for record in sorted(records, key=lambda item: item.lease_id)
            ],
        }
        temporary = self.storage_path.with_name(
            f".{self.storage_path.name}.{uuid4().hex}.tmp"
        )
        temporary.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        os.replace(temporary, self.storage_path)

    def _registry_lock(self, deadline: float) -> _DirectoryLock:
        return _DirectoryLock(
            self.lock_path,
            deadline=deadline,
            stale_after_seconds=self.policy.stale_lock_seconds,
            clock=self.clock,
            sleep=self.sleep,
            poll_interval_seconds=self.policy.poll_interval_seconds,
        )


class _DirectoryLock:
    def __init__(
        self,
        path: Path,
        *,
        deadline: float,
        stale_after_seconds: float,
        clock: Callable[[], float],
        sleep: Callable[[float], None],
        poll_interval_seconds: float,
    ) -> None:
        self.path = path
        self.deadline = deadline
        self.stale_after_seconds = stale_after_seconds
        self.clock = clock
        self.sleep = sleep
        self.poll_interval_seconds = poll_interval_seconds
        self.acquired = False

    def __enter__(self) -> _DirectoryLock:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        while True:
            try:
                self.path.mkdir()
                (self.path / "created_at").write_text(
                    str(self.clock()),
                    encoding="ascii",
                )
                self.acquired = True
                return self
            except FileExistsError:
                self._reclaim_if_stale()
                if self.clock() >= self.deadline:
                    raise TimeoutError("resource lease registry lock timed out")
                self.sleep(
                    min(
                        self.poll_interval_seconds,
                        max(0.0, self.deadline - self.clock()),
                    )
                )

    def __exit__(self, exc_type, exc, traceback) -> None:
        if self.acquired:
            shutil.rmtree(self.path, ignore_errors=True)
            self.acquired = False

    def _reclaim_if_stale(self) -> None:
        try:
            fallback_created_at = self.path.stat().st_mtime
        except (FileNotFoundError, OSError):
            return
        try:
            created_at = float(
                (self.path / "created_at").read_text(encoding="ascii")
            )
        except (FileNotFoundError, OSError, ValueError):
            created_at = fallback_created_at
        if self.clock() - created_at <= self.stale_after_seconds:
            return
        tombstone = self.path.with_name(f"{self.path.name}.{uuid4().hex}.stale")
        try:
            self.path.rename(tombstone)
        except (FileNotFoundError, FileExistsError, PermissionError, OSError):
            return
        shutil.rmtree(tombstone, ignore_errors=True)


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
    records: list[_LeaseRecord],
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
