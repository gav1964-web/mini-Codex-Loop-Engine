"""Read-only evidence collection with strict criterion verification."""

from __future__ import annotations

from pathlib import Path

from ..adapters import (
    BoundedFilesystem,
    ToolRegistryExecutor,
    ValidatedEvidenceVerifier,
    ValidatedLLMPlanner,
)
from ..checkpoint import JsonCheckpointStore
from ..engine import LoopEngine
from ..models import (
    Decision,
    Judgement,
    LoopBudget,
    LoopDefinition,
    LoopState,
    VerificationResult,
)
from ..ports import JSONLLMClient

READ_ONLY_TOOLS = {"list_files", "read_text", "search_text"}


class EvidenceJudge:
    def judge(
        self,
        state: LoopState,
        verification: VerificationResult,
    ) -> Judgement:
        if verification.status == "passed":
            return Judgement(
                decision=Decision.COMPLETE,
                reason="read-only evidence satisfies all success criteria",
                progress_signals=list(verification.passed),
            )
        if verification.status == "blocked":
            return Judgement(
                decision=Decision.STOP,
                reason="read-only evidence collection is blocked",
                next_focus=verification.failed[0] if verification.failed else None,
            )
        if state.iteration < state.definition.budget.max_iterations:
            missing = verification.evidence.get("missing_evidence", [])
            return Judgement(
                decision=Decision.REPLAN,
                reason="more read-only evidence is required",
                progress_signals=list(verification.passed),
                next_focus=(
                    "; ".join(str(item) for item in missing[:3])
                    if missing
                    else verification.failed[0]
                    if verification.failed
                    else state.current_focus
                ),
            )
        return Judgement(
            decision=Decision.STOP,
            reason="read-only evidence iteration budget exhausted",
            progress_signals=list(verification.passed),
            next_focus=verification.failed[0] if verification.failed else None,
        )


def build_llm_evidence_loop(
    *,
    workspace_root: str | Path,
    goal: str,
    success_criteria: list[str],
    llm_client: JSONLLMClient,
    allowed_tools: set[str] | None = None,
    max_iterations: int = 3,
    max_actions: int = 9,
    max_actions_per_plan: int = 3,
    contract_repair_attempts: int = 1,
    checkpoint_root: str | Path | None = None,
    llm_metadata: dict[str, str | float | int] | None = None,
) -> tuple[LoopEngine, LoopDefinition]:
    normalized_criteria = [item.strip() for item in success_criteria if item.strip()]
    if not normalized_criteria:
        raise ValueError("read-only evidence success_criteria are required")
    selected_tools = set(
        READ_ONLY_TOOLS if allowed_tools is None else allowed_tools
    )
    unknown = selected_tools - READ_ONLY_TOOLS
    if unknown:
        raise ValueError(f"non-read-only tools requested: {sorted(unknown)}")
    if not selected_tools:
        raise ValueError("allowed_tools must be non-empty")
    if max_iterations <= 0 or max_actions <= 0:
        raise ValueError("read-only evidence budgets must be positive")

    executor = ToolRegistryExecutor()
    BoundedFilesystem(workspace_root).register_read_only(executor, selected_tools)
    store = JsonCheckpointStore(checkpoint_root) if checkpoint_root else None
    engine = LoopEngine(
        planner=ValidatedLLMPlanner(
            llm_client,
            allowed_tools=selected_tools,
            max_actions_per_plan=max_actions_per_plan,
            contract_repair_attempts=contract_repair_attempts,
        ),
        executor=executor,
        verifier=ValidatedEvidenceVerifier(
            llm_client,
            contract_repair_attempts=contract_repair_attempts,
        ),
        judge=EvidenceJudge(),
        checkpoint_store=store,
    )
    definition = LoopDefinition(
        goal=goal,
        success_criteria=normalized_criteria,
        constraints=[
            "only bounded read-only filesystem tools are available",
            "all paths must be workspace-relative",
            "every satisfied criterion requires catalogue evidence references",
            "completion authority belongs to evidence verifier and judge",
        ],
        budget=LoopBudget(
            max_iterations=max_iterations,
            max_actions=max_actions,
            timeout_seconds=max(45 * max_iterations, 60),
        ),
        metadata={
            "profile": "llm_evidence",
            "workspace_root": str(Path(workspace_root).resolve()),
            "goal": goal,
            "allowed_tools": sorted(selected_tools),
            "max_iterations": max_iterations,
            "max_actions": max_actions,
            "max_actions_per_plan": max_actions_per_plan,
            "contract_repair_attempts": contract_repair_attempts,
            "llm": dict(llm_metadata or {}),
        },
    )
    return engine, definition
