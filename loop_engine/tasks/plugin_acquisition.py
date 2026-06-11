"""Bounded capability acquisition through the standalone Plugin Generator."""

from __future__ import annotations

import hashlib
import json
import os
import re
import sys
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from ..adapters import BoundedSubprocessTool, SubprocessSpec
from ..models import LoopDefinition, LoopState
from .models import CapabilityResolution, TaskGraph, TaskNode

_NAME_PATTERN = re.compile(r"^[a-z][a-z0-9_.-]{0,63}$")
_PLUGIN_ID_PATTERN = re.compile(r"^[a-z0-9][a-z0-9_-]{0,127}$")
_REQUIRED_FILES = {"plugin.py", "manifest.json", "README.md"}


@dataclass(frozen=True, slots=True)
class GeneratedCapability:
    capability: str
    family: str
    plugin_id: str
    plugin_root: str
    manifest_path: str
    file_sha256: dict[str, str]


class PersistentCapabilityRegistry:
    def __init__(
        self,
        storage_path: str | Path,
        *,
        artifact_root: str | Path,
        built_in: set[str] | None = None,
    ) -> None:
        self.storage_path = Path(storage_path).resolve()
        self.artifact_root = Path(artifact_root).resolve()
        self.built_in = set(built_in or set())
        self.generated: dict[str, GeneratedCapability] = {}
        self._load()

    def resolve(self, node: TaskNode, graph: TaskGraph) -> CapabilityResolution:
        known = self.built_in | {
            capability
            for capability, descriptor in self.generated.items()
            if self._descriptor_is_current(descriptor)
        }
        required = set(node.required_capabilities)
        return CapabilityResolution(
            available=sorted(required & known),
            missing=sorted(required - known),
        )

    def register(self, descriptor: GeneratedCapability) -> None:
        if not self._descriptor_inside_root(descriptor):
            raise ValueError("capability descriptor escapes artifact_root")
        existing = self.generated.get(descriptor.capability)
        if existing is not None and existing != descriptor:
            raise ValueError(
                f"capability already registered with another plugin: "
                f"{descriptor.capability}"
            )
        self.generated[descriptor.capability] = descriptor
        self._save()

    def get(self, capability: str) -> GeneratedCapability | None:
        descriptor = self.generated.get(capability)
        if descriptor is None or not self._descriptor_is_current(descriptor):
            return None
        return descriptor

    def _load(self) -> None:
        if not self.storage_path.exists():
            return
        payload = json.loads(self.storage_path.read_text(encoding="utf-8"))
        if payload.get("schema_version") != 1:
            raise ValueError("unsupported capability registry schema_version")
        rows = payload.get("generated")
        if not isinstance(rows, list):
            raise ValueError("capability registry generated must be an array")
        loaded: dict[str, GeneratedCapability] = {}
        for row in rows:
            descriptor = GeneratedCapability(**dict(row))
            if descriptor.capability in loaded:
                raise ValueError("duplicate capability in registry")
            if not self._descriptor_inside_root(descriptor):
                raise ValueError("capability descriptor escapes artifact_root")
            loaded[descriptor.capability] = descriptor
        self.generated = loaded

    def _descriptor_inside_root(self, descriptor: GeneratedCapability) -> bool:
        root = Path(descriptor.plugin_root).resolve()
        manifest = Path(descriptor.manifest_path).resolve()
        try:
            root.relative_to(self.artifact_root)
            manifest.relative_to(root)
        except ValueError:
            return False
        return True

    def _descriptor_is_current(self, descriptor: GeneratedCapability) -> bool:
        root = Path(descriptor.plugin_root).resolve()
        if not self._descriptor_inside_root(descriptor):
            return False
        expected_names = _REQUIRED_FILES
        if set(descriptor.file_sha256) != expected_names:
            return False
        for name, expected_digest in descriptor.file_sha256.items():
            path = root / name
            if not path.is_file():
                return False
            if hashlib.sha256(path.read_bytes()).hexdigest() != expected_digest:
                return False
        return (root / "manifest.json").resolve() == Path(
            descriptor.manifest_path
        ).resolve()

    def _save(self) -> None:
        self.storage_path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "schema_version": 1,
            "generated": [
                asdict(self.generated[key])
                for key in sorted(self.generated)
            ],
        }
        temporary = self.storage_path.with_name(
            f".{self.storage_path.name}.tmp"
        )
        temporary.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        os.replace(temporary, self.storage_path)


