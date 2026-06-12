from __future__ import annotations

import sys

import pytest

from loop_engine import LoopStatus
from loop_engine.adapters import (
    BoundedSubprocessTool,
    SubprocessSpec,
    get_global_process_registry,
)
from loop_engine.models import LoopDefinition, LoopState
from loop_engine.profiles import build_coding_check_loop


def _state() -> LoopState:
    return LoopState(run_id="subprocess-test", definition=LoopDefinition(goal="test"))


def test_subprocess_timeout_returns_structured_result(tmp_path) -> None:
    tool = BoundedSubprocessTool(
        tmp_path,
        SubprocessSpec(
            argv=(sys.executable, "-c", "import time; time.sleep(5)"),
            timeout_seconds=0.1,
        ),
    )

    result = tool({}, _state())

    assert result["timed_out"] is True
    assert result["exit_code"] != 0
    assert result["duration_seconds"] < 3
    assert len(result["process_record_id"]) == 32
    record = get_global_process_registry().get(result["process_record_id"])
    assert record is not None
    assert record.owner_run_id == "subprocess-test"
    assert record.status == "timed_out"


def test_subprocess_output_is_bounded_while_stream_is_drained(tmp_path) -> None:
    tool = BoundedSubprocessTool(
        tmp_path,
        SubprocessSpec(
            argv=(sys.executable, "-c", "print('x' * 10000)"),
            max_output_bytes=128,
        ),
    )

    result = tool({}, _state())

    assert result["exit_code"] == 0
    assert len(result["stdout"].encode("utf-8")) == 128
    assert result["stdout_truncated"] is True


def test_subprocess_cwd_cannot_escape_workspace(tmp_path) -> None:
    with pytest.raises(ValueError, match="escapes workspace"):
        BoundedSubprocessTool(
            tmp_path,
            SubprocessSpec(argv=(sys.executable, "-V"), cwd=".."),
        )


@pytest.mark.parametrize(
    ("exit_code", "expected_status"),
    [(0, LoopStatus.COMPLETED), (3, LoopStatus.STOPPED)],
)
def test_coding_check_loop_uses_process_exit_code(
    tmp_path,
    exit_code: int,
    expected_status: LoopStatus,
) -> None:
    engine, definition = build_coding_check_loop(
        workspace_root=tmp_path,
        command=[sys.executable, "-c", f"raise SystemExit({exit_code})"],
    )

    state = engine.run(definition)

    assert state.status == expected_status
    assert state.action_count == 1
    assert state.latest_verification.evidence["exit_code"] == exit_code
