from __future__ import annotations

import json
import threading

import pytest

from loop_engine.adapters import (
    FileResourceLeaseManager,
    FileResourceLeasePolicy,
)
from loop_engine.tasks import ResourceClaim, ResourceLease, run_fenced_operation


def _managers(path):
    now = [100.0]
    identities = {100: "process-100", 200: "process-200"}
    policy = FileResourceLeasePolicy(
        acquire_timeout_seconds=0.02,
        poll_interval_seconds=0.001,
        stale_lock_seconds=1,
        lease_ttl_seconds=2,
        heartbeat_interval_seconds=0.5,
    )

    def create(pid):
        return FileResourceLeaseManager(
            path,
            policy=policy,
            clock=lambda: now[0],
            sleep=lambda seconds: now.__setitem__(0, now[0] + seconds),
            pid=pid,
            identity_lookup=identities.get,
        )

    return now, create(100), create(200)


class AtomicFencedAdapter:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._highest: dict[str, int] = {}

    def execute_fenced(
        self,
        *,
        resource: str,
        fencing_token: int,
        operation,
    ):
        with self._lock:
            highest = self._highest.get(resource, 0)
            if fencing_token < highest:
                raise RuntimeError(
                    f"stale_fencing_token:{resource}:{fencing_token}<{highest}"
                )
            self._highest[resource] = fencing_token
            return operation()


def test_write_tokens_increase_across_release_and_reacquire(tmp_path) -> None:
    path = tmp_path / "leases.json"
    _, first, second = _managers(path)
    claim = ResourceClaim.create("workspace:shared", mode="write")

    first_attempt = first.acquire(owner_id="first", claims=(claim,))
    assert first_attempt.lease is not None
    first.release(first_attempt.lease)
    second_attempt = second.acquire(owner_id="second", claims=(claim,))
    assert second_attempt.lease is not None

    assert first_attempt.lease.fencing_token(claim.resource) == 1
    assert second_attempt.lease.fencing_token(claim.resource) == 2
    payload = json.loads(path.read_text(encoding="utf-8"))
    assert payload["schema_version"] == 3
    assert payload["fencing_counters"] == {"workspace:shared": 2}


def test_expiry_recovery_issues_new_token_while_old_owner_is_alive(
    tmp_path,
) -> None:
    now, first, second = _managers(tmp_path / "leases.json")
    claim = ResourceClaim.create("workspace:shared", mode="write")
    old = first.acquire(owner_id="first", claims=(claim,))
    assert old.lease is not None
    now[0] += 3

    new = second.acquire(owner_id="second", claims=(claim,))

    assert new.lease is not None
    assert old.lease.fencing_token(claim.resource) == 1
    assert new.lease.fencing_token(claim.resource) == 2


def test_renew_preserves_token_and_independent_resources_have_counters(
    tmp_path,
) -> None:
    now, owner, _ = _managers(tmp_path / "leases.json")
    claims = (
        ResourceClaim.create("workspace:a", mode="write"),
        ResourceClaim.create("workspace:b", mode="write"),
    )
    attempt = owner.acquire(owner_id="owner", claims=claims)
    assert attempt.lease is not None
    now[0] += 1

    renewed = owner.renew(attempt.lease)

    assert dict(renewed.fencing_tokens) == {
        "workspace:a": 1,
        "workspace:b": 1,
    }


def test_read_claim_has_no_fencing_token(tmp_path) -> None:
    _, owner, _ = _managers(tmp_path / "leases.json")
    claim = ResourceClaim.create("workspace:shared", mode="read")
    attempt = owner.acquire(owner_id="reader", claims=(claim,))
    assert attempt.lease is not None

    assert dict(attempt.lease.fencing_tokens) == {}
    with pytest.raises(ValueError, match="no fencing token"):
        attempt.lease.fencing_token(claim.resource)


def test_adapter_rejects_stale_owner_after_new_token_is_observed(
    tmp_path,
) -> None:
    path = tmp_path / "leases.json"
    _, first, second = _managers(path)
    claim = ResourceClaim.create("workspace:shared", mode="write")
    old = first.acquire(owner_id="first", claims=(claim,))
    assert old.lease is not None
    first.release(old.lease)
    new = second.acquire(owner_id="second", claims=(claim,))
    assert new.lease is not None
    adapter = AtomicFencedAdapter()
    values: list[str] = []

    run_fenced_operation(
        lease=new.lease,
        resource=claim.resource,
        adapter=adapter,
        operation=lambda: values.append("new"),
    )

    with pytest.raises(RuntimeError, match="stale_fencing_token"):
        run_fenced_operation(
            lease=old.lease,
            resource=claim.resource,
            adapter=adapter,
            operation=lambda: values.append("old"),
        )
    assert values == ["new"]


def test_fencing_contract_rejects_invalid_or_unclaimed_tokens() -> None:
    write = ResourceClaim.create("workspace:a", mode="write")
    read = ResourceClaim.create("workspace:b", mode="read")
    with pytest.raises(ValueError, match="matching write claims"):
        ResourceLease(
            lease_id="lease",
            owner_id="owner",
            claims=(read,),
            expires_at=1,
            fencing_tokens={"workspace:b": 1},
        )
    with pytest.raises(ValueError, match="positive integers"):
        ResourceLease(
            lease_id="lease",
            owner_id="owner",
            claims=(write,),
            expires_at=1,
            fencing_tokens={"workspace:a": 0},
        )


def test_registry_rejects_tampered_fencing_state(tmp_path) -> None:
    path = tmp_path / "leases.json"
    _, owner, _ = _managers(path)
    claim = ResourceClaim.create("workspace:a", mode="write")
    owner.acquire(owner_id="owner", claims=(claim,))
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["fencing_counters"]["workspace:a"] = 1
    payload["leases"][0]["fencing_tokens"]["workspace:a"] = 2
    path.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(ValueError, match="exceeds persisted counter"):
        owner.acquire(
            owner_id="next",
            claims=(ResourceClaim.create("workspace:b", mode="write"),),
        )


def test_schema_v2_registry_fails_closed_instead_of_resetting_counters(
    tmp_path,
) -> None:
    path = tmp_path / "leases.json"
    path.write_text(
        json.dumps({"schema_version": 2, "leases": []}),
        encoding="utf-8",
    )
    _, owner, _ = _managers(path)

    with pytest.raises(ValueError, match="schema_version"):
        owner.acquire(
            owner_id="owner",
            claims=(ResourceClaim.create("workspace:a", mode="write"),),
        )
