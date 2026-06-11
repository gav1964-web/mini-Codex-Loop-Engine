"""Safe mapping from atomic coding contracts to existing loop profiles."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from ..ports import JSONLLMClient
from ..profiles import build_coding_check_loop, build_llm_repair_loop
from .adapters import LoopEngineLeafExecutor
from .models import LeafExecutionResult, TaskGraph, TaskNode

_READ_CAPABILITIES = {
    "filesystem.list",
    "filesystem.read",
    "filesystem.search",
}
_PATCH_CAPABILITY = "filesystem.patch"
_VERIFY_CAPABILITY = "process.verify"
_KNOWN_CAPABILITIES = {
    *_READ_CAPABILITIES,
    _PATCH_CAPABILITY,
    _VERIFY_CAPABILITY,
}
_RESERVED_METADATA = {
    "api_key",
    "command",
    "gateway_url",
    "llm",
    "model",
    "process_command",
    "verification_command",
    "workspace_root",
}


@dataclass(frozen=True, slots=True)
class CodingLeafPolicy:
    workspace_root: Path
    verification_command: tuple[str, ...]
    subprocess_timeout_seconds: float = 60.0
    max_output_bytes: int = 64 * 1024
    max_iterations: int = 4
    max_actions: int = 16
    max_actions_per_plan: int = 5
    contract_repair_attempts: int = 1
    checkpoint_root: Path | None = None

    @classmethod
    def create(
        cls,
        *,
        workspace_root: str | Path,
        verification_command: list[str] | tuple[str, ...],
        subprocess_timeout_seconds: float = 60.0,
        max_output_bytes: int = 64 * 1024,
        max_iterations: int = 4,
        max_actions: int = 16,
        max_actions_per_plan: int = 5,
        contract_repair_attempts: int = 1,
        checkpoint_root: str | Path | None = None,
    ) -> CodingLeafPolicy:
        root = Path(workspace_root).resolve()
        command = tuple(str(item) for item in verification_command)
        if not root.is_dir():
            raise ValueError("coding leaf workspace_root must be an existing directory")
        if not command or any(not item for item in command):
            raise ValueError("coding leaf verification_command is required")
        if subprocess_timeout_seconds <= 0 or max_output_bytes <= 0:
            raise ValueError("coding leaf subprocess bounds must be positive")
        if max_iterations <= 0 or max_actions <= 0 or max_actions_per_plan <= 0:
            raise ValueError("coding leaf LLM budgets must be positive")
        if contract_repair_attempts not in {0, 1}:
            raise ValueError("contract_repair_attempts must be 0 or 1")
        return cls(
            workspace_root=root,
            verification_command=command,
            subprocess_timeout_seconds=subprocess_timeout_seconds,
            max_output_bytes=max_output_bytes,
            max_iterations=max_iterations,
            max_actions=max_actions,
            max_actions_per_plan=max_actions_per_plan,
            contract_repair_attempts=contract_repair_attempts,
            checkpoint_root=(
                Path(checkpoint_root).resolve()
                if checkpoint_root is not None
                else None
            ),
        )


class CodingLeafExecutor:
    """Execute only supported atomic coding capability combinations."""

    def __init__(
        self,
        policy: CodingLeafPolicy,
        *,
        llm_client: JSONLLMClient | None = None,
        llm_metadata: dict[str, str | float | int] | None = None,
    ) -> None:
        self.policy = policy
        self.llm_client = llm_client
        self.llm_metadata = dict(llm_metadata or {})

    def execute(self, node: TaskNode, graph: TaskGraph) -> LeafExecutionResult:
        contract_error = self._validate_node(node)
        if contract_error is not None:
            return LeafExecutionResult(
                status="blocked",
                summary="coding leaf contract is not executable",
                error=contract_error,
                evidence={"required_capabilities": node.required_capabilities},
            )

        capabilities = set(node.required_capabilities)
        if capabilities == {_VERIFY_CAPABILITY}:
            profile = "coding_check"
            delegate = LoopEngineLeafExecutor(self._build_check)
        elif _PATCH_CAPABILITY in capabilities:
            if self.llm_client is None:
                return LeafExecutionResult(
                    status="blocked",
                    summary="coding repair leaf requires an LLM client",
                    error="coding_leaf_llm_client_missing",
                )
            profile = "llm_repair"
            delegate = LoopEngineLeafExecutor(self._build_repair)
        else:
            return LeafExecutionResult(
                status="blocked",
                summary="read-only evidence leaves are not executable yet",
                error="unsupported_read_only_coding_leaf",
                evidence={"required_capabilities": sorted(capabilities)},
            )

        result = delegate.execute(node, graph)
        result.evidence["coding_leaf_profile"] = profile
        return result

    def _build_check(self, node: TaskNode, graph: TaskGraph):
        engine, definition = build_coding_check_loop(
            workspace_root=self.policy.workspace_root,
            command=list(self.policy.verification_command),
            timeout_seconds=self.policy.subprocess_timeout_seconds,
            max_output_bytes=self.policy.max_output_bytes,
            checkpoint_root=self.policy.checkpoint_root,
        )
        definition.goal = node.goal
        definition.success_criteria = list(node.success_criteria)
        definition.metadata["task_graph_id"] = graph.id
        definition.metadata["task_node_id"] = node.id
        return engine, definition

    def _build_repair(self, node: TaskNode, graph: TaskGraph):
        if self.llm_client is None:
            raise RuntimeError("coding leaf LLM client is unavailable")
        engine, definition = build_llm_repair_loop(
            workspace_root=self.policy.workspace_root,
            goal=node.goal,
            llm_client=self.llm_client,
            verification_command=list(self.policy.verification_command),
            max_iterations=self.policy.max_iterations,
            max_actions=self.policy.max_actions,
            max_actions_per_plan=self.policy.max_actions_per_plan,
            contract_repair_attempts=self.policy.contract_repair_attempts,
            timeout_seconds=self.policy.subprocess_timeout_seconds,
            max_output_bytes=self.policy.max_output_bytes,
            checkpoint_root=self.policy.checkpoint_root,
            llm_metadata=self.llm_metadata,
        )
        definition.success_criteria = list(node.success_criteria)
        definition.metadata["task_graph_id"] = graph.id
        definition.metadata["task_node_id"] = node.id
        return engine, definition

    @staticmethod
    def _validate_node(node: TaskNode) -> str | None:
        if not node.goal.strip():
            return "coding_leaf_goal_missing"
        if not node.success_criteria:
            return "coding_leaf_success_criteria_missing"
        capabilities = set(node.required_capabilities)
        if not capabilities:
            return "coding_leaf_capabilities_missing"
        unknown = capabilities - _KNOWN_CAPABILITIES
        if unknown:
            return f"unsupported_coding_capabilities:{','.join(sorted(unknown))}"
        reserved = set(node.metadata) & _RESERVED_METADATA
        if reserved:
            return f"reserved_coding_metadata:{','.join(sorted(reserved))}"
        if _PATCH_CAPABILITY in capabilities and _VERIFY_CAPABILITY not in capabilities:
            return "coding_patch_requires_process_verify"
        return None
