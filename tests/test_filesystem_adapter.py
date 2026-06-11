from __future__ import annotations

import hashlib

import pytest

from loop_engine.adapters import BoundedFilesystem, ToolRegistryExecutor
from loop_engine.models import Action, LoopDefinition, LoopState


def _state() -> LoopState:
    return LoopState(run_id="filesystem-test", definition=LoopDefinition(goal="test"))


def _execute(tmp_path, tool: str, arguments: dict):
    executor = ToolRegistryExecutor()
    BoundedFilesystem(tmp_path, max_read_bytes=128).register(executor)
    return executor.execute(Action(tool=tool, arguments=arguments), _state())


def test_list_read_and_search_are_bounded_to_workspace(tmp_path) -> None:
    (tmp_path / "src").mkdir()
    target = tmp_path / "src" / "sample.py"
    target.write_text("alpha\nneedle\nomega\n", encoding="utf-8")

    listing = _execute(tmp_path, "list_files", {"path": "."})
    reading = _execute(tmp_path, "read_text", {"path": "src/sample.py"})
    search = _execute(tmp_path, "search_text", {"path": ".", "query": "needle"})

    assert listing.status == "ok"
    assert any(item["path"] == "src/sample.py" for item in listing.output["entries"])
    assert reading.output["sha256"] == hashlib.sha256(target.read_bytes()).hexdigest()
    assert search.output["matches"] == [
        {"path": "src/sample.py", "line": 2, "text": "needle"}
    ]


def test_path_traversal_becomes_structured_tool_error(tmp_path) -> None:
    result = _execute(tmp_path, "read_text", {"path": "../outside.txt"})

    assert result.status == "error"
    assert "escapes workspace" in (result.error or "")


@pytest.mark.parametrize("path", [".env", ".env.local", ".git/config"])
def test_protected_project_files_are_not_readable(tmp_path, path: str) -> None:
    target = tmp_path / path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("secret", encoding="utf-8")

    result = _execute(tmp_path, "read_text", {"path": path})

    assert result.status == "error"
    assert "protected" in (result.error or "")


def test_env_example_remains_readable(tmp_path) -> None:
    (tmp_path / ".env.example").write_text("TOKEN=", encoding="utf-8")

    result = _execute(tmp_path, "read_text", {"path": ".env.example"})

    assert result.status == "ok"
    assert result.output["text"] == "TOKEN="


def test_symlink_paths_are_rejected(tmp_path) -> None:
    target = tmp_path / "target.txt"
    target.write_text("content", encoding="utf-8")
    link = tmp_path / "link.txt"
    try:
        link.symlink_to(target)
    except OSError:
        pytest.skip("symlinks are not available in this environment")

    result = _execute(tmp_path, "read_text", {"path": "link.txt"})

    assert result.status == "error"
    assert "symlink" in (result.error or "")


def test_read_text_truncates_captured_content(tmp_path) -> None:
    (tmp_path / "large.txt").write_text("x" * 500, encoding="utf-8")

    result = _execute(tmp_path, "read_text", {"path": "large.txt", "max_bytes": 32})

    assert result.status == "ok"
    assert len(result.output["text"]) == 32
    assert result.output["size"] == 500
    assert result.output["truncated"] is True


def test_apply_patch_is_atomic_and_idempotent(tmp_path) -> None:
    target = tmp_path / "sample.py"
    target.write_text("def value():\n    return 1\n", encoding="utf-8")
    arguments = {
        "path": "sample.py",
        "old_text": "return 1",
        "new_text": "return 2",
    }

    first = _execute(tmp_path, "apply_patch", arguments)
    second = _execute(tmp_path, "apply_patch", arguments)

    assert first.status == "ok"
    assert first.output["status"] == "applied"
    assert second.status == "ok"
    assert second.output["status"] == "already_applied"
    assert "return 2" in target.read_text(encoding="utf-8")
    assert not list(tmp_path.glob(".*.tmp"))


def test_apply_patch_rejects_stale_sha(tmp_path) -> None:
    target = tmp_path / "sample.py"
    target.write_text("old", encoding="utf-8")

    result = _execute(
        tmp_path,
        "apply_patch",
        {
            "path": "sample.py",
            "old_text": "old",
            "new_text": "new",
            "expected_sha256": "0" * 64,
        },
    )

    assert result.status == "error"
    assert "sha256" in (result.error or "")
    assert target.read_text(encoding="utf-8") == "old"


def test_apply_patch_rejects_files_above_limit(tmp_path) -> None:
    (tmp_path / "large.txt").write_text("x" * 500, encoding="utf-8")

    result = _execute(
        tmp_path,
        "apply_patch",
        {"path": "large.txt", "old_text": "x", "new_text": "y"},
    )

    assert result.status == "error"
    assert "size limit" in (result.error or "")
