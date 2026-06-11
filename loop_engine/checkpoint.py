"""JSON checkpoint persistence."""

from __future__ import annotations

import json
from pathlib import Path
import re

from .models import LoopState

CHECKPOINT_SCHEMA_VERSION = 1
_RUN_ID_PATTERN = re.compile(r"^[A-Za-z0-9_-]+$")


class JsonCheckpointStore:
    def __init__(self, root: str | Path) -> None:
        self.root = Path(root)

    def save(self, state: LoopState) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        target = self._target(state.run_id)
        temporary = target.with_suffix(".json.tmp")
        temporary.write_text(
            json.dumps(
                {
                    "schema_version": CHECKPOINT_SCHEMA_VERSION,
                    "state": state.to_dict(),
                },
                ensure_ascii=False,
                indent=2,
                default=str,
            ),
            encoding="utf-8",
        )
        temporary.replace(target)

    def load(self, run_id: str) -> LoopState:
        payload = json.loads(self._target(run_id).read_text(encoding="utf-8"))
        if "schema_version" not in payload:
            return LoopState.from_dict(payload)
        version = payload["schema_version"]
        if version != CHECKPOINT_SCHEMA_VERSION:
            raise ValueError(f"unsupported checkpoint schema version: {version}")
        return LoopState.from_dict(dict(payload["state"]))

    def _target(self, run_id: str) -> Path:
        if not _RUN_ID_PATTERN.fullmatch(run_id):
            raise ValueError("run_id may contain only letters, digits, underscore, and hyphen")
        return self.root / f"{run_id}.json"
