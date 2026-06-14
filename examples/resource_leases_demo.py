"""Demonstrate coordination through one persistent resource lease registry."""

from __future__ import annotations

import json
import tempfile
import threading
import time
from pathlib import Path

from loop_engine.adapters import (
    FileResourceLeaseManager,
    FileResourceLeasePolicy,
)
from loop_engine.tasks import ResourceClaim
from loop_engine.tasks.resource_leases import (
    run_fenced_operation,
    run_leased_operation,
)


class DemoFencedAdapter:
    def __init__(self) -> None:
        self.highest_token = 0

    def execute_fenced(
        self,
        *,
        resource: str,
        fencing_token: int,
        operation,
    ):
        if fencing_token < self.highest_token:
            raise RuntimeError("stale_fencing_token")
        self.highest_token = fencing_token
        return operation()


def main() -> None:
    with tempfile.TemporaryDirectory() as temporary:
        registry = Path(temporary) / "resource-leases.json"
        policy = FileResourceLeasePolicy(
            acquire_timeout_seconds=0.08,
            poll_interval_seconds=0.005,
            lease_ttl_seconds=0.4,
            heartbeat_interval_seconds=0.2,
        )
        first = FileResourceLeaseManager(registry, policy=policy)
        second = FileResourceLeaseManager(registry, policy=policy)
        claim = ResourceClaim.create("workspace:shared", mode="write")

        started = threading.Event()
        outcomes = []

        def operation():
            started.set()
            time.sleep(0.7)
            return "completed"

        worker = threading.Thread(
            target=lambda: outcomes.append(
                run_leased_operation(
                    manager=first,
                    owner_id="scheduler-a",
                    claims=(claim,),
                    on_acquired=lambda lease: None,
                    operation=operation,
                )
            )
        )
        worker.start()
        if not started.wait(timeout=1):
            raise RuntimeError("leased operation did not start")
        time.sleep(0.03)
        denied = second.acquire(owner_id="scheduler-b", claims=(claim,))
        worker.join(timeout=2)
        if worker.is_alive() or not outcomes:
            raise RuntimeError("leased operation did not finish")
        acquired_after_release = second.acquire(
            owner_id="scheduler-b",
            claims=(claim,),
        )
        if outcomes[0].lease is None or acquired_after_release.lease is None:
            raise RuntimeError("expected write leases with fencing tokens")
        adapter = DemoFencedAdapter()
        side_effects: list[str] = []
        run_fenced_operation(
            lease=acquired_after_release.lease,
            resource=claim.resource,
            adapter=adapter,
            operation=lambda: side_effects.append("new-owner"),
        )
        stale_rejected = False
        try:
            run_fenced_operation(
                lease=outcomes[0].lease,
                resource=claim.resource,
                adapter=adapter,
                operation=lambda: side_effects.append("stale-owner"),
            )
        except RuntimeError as exc:
            stale_rejected = str(exc) == "stale_fencing_token"

        print(
            json.dumps(
                {
                    "operation_exceeded_initial_ttl": True,
                    "heartbeat_error": outcomes[0].heartbeat_error,
                    "second_blocked_while_held": not denied.granted,
                    "conflicting_resources": denied.conflicting_resources,
                    "second_acquired_after_release": (
                        acquired_after_release.granted
                    ),
                    "old_fencing_token": outcomes[0].lease.fencing_token(
                        claim.resource
                    ),
                    "new_fencing_token": (
                        acquired_after_release.lease.fencing_token(
                            claim.resource
                        )
                    ),
                    "stale_side_effect_rejected": stale_rejected,
                    "side_effects": side_effects,
                    "registry": str(registry),
                },
                ensure_ascii=False,
                indent=2,
            )
        )


if __name__ == "__main__":
    main()
