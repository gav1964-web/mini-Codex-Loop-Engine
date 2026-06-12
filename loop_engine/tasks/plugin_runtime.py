"""Policy-driven bounded invocation of admitted generated plugins."""

from __future__ import annotations

import json
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ..adapters import BoundedSubprocessTool, SubprocessSpec
from ..models import LoopDefinition, LoopState
from .models import LeafExecutionResult, TaskGraph, TaskNode
from .plugin_acquisition import PersistentCapabilityRegistry


@dataclass(frozen=True, slots=True)
class PluginInvocationSpec:
    payload: dict[str, Any]
    required_output_fields: tuple[str, ...] = ("status",)
    success_statuses: tuple[str, ...] = ("ok", "success")

    @classmethod
    def create(
        cls,
        *,
        payload: dict[str, Any] | None = None,
        required_output_fields: list[str] | tuple[str, ...] = ("status",),
        success_statuses: list[str] | tuple[str, ...] = ("ok", "success"),
    ) -> PluginInvocationSpec:
        fields = tuple(str(item).strip() for item in required_output_fields)
        statuses = tuple(str(item).strip() for item in success_statuses)
        if not fields or any(not item for item in fields):
            raise ValueError("required_output_fields must be non-empty")
        if not statuses or any(not item for item in statuses):
            raise ValueError("success_statuses must be non-empty")
        normalized_payload = dict(payload or {})
        try:
            json.dumps(
                normalized_payload,
                ensure_ascii=False,
                allow_nan=False,
            )
        except (TypeError, ValueError) as exc:
            raise ValueError("plugin payload must be JSON serializable") from exc
        return cls(
            payload=normalized_payload,
            required_output_fields=fields,
            success_statuses=statuses,
        )


@dataclass(frozen=True, slots=True)
class PluginInvocationPolicy:
    invocations: dict[str, PluginInvocationSpec]
    python_executable: str = sys.executable
    timeout_seconds: float = 30.0
    max_output_bytes: int = 256 * 1024
    max_payload_bytes: int = 32 * 1024

    @classmethod
    def create(
        cls,
        *,
        invocations: dict[str, PluginInvocationSpec],
        python_executable: str = sys.executable,
        timeout_seconds: float = 30.0,
        max_output_bytes: int = 256 * 1024,
        max_payload_bytes: int = 32 * 1024,
    ) -> PluginInvocationPolicy:
        if timeout_seconds <= 0 or max_output_bytes <= 0 or max_payload_bytes <= 0:
            raise ValueError("plugin invocation bounds must be positive")
        if not invocations:
            raise ValueError("plugin invocations must be non-empty")
        normalized: dict[str, PluginInvocationSpec] = {}
        for capability, spec in invocations.items():
            name = capability.strip()
            if not name:
                raise ValueError("plugin invocation capability is required")
            if not isinstance(spec, PluginInvocationSpec):
                raise TypeError("plugin invocation values must be PluginInvocationSpec")
            payload_json = _payload_json(spec.payload)
            if len(payload_json.encode("utf-8")) > max_payload_bytes:
                raise ValueError(f"plugin payload exceeds limit: {name}")
            normalized[name] = spec
        return cls(
            invocations=normalized,
            python_executable=str(python_executable),
            timeout_seconds=timeout_seconds,
            max_output_bytes=max_output_bytes,
            max_payload_bytes=max_payload_bytes,
        )


