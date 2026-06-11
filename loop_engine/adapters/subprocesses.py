"""Bounded subprocess execution with timeout and process-tree termination."""

from __future__ import annotations

import os
import signal
import subprocess
import threading
from dataclasses import dataclass, field
from pathlib import Path
from time import perf_counter
from typing import Any, BinaryIO

from ..models import LoopState


@dataclass(frozen=True, slots=True)
class SubprocessSpec:
    argv: tuple[str, ...]
    cwd: str = "."
    timeout_seconds: float = 60.0
    max_output_bytes: int = 64 * 1024
    environment: dict[str, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.argv or not self.argv[0].strip():
            raise ValueError("subprocess argv is required")
        if self.timeout_seconds <= 0:
            raise ValueError("subprocess timeout must be positive")
        if self.max_output_bytes <= 0:
            raise ValueError("subprocess output limit must be positive")


class _BoundedStream:
    def __init__(self, limit: int) -> None:
        self.limit = limit
        self.data = bytearray()
        self.truncated = False

    def drain(self, stream: BinaryIO) -> None:
        try:
            while chunk := stream.read(8192):
                remaining = self.limit - len(self.data)
                if remaining > 0:
                    self.data.extend(chunk[:remaining])
                if len(chunk) > remaining:
                    self.truncated = True
        finally:
            stream.close()

    def text(self) -> str:
        return self.data.decode("utf-8", errors="replace")


class BoundedSubprocessTool:
    """Execute one immutable command specification inside a bounded workspace."""

    def __init__(self, workspace_root: str | Path, spec: SubprocessSpec) -> None:
        self.workspace_root = Path(workspace_root).resolve()
        self.spec = spec
        self.cwd = self._resolve_cwd(spec.cwd)

    def _resolve_cwd(self, cwd: str) -> Path:
        candidate = Path(cwd)
        if not candidate.is_absolute():
            candidate = self.workspace_root / candidate
        resolved = candidate.resolve()
        try:
            resolved.relative_to(self.workspace_root)
        except ValueError as exc:
            raise ValueError(f"subprocess cwd escapes workspace: {resolved}") from exc
        if not resolved.is_dir():
            raise ValueError(f"subprocess cwd does not exist: {resolved}")
        return resolved

    def __call__(self, arguments: dict[str, Any], state: LoopState) -> dict[str, Any]:
        if arguments:
            raise ValueError("bounded subprocess tool does not accept runtime arguments")

        started = perf_counter()
        stdout = _BoundedStream(self.spec.max_output_bytes)
        stderr = _BoundedStream(self.spec.max_output_bytes)
        environment = os.environ.copy()
        environment.update(self.spec.environment)

        popen_options: dict[str, Any] = {}
        if os.name == "nt":
            popen_options["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
        else:
            popen_options["start_new_session"] = True

        process = subprocess.Popen(
            list(self.spec.argv),
            cwd=self.cwd,
            env=environment,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            shell=False,
            **popen_options,
        )
        assert process.stdout is not None
        assert process.stderr is not None

        stdout_thread = threading.Thread(target=stdout.drain, args=(process.stdout,), daemon=True)
        stderr_thread = threading.Thread(target=stderr.drain, args=(process.stderr,), daemon=True)
        stdout_thread.start()
        stderr_thread.start()

        timed_out = False
        try:
            process.wait(timeout=self.spec.timeout_seconds)
        except subprocess.TimeoutExpired:
            timed_out = True
            self._terminate_tree(process)
            process.wait(timeout=5)
        finally:
            stdout_thread.join(timeout=5)
            stderr_thread.join(timeout=5)

        return {
            "argv": list(self.spec.argv),
            "cwd": str(self.cwd),
            "exit_code": process.returncode,
            "timed_out": timed_out,
            "stdout": stdout.text(),
            "stderr": stderr.text(),
            "stdout_truncated": stdout.truncated,
            "stderr_truncated": stderr.truncated,
            "duration_seconds": perf_counter() - started,
        }

    @staticmethod
    def _terminate_tree(process: subprocess.Popen[bytes]) -> None:
        if process.poll() is not None:
            return
        if os.name == "nt":
            subprocess.run(
                ["taskkill", "/PID", str(process.pid), "/T", "/F"],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=5,
                check=False,
            )
        else:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
        if process.poll() is None:
            process.kill()
