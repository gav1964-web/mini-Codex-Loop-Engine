"""Demonstrate parallel mutation admission for independent workspaces."""

from __future__ import annotations

import json
import tempfile
import threading
import time
from pathlib import Path

from loop_engine.tasks import (
    ChildTaskSpec,
    FunctionIntegrationVerifier,
    FunctionLeafExecutor,
    InMemoryCapabilityResolver,
    LeafExecutionResult,
    ResourceClaim,
    ScriptedTaskDecomposer,
    TaskGraph,
    TaskScheduler,
    TaskSchedulerPolicy,
)


def main() -> None:
    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary)
        workspace_a = root / "a"
        workspace_b = root / "b"
        workspace_a.mkdir()
        workspace_b.mkdir()
        lock = threading.Lock()
        active: set[str] = set()
        overlaps: list[list[str]] = []
        maximum = 0

        def execute(node, graph):
            nonlocal maximum
            with lock:
                overlaps.extend(sorted([node.id, other]) for other in active)
                active.add(node.id)
                maximum = max(maximum, len(active))
            time.sleep(0.05)
            with lock:
                active.remove(node.id)
            return LeafExecutionResult(
                status="completed",
                summary=f"{node.id} completed",
            )

        children = [
            ChildTaskSpec(
                key=key,
                goal=f"Mutate workspace {key}",
                required_capabilities=["workspace.mutate"],
            )
            for key in ["a", "b"]
        ]
        policy = TaskSchedulerPolicy.create(
            max_parallel_leaves=2,
            parallel_safe_capabilities={"workspace.mutate"},
            mutation_capabilities={"workspace.mutate"},
            resource_claims={
                "root.a": [ResourceClaim.workspace(workspace_a, mode="write")],
                "root.b": [ResourceClaim.workspace(workspace_b, mode="write")],
            },
        )
        result = TaskScheduler(
            decomposer=ScriptedTaskDecomposer({"root": children}),
            capability_resolver=InMemoryCapabilityResolver(
                {"workspace.mutate"}
            ),
            leaf_executor=FunctionLeafExecutor(execute),
            integration_verifier=FunctionIntegrationVerifier(),
            policy=policy,
        ).run(TaskGraph.create("Independent workspace mutations"))

        print(
            json.dumps(
                {
                    "status": result.root.status,
                    "maximum_parallel_leaves": maximum,
                    "overlaps": overlaps,
                    "claim_modes": {
                        node_id: [claim.mode for claim in claims]
                        for node_id, claims in policy.resource_claims.items()
                    },
                    "resources_are_distinct": (
                        policy.resource_claims["root.a"][0].resource
                        != policy.resource_claims["root.b"][0].resource
                    ),
                },
                ensure_ascii=False,
                indent=2,
            )
        )


if __name__ == "__main__":
    main()