@dataclass(frozen=True, slots=True)
class PluginAcquisitionPolicy:
    generator_root: Path
    output_root: Path
    capability_families: dict[str, str]
    python_executable: str = sys.executable
    constraints: tuple[str, ...] = ("offline",)
    timeout_seconds: float = 60.0
    max_output_bytes: int = 512 * 1024

    @classmethod
    def create(
        cls,
        *,
        generator_root: str | Path,
        output_root: str | Path,
        capability_families: dict[str, str],
        python_executable: str = sys.executable,
        constraints: list[str] | tuple[str, ...] = ("offline",),
        timeout_seconds: float = 60.0,
        max_output_bytes: int = 512 * 1024,
    ) -> PluginAcquisitionPolicy:
        root = Path(generator_root).resolve()
        output = Path(output_root).resolve()
        if not root.is_dir():
            raise ValueError("plugin generator root must be an existing directory")
        if not (root / "plugin_generator").is_dir():
            raise ValueError("plugin generator public package is missing")
        if timeout_seconds <= 0 or max_output_bytes <= 0:
            raise ValueError("plugin acquisition bounds must be positive")
        normalized: dict[str, str] = {}
        for capability, family in capability_families.items():
            capability_name = capability.strip()
            family_name = family.strip()
            if not _NAME_PATTERN.fullmatch(capability_name):
                raise ValueError(f"invalid capability name: {capability}")
            if not _NAME_PATTERN.fullmatch(family_name):
                raise ValueError(f"invalid plugin family: {family}")
            normalized[capability_name] = family_name
        if not normalized:
            raise ValueError("capability_families must be non-empty")
        return cls(
            generator_root=root,
            output_root=output,
            capability_families=normalized,
            python_executable=str(python_executable),
            constraints=tuple(str(item) for item in constraints),
            timeout_seconds=timeout_seconds,
            max_output_bytes=max_output_bytes,
        )


