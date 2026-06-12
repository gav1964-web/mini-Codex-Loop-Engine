"""Trusted child-process entrypoint for one admitted generated plugin."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
from pathlib import Path
from typing import Any


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser()
    parser.add_argument("--plugin", type=Path, required=True)
    parser.add_argument("--expected-sha256", required=True)
    parser.add_argument("--payload-json", required=True)
    return parser


def _load_payload(raw: str) -> dict[str, Any]:
    payload = json.loads(raw)
    if not isinstance(payload, dict):
        raise ValueError("plugin payload must be a JSON object")
    return payload


def _execute(
    plugin_path: Path,
    expected_sha256: str,
    payload: dict[str, Any],
) -> dict[str, Any]:
    source = plugin_path.read_bytes()
    actual_sha256 = hashlib.sha256(source).hexdigest()
    if actual_sha256 != expected_sha256:
        raise ValueError("plugin source hash mismatch")

    namespace: dict[str, Any] = {
        "__file__": str(plugin_path),
        "__name__": "_generated_plugin",
        "__package__": None,
    }
    with open(os.devnull, "w", encoding="utf-8") as sink:
        old_stdout = os.dup(1)
        old_stderr = os.dup(2)
        try:
            os.dup2(sink.fileno(), 1)
            os.dup2(sink.fileno(), 2)
            exec(compile(source, str(plugin_path), "exec"), namespace)
            entrypoint = namespace.get("run")
            if not callable(entrypoint):
                raise ValueError("plugin entrypoint run is missing")
            output = entrypoint(payload)
        finally:
            try:
                sys.stdout.flush()
                sys.stderr.flush()
            except BaseException:
                pass
            os.dup2(old_stdout, 1)
            os.dup2(old_stderr, 2)
            os.close(old_stdout)
            os.close(old_stderr)

    if not isinstance(output, dict):
        raise ValueError("plugin output must be a JSON object")
    json.dumps(output, ensure_ascii=False, allow_nan=False)
    return output


def main() -> int:
    args = _parser().parse_args()
    try:
        output = _execute(
            args.plugin.resolve(),
            args.expected_sha256,
            _load_payload(args.payload_json),
        )
    except BaseException as exc:
        message = str(exc).replace("\r", " ").replace("\n", " ")[:1000]
        print(
            json.dumps(
                {
                    "status": "error",
                    "error_type": type(exc).__name__,
                    "error": message,
                },
                ensure_ascii=False,
            )
        )
        return 1

    print(
        json.dumps(
            {"status": "ok", "output": output},
            ensure_ascii=False,
            allow_nan=False,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
