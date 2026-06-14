"""Bounded stale-reclaiming directory lock for local JSON registries."""

from __future__ import annotations

import shutil
from pathlib import Path
from typing import Callable
from uuid import uuid4


class DirectoryLock:
    def __init__(
        self,
        path: Path,
        *,
        deadline: float,
        stale_after_seconds: float,
        clock: Callable[[], float],
        sleep: Callable[[float], None],
        poll_interval_seconds: float,
    ) -> None:
        self.path = path
        self.deadline = deadline
        self.stale_after_seconds = stale_after_seconds
        self.clock = clock
        self.sleep = sleep
        self.poll_interval_seconds = poll_interval_seconds
        self.acquired = False

    def __enter__(self) -> DirectoryLock:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        while True:
            if self.path.exists():
                self._reclaim_if_stale()
            try:
                self.path.mkdir()
                (self.path / "created_at").write_text(
                    str(self.clock()),
                    encoding="ascii",
                )
                self.acquired = True
                return self
            except FileExistsError:
                if self.clock() >= self.deadline:
                    raise TimeoutError("resource lease registry lock timed out")
                self.sleep(
                    min(
                        self.poll_interval_seconds,
                        max(0.0, self.deadline - self.clock()),
                    )
                )

    def __exit__(self, exc_type, exc, traceback) -> None:
        if self.acquired:
            shutil.rmtree(self.path, ignore_errors=True)
            self.acquired = False

    def _reclaim_if_stale(self) -> None:
        try:
            fallback_created_at = self.path.stat().st_mtime
        except (FileNotFoundError, OSError):
            return
        try:
            created_at = float(
                (self.path / "created_at").read_text(encoding="ascii")
            )
        except (FileNotFoundError, OSError, ValueError):
            created_at = fallback_created_at
        if self.clock() - created_at <= self.stale_after_seconds:
            return
        tombstone = self.path.with_name(f"{self.path.name}.{uuid4().hex}.stale")
        try:
            self.path.rename(tombstone)
        except (FileNotFoundError, FileExistsError, PermissionError, OSError):
            return
        shutil.rmtree(tombstone, ignore_errors=True)