class PluginGeneratorAcquirer:
    def __init__(
        self,
        policy: PluginAcquisitionPolicy,
        registry: PersistentCapabilityRegistry,
    ) -> None:
        self.policy = policy
        self.registry = registry
        if self.registry.artifact_root != self.policy.output_root:
            raise ValueError(
                "capability registry artifact_root must match acquisition output_root"
            )

    def acquire(
        self,
        capability: str,
        node: TaskNode,
        graph: TaskGraph,
    ) -> bool:
        if self.registry.get(capability) is not None:
            return True
        family = self.policy.capability_families.get(capability)
        if family is None:
            return False
        self.policy.output_root.mkdir(parents=True, exist_ok=True)
        argv = [
            self.policy.python_executable,
            "-m",
            "plugin_generator",
            "generate",
            "--family",
            family,
            "--capability",
            capability,
            "--output",
            str(self.policy.output_root),
        ]
        for constraint in self.policy.constraints:
            argv.extend(["--constraint", constraint])
        tool = BoundedSubprocessTool(
            self.policy.generator_root,
            SubprocessSpec(
                argv=tuple(argv),
                timeout_seconds=self.policy.timeout_seconds,
                max_output_bytes=self.policy.max_output_bytes,
                environment={
                    "PYTHONIOENCODING": "utf-8",
                    "PYTHONUTF8": "1",
                },
            ),
        )
        process = tool(
            {},
            LoopState(
                run_id=f"acquire-{graph.id}-{node.id}",
                definition=LoopDefinition(goal=f"Acquire {capability}"),
            ),
        )
        descriptor = self._validate_result(
            process,
            capability=capability,
            family=family,
        )
        self.registry.register(descriptor)
        return True

    def _validate_result(
        self,
        process: dict[str, Any],
        *,
        capability: str,
        family: str,
    ) -> GeneratedCapability:
        if process.get("timed_out"):
            raise RuntimeError("plugin generator timed out")
        if process.get("stdout_truncated") or process.get("stderr_truncated"):
            raise RuntimeError("plugin generator output was truncated")
        if process.get("exit_code") != 0:
            stderr = str(process.get("stderr", ""))[:2000]
            raise RuntimeError(
                f"plugin generator exited with code {process.get('exit_code')}: "
                f"{stderr}"
            )
        try:
            payload = json.loads(str(process.get("stdout", "")))
        except json.JSONDecodeError as exc:
            raise ValueError("plugin generator stdout is not valid JSON") from exc
        if not isinstance(payload, dict) or payload.get("status") != "success":
            raise ValueError("plugin generator did not return success")
        plugin_id = payload.get("plugin_id")
        if not isinstance(plugin_id, str) or not _PLUGIN_ID_PATTERN.fullmatch(
            plugin_id
        ):
            raise ValueError("plugin generator returned invalid plugin_id")
        if payload.get("family") != family:
            raise ValueError("generated plugin family does not match policy")
        materialized_root = Path(str(payload.get("materialized_root", ""))).resolve()
        if not materialized_root.is_dir() or not materialized_root.samefile(
            self.policy.output_root
        ):
            raise ValueError("generated materialized_root does not match policy")
        plugin_root = (self.policy.output_root / plugin_id).resolve()
        self._inside_output_root(plugin_root)
        if not plugin_root.is_dir():
            raise ValueError("generated plugin root is missing")

        written_files = payload.get("written_files")
        if not isinstance(written_files, list):
            raise ValueError("generated written_files must be an array")
        resolved_files = {Path(str(item)).resolve() for item in written_files}
        for path in resolved_files:
            self._inside_output_root(path)
        expected_files = {plugin_root / name for name in _REQUIRED_FILES}
        if len(resolved_files) != len(expected_files) or not all(
            any(path.is_file() and path.samefile(expected) for path in resolved_files)
            for expected in expected_files
        ):
            raise ValueError("generated plugin file set is incomplete or unexpected")
        if not all(path.is_file() for path in expected_files):
            raise ValueError("generated plugin files are missing")

        manifest_path = plugin_root / "manifest.json"
        manifest_bytes = manifest_path.read_bytes()
        manifest = json.loads(manifest_bytes.decode("utf-8"))
        if manifest.get("plugin_id") != plugin_id:
            raise ValueError("manifest plugin_id mismatch")
        if manifest.get("plugin_family") != family:
            raise ValueError("manifest plugin_family mismatch")
        if manifest.get("entrypoint") != "plugin.py:run":
            raise ValueError("manifest entrypoint mismatch")
        capabilities = manifest.get("requested_capabilities")
        if not isinstance(capabilities, list) or capability not in capabilities:
            raise ValueError("manifest does not declare requested capability")

        return GeneratedCapability(
            capability=capability,
            family=family,
            plugin_id=plugin_id,
            plugin_root=str(plugin_root),
            manifest_path=str(manifest_path),
            file_sha256={
                path.name: hashlib.sha256(path.read_bytes()).hexdigest()
                for path in sorted(expected_files)
            },
        )

    def _inside_output_root(self, path: Path) -> None:
        if path.exists():
            for parent in (path, *path.parents):
                try:
                    if parent.samefile(self.policy.output_root):
                        return
                except OSError:
                    continue
        try:
            path.relative_to(self.policy.output_root)
        except ValueError as exc:
            raise ValueError(f"generated path escapes output root: {path}") from exc
