from __future__ import annotations

import json
import sys

import pytest

from loop_engine.release_gate import (
    CompositeReleaseGateReport,
    ReleaseStageReport,
)
from loop_engine.release_history import (
    JsonReleaseHistoryStore,
    ReleaseHistoryAnalyzer,
    ReleaseRegressionPolicy,
    write_release_trend,
)
from tools.release_trends import main as release_trends_main


def _report(
    *,
    status: str = "passed",
    durations: tuple[float, float, float] = (10, 5, 2),
    started_at: float = 1,
) -> CompositeReleaseGateReport:
    stages = tuple(
        ReleaseStageReport(
            name=name,
            status=status,
            releasable=status != "failed",
            exit_code=0 if status != "failed" else 1,
            timed_out=False,
            stdout_truncated=False,
            stderr_truncated=False,
            duration_seconds=duration,
        )
        for name, duration in zip(
            ("pytest", "wheel_smoke", "sandbox"),
            durations,
            strict=True,
        )
    )
    return CompositeReleaseGateReport(
        schema_version=1,
        status=status,
        releasable=status != "failed",
        degraded=status == "degraded",
        started_at=started_at,
        finished_at=started_at + sum(durations),
        stages=stages,
        error=None if status != "failed" else "release_stages_failed:pytest",
    )


def test_history_store_round_trip_and_newest_first_limit(tmp_path) -> None:
    store = JsonReleaseHistoryStore(tmp_path / "history")
    store.record(_report(started_at=1), release_id="old")
    store.record(_report(started_at=5), release_id="new")

    loaded = store.load("old")
    entries = store.list(limit=1)

    assert loaded.release_id == "old"
    assert loaded.report.to_dict() == _report(started_at=1).to_dict()
    assert [entry.release_id for entry in entries] == ["new"]


def test_history_snapshot_cannot_be_overwritten(tmp_path) -> None:
    store = JsonReleaseHistoryStore(tmp_path)
    store.record(_report(), release_id="immutable")

    with pytest.raises(FileExistsError, match="already exists"):
        store.record(_report(started_at=10), release_id="immutable")


def test_first_release_has_insufficient_history(tmp_path) -> None:
    store = JsonReleaseHistoryStore(tmp_path)
    current = store.record(_report(), release_id="current")

    trend = ReleaseHistoryAnalyzer().analyze((current,))

    assert trend.status == "insufficient_history"
    assert trend.baseline_release_ids == ()
    assert trend.regressions == ()


def test_status_downgrade_is_a_regression(tmp_path) -> None:
    store = JsonReleaseHistoryStore(tmp_path)
    previous = store.record(
        _report(status="passed", started_at=1),
        release_id="previous",
    )
    current = store.record(
        _report(status="degraded", started_at=10),
        release_id="current",
    )

    trend = ReleaseHistoryAnalyzer().analyze((current, previous))

    assert trend.status == "regressed"
    assert "gate_status:passed->degraded" in trend.regressions
    assert "stage_status:pytest:passed->degraded" in trend.regressions


def test_duration_uses_rolling_median_and_dual_threshold(tmp_path) -> None:
    store = JsonReleaseHistoryStore(tmp_path)
    baseline = tuple(
        store.record(
            _report(
                durations=(duration, 5, 2),
                started_at=float(index),
            ),
            release_id=f"baseline-{index}",
        )
        for index, duration in enumerate((9, 10, 11), start=1)
    )
    current = store.record(
        _report(durations=(14, 5.2, 2), started_at=20),
        release_id="current",
    )
    analyzer = ReleaseHistoryAnalyzer(
        ReleaseRegressionPolicy(
            history_window=3,
            duration_ratio=1.25,
            duration_absolute_seconds=1,
        )
    )

    trend = analyzer.analyze((current, *baseline))
    by_name = {item.name: item for item in trend.stage_trends}

    assert trend.status == "regressed"
    assert by_name["pytest"].baseline_median_seconds == 10
    assert by_name["pytest"].ratio == 1.4
    assert by_name["pytest"].regressed is True
    assert by_name["wheel_smoke"].regressed is False
    assert trend.regressions == ("stage_duration:pytest",)


def test_small_absolute_change_does_not_trigger_ratio_only_regression(
    tmp_path,
) -> None:
    store = JsonReleaseHistoryStore(tmp_path)
    previous = store.record(
        _report(durations=(0.1, 5, 2), started_at=1),
        release_id="previous",
    )
    current = store.record(
        _report(durations=(0.2, 5, 2), started_at=10),
        release_id="current",
    )

    trend = ReleaseHistoryAnalyzer().analyze((current, previous))

    assert trend.status == "stable"
    assert trend.stage_trends[0].ratio == 2
    assert trend.stage_trends[0].regressed is False


def test_improved_gate_status_is_reported_without_hiding_latency(tmp_path) -> None:
    store = JsonReleaseHistoryStore(tmp_path)
    previous = store.record(
        _report(status="degraded", started_at=1),
        release_id="previous",
    )
    current = store.record(
        _report(status="passed", started_at=10),
        release_id="current",
    )

    trend = ReleaseHistoryAnalyzer().analyze((current, previous))

    assert trend.status == "improved"


def test_trend_report_is_written_atomically(tmp_path) -> None:
    store = JsonReleaseHistoryStore(tmp_path / "history")
    entry = store.record(_report(), release_id="only")
    trend = ReleaseHistoryAnalyzer().analyze((entry,))

    path = write_release_trend(tmp_path / "trend.json", trend)

    assert json.loads(path.read_text(encoding="utf-8")) == trend.to_dict()
    assert not path.with_name(f".{path.name}.tmp").exists()


def test_history_contracts_fail_closed(tmp_path) -> None:
    store = JsonReleaseHistoryStore(tmp_path)
    with pytest.raises(ValueError, match="release_id"):
        store.record(_report(), release_id="../escape")
    with pytest.raises(ValueError, match="limit"):
        store.list(limit=101)
    with pytest.raises(ValueError, match="window"):
        ReleaseRegressionPolicy(history_window=0)
    with pytest.raises(ValueError, match="ratio"):
        ReleaseRegressionPolicy(duration_ratio=1)
    with pytest.raises(ValueError, match="absolute"):
        ReleaseRegressionPolicy(duration_absolute_seconds=-1)
    with pytest.raises(ValueError, match="escapes workspace"):
        JsonReleaseHistoryStore(
            tmp_path.parent / "outside",
            workspace_root=tmp_path,
        )


def test_release_trends_cli_archives_and_writes_report(
    tmp_path,
    monkeypatch,
    capsys,
) -> None:
    release_path = tmp_path / "release.json"
    history_root = tmp_path / "history"
    trend_path = tmp_path / "trend.json"
    release_path.write_text(
        json.dumps(_report().to_dict()),
        encoding="utf-8",
    )
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "release_trends",
            "--record-report",
            str(release_path),
            "--history-root",
            str(history_root),
            "--trend-report",
            str(trend_path),
        ],
    )

    exit_code = release_trends_main()
    output = json.loads(capsys.readouterr().out)

    assert exit_code == 0
    assert output["status"] == "insufficient_history"
    assert json.loads(trend_path.read_text(encoding="utf-8")) == output
    assert len(JsonReleaseHistoryStore(history_root).list()) == 1
