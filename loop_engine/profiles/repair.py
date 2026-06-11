"""Deterministic inspect-edit-verify loop for bounded repair experiments."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from ..adapters import (
    BoundedFilesystem,
    BoundedSubprocessTool,
    FunctionPlanner,
    FunctionVerifier,
    SubprocessSpec,
    ToolRegistryExecutor,
)
from ..checkpoint import JsonCheckpointStore
from ..engine import LoopEngine
from ..models import (
    Action,
    Decision,
    Judgement,
    LoopBudget,
    LoopDefinition,
    LoopState,
    Plan,
    VerificationResult,
)


class ScriptedRepairJudge:
    def judge(self, state: LoopState, verification: VerificationResult) -> Judgement:
        if verification.status == "passed":
            return Judgement(
                decision=Decision.COMPLETE,
                reason="repair passed verification",
                progress_signals=list(verification.passed),
            )
        patch_count = len(state.definition.metadata.get("patches", []))
        if verification.status == "failed" and state.iteration < patch_count:
            return Judgement(
                decision=Decision.REPLAN,
                reason="verification failed; try next bounded patch",
                next_focus=verification.failed[0] if verification.failed else "next patch",
            )
        return Judgement(
            decision=Decision.STOP,
            reason="repair attempts exhausted or blocked",
            next_focus=verification.failed[0] if verification.failed else None,
        )


def verify_repair_results(
    state: LoopState,
    results: list,
) -> VerificationResult:
    errors = [
        f"{result.action.tool}: {result.error}"
        for result in results
        if result.status != "ok"
    ]
    if errors:
        return VerificationResult(status="blocked", failed=errors)
    process_results = [
        result.output for result in results if result.action.tool == "run_verification"
    ]
    if not process_results:
        return VerificationResult(
            status="incomplete",
            failed=["verification has not been run yet"],
            evidence={
                "completed_tools": [result.action.tool for result in results],
            },
        )
    process = process_results[-1]
    if process.get("timed_out"):
        return VerificationResult(
            status="blocked",
            failed=["verification command timed out"],
            evidence=process,
        )
    exit_code = process.get("exit_code")
    return VerificationResult(
        status="passed" if exit_code == 0 else "failed",
        passed=["repair verification exited with code 0"] if exit_code == 0 else [],
        failed=[] if exit_code == 0 else [f"repair verification exited with code {exit_code}"],
        evidence=process,
    )


def build_scripted_repair_loop(
    *,
    workspace_root: str | Path,
    patches: list[dict[str, Any]],
    verification_command: list[str],
    timeout_seconds: float = 60.0,
    max_output_bytes: int = 64 * 1024,
    checkpoint_root: str | Path | None = None,
) -> tuple[LoopEngine, LoopDefinition]:
    if not patches:
        raise ValueError("at least one patch is required")

    executor = ToolRegistryExecutor()
    filesystem = BoundedFilesystem(workspace_root)
    filesystem.register(executor)
    executor.register(
        "run_verification",
        BoundedSubprocessTool(
            workspace_root,
            SubprocessSpec(
                argv=tuple(verification_command),
                timeout_seconds=timeout_seconds,
                max_output_bytes=max_output_bytes,
            ),
        ),
    )

    def plan(state: LoopState) -> Plan:
        patch = patches[state.iteration - 1]
        path = str(patch["path"])
        actions = []
        if state.iteration == 1:
            actions.extend(
                [
                    Action(
                        tool="list_files",
                        arguments={"path": ".", "recursive": True},
                        reason="inspect workspace structure",
                    ),
                    Action(
                        tool="read_text",
                        arguments={"path": path},
                        reason="inspect target file before editing",
                    ),
                    Action(
                        tool="search_text",
                        arguments={"path": ".", "query": str(patch["old_text"])},
                        reason="locate the exact repair target",
                    ),
                ]
            )
        actions.extend(
            [
                Action(
                    tool="apply_patch",
                    arguments=dict(patch),
                    reason="apply one bounded exact-text repair",
                ),
                Action(
                    tool="run_verification",
                    reason="verify the repaired workspace",
                ),
            ]
        )
        return Plan(
            actions=actions,
            rationale=f"inspect, apply repair attempt {state.iteration}, and verify",
            expected_evidence=["target file evidence", "patch result", "verification exit code"],
        )

    store = JsonCheckpointStore(checkpoint_root) if checkpoint_root else None
    engine = LoopEngine(
        planner=FunctionPlanner(plan),
        executor=executor,
        verifier=FunctionVerifier(verify_repair_results),
        judge=ScriptedRepairJudge(),
        checkpoint_store=store,
    )
    definition = LoopDefinition(
        goal="Inspect, repair, and verify a workspace with bounded tools",
        success_criteria=["verification command exits with code 0 after a patch"],
        constraints=[
            "all filesystem paths remain inside workspace root",
            "patches replace exact text in existing UTF-8 files",
            "each repair attempt is bounded and checkpointed",
            "verification runs through the process-tree supervisor",
        ],
        budget=LoopBudget(
            max_iterations=len(patches),
            max_actions=5 + max(0, len(patches) - 1) * 2,
            timeout_seconds=max((timeout_seconds + 10) * len(patches), 30),
        ),
        metadata={
            "profile": "scripted_repair",
            "workspace_root": str(Path(workspace_root).resolve()),
            "patches": patches,
            "command": list(verification_command),
            "subprocess_timeout_seconds": timeout_seconds,
            "max_output_bytes": max_output_bytes,
        },
    )
    return engine, definition
