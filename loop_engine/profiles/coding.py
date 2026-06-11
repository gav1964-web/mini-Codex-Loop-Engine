"""Minimal coding profile built entirely from public loop contracts."""

from __future__ import annotations

from pathlib import Path

from ..adapters import (
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


class CodingCheckJudge:
    def judge(self, state: LoopState, verification: VerificationResult) -> Judgement:
        if verification.status == "passed":
            return Judgement(
                decision=Decision.COMPLETE,
                reason="verification command passed",
                progress_signals=list(verification.passed),
            )
        return Judgement(
            decision=Decision.STOP,
            reason="verification command did not pass",
            progress_signals=list(verification.passed),
            next_focus=verification.failed[0] if verification.failed else None,
        )


def build_coding_check_loop(
    *,
    workspace_root: str | Path,
    command: list[str],
    timeout_seconds: float = 60.0,
    max_output_bytes: int = 64 * 1024,
    checkpoint_root: str | Path | None = None,
) -> tuple[LoopEngine, LoopDefinition]:
    executor = ToolRegistryExecutor()
    executor.register(
        "run_verification",
        BoundedSubprocessTool(
            workspace_root,
            SubprocessSpec(
                argv=tuple(command),
                timeout_seconds=timeout_seconds,
                max_output_bytes=max_output_bytes,
            ),
        ),
    )

    def plan(state: LoopState) -> Plan:
        return Plan(
            actions=[
                Action(
                    tool="run_verification",
                    reason="collect objective coding verification evidence",
                )
            ],
            rationale="run the configured verification command once",
            expected_evidence=["process exit code", "bounded stdout", "bounded stderr"],
        )

    def verify(state: LoopState, results) -> VerificationResult:
        if not results or results[0].status != "ok":
            error = results[0].error if results else "verification action produced no result"
            return VerificationResult(status="blocked", failed=[error or "tool error"])
        output = results[0].output
        if output.get("timed_out"):
            return VerificationResult(
                status="blocked",
                failed=["verification command timed out"],
                evidence=output,
            )
        exit_code = output.get("exit_code")
        return VerificationResult(
            status="passed" if exit_code == 0 else "failed",
            passed=["verification command exited with code 0"] if exit_code == 0 else [],
            failed=[] if exit_code == 0 else [f"verification command exited with code {exit_code}"],
            evidence=output,
        )

    store = JsonCheckpointStore(checkpoint_root) if checkpoint_root else None
    engine = LoopEngine(
        planner=FunctionPlanner(plan),
        executor=executor,
        verifier=FunctionVerifier(verify),
        judge=CodingCheckJudge(),
        checkpoint_store=store,
    )
    definition = LoopDefinition(
        goal="Run a bounded coding verification command and evaluate its evidence",
        success_criteria=["verification command exits with code 0"],
        constraints=[
            "command is immutable after loop construction",
            "working directory remains inside workspace root",
            "process tree is terminated on timeout",
            "captured output is bounded",
        ],
        budget=LoopBudget(
            max_iterations=1,
            max_actions=1,
            timeout_seconds=max(timeout_seconds + 10, 15),
        ),
        metadata={
            "workspace_root": str(Path(workspace_root).resolve()),
            "command": list(command),
            "subprocess_timeout_seconds": timeout_seconds,
            "max_output_bytes": max_output_bytes,
        },
    )
    return engine, definition
