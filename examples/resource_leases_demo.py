"""Demonstrate coordination through one persistent resource lease registry."""

from __future__ import annotations

import json
import tempfile
from pathlib import Path

from loop_engine.adapters import (
    FileResourceLeaseManager,
    FileResourceLeasePolicy,
)
from loop_engine.tasks import ResourceClaim


def main() -> None:
    with tempfile.TemporaryDirectory() as temporary:
        registry = Path(temporary) / "resource-leases.json"
        policy = FileResourceLeasePolicy(
            acquire_timeout_seconds=0.05,
            poll_interval_seconds=0.005,
        )
        first = FileResourceLeaseManager(registry, policy=policy)
        second = FileResourceLeaseManager(registry, policy=policy)
        claim = ResourceClaim.create("workspace:shared", mode="write")

        held = first.acquire(owner_id="scheduler-a", claims=(claim,))
        denied = second.acquire(owner_id="scheduler-b", claims=(claim,))
        if held.lease is None:
            raise RuntimeError("first scheduler did not acquire its lease")
        first.release(held.lease)
        acquired_after_release = second.acquire(
            owner_id="scheduler-b",
            claims=(claim,),
        )

        print(
            json.dumps(
                {
                    "first_acquired": held.granted,
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
