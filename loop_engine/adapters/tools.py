"""Bounded named-tool executor."""

from __future__ import annotations

from collections.abc import Callable
from time import perf_counter
from typing import Any

from ..models import Action, ActionResult, LoopState

Tool = Callable[[dict[str, Any], LoopState], dict[str, Any]]


class ToolRegistryExecutor:
    def __init__(self) -> None:
        self._tools: dict[str, Tool] = {}

    def register(self, name: str, tool: Tool) -> None:
        normalized = name.strip()
        if not normalized:
            raise ValueError("tool name is required")
        if normalized in self._tools:
            raise ValueError(f"tool already registered: {normalized}")
        self._tools[normalized] = tool

    def execute(self, action: Action, state: LoopState) -> ActionResult:
        tool = self._tools.get(action.tool)
        if tool is None:
            return ActionResult(action=action, status="error", error=f"unknown tool: {action.tool}")
        started = perf_counter()
        try:
            output = tool(dict(action.arguments), state)
        except Exception as exc:
            return ActionResult(
                action=action,
                status="error",
                error=f"{type(exc).__name__}: {exc}",
                duration_seconds=perf_counter() - started,
            )
        if not isinstance(output, dict):
            return ActionResult(
                action=action,
                status="error",
                error="tool output must be a JSON object",
                duration_seconds=perf_counter() - started,
            )
        return ActionResult(
            action=action,
            status="ok",
            output=output,
            duration_seconds=perf_counter() - started,
        )
