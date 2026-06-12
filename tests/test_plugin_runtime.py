from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path

from loop_engine.tasks import (
    FunctionIntegrationVerifier,
    GeneratedCapability,
    GeneratedPluginLeafExecutor,
    PersistentCapabilityRegistry,
    PluginInvocationPolicy,
    PluginInvocationSpec,
    ScriptedTaskDecomposer,
    TaskGraph,
    TaskScheduler,
    TaskStatus,
)


class FakeSandbox:
    backend_name = "fake_os_sandbox"

    def __init__(self, *, available: bool = True) -> None:
        self.available = available
        self.probes = 0

    def probe(self, workspace_root, *, run_id):
        self.probes += 1
        return self.available

    def build_argv(
        self,
        *,
        plugin_root,
        worker_path,
        expected_sha256,
        payload_json,
    ):
        return (
            sys.executable,
            "-I",
            str(worker_path),
            "--plugin",
            str(Path(plugin_root) / "plugin.py"),
            "--expected-sha256",
            expected_sha256,
            "--payload-json",
            payload_json,
        )


def _registered_plugin(
    tmp_path: Path,
    source: str,
    *,
    capability: str = "project.loc_report",
) -> PersistentCapabilityRegistry:
    artifact_root = tmp_path / "generated"
    plugin_root = artifact_root / "test-plugin"
    plugin_root.mkdir(parents=True)
    files = {
        "plugin.py": source,
        "manifest.json": json.dumps(
            {
                "plugin_id": "test-plugin",
                "plugin_family": "test_family",
                "entrypoint": "plugin.py:run",
                "requested_capabilities": [capability],
            }
        ),
        "README.md": "# Test plugin\n",
    }
    for name, content in files.items():
        (plugin_root / name).write_text(content, encoding="utf-8")

    registry = PersistentCapabilityRegistry(
        tmp_path / "capabilities.json",
        artifact_root=artifact_root,
    )
    registry.register(
        GeneratedCapability(
            capability=capability,
            family="test_family",
            plugin_id="test-plugin",
            plugin_root=str(plugin_root.resolve()),
            manifest_path=str((plugin_root / "manifest.json").resolve()),
            file_sha256={
                name: hashlib.sha256((plugin_root / name).read_bytes()).hexdigest()
                for name in files
            },
        )
    )
    return registry


def _executor(
    registry: PersistentCapabilityRegistry,
    *,
    payload: dict | None = None,
    timeout_seconds: float = 5,
    requires_os_sandbox: bool = False,
    sandbox_launcher=None,
) -> GeneratedPluginLeafExecutor:
    return GeneratedPluginLeafExecutor(
        registry,
        PluginInvocationPolicy.create(
            invocations={
                "project.loc_report": PluginInvocationSpec.create(
                    payload=payload or {"root_path": "policy-root"},
                    required_output_fields=("status", "received_root"),
                    requires_os_sandbox=requires_os_sandbox,
                )
            },
            python_executable=sys.executable,
            timeout_seconds=timeout_seconds,
            sandbox_launcher=sandbox_launcher,
        ),
    )


def _run(
    registry: PersistentCapabilityRegistry,
    executor: GeneratedPluginLeafExecutor,
    *,
    metadata: dict | None = None,
) -> TaskGraph:
    graph = TaskGraph.create(
        "Run generated report",
        required_capabilities=["project.loc_report"],
    )
    graph.root.metadata.update(metadata or {})
    return TaskScheduler(
        decomposer=ScriptedTaskDecomposer({}),
        capability_resolver=registry,
        leaf_executor=executor,
        integration_verifier=FunctionIntegrationVerifier(),
    ).run(graph)


def test_scheduler_invokes_registered_plugin_with_policy_payload(tmp_path) -> None:
    registry = _registered_plugin(
        tmp_path,
        """
def run(payload):
    print("untrusted plugin noise")
    return {
        "status": "ok",
        "received_root": payload["root_path"],
        "summary": "report complete",
    }
""".strip()
        + "\n",
    )

    result = _run(
        registry,
        _executor(registry),
        metadata={"payload": {"root_path": "task-controlled-root"}},
    )

    assert result.root.status == TaskStatus.COMPLETED
    assert result.root.result is not None
    assert result.root.result.summary == "report complete"
    assert result.root.result.evidence["output"]["received_root"] == "policy-root"
    assert result.root.result.evidence["plugin_id"] == "test-plugin"
    assert result.root.result.evidence["sandbox_backend"] == "process_only"


