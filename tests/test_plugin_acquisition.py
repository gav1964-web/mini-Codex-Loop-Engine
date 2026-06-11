from __future__ import annotations

import json
import sys
from pathlib import Path

from loop_engine.tasks import (
    FunctionIntegrationVerifier,
    FunctionLeafExecutor,
    LeafExecutionResult,
    PersistentCapabilityRegistry,
    PluginAcquisitionPolicy,
    PluginGeneratorAcquirer,
    ScriptedTaskDecomposer,
    TaskGraph,
    TaskScheduler,
    TaskStatus,
)


def _fake_generator(root, *, corrupt_manifest: bool = False) -> None:
    package = root / "plugin_generator"
    package.mkdir(parents=True)
    (package / "__init__.py").write_text("", encoding="utf-8")
    manifest_family = "wrong_family" if corrupt_manifest else "project_loc_reporter"
    (package / "__main__.py").write_text(
        f"""
import argparse
import json
from pathlib import Path

parser = argparse.ArgumentParser()
sub = parser.add_subparsers(dest="command", required=True)
generate = sub.add_parser("generate")
generate.add_argument("--family", required=True)
generate.add_argument("--capability", action="append", default=[])
generate.add_argument("--constraint", action="append", default=[])
generate.add_argument("--output", type=Path, required=True)
args = parser.parse_args()

counter = Path("calls.txt")
count = int(counter.read_text() or "0") if counter.exists() else 0
counter.write_text(str(count + 1))

capability = args.capability[0]
plugin_id = "project_loc_reporter-project-loc-report"
plugin_root = args.output.resolve() / plugin_id
plugin_root.mkdir(parents=True, exist_ok=True)
(plugin_root / "plugin.py").write_text(
    "def run(payload=None):\\n    return {{'status': 'ok'}}\\n",
    encoding="utf-8",
)
(plugin_root / "README.md").write_text("# Generated\\n", encoding="utf-8")
manifest = {{
    "plugin_id": plugin_id,
    "plugin_family": "{manifest_family}",
    "entrypoint": "plugin.py:run",
    "requested_capabilities": [capability],
}}
(plugin_root / "manifest.json").write_text(
    json.dumps(manifest),
    encoding="utf-8",
)
written = [
    str((plugin_root / name).resolve())
    for name in ("plugin.py", "manifest.json", "README.md")
]
print(json.dumps({{
    "status": "success",
    "plugin_id": plugin_id,
    "family": args.family,
    "materialized_root": str(args.output.resolve()),
    "written_files": written,
}}))
""".strip()
        + "\n",
        encoding="utf-8",
    )


def _policy(generator_root, output_root) -> PluginAcquisitionPolicy:
    return PluginAcquisitionPolicy.create(
        generator_root=generator_root,
        output_root=output_root,
        capability_families={
            "project.loc_report": "project_loc_reporter",
        },
        python_executable=sys.executable,
        timeout_seconds=10,
    )


def _scheduler(registry, acquirer):
    return TaskScheduler(
        decomposer=ScriptedTaskDecomposer({}),
        capability_resolver=registry,
        capability_acquirer=acquirer,
        leaf_executor=FunctionLeafExecutor(
            lambda node, graph: LeafExecutionResult(
                status="completed",
                summary="generated capability is available",
                evidence={"capability": node.required_capabilities[0]},
            )
        ),
        integration_verifier=FunctionIntegrationVerifier(),
    )


def _registry(tmp_path) -> PersistentCapabilityRegistry:
    return PersistentCapabilityRegistry(
        tmp_path / "capabilities.json",
        artifact_root=tmp_path / "generated",
    )


def test_missing_capability_is_generated_validated_and_persisted(tmp_path) -> None:
    generator_root = tmp_path / "generator"
    _fake_generator(generator_root)
    registry_path = tmp_path / "state" / "capabilities.json"
    registry = PersistentCapabilityRegistry(
        registry_path,
        artifact_root=tmp_path / "generated",
    )
    acquirer = PluginGeneratorAcquirer(
        _policy(generator_root, tmp_path / "generated"),
        registry,
    )
    graph = TaskGraph.create(
        "Report large Python files",
        required_capabilities=["project.loc_report"],
    )

    result = _scheduler(registry, acquirer).run(graph)

    assert result.root.status == TaskStatus.COMPLETED
    descriptor = registry.get("project.loc_report")
    assert descriptor is not None
    assert descriptor.family == "project_loc_reporter"
    assert descriptor.plugin_id == "project_loc_reporter-project-loc-report"
    assert set(descriptor.file_sha256) == {
        "plugin.py",
        "manifest.json",
        "README.md",
    }
    assert all(len(value) == 64 for value in descriptor.file_sha256.values())
    assert registry_path.is_file()
    loaded = PersistentCapabilityRegistry(
        registry_path,
        artifact_root=tmp_path / "generated",
    )
    assert loaded.get("project.loc_report") == descriptor
    assert any(
        event.event_type == "capability_acquisition_requested"
        for event in result.events
    )


