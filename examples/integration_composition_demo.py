"""Run two mandatory parent integration checks through one composite policy."""

from __future__ import annotations

import json

from loop_engine.tasks import (
    ChildTaskSpec,
    CompositeIntegrationVerifier,
    FunctionIntegrationVerifier,
    FunctionLeafExecutor,
    InMemoryCapabilityResolver,
    IntegrationCompositionPolicy,
    IntegrationPlan,
    IntegrationRoute,
    IntegrationSelector,
    LeafExecutionResult,
    ScriptedTaskDecomposer,
    TaskGraph,
    TaskScheduler,
)


def _check(name: str) -> FunctionIntegrationVerifier:
    return FunctionIntegrationVerifier(
        lambda node, graph: LeafExecutionResult(
            status="completed",
            summary=f"{name} passed",
            evidence={
                "check": name,
                "completed_children": list(node.children),
            },
        )
    )


def main() -> int:
    verifier = CompositeIntegrationVerifier(
        {
            "contract": _check("contract"),
            "system": _check("system"),
        },
        IntegrationCompositionPolicy.create(
            selector_routes=[
                IntegrationRoute(
                    "root-depth",
                    IntegrationSelector.depth(0),
                    IntegrationPlan.create(["contract", "system"]),
                )
            ]
        ),
    )
    result = TaskScheduler(
        decomposer=ScriptedTaskDecomposer(
            {
                "root": [
                    ChildTaskSpec(key="build", goal="Build"),
                    ChildTaskSpec(
                        key="verify",
                        goal="Verify",
                        depends_on=["build"],
                    ),
                ]
            }
        ),
        capability_resolver=InMemoryCapabilityResolver(),
        leaf_executor=FunctionLeafExecutor(
            lambda node, graph: LeafExecutionResult(
                status="completed",
                summary=f"{node.id} completed",
                evidence={"node": node.id},
            )
        ),
        integration_verifier=verifier,
    ).run(TaskGraph.create("Build and verify"))
    print(
        json.dumps(
            {
                "status": str(result.root.status),
                "error": result.root.error,
                "evidence": (
                    result.root.result.evidence
                    if result.root.result is not None
                    else {}
                ),
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0 if str(result.root.status) == "completed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