def test_plugin_timeout_blocks_leaf(tmp_path) -> None:
    registry = _registered_plugin(
        tmp_path,
        """
import time

def run(payload):
    time.sleep(10)
    return {"status": "ok", "received_root": payload["root_path"]}
""".strip()
        + "\n",
    )

    result = _run(
        registry,
        _executor(registry, timeout_seconds=0.1),
    )

    assert result.root.status == TaskStatus.BLOCKED
    assert result.root.error == "generated_plugin_timeout"


def test_tampered_plugin_is_blocked_before_invocation(tmp_path) -> None:
    registry = _registered_plugin(
        tmp_path,
        """
def run(payload):
    return {"status": "ok", "received_root": payload["root_path"]}
""".strip()
        + "\n",
    )
    descriptor = registry.get("project.loc_report")
    assert descriptor is not None
    (Path(descriptor.plugin_root) / "plugin.py").write_text(
        "def run(payload):\n    return {'status': 'tampered'}\n",
        encoding="utf-8",
    )

    result = _run(registry, _executor(registry))

    assert result.root.status == TaskStatus.BLOCKED
    assert result.root.error == "missing_capabilities:project.loc_report"


def test_non_object_plugin_output_fails_leaf(tmp_path) -> None:
    registry = _registered_plugin(
        tmp_path,
        """
def run(payload):
    return ["not", "an", "object"]
""".strip()
        + "\n",
    )

    result = _run(registry, _executor(registry))

    assert result.root.status == TaskStatus.FAILED
    assert result.root.error is not None
    assert result.root.error.startswith("generated_plugin_error:")
    assert "plugin output must be a JSON object" in result.root.error


def test_capability_without_invocation_admission_is_blocked(tmp_path) -> None:
    registry = _registered_plugin(
        tmp_path,
        """
def run(payload):
    return {"status": "ok"}
""".strip()
        + "\n",
    )
    executor = GeneratedPluginLeafExecutor(
        registry,
        PluginInvocationPolicy.create(
            invocations={
                "another.capability": PluginInvocationSpec.create(
                    payload={},
                )
            }
        ),
    )

    result = _run(registry, executor)

    assert result.root.status == TaskStatus.BLOCKED
    assert result.root.error == (
        "generated_plugin_invocation_not_admitted:project.loc_report"
    )


def test_strict_plugin_without_sandbox_is_blocked(tmp_path) -> None:
    registry = _registered_plugin(
        tmp_path,
        """
def run(payload):
    return {"status": "ok", "received_root": payload["root_path"]}
""".strip()
        + "\n",
    )

    result = _run(
        registry,
        _executor(registry, requires_os_sandbox=True),
        metadata={"requires_os_sandbox": False},
    )

    assert result.root.status == TaskStatus.BLOCKED
    assert result.root.error == "generated_plugin_os_sandbox_not_configured"
    assert result.root.result.evidence["sandbox_backend"] == "not_configured"


def test_unavailable_sandbox_does_not_fallback_to_direct_process(tmp_path) -> None:
    registry = _registered_plugin(
        tmp_path,
        """
def run(payload):
    return {"status": "ok", "received_root": payload["root_path"]}
""".strip()
        + "\n",
    )
    sandbox = FakeSandbox(available=False)

    result = _run(
        registry,
        _executor(
            registry,
            requires_os_sandbox=True,
            sandbox_launcher=sandbox,
        ),
    )

    assert result.root.status == TaskStatus.BLOCKED
    assert result.root.error == (
        "generated_plugin_os_sandbox_unavailable:fake_os_sandbox"
    )
    assert result.root.result.evidence["sandbox_backend"] == "fake_os_sandbox"
    assert sandbox.probes == 1


def test_strict_plugin_runs_only_through_configured_sandbox(tmp_path) -> None:
    registry = _registered_plugin(
        tmp_path,
        """
def run(payload):
    return {
        "status": "ok",
        "received_root": payload["root_path"],
        "summary": "sandboxed",
    }
""".strip()
        + "\n",
    )
    sandbox = FakeSandbox()

    result = _run(
        registry,
        _executor(
            registry,
            requires_os_sandbox=True,
            sandbox_launcher=sandbox,
        ),
    )

    assert result.root.status == TaskStatus.COMPLETED
    assert result.root.result is not None
    assert result.root.result.evidence["sandbox_backend"] == "fake_os_sandbox"
    assert sandbox.probes == 1
