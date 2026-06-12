from __future__ import annotations

import json
import sys
import threading
import time

from loop_engine.adapters import (
    BoundedSubprocessTool,
    ProcessRegistry,
    SubprocessSpec,
)
from loop_engine.models import LoopDefinition, LoopState


def _state(run_id: str = "registry-owner") -> LoopState:
    return LoopState(run_id=run_id, definition=LoopDefinition(goal="test"))


def test_bounded_process_records_completed_lifecycle(tmp_path) -> None:
    registry = ProcessRegistry(tmp_path / "processes.json")
    tool = BoundedSubprocessTool(
        tmp_path,
        SubprocessSpec(
            argv=(sys.executable, "-c", "print('done')"),
            heartbeat_seconds=0.05,
        ),
        process_registry=registry,
    )

    result = tool({}, _state("run-complete"))

    record = registry.get(result["process_record_id"])
    assert record is not None
    assert record.owner_run_id == "run-complete"
    assert record.status == "completed"
    assert record.exit_code == 0
    assert len(record.command_sha256) == 64
    assert record.finished_at is not None
    assert "done" not in json.dumps(record.command_sha256)

    loaded = ProcessRegistry(tmp_path / "processes.json")
    assert loaded.get(record.record_id) == record


def test_running_process_updates_heartbeat(tmp_path) -> None:
    registry = ProcessRegistry()
    tool = BoundedSubprocessTool(
        tmp_path,
        SubprocessSpec(
            argv=(sys.executable, "-c", "import time; time.sleep(0.4)"),
            timeout_seconds=2,
            heartbeat_seconds=0.05,
        ),
        process_registry=registry,
    )
    result: dict = {}

    thread = threading.Thread(
        target=lambda: result.update(tool({}, _state("run-heartbeat"))),
        daemon=True,
    )
    thread.start()
    deadline = time.time() + 1
    record = None
    while time.time() < deadline:
        running = registry.records(status="running")
        if running and running[0].heartbeat_at > running[0].started_at:
            record = running[0]
            break
        time.sleep(0.02)
    thread.join(timeout=2)

    assert record is not None
    assert not thread.is_alive()
    terminal = registry.get(result["process_record_id"])
    assert terminal is not None
    assert terminal.status == "completed"


def test_timeout_is_recorded_as_terminal_outcome(tmp_path) -> None:
    registry = ProcessRegistry()
    result = BoundedSubprocessTool(
        tmp_path,
        SubprocessSpec(
            argv=(sys.executable, "-c", "import time; time.sleep(5)"),
            timeout_seconds=0.1,
            heartbeat_seconds=0.02,
        ),
        process_registry=registry,
    )({}, _state("run-timeout"))

    record = registry.get(result["process_record_id"])
    assert record is not None
    assert record.status == "timed_out"
    assert record.reason == "timeout"
    assert record.exit_code != 0


def test_stale_reaper_terminates_only_matching_process_identity(tmp_path) -> None:
    now = [100.0]
    registry = ProcessRegistry(clock=lambda: now[0])
    matching = registry.register(
        owner_run_id="owner-a",
        pid=101,
        process_identity="identity-a",
        argv=("python", "-V"),
        cwd=str(tmp_path),
        timeout_seconds=60,
    )
    changed = registry.register(
        owner_run_id="owner-b",
        pid=202,
        process_identity="identity-b",
        argv=("python", "-V"),
        cwd=str(tmp_path),
        timeout_seconds=60,
    )
    now[0] = 200.0
    terminated: list[int] = []

    reaped = registry.reap_stale(
        stale_after_seconds=10,
        identity_lookup=lambda pid: "identity-a" if pid == 101 else "new-process",
        terminate=terminated.append,
    )

    assert terminated == [101]
    assert {record.record_id for record in reaped} == {
        matching.record_id,
        changed.record_id,
    }
    assert registry.get(matching.record_id).status == "terminated"
    assert registry.get(matching.record_id).reason == "stale_heartbeat"
    assert registry.get(changed.record_id).status == "lost"
    assert registry.get(changed.record_id).reason == (
        "process_identity_missing_or_changed"
    )


def test_command_arguments_are_not_persisted(tmp_path) -> None:
    path = tmp_path / "processes.json"
    registry = ProcessRegistry(path)
    secret = "super-secret-command-argument"
    record = registry.register(
        owner_run_id="owner",
        pid=123,
        process_identity="identity",
        argv=("tool", "--token", secret),
        cwd=str(tmp_path),
        timeout_seconds=1,
    )

    serialized = path.read_text(encoding="utf-8")

    assert secret not in serialized
    assert record.command_sha256 in serialized


def test_old_terminal_records_can_be_pruned(tmp_path) -> None:
    now = [10.0]
    registry = ProcessRegistry(clock=lambda: now[0])
    record = registry.register(
        owner_run_id="owner",
        pid=123,
        process_identity="identity",
        argv=("tool",),
        cwd=str(tmp_path),
        timeout_seconds=1,
    )
    registry.finish(record.record_id, status="completed", exit_code=0)
    now[0] = 100.0

    removed = registry.prune_terminal(retain_seconds=20)

    assert removed == 1
    assert registry.get(record.record_id) is None
