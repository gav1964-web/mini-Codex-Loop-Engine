"""Compare two deterministic decomposition strategies on one task case."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from loop_engine.tasks import (
    ChildTaskSpec,
    DecompositionStrategyRunner,
    FunctionIntegrationVerifier,
    FunctionLeafExecutor,
    InMemoryCapabilityResolver,
    LeafExecutionResult,
    ReplayTaskCase,
    ScriptedTaskDecomposer,
    TaskScheduler,
)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--output",
        type=Path,
        default=Path("build/decomposition_comparison.json"),
    )
    return parser


def _scheduler(decomposer) -> TaskScheduler:
    return TaskScheduler(
        decomposer=decomposer,
        capability_resolver=InMemoryCapabilityResolver({"work"}),
        leaf_executor=FunctionLeafExecutor(
            lambda node, graph: LeafExecutionResult(
                status="completed",
                summary=f"{node.id} completed",
                evidence={"node": node.id},
            )
        ),
        integration_verifier=FunctionIntegrationVerifier(),
    )


def _staged() -> ScriptedTaskDecomposer:
    return ScriptedTaskDecomposer(
        {
            "root": [
                ChildTaskSpec(
                    key="inspect",
                    goal="Inspect target",
                    required_capabilities=["work"],
                ),
                ChildTaskSpec(
                    key="apply",
                    goal="Apply bounded change",
                    required_capabilities=["work"],
                    depends_on=["inspect"],
                ),
                ChildTaskSpec(
                    key="verify",
                    goal="Verify integrated result",
                    required_capabilities=["work"],
                    depends_on=["apply"],
                ),
            ]
        }
    )


def main() -> int:
    args = _parser().parse_args()
    comparison = DecompositionStrategyRunner(_scheduler).compare(
        ReplayTaskCase(
            name="inspect-apply-verify",
            goal="Inspect, change, and verify target",
            required_capabilities=("work",),
        ),
        {
            "atomic": lambda: ScriptedTaskDecomposer({}),
            "staged": _staged,
        },
    )
    comparison.save(args.output)
    print(json.dumps(comparison.to_dict(), ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