def test_repeated_acquisition_is_idempotent(tmp_path) -> None:
    generator_root = tmp_path / "generator"
    _fake_generator(generator_root)
    registry = _registry(tmp_path)
    acquirer = PluginGeneratorAcquirer(
        _policy(generator_root, tmp_path / "generated"),
        registry,
    )
    node_graph = TaskGraph.create(
        "Acquire capability",
        required_capabilities=["project.loc_report"],
    )

    assert acquirer.acquire("project.loc_report", node_graph.root, node_graph)
    assert acquirer.acquire("project.loc_report", node_graph.root, node_graph)

    assert (generator_root / "calls.txt").read_text() == "1"


def test_tampered_plugin_is_reacquired_before_use(tmp_path) -> None:
    generator_root = tmp_path / "generator"
    _fake_generator(generator_root)
    registry = _registry(tmp_path)
    acquirer = PluginGeneratorAcquirer(
        _policy(generator_root, tmp_path / "generated"),
        registry,
    )
    graph = TaskGraph.create(
        "Acquire capability",
        required_capabilities=["project.loc_report"],
    )
    assert acquirer.acquire("project.loc_report", graph.root, graph)
    descriptor = registry.get("project.loc_report")
    assert descriptor is not None
    (Path(descriptor.plugin_root) / "plugin.py").write_text(
        "def run(payload=None):\n    return {'status': 'tampered'}\n",
        encoding="utf-8",
    )
    assert registry.get("project.loc_report") is None

    result = _scheduler(registry, acquirer).run(graph)

    assert result.root.status == TaskStatus.COMPLETED
    assert (generator_root / "calls.txt").read_text() == "2"
    assert registry.get("project.loc_report") is not None


def test_unmapped_capability_remains_blocked_without_generator_call(
    tmp_path,
) -> None:
    generator_root = tmp_path / "generator"
    _fake_generator(generator_root)
    registry = _registry(tmp_path)
    acquirer = PluginGeneratorAcquirer(
        _policy(generator_root, tmp_path / "generated"),
        registry,
    )
    graph = TaskGraph.create(
        "Use unknown capability",
        required_capabilities=["unknown.capability"],
    )

    result = _scheduler(registry, acquirer).run(graph)

    assert result.root.status == TaskStatus.BLOCKED
    assert result.root.error == "missing_capabilities:unknown.capability"
    assert not (generator_root / "calls.txt").exists()


def test_corrupt_manifest_is_rejected_and_capability_stays_missing(
    tmp_path,
) -> None:
    generator_root = tmp_path / "generator"
    _fake_generator(generator_root, corrupt_manifest=True)
    registry = _registry(tmp_path)
    acquirer = PluginGeneratorAcquirer(
        _policy(generator_root, tmp_path / "generated"),
        registry,
    )
    graph = TaskGraph.create(
        "Report large Python files",
        required_capabilities=["project.loc_report"],
    )

    result = _scheduler(registry, acquirer).run(graph)

    assert result.root.status == TaskStatus.BLOCKED
    assert registry.get("project.loc_report") is None
    failures = [
        event
        for event in result.events
        if event.event_type == "capability_acquisition_failed"
    ]
    assert len(failures) == 1
    assert "manifest plugin_family mismatch" in failures[0].payload["error"]


def test_registry_rejects_unsupported_schema(tmp_path) -> None:
    path = tmp_path / "capabilities.json"
    path.write_text(
        json.dumps({"schema_version": 999, "generated": []}),
        encoding="utf-8",
    )

    try:
        PersistentCapabilityRegistry(
            path,
            artifact_root=tmp_path / "generated",
        )
    except ValueError as exc:
        assert "unsupported capability registry" in str(exc)
    else:
        raise AssertionError("unsupported registry schema must be rejected")


def test_registry_rejects_descriptor_outside_artifact_root(tmp_path) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    path = tmp_path / "capabilities.json"
    path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "generated": [
                    {
                        "capability": "project.loc_report",
                        "family": "project_loc_reporter",
                        "plugin_id": "outside-plugin",
                        "plugin_root": str(outside),
                        "manifest_path": str(outside / "manifest.json"),
                        "file_sha256": {
                            "plugin.py": "0" * 64,
                            "manifest.json": "0" * 64,
                            "README.md": "0" * 64,
                        },
                    }
                ],
            }
        ),
        encoding="utf-8",
    )

    try:
        PersistentCapabilityRegistry(
            path,
            artifact_root=tmp_path / "generated",
        )
    except ValueError as exc:
        assert "escapes artifact_root" in str(exc)
    else:
        raise AssertionError("external descriptor path must be rejected")
