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
from loop_engine.tasks.resource_leases import run_leased_operation


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
                    "registry": str(registry),
                },
                ensure_ascii=False,
                indent=2,
            )
        )


if __name__ == "__main__":
    main()
