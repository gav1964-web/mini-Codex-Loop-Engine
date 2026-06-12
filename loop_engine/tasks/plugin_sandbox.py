"""Fail-closed OS sandbox command construction for generated plugins."""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Protocol

from ..adapters import BoundedSubprocessTool, SubprocessSpec
from ..models import LoopDefinition, LoopState

_WINDOWS_DRIVE = re.compile(r"^([A-Za-z]):[\\/](.*)$")


class PluginSandboxLauncher(Protocol):
    backend_name: str

    def probe(self, workspace_root: Path, *, run_id: str) -> bool:
        ...

    def build_argv(
        self,
        *,
        plugin_root: Path,
        worker_path: Path,
        expected_sha256: str,
        payload_json: str,
    ) -> tuple[str, ...]:
        ...


@dataclass(frozen=True, slots=True)
class SandboxMount:
    host_path: Path
    sandbox_path: str
    writable: bool = False

    @classmethod
    def create(
        cls,
        *,
        host_path: str | Path,
        sandbox_path: str,
        writable: bool = False,
    ) -> SandboxMount:
        host = Path(host_path).resolve()
        if not host.exists():
            raise ValueError(f"sandbox mount host path is missing: {host}")
        target = PurePosixPath(sandbox_path)
        if not target.is_absolute() or ".." in target.parts:
            raise ValueError("sandbox mount path must be absolute and normalized")
        if target == PurePosixPath("/") or target.parts[1] not in {"data", "output"}:
            raise ValueError("sandbox mounts are restricted to /data or /output")
        if writable and target.parts[1] != "output":
            raise ValueError("writable sandbox mounts are restricted to /output")
        return cls(
            host_path=host,
            sandbox_path=str(target),
            writable=writable,
        )


@dataclass(frozen=True, slots=True)
class WslBubblewrapSandbox:
    distribution: str
    mounts: tuple[SandboxMount, ...] = ()
    wsl_executable: str = "wsl.exe"
    bwrap_path: str = "/usr/bin/bwrap"
    python_path: str = "/usr/bin/python3"
    probe_timeout_seconds: float = 10.0
    backend_name: str = "wsl_bubblewrap"

    @classmethod
    def create(
        cls,
        *,
        distribution: str,
        mounts: list[SandboxMount] | tuple[SandboxMount, ...] = (),
        wsl_executable: str = "wsl.exe",
        bwrap_path: str = "/usr/bin/bwrap",
        python_path: str = "/usr/bin/python3",
        probe_timeout_seconds: float = 10.0,
    ) -> WslBubblewrapSandbox:
        distro = distribution.strip()
        if not distro:
            raise ValueError("WSL sandbox distribution is required")
        if probe_timeout_seconds <= 0:
            raise ValueError("sandbox probe timeout must be positive")
        normalized_mounts = tuple(mounts)
        targets = [mount.sandbox_path for mount in normalized_mounts]
        if len(targets) != len(set(targets)):
            raise ValueError("sandbox mount targets must be unique")
        if any(not isinstance(mount, SandboxMount) for mount in normalized_mounts):
            raise TypeError("sandbox mounts must be SandboxMount values")
        return cls(
            distribution=distro,
            mounts=normalized_mounts,
            wsl_executable=str(wsl_executable),
            bwrap_path=_absolute_linux_path(bwrap_path),
            python_path=_absolute_linux_path(python_path),
            probe_timeout_seconds=probe_timeout_seconds,
        )

    def probe(self, workspace_root: Path, *, run_id: str) -> bool:
        process = BoundedSubprocessTool(
            workspace_root,
            SubprocessSpec(
                argv=(
                    self.wsl_executable,
                    "--distribution",
                    self.distribution,
                    "--exec",
                    "/usr/bin/test",
                    "-x",
                    self.bwrap_path,
                ),
                timeout_seconds=self.probe_timeout_seconds,
                max_output_bytes=8 * 1024,
            ),
        )(
            {},
            LoopState(
                run_id=f"sandbox-probe-{run_id}",
                definition=LoopDefinition(goal="Probe WSL bubblewrap sandbox"),
            ),
        )
        return (
            not process.get("timed_out")
            and process.get("exit_code") == 0
            and not process.get("stdout_truncated")
            and not process.get("stderr_truncated")
        )

    def build_argv(
        self,
        *,
        plugin_root: Path,
        worker_path: Path,
        expected_sha256: str,
        payload_json: str,
    ) -> tuple[str, ...]:
        plugin_source = _to_wsl_path(plugin_root.resolve())
        worker_source = _to_wsl_path(worker_path.resolve())
        bwrap = [
            self.wsl_executable,
            "--distribution",
            self.distribution,
            "--exec",
            self.bwrap_path,
            "--die-with-parent",
            "--new-session",
            "--unshare-all",
            "--clearenv",
            "--ro-bind",
            "/usr",
            "/usr",
            "--ro-bind",
            "/lib",
            "/lib",
            "--ro-bind",
            "/lib64",
            "/lib64",
            "--proc",
            "/proc",
            "--dev",
            "/dev",
            "--tmpfs",
            "/tmp",
            "--dir",
            "/runtime",
            "--dir",
            "/data",
            "--dir",
            "/output",
            "--ro-bind",
            plugin_source,
            "/plugin",
            "--ro-bind",
            worker_source,
            "/runtime/plugin_worker.py",
        ]
        for mount in self.mounts:
            bwrap.extend(
                [
                    "--bind" if mount.writable else "--ro-bind",
                    _to_wsl_path(mount.host_path),
                    mount.sandbox_path,
                ]
            )
        bwrap.extend(
            [
                "--chdir",
                "/plugin",
                "--setenv",
                "HOME",
                "/tmp",
                "--setenv",
                "TMPDIR",
                "/tmp",
                "--setenv",
                "PYTHONDONTWRITEBYTECODE",
                "1",
                "--setenv",
                "PYTHONIOENCODING",
                "utf-8",
                self.python_path,
                "-I",
                "/runtime/plugin_worker.py",
                "--plugin",
                "/plugin/plugin.py",
                "--expected-sha256",
                expected_sha256,
                "--payload-json",
                payload_json,
            ]
        )
        return tuple(bwrap)


def _absolute_linux_path(value: str) -> str:
    path = PurePosixPath(value)
    if not path.is_absolute() or ".." in path.parts:
        raise ValueError("sandbox executable path must be absolute")
    return str(path)


def _to_wsl_path(path: Path) -> str:
    value = str(path)
    match = _WINDOWS_DRIVE.match(value)
    if match is None:
        if path.is_absolute() and path.as_posix().startswith("/"):
            return path.as_posix()
        raise ValueError(f"path cannot be translated to WSL: {path}")
    drive, remainder = match.groups()
    normalized = remainder.replace("\\", "/")
    return f"/mnt/{drive.lower()}/{normalized}"
