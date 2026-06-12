"""Bounded command verification for completed parent task nodes."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ..adapters import BoundedSubprocessTool, SubprocessSpec
from ..models import LoopDefinition, LoopState
from .models import LeafExecutionResult, TaskGraph, TaskNode, TaskStatus


@dataclass(frozen=True, slots=True)
class IntegrationCommandSpec:
    argv: tuple[str, ...]
    cwd: str = "."

    @classmethod
    def create(
        cls,
        argv: list[str] | tuple[str, ...],
        *,
        cwd: str = ".",
    ) -> IntegrationCommandSpec:
        command = tuple(str(item) for item in argv)
        if not command or any(not item for item in command):
            raise ValueError("integration verification command is required")
        normalized_cwd = str(cwd).strip()
        if not normalized_cwd:
            raise ValueError("integration verification cwd is required")
        return cls(argv=command, cwd=normalized_cwd)


@dataclass(frozen=True, slots=True)
class BoundedIntegrationPolicy:
    workspace_root: Path
    commands: dict[str, IntegrationCommandSpec]
    default_command: IntegrationCommandSpec | None = None
    timeout_seconds: float = 60.0
    max_output_bytes: int = 64 * 1024

    @classmethod
    def create(
        cls,
        *,
        workspace_root: str | Path,
        commands: dict[str, IntegrationCommandSpec] | None = None,
        default_command: IntegrationCommandSpec | None = None,
        timeout_seconds: float = 60.0,
        max_output_bytes: int = 64 * 1024,
    ) -> BoundedIntegrationPolicy:
        root = Path(workspace_root).resolve()
        if not root.is_dir():
            raise ValueError(
                "integration verification workspace_root must be an existing directory"
            )
        if timeout_seconds <= 0 or max_output_bytes <= 0:
            raise ValueError("integration verification bounds must be positive")
        normalized: dict[str, IntegrationCommandSpec] = {}
        for node_id, spec in (commands or {}).items():
            key = node_id.strip()
            if not key:
                raise ValueError("integration verification node id is required")
            if not isinstance(spec, IntegrationCommandSpec):
                raise TypeError(
                    "integration verification commands must be IntegrationCommandSpec"
                )
            cls._validate_cwd(root, spec)
            normalized[key] = spec
        if default_command is not None:
            if not isinstance(default_command, IntegrationCommandSpec):
                raise TypeError(
                    "default integration command must be IntegrationCommandSpec"
                )
            cls._validate_cwd(root, default_command)
        if not normalized and default_command is None:
            raise ValueError("at least one integration verification command is required")
        return cls(
            workspace_root=root,
            commands=normalized,
            default_command=default_command,
            timeout_seconds=timeout_seconds,
            max_output_bytes=max_output_bytes,
        )

    @staticmethod
    def _validate_cwd(root: Path, spec: IntegrationCommandSpec) -> None:
        candidate = Path(spec.cwd)
        if not candidate.is_absolute():
            candidate = root / candidate
        resolved = candidate.resolve()
        try:
            resolved.relative_to(root)
        except ValueError as exc:
            raise ValueError(
                f"integration verification cwd escapes workspace: {resolved}"
            ) from exc
        if not resolved.is_dir():
            raise ValueError(
                f"integration verification cwd does not exist: {resolved}"
            )


class BoundedCommandIntegrationVerifier:
    """Verify parent integration with one externally configured command."""

    def __init__(self, policy: BoundedIntegrationPolicy) -> None:
        self.policy = policy

    def verify(self, node: TaskNode, graph: TaskGraph) -> LeafExecutionResult:
        child_error = self._validate_children(node, graph)
        if child_error is not None:
            return LeafExecutionResult(
                status="blocked",
                summary="parent integration command is not ready",
                error=child_error,
                evidence=self._child_evidence(node, graph),
            )
        command = self.policy.commands.get(node.id, self.policy.default_command)
        if command is None:
            return LeafExecutionResult(
                status="blocked",
                summary="parent integration command is not configured",
                error=f"integration_command_missing:{node.id}",
                evidence=self._child_evidence(node, graph),
            )

        process = BoundedSubprocessTool(
            self.policy.workspace_root,
            SubprocessSpec(
                argv=command.argv,
                cwd=command.cwd,
                timeout_seconds=self.policy.timeout_seconds,
                max_output_bytes=self.policy.max_output_bytes,
                environment={
                    "PYTHONIOENCODING": "utf-8",
                    "PYTHONUTF8": "1",
                },
            ),
        )(
            {},
            LoopState(
                run_id=f"integration-{graph.id}-{node.id}",
                definition=LoopDefinition(goal=f"Verify integration for {node.id}"),
            ),
        )
        evidence = {
            **self._child_evidence(node, graph),
            "integration_process": self._process_evidence(process),
        }
        if process.get("timed_out"):
            return LeafExecutionResult(
                status="blocked",
                summary="parent integration command timed out",
                error="integration_command_timeout",
                evidence=evidence,
            )
        if process.get("stdout_truncated") or process.get("stderr_truncated"):
            return LeafExecutionResult(
                status="failed",
                summary="parent integration command output was truncated",
                error="integration_command_output_truncated",
                evidence=evidence,
            )
        exit_code = process.get("exit_code")
        if exit_code != 0:
            return LeafExecutionResult(
                status="failed",
                summary="parent integration command failed",
                error=f"integration_command_exit_code:{exit_code}",
                evidence=evidence,
            )
        return LeafExecutionResult(
            status="completed",
            summary="parent integration command passed",
            evidence=evidence,
        )

    @staticmethod
    def _validate_children(node: TaskNode, graph: TaskGraph) -> str | None:
        if not node.children:
            return "integration_parent_has_no_children"
        missing = [child_id for child_id in node.children if child_id not in graph.nodes]
        if missing:
            return f"integration_children_missing:{','.join(sorted(missing))}"
        incomplete = [
            child_id
            for child_id in node.children
            if graph.nodes[child_id].status != TaskStatus.COMPLETED
        ]
        if incomplete:
            return f"integration_children_incomplete:{','.join(sorted(incomplete))}"
        return None

    @staticmethod
    def _child_evidence(node: TaskNode, graph: TaskGraph) -> dict[str, Any]:
        return {
            "children": {
                child_id: (
                    graph.nodes[child_id].result.evidence
                    if child_id in graph.nodes
                    and graph.nodes[child_id].result is not None
                    else {}
                )
                for child_id in node.children
            }
        }

    @staticmethod
    def _process_evidence(process: dict[str, Any]) -> dict[str, Any]:
        return {
            "cwd": process.get("cwd"),
            "exit_code": process.get("exit_code"),
            "timed_out": process.get("timed_out"),
            "stdout": process.get("stdout"),
            "stderr": process.get("stderr"),
            "stdout_truncated": process.get("stdout_truncated"),
            "stderr_truncated": process.get("stderr_truncated"),
            "duration_seconds": process.get("duration_seconds"),
        }
