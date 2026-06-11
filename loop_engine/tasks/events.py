from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from .models import TaskEvent, TaskGraph


def record_task_event(
    graph: TaskGraph,
    event_type: str,
    node_id: str,
    payload: dict[str, Any] | None = None,
) -> None:
    graph.events.append(
        TaskEvent(
            sequence=len(graph.events) + 1,
            event_type=event_type,
            node_id=node_id,
            payload=dict(payload or {}),
            timestamp=datetime.now(timezone.utc).isoformat(),
        )
    )
