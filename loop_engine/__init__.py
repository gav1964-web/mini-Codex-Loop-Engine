"""Public API for mini-Codex Loop Engine."""

import sys

if sys.version_info < (3, 11):
    raise RuntimeError("mini-Codex Loop Engine requires Python 3.11 or newer")

from .engine import LoopEngine
from .models import (
    Action,
    ActionResult,
    Decision,
    Judgement,
    LoopBudget,
    LoopDefinition,
    LoopPhase,
    LoopState,
    LoopStatus,
    Plan,
    VerificationResult,
)

__all__ = [
    "Action",
    "ActionResult",
    "Decision",
    "Judgement",
    "LoopBudget",
    "LoopDefinition",
    "LoopEngine",
    "LoopPhase",
    "LoopState",
    "LoopStatus",
    "Plan",
    "VerificationResult",
]
