"""Deterministic loop safety and stop policies."""

from __future__ import annotations

import hashlib
import json
from datetime import datetime

from .models import LoopState, VerificationResult


def budget_stop_reason(
    state: LoopState,
    *,
    now: datetime,
    check_iteration: bool = True,
) -> str | None:
    budget = state.definition.budget
    if check_iteration and state.iteration >= max(1, budget.max_iterations):
        return "iteration_budget_exhausted"
    if state.action_count >= max(1, budget.max_actions):
        return "action_budget_exhausted"
    if state.started_at:
        started = datetime.fromisoformat(state.started_at)
        if (now - started).total_seconds() >= max(0.1, budget.timeout_seconds):
            return "wall_clock_budget_exhausted"
    return None


def observation_signature(verification: VerificationResult) -> str:
    normalized = {
        "status": verification.status,
        "passed": sorted(verification.passed),
        "failed": sorted(verification.failed),
        "evidence": verification.evidence,
    }
    raw = json.dumps(normalized, ensure_ascii=True, sort_keys=True, default=str)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def stagnation_stop_reason(state: LoopState, signature: str) -> str | None:
    limit = max(2, state.definition.budget.max_repeated_observations)
    recent = [*state.observation_signatures, signature][-limit:]
    if len(recent) == limit and len(set(recent)) == 1:
        return "repeated_observation_stagnation"
    return None
