"""Schema-v3 persistence for resource leases and fencing counters."""

from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Mapping
from uuid import uuid4

from loop_engine.tasks.parallel import ResourceClaim
from loop_engine.tasks.resource_leases import ResourceLease

RESOURCE_LEASE_SCHEMA_VERSION = 3


@dataclass(frozen=True, slots=True)
class LeaseRecord:
    lease_id: str
    owner_id: str
    owner_pid: int
    process_identity: str
    claims: tuple[ResourceClaim, ...]
    acquired_at: float
    heartbeat_at: float
    expires_at: float
    fencing_tokens: Mapping[str, int]


@dataclass(frozen=True, slots=True)
class LeaseRegistry:
    records: tuple[LeaseRecord, ...]
    fencing_counters: Mapping[str, int]


def load_registry(path: Path) -> LeaseRegistry:
    if not path.exists():
        return LeaseRegistry(records=(), fencing_counters={})
    payload = json.loads(path.read_text(encoding="utf-8"))
    if payload.get("schema_version") != RESOURCE_LEASE_SCHEMA_VERSION:
        raise ValueError("unsupported resource lease schema_version")
    rows = payload.get("leases")
    if not isinstance(rows, list):
        raise ValueError("resource lease rows must be an array")
    raw_counters = payload.get("fencing_counters")
    _validate_counters(raw_counters)
    records: list[LeaseRecord] = []
    seen: set[str] = set()
    for row in rows:
        record = _record_from_dict(dict(row), raw_counters)
        if record.lease_id in seen:
            raise ValueError("duplicate resource lease_id")
        seen.add(record.lease_id)
        records.append(record)
    return LeaseRegistry(
        records=tuple(records),
        fencing_counters=dict(raw_counters),
    )


def save_registry(
    path: Path,
    records: list[LeaseRecord],
    fencing_counters: dict[str, int],
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "schema_version": RESOURCE_LEASE_SCHEMA_VERSION,
        "fencing_counters": dict(sorted(fencing_counters.items())),
        "leases": [
            {
                "lease_id": record.lease_id,
                "owner_id": record.owner_id,
                "owner_pid": record.owner_pid,
                "process_identity": record.process_identity,
                "acquired_at": record.acquired_at,
                "heartbeat_at": record.heartbeat_at,
                "expires_at": record.expires_at,
                "claims": [asdict(claim) for claim in record.claims],
                "fencing_tokens": dict(record.fencing_tokens),
            }
            for record in sorted(records, key=lambda item: item.lease_id)
        ],
    }
    temporary = path.with_name(f".{path.name}.{uuid4().hex}.tmp")
    temporary.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    os.replace(temporary, path)


def _record_from_dict(
    values: dict,
    counters: dict[str, int],
) -> LeaseRecord:
    raw_claims = values.pop("claims")
    raw_tokens = values.pop("fencing_tokens")
    if not isinstance(raw_claims, list) or not isinstance(raw_tokens, dict):
        raise ValueError("resource lease fencing contract is invalid")
    claims = tuple(ResourceClaim(**dict(claim)) for claim in raw_claims)
    write_resources = {
        claim.resource for claim in claims if claim.mode == "write"
    }
    if set(raw_tokens) != write_resources:
        raise ValueError(
            "resource lease fencing_tokens do not match write claims"
        )
    lease = ResourceLease(
        lease_id=str(values["lease_id"]),
        owner_id=str(values["owner_id"]),
        claims=claims,
        expires_at=float(values["expires_at"]),
        fencing_tokens=raw_tokens,
    )
    if any(
        counters.get(resource, 0) < token
        for resource, token in lease.fencing_tokens.items()
    ):
        raise ValueError("resource lease fencing token exceeds persisted counter")
    return LeaseRecord(
        claims=claims,
        fencing_tokens=dict(lease.fencing_tokens),
        **values,
    )


def _validate_counters(value) -> None:
    if not isinstance(value, dict) or any(
        not isinstance(resource, str)
        or not resource
        or not isinstance(token, int)
        or isinstance(token, bool)
        or token <= 0
        for resource, token in value.items()
    ):
        raise ValueError("resource lease fencing_counters are invalid")
