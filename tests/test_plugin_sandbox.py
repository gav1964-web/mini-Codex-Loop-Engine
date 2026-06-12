from __future__ import annotations

import sys
from pathlib import Path

import pytest

from loop_engine.tasks import (
    PluginInvocationPolicy,
    PluginInvocationSpec,
    SandboxMount,
    WslBubblewrapSandbox,
)


def test_wsl_bubblewrap_command_is_deny_by_default(tmp_path) -> None:
    plugin_root = tmp_path / "plugin"
    plugin_root.mkdir()
    worker = tmp_path / "plugin_worker.py"
    worker.write_text("", encoding="utf-8")
    project = tmp_path / "project"
    project.mkdir()
    output = tmp_path / "output"
    output.mkdir()
    sandbox = WslBubblewrapSandbox.create(
        distribution="Ubuntu-22.04",
        mounts=[
            SandboxMount.create(
                host_path=project,
                sandbox_path="/data/project",
            ),
            SandboxMount.create(
                host_path=output,
                sandbox_path="/output/result",
                writable=True,
            ),
        ],
    )

    argv = sandbox.build_argv(
        plugin_root=plugin_root,
        worker_path=worker,
        expected_sha256="a" * 64,
        payload_json='{"root":"/data/project"}',
    )

    assert argv[:6] == (
        "wsl.exe",
        "--distribution",
        "Ubuntu-22.04",
        "--exec",
        "/usr/bin/bwrap",
        "--die-with-parent",
    )
    assert "--unshare-all" in argv
    assert "--clearenv" in argv
    assert "--share-net" not in argv
    assert ("--ro-bind", _wsl(plugin_root), "/plugin") in _triples(argv)
    assert "/plugin/plugin.py" in argv
    assert "/runtime/plugin_worker.py" in argv
    assert ("--ro-bind", _wsl(project), "/data/project") in _triples(argv)
    assert ("--bind", _wsl(output), "/output/result") in _triples(argv)


def test_sandbox_mount_restricts_writable_targets(tmp_path) -> None:
    target = tmp_path / "target"
    target.mkdir()

    with pytest.raises(ValueError, match="writable.*restricted"):
        SandboxMount.create(
            host_path=target,
            sandbox_path="/data/project",
            writable=True,
        )
    with pytest.raises(ValueError, match="restricted to /data or /output"):
        SandboxMount.create(
            host_path=target,
            sandbox_path="/etc/project",
        )


def test_parallel_mount_targets_must_be_unique(tmp_path) -> None:
    one = tmp_path / "one"
    two = tmp_path / "two"
    one.mkdir()
    two.mkdir()

    with pytest.raises(ValueError, match="targets must be unique"):
        WslBubblewrapSandbox.create(
            distribution="Ubuntu",
            mounts=[
                SandboxMount.create(
                    host_path=one,
                    sandbox_path="/data/project",
                ),
                SandboxMount.create(
                    host_path=two,
                    sandbox_path="/data/project",
                ),
            ],
        )


def test_strict_invocation_policy_accepts_external_launcher() -> None:
    sandbox = WslBubblewrapSandbox.create(distribution="Ubuntu")

    policy = PluginInvocationPolicy.create(
        invocations={
            "example.capability": PluginInvocationSpec.create(
                payload={},
                requires_os_sandbox=True,
            )
        },
        python_executable=sys.executable,
        sandbox_launcher=sandbox,
    )

    assert policy.sandbox_launcher is sandbox
    assert policy.invocations["example.capability"].requires_os_sandbox is True


def _triples(argv: tuple[str, ...]) -> set[tuple[str, str, str]]:
    return {
        (argv[index], argv[index + 1], argv[index + 2])
        for index in range(len(argv) - 2)
    }


def _wsl(path: Path) -> str:
    value = str(path.resolve()).replace("\\", "/")
    drive, remainder = value.split(":/", 1)
    return f"/mnt/{drive.lower()}/{remainder}"
