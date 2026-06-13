"""Run a deterministic bounded process-reaper service demonstration."""

from __future__ import annotations

import json
from dataclasses import asdict

from loop_engine.adapters import (
    ProcessReaperPolicy,
    ProcessReaperService,
    ProcessRegistry,
    ProcessRetentionPolicy,
)


def main() -> None:
    now = [100.0]
    registry = ProcessRegistry(clock=lambda: now[0])
    old = registry.register(
        owner_run_id="old-demo-run",
        pid=99,
        process_identity="old-demo-identity",
        argv=("old-demo-command",),
        cwd=".",
        timeout_seconds=60,
    )
    registry.finish(old.record_id, status="completed", exit_code=0)
    stale = registry.register(
        owner_run_id="demo-run",
        pid=101,
        process_identity="demo-identity",
        argv=("demo-command", "--secret-is-not-persisted"),
        cwd=".",
        timeout_seconds=60,
    )
    now[0] = 200.0

    def reap(current, stale_after):
        return current.reap_stale(
            stale_after_seconds=stale_after,
            identity_lookup=lambda pid: "demo-identity",
            terminate=lambda pid: None,
        )

    report = ProcessReaperService(
        registry,
        ProcessReaperPolicy(
            stale_after_seconds=30,
            interval_seconds=0.001,
            max_cycles=2,
            retention=ProcessRetentionPolicy(
                retain_seconds=50,
                prune_every_cycles=1,
                max_pruned_per_cycle=1,
            ),
        ),
        reaper=reap,
        clock=lambda: now[0],
    ).run()
    payload = {
        **asdict(report),
        "reaped_count": report.reaped_count,
        "final_record_status": registry.get(stale.record_id).status,
        "old_record_pruned": registry.get(old.record_id) is None,
    }
    print(json.dumps(payload, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
