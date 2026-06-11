"""Event recording helpers."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from .models import LoopEvent, LoopState


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def record_event(state: LoopState, event_type: str, payload: dict[str, Any]) -> None:
    state.events.append(
        LoopEvent(
            sequence=len(state.events) + 1,
            event_type=event_type,
            iteration=state.iteration,
            payload=dict(payload),
            timestamp=utc_now(),
        )
    )
