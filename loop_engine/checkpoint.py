"""JSON checkpoint persistence."""

from __future__ import annotations

import json
from pathlib import Path

from .models import LoopState


class JsonCheckpointStore:
    def __init__(self, root: str | Path) -> None:
        self.root = Path(root)

    def save(self, state: LoopState) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        target = self.root / f"{state.run_id}.json"
        temporary = target.with_suffix(".json.tmp")
        temporary.write_text(
            json.dumps(state.to_dict(), ensure_ascii=False, indent=2, default=str),
            encoding="utf-8",
        )
        temporary.replace(target)
