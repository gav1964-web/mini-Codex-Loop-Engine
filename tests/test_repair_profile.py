from __future__ import annotations

import json
import sys

from loop_engine import Action, LoopPhase, LoopState, LoopStatus, Plan
from loop_engine.cli import main
from loop_engine.events import utc_now
from loop_engine.profiles import build_scripted_repair_loop


def test_scripted_repair_loop_inspects_edits_and_verifies(tmp_path) -> None:
    target = tmp_path / "target.py"
    target.write_text("def value():\n    return 1\n", encoding="utf-8")
    engine, definition = build_scripted_repair_loop(
        workspace_root=tmp_path,
        patches=[
            {
                "path": "target.py",
                "old_text": "return 1",
                "new_text": "return 2",
            }
        ],
        verification_command=[
            sys.executable,
            "-c",
            "from target import value; raise SystemExit(0 if value() == 2 else 1)",
        ],
    )

    state = engine.run(definition)

    assert state.status == LoopStatus.COMPLETED
    assert state.iteration == 1
    assert [result.action.tool for result in state.action_results] == [
        "list_files",
        "read_text",
        "search_text",
        "apply_patch",
        "run_verification",
    ]
    assert "return 2" in target.read_text(encoding="utf-8")
    assert state.latest_verification.evidence["exit_code"] == 0


def test_scripted_repair_loop_can_replan_to_second_patch(tmp_path) -> None:
    target = tmp_path / "target.py"
    target.write_text("value = 0\n", encoding="utf-8")
    engine, definition = build_scripted_repair_loop(
        workspace_root=tmp_path,
        patches=[
            {"path": "target.py", "old_text": "value = 0", "new_text": "value = 1"},
            {"path": "target.py", "old_text": "value = 1", "new_text": "value = 2"},
        ],
        verification_command=[
            sys.executable,
            "-c",
            (
                "from pathlib import Path; ns = {}; "
                "exec(Path('target.py').read_text(), ns); "
                "raise SystemExit(0 if ns['value'] == 2 else 1)"
            ),
        ],
    )

    state = engine.run(definition)

    assert state.status == LoopStatus.COMPLETED
    assert state.iteration == 2
    assert state.action_count == 7
    assert target.read_text(encoding="utf-8") == "value = 2\n"


def test_repair_cli_accepts_json_patch_file(tmp_path, capsys) -> None:
    target = tmp_path / "target.py"
    target.write_text("value = 1\n", encoding="utf-8")
    patch_file = tmp_path / "patch.json"
    patch_file.write_text(
        json.dumps(
            {
                "path": "target.py",
                "old_text": "value = 1",
                "new_text": "value = 2",
            }
        ),
        encoding="utf-8",
    )

    exit_code = main(
        [
            "repair",
            "--workspace",
            str(tmp_path),
            "--patch-file",
            str(patch_file),
            "--",
            sys.executable,
            "-c",
            "from target import value; raise SystemExit(0 if value == 2 else 1)",
        ]
    )

    output = json.loads(capsys.readouterr().out)
    assert exit_code == 0
    assert output["status"] == "completed"
    assert output["phase"] == "terminal"


def test_recovery_after_uncheckpointed_patch_side_effect_is_idempotent(tmp_path) -> None:
    target = tmp_path / "target.py"
    target.write_text("value = 2\n", encoding="utf-8")
    patch = {
        "path": "target.py",
        "old_text": "value = 1",
        "new_text": "value = 2",
    }
    engine, definition = build_scripted_repair_loop(
        workspace_root=tmp_path,
        patches=[patch],
        verification_command=[
            sys.executable,
            "-c",
            "from target import value; raise SystemExit(0 if value == 2 else 1)",
        ],
    )
    state = LoopState(
        run_id="in-flight-repair",
        definition=definition,
        status=LoopStatus.RUNNING,
        phase=LoopPhase.EXECUTING,
        iteration=1,
        started_at=utc_now(),
        current_focus=definition.goal,
        latest_plan=Plan(
            actions=[
                Action(tool="apply_patch", arguments=patch),
                Action(tool="run_verification"),
            ]
        ),
    )

    recovered = engine.resume(state)

    assert recovered.status == LoopStatus.COMPLETED
    assert recovered.action_results[0].output["status"] == "already_applied"
    assert target.read_text(encoding="utf-8") == "value = 2\n"