class GeneratedPluginLeafExecutor:
    """Invoke one policy-admitted generated capability in a bounded process."""

    def __init__(
        self,
        registry: PersistentCapabilityRegistry,
        policy: PluginInvocationPolicy,
    ) -> None:
        self.registry = registry
        self.policy = policy
        self.worker_path = Path(__file__).with_name("plugin_worker.py").resolve()

    def execute(self, node: TaskNode, graph: TaskGraph) -> LeafExecutionResult:
        capabilities = list(dict.fromkeys(node.required_capabilities))
        if len(capabilities) != 1:
            return self._blocked(
                "generated_plugin_requires_exactly_one_capability",
                capabilities=capabilities,
            )
        capability = capabilities[0]
        invocation = self.policy.invocations.get(capability)
        if invocation is None:
            return self._blocked(
                f"generated_plugin_invocation_not_admitted:{capability}",
                capabilities=capabilities,
            )
        descriptor = self.registry.get(capability)
        if descriptor is None:
            return self._blocked(
                f"generated_plugin_descriptor_unavailable:{capability}",
                capabilities=capabilities,
            )

        plugin_path = Path(descriptor.plugin_root) / "plugin.py"
        payload_json = _payload_json(invocation.payload)
        process = BoundedSubprocessTool(
            descriptor.plugin_root,
            SubprocessSpec(
                argv=(
                    self.policy.python_executable,
                    "-I",
                    str(self.worker_path),
                    "--plugin",
                    str(plugin_path),
                    "--expected-sha256",
                    descriptor.file_sha256["plugin.py"],
                    "--payload-json",
                    payload_json,
                ),
                timeout_seconds=self.policy.timeout_seconds,
                max_output_bytes=self.policy.max_output_bytes,
                environment={
                    "PYTHONDONTWRITEBYTECODE": "1",
                    "PYTHONIOENCODING": "utf-8",
                    "PYTHONUTF8": "1",
                },
            ),
        )(
            {},
            LoopState(
                run_id=f"plugin-{graph.id}-{node.id}",
                definition=LoopDefinition(goal=f"Invoke {capability}"),
            ),
        )
        return self._interpret(
            process,
            capability=capability,
            plugin_id=descriptor.plugin_id,
            invocation=invocation,
        )

    def _interpret(
        self,
        process: dict[str, Any],
        *,
        capability: str,
        plugin_id: str,
        invocation: PluginInvocationSpec,
    ) -> LeafExecutionResult:
        base_evidence = {
            "capability": capability,
            "plugin_id": plugin_id,
            "duration_seconds": process.get("duration_seconds"),
        }
        if process.get("timed_out"):
            return self._blocked(
                "generated_plugin_timeout",
                evidence=base_evidence,
            )
        if process.get("stdout_truncated") or process.get("stderr_truncated"):
            return self._failed(
                "generated_plugin_output_truncated",
                evidence=base_evidence,
            )
        try:
            envelope = json.loads(str(process.get("stdout", "")))
        except json.JSONDecodeError:
            return self._failed(
                "generated_plugin_worker_invalid_json",
                evidence=base_evidence,
            )
        if not isinstance(envelope, dict):
            return self._failed(
                "generated_plugin_worker_invalid_envelope",
                evidence=base_evidence,
            )
        if process.get("exit_code") != 0 or envelope.get("status") != "ok":
            detail = str(envelope.get("error", ""))[:1000]
            return self._failed(
                f"generated_plugin_error:{detail or 'worker_failed'}",
                evidence={
                    **base_evidence,
                    "worker_error_type": envelope.get("error_type"),
                },
            )
        output = envelope.get("output")
        if not isinstance(output, dict):
            return self._failed(
                "generated_plugin_output_not_object",
                evidence=base_evidence,
            )
        missing = [
            field for field in invocation.required_output_fields if field not in output
        ]
        if missing:
            return self._failed(
                f"generated_plugin_output_missing:{','.join(missing)}",
                evidence={**base_evidence, "output": output},
            )
        if output.get("status") not in invocation.success_statuses:
            return self._failed(
                f"generated_plugin_unsuccessful_status:{output.get('status')}",
                evidence={**base_evidence, "output": output},
            )
        summary = output.get("summary")
        if not isinstance(summary, str) or not summary.strip():
            summary = f"generated plugin {plugin_id} completed"
        return LeafExecutionResult(
            status="completed",
            summary=summary,
            evidence={**base_evidence, "output": output},
        )

    @staticmethod
    def _blocked(
        error: str,
        *,
        capabilities: list[str] | None = None,
        evidence: dict[str, Any] | None = None,
    ) -> LeafExecutionResult:
        details = dict(evidence or {})
        if capabilities is not None:
            details["required_capabilities"] = capabilities
        return LeafExecutionResult(
            status="blocked",
            summary="generated plugin invocation is blocked",
            error=error,
            evidence=details,
        )

    @staticmethod
    def _failed(
        error: str,
        *,
        evidence: dict[str, Any],
    ) -> LeafExecutionResult:
        return LeafExecutionResult(
            status="failed",
            summary="generated plugin invocation failed",
            error=error,
            evidence=evidence,
        )


def _payload_json(payload: dict[str, Any]) -> str:
    return json.dumps(
        payload,
        ensure_ascii=False,
        separators=(",", ":"),
        allow_nan=False,
    )
