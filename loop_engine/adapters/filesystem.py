"""Bounded filesystem tools rooted in one workspace."""

from __future__ import annotations

import hashlib
import os
import re
from pathlib import Path
from typing import Any

from ..models import LoopState
from .tools import ToolRegistryExecutor


class BoundedFilesystem:
    def __init__(
        self,
        workspace_root: str | Path,
        *,
        max_read_bytes: int = 256 * 1024,
        max_list_entries: int = 500,
        max_search_matches: int = 200,
    ) -> None:
        self.root = Path(workspace_root).resolve()
        if not self.root.is_dir():
            raise ValueError(f"workspace does not exist: {self.root}")
        self.max_read_bytes = max_read_bytes
        self.max_list_entries = max_list_entries
        self.max_search_matches = max_search_matches

    def register(self, executor: ToolRegistryExecutor) -> None:
        executor.register("list_files", self.list_files)
        executor.register("read_text", self.read_text)
        executor.register("search_text", self.search_text)
        executor.register("apply_patch", self.apply_patch)

    def list_files(self, arguments: dict[str, Any], state: LoopState) -> dict[str, Any]:
        base = self._resolve_existing(arguments.get("path", "."), require_directory=True)
        recursive = bool(arguments.get("recursive", True))
        requested_limit = int(arguments.get("max_entries", self.max_list_entries))
        limit = min(max(1, requested_limit), self.max_list_entries)
        iterator = base.rglob("*") if recursive else base.iterdir()
        entries: list[dict[str, Any]] = []
        truncated = False
        for path in iterator:
            if self._is_ignored(path):
                continue
            if len(entries) >= limit:
                truncated = True
                break
            entries.append(
                {
                    "path": path.relative_to(self.root).as_posix(),
                    "type": "directory" if path.is_dir() else "file",
                    "size": path.stat().st_size if path.is_file() else None,
                }
            )
        return {"entries": entries, "truncated": truncated, "root": str(self.root)}

    def read_text(self, arguments: dict[str, Any], state: LoopState) -> dict[str, Any]:
        path = self._resolve_existing(self._required(arguments, "path"), require_file=True)
        requested_limit = int(arguments.get("max_bytes", self.max_read_bytes))
        limit = min(max(1, requested_limit), self.max_read_bytes)
        digest = hashlib.sha256()
        captured = bytearray()
        size = 0
        with path.open("rb") as stream:
            while chunk := stream.read(64 * 1024):
                digest.update(chunk)
                size += len(chunk)
                remaining = limit - len(captured)
                if remaining > 0:
                    captured.extend(chunk[:remaining])
        return {
            "path": path.relative_to(self.root).as_posix(),
            "text": bytes(captured).decode("utf-8", errors="replace"),
            "size": size,
            "sha256": digest.hexdigest(),
            "truncated": size > limit,
        }

    def search_text(self, arguments: dict[str, Any], state: LoopState) -> dict[str, Any]:
        base = self._resolve_existing(arguments.get("path", "."), require_directory=True)
        query = str(self._required(arguments, "query"))
        use_regex = bool(arguments.get("regex", False))
        case_sensitive = bool(arguments.get("case_sensitive", True))
        requested_limit = int(arguments.get("max_matches", self.max_search_matches))
        limit = min(max(1, requested_limit), self.max_search_matches)
        flags = 0 if case_sensitive else re.IGNORECASE
        pattern = re.compile(query if use_regex else re.escape(query), flags)
        matches: list[dict[str, Any]] = []
        truncated = False

        for path in base.rglob("*"):
            if self._is_ignored(path) or not path.is_file():
                continue
            try:
                if path.stat().st_size > self.max_read_bytes:
                    continue
                lines = path.read_text(encoding="utf-8").splitlines()
            except (OSError, UnicodeDecodeError):
                continue
            for line_number, line in enumerate(lines, start=1):
                if pattern.search(line):
                    if len(matches) >= limit:
                        truncated = True
                        break
                    matches.append(
                        {
                            "path": path.relative_to(self.root).as_posix(),
                            "line": line_number,
                            "text": line,
                        }
                    )
            if truncated:
                break
        return {"matches": matches, "truncated": truncated, "query": query}

    def apply_patch(self, arguments: dict[str, Any], state: LoopState) -> dict[str, Any]:
        path = self._resolve_existing(self._required(arguments, "path"), require_file=True)
        old_text = str(self._required(arguments, "old_text"))
        new_text = str(arguments.get("new_text", ""))
        expected_replacements = int(arguments.get("expected_replacements", 1))
        expected_sha256 = arguments.get("expected_sha256")
        if not old_text:
            raise ValueError("old_text must not be empty")
        if expected_replacements <= 0:
            raise ValueError("expected_replacements must be positive")

        if path.stat().st_size > self.max_read_bytes:
            raise ValueError(f"file exceeds patch size limit: {self.max_read_bytes} bytes")
        raw = path.read_bytes()
        before_sha256 = hashlib.sha256(raw).hexdigest()
        if expected_sha256 and before_sha256 != str(expected_sha256):
            raise ValueError("file sha256 does not match expected_sha256")
        text = raw.decode("utf-8")
        occurrences = text.count(old_text)
        if occurrences == 0 and new_text and text.count(new_text) >= expected_replacements:
            return {
                "path": path.relative_to(self.root).as_posix(),
                "status": "already_applied",
                "replacements": 0,
                "before_sha256": before_sha256,
                "after_sha256": before_sha256,
            }
        if occurrences != expected_replacements:
            raise ValueError(
                f"expected {expected_replacements} old_text occurrence(s), found {occurrences}"
            )

        updated = text.replace(old_text, new_text, expected_replacements)
        encoded = updated.encode("utf-8")
        run_key = hashlib.sha256(state.run_id.encode("utf-8")).hexdigest()[:12]
        temporary = path.with_name(f".{path.name}.{run_key}.tmp")
        try:
            temporary.write_bytes(encoded)
            os.replace(temporary, path)
        finally:
            if temporary.exists():
                temporary.unlink()
        return {
            "path": path.relative_to(self.root).as_posix(),
            "status": "applied",
            "replacements": expected_replacements,
            "before_sha256": before_sha256,
            "after_sha256": hashlib.sha256(encoded).hexdigest(),
        }

    def _resolve_existing(
        self,
        value: str | Path,
        *,
        require_file: bool = False,
        require_directory: bool = False,
    ) -> Path:
        candidate = Path(value)
        if not candidate.is_absolute():
            candidate = self.root / candidate
        lexical = Path(os.path.abspath(candidate))
        try:
            relative = lexical.relative_to(self.root)
        except ValueError as exc:
            raise ValueError(f"path escapes workspace: {lexical}") from exc
        if self._is_protected(relative):
            raise ValueError(f"path is protected: {relative.as_posix()}")
        current = self.root
        for part in relative.parts:
            current /= part
            if current.is_symlink():
                raise ValueError(f"symlink paths are not allowed: {relative.as_posix()}")
        resolved = candidate.resolve(strict=True)
        if require_file and not resolved.is_file():
            raise ValueError(f"path is not a file: {resolved}")
        if require_directory and not resolved.is_dir():
            raise ValueError(f"path is not a directory: {resolved}")
        return resolved

    def _is_ignored(self, path: Path) -> bool:
        if path.is_symlink():
            return True
        try:
            relative = path.relative_to(self.root)
        except ValueError:
            return True
        return self._is_protected(relative)

    @staticmethod
    def _is_protected(relative: Path) -> bool:
        if any(part in {".git", "__pycache__", ".pytest_cache"} for part in relative.parts):
            return True
        name = relative.name
        return name == ".env" or (name.startswith(".env.") and name != ".env.example")

    @staticmethod
    def _required(arguments: dict[str, Any], name: str) -> Any:
        if name not in arguments:
            raise ValueError(f"{name} is required")
        return arguments[name]
