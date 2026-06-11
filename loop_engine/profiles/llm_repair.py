"""LLM-planned repair profile with deterministic validation and verification."""

from __future__ import annotations

from pathlib import Path

from ..adapters import (
    BoundedFilesystem,
    BoundedSubprocessTool,
    FunctionVerifier,
    SubprocessSpec,
    ToolRegistryExecutor,
    ValidatedLLMPlanner,
)
from ..checkpoint import JsonCheckpointStore
from ..engine import LoopEngine
from ..models import Decision, Judgement, LoopBudget, LoopDefinition, LoopState, VerificationResult
from ..ports import JSONLLMClient
from .repair import verify_repair_results


class LLMRepairJudge:
    def judge(self, state: LoopState, verification: VerificationResult) -> Judgement:
        if verification.status == "passed":
            return Judgement(
                decision=Decision.COMPLETE,
                reason="LLM-planned repair passed verification",
                progress_signals=list(verification.passed),
            )
        if verification.status == "blocked":
            return Judgement(
                decision=Decision.STOP,
                reason="repair tool or verification is blocked",
                next_focus=verification.failed[0] if verification.failed else None,
            )
        if state.iteration < state.definition.budget.max_iterations:
            return Judgement(
                decision=Decision.REPLAN,
                reason="more inspection or repair is required",
                next_focus=verification.failed[0] if verification.failed else state.current_focus,
            )
        return Judgement(
            decision=Decision.STOP,
            reason="LLM repair iteration budget exhausted",
            next_focus=verification.failed[0] if verification.failed else None,
        )


def build_llm_repair_loop(
    *,
    workspace_root: str | Path,
    goal: str,
    llm_client: JSONLLMClient,
    verification_command: list[str],
    max_iterations: int = 4,
    max_actions: int = 16,
    max_actions_per_plan: int = 5,
    timeout_seconds: float = 60.0,
    max_output_bytes: int = 64 * 1024,
    checkpoint_root: str | Path | None = None,
    llm_metadata: dict[str, str | float | int] | None = None,
) -> tuple[LoopEngine, LoopDefinition]:
    if max_iterations <= 0 or max_actions <= 0:
        raise ValueError("LLM repair budgets must be positive")

    executor = ToolRegistryExecutor()
    BoundedFilesystem(workspace_root).register(executor)
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
    store = JsonCheckpointStore(checkpoint_root) if checkpoint_root else None
    engine = LoopEngine(
        planner=ValidatedLLMPlanner(
            llm_client,
            max_actions_per_plan=max_actions_per_plan,
        ),
        executor=executor,
        verifier=FunctionVerifier(verify_repair_results),
        judge=LLMRepairJudge(),
        checkpoint_store=store,
    )
    definition = LoopDefinition(
        goal=goal,
        success_criteria=["configured verification command exits with code 0"],
        constraints=[
            "LLM may only propose registered bounded capabilities",
            "all paths must be workspace-relative",
            "filesystem mutations use exact-text apply_patch",
            "completion authority belongs to verifier and judge",
        ],
        budget=LoopBudget(
            max_iterations=max_iterations,
            max_actions=max_actions,
            timeout_seconds=max((timeout_seconds + 15) * max_iterations, 60),
        ),
        metadata={
            "profile": "llm_repair",
            "workspace_root": str(Path(workspace_root).resolve()),
            "goal": goal,
            "command": list(verification_command),
            "max_iterations": max_iterations,
            "max_actions": max_actions,
            "max_actions_per_plan": max_actions_per_plan,
            "subprocess_timeout_seconds": timeout_seconds,
            "max_output_bytes": max_output_bytes,
            "llm": dict(llm_metadata or {}),
        },
    )
    return engine, definition
