"""Run a real WSL bubblewrap isolation smoke for a generated plugin."""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import sys
from pathlib import Path

from loop_engine.tasks import (
    FunctionIntegrationVerifier,
    GeneratedCapability,
    GeneratedPluginLeafExecutor,
    PersistentCapabilityRegistry,
    PluginInvocationPolicy,
    PluginInvocationSpec,
    SandboxMount,
    ScriptedTaskDecomposer,
    TaskGraph,
    TaskScheduler,
    WslBubblewrapSandbox,
)

_PLUGIN_SOURCE = """
from pathlib import Path
import socket


def run(payload):
    input_text = Path(payload["input_path"]).read_text(encoding="utf-8").strip()
    try:
        Path("/data/project/forbidden.txt").write_text("bad", encoding="utf-8")
        data_write_blocked = False
    except OSError:
        data_write_blocked = True
    host_hidden = not Path(payload["host_probe_path"]).exists()
    try:
        socket.create_connection(("1.1.1.1", 53), timeout=0.5).close()
        network_blocked = False
    except OSError:
        network_blocked = True
    output = Path(payload["output_path"])
    output.write_text("sandbox-output\\n", encoding="utf-8")
    secure = data_write_blocked and host_hidden and network_blocked
    return {
        "status": "ok" if secure else "failed",
        "summary": "real sandbox isolation verified" if secure else "isolation failed",
        "input_text": input_text,
        "data_write_blocked": data_write_blocked,
        "host_hidden": host_hidden,
        "network_blocked": network_blocked,
        "output_written": output.is_file(),
    }
""".lstrip()


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser()
    parser.add_argument("--distribution", default="Ubuntu-22.04")
    parser.add_argument(
        "--work-root",
        type=Path,
        default=Path("build/plugin_sandbox_smoke"),
    )
    return parser


def _write_bundle(plugin_root: Path) -> dict[str, str]:
    files = {
        "plugin.py": _PLUGIN_SOURCE,
        "manifest.json": json.dumps(
            {
                "plugin_id": "sandbox-smoke",
                "plugin_family": "sandbox_smoke",
                "entrypoint": "plugin.py:run",
                "requested_capabilities": ["sandbox.smoke"],
            }
        ),
        "README.md": "# Sandbox smoke\n",
    }
    plugin_root.mkdir(parents=True)
    for name, content in files.items():
        (plugin_root / name).write_text(content, encoding="utf-8")
    return {
        name: hashlib.sha256((plugin_root / name).read_bytes()).hexdigest()
        for name in files
    }


def main() -> int:
    args = _parser().parse_args()
    work_root = args.work_root.resolve()
    if work_root.exists():
        shutil.rmtree(work_root)
    plugin_root = work_root / "generated" / "sandbox-smoke"
    data_root = work_root / "data"
    output_root = work_root / "output"
    data_root.mkdir(parents=True)
    output_root.mkdir(parents=True)
    (data_root / "input.txt").write_text("sandbox-input\n", encoding="utf-8")

    registry = PersistentCapabilityRegistry(
        work_root / "capabilities.json",
        artifact_root=work_root / "generated",
    )
    registry.register(
        GeneratedCapability(
            capability="sandbox.smoke",
            family="sandbox_smoke",
            plugin_id="sandbox-smoke",
            plugin_root=str(plugin_root),
            manifest_path=str(plugin_root / "manifest.json"),
            file_sha256=_write_bundle(plugin_root),
        )
    )
    sandbox = WslBubblewrapSandbox.create(
        distribution=args.distribution,
        mounts=[
            SandboxMount.create(
                host_path=data_root,
                sandbox_path="/data/project",
            ),
            SandboxMount.create(
                host_path=output_root,
                sandbox_path="/output/result",
                writable=True,
            ),
        ],
        probe_timeout_seconds=20,
    )
    if not sandbox.probe(work_root, run_id="real-sandbox-smoke"):
        print(
            json.dumps(
                {
                    "status": "blocked",
                    "error": "wsl_bubblewrap_unavailable",
                },
                indent=2,
            )
        )
        return 2

    host_probe = (Path.cwd() / "pyproject.toml").resolve()
    policy = PluginInvocationPolicy.create(
        invocations={
            "sandbox.smoke": PluginInvocationSpec.create(
                payload={
                    "input_path": "/data/project/input.txt",
                    "output_path": "/output/result/result.txt",
                    "host_probe_path": _to_wsl_path(host_probe),
                },
                required_output_fields=(
                    "status",
                    "input_text",
                    "data_write_blocked",
                    "host_hidden",
                    "network_blocked",
                    "output_written",
                ),
                requires_os_sandbox=True,
            )
        },
        python_executable=sys.executable,
        timeout_seconds=30,
        sandbox_launcher=sandbox,
    )
    graph = TaskGraph.create(
        "Verify real OS sandbox",
        required_capabilities=["sandbox.smoke"],
    )
    result = TaskScheduler(
        decomposer=ScriptedTaskDecomposer({}),
        capability_resolver=registry,
        leaf_executor=GeneratedPluginLeafExecutor(registry, policy),
        integration_verifier=FunctionIntegrationVerifier(),
    ).run(graph)
    evidence = result.root.result.evidence if result.root.result is not None else {}
    output = evidence.get("output", {})
    checks = {
        "completed": str(result.root.status) == "completed",
        "backend": evidence.get("sandbox_backend") == "wsl_bubblewrap",
        "data_write_blocked": output.get("data_write_blocked") is True,
        "host_hidden": output.get("host_hidden") is True,
        "network_blocked": output.get("network_blocked") is True,
        "output_written": output.get("output_written") is True,
        "read_only_data_unchanged": not (data_root / "forbidden.txt").exists(),
        "output_materialized": (output_root / "result.txt").is_file(),
    }
    print(
        json.dumps(
            {
                "status": str(result.root.status),
                "error": result.root.error,
                "checks": checks,
                "evidence": evidence,
            },
            ensure_ascii=False,
            indent=2,
            default=str,
        )
    )
    return 0 if all(checks.values()) else 1


def _to_wsl_path(path: Path) -> str:
    value = str(path).replace("\\", "/")
    drive, remainder = value.split(":/", 1)
    return f"/mnt/{drive.lower()}/{remainder}"


if __name__ == "__main__":
    raise SystemExit(main())
