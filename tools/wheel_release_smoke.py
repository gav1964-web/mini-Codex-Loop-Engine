"""Build, install, and import-smoke the current wheel in a clean target."""

from __future__ import annotations

import argparse
import json
import shutil
import sys
import tempfile
from pathlib import Path

from loop_engine.adapters import BoundedSubprocessTool, SubprocessSpec
from loop_engine.models import LoopDefinition, LoopState


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser()
    parser.add_argument("--wheel-dir", type=Path, required=True)
    parser.add_argument("--timeout", type=float, default=120.0)
    return parser


def main() -> int:
    args = _parser().parse_args()
    wheel_dir = args.wheel_dir.resolve()
    if wheel_dir.exists():
        shutil.rmtree(wheel_dir)
    wheel_dir.mkdir(parents=True)
    build = _run(
        (
            sys.executable,
            "-m",
            "pip",
            "wheel",
            ".",
            "--no-deps",
            "--wheel-dir",
            str(wheel_dir),
        ),
        timeout=args.timeout,
    )
    wheels = sorted(wheel_dir.glob("mini_codex_loop_engine-*.whl"))
    if build["exit_code"] != 0 or len(wheels) != 1:
        return _emit(
            "failed",
            "wheel_build_failed",
            build=build,
            wheel=None,
        )

    with tempfile.TemporaryDirectory() as temporary:
        target = Path(temporary)
        install = _run(
            (
                sys.executable,
                "-m",
                "pip",
                "install",
                str(wheels[0]),
                "--target",
                str(target),
                "--no-deps",
                "--quiet",
            ),
            timeout=args.timeout,
        )
        if install["exit_code"] != 0:
            return _emit(
                "failed",
                "wheel_install_failed",
                build=build,
                install=install,
                wheel=wheels[0],
            )
        smoke = _run(
            (
                sys.executable,
                "-I",
                "-c",
                (
                    "import sys;"
                    f"sys.path.insert(0, {str(target)!r});"
                    "from loop_engine.adapters import FileResourceLeaseManager;"
                    "from loop_engine.release_gate import CompositeReleaseGate;"
                    "from loop_engine.tasks import ResourceLease;"
                    "print('wheel-smoke-ok')"
                ),
            ),
            timeout=args.timeout,
        )
        status = "passed" if smoke["exit_code"] == 0 else "failed"
        error = None if smoke["exit_code"] == 0 else "wheel_import_smoke_failed"
        return _emit(
            status,
            error,
            build=build,
            install=install,
            smoke=smoke,
            wheel=wheels[0],
        )


def _run(argv: tuple[str, ...], *, timeout: float) -> dict:
    return BoundedSubprocessTool(
        Path.cwd(),
        SubprocessSpec(
            argv=argv,
            cwd=".",
            timeout_seconds=timeout,
            max_output_bytes=256 * 1024,
        ),
    )(
        {},
        LoopState(
            run_id="wheel-release-smoke",
            definition=LoopDefinition(goal="Build and verify release wheel"),
        ),
    )


def _emit(
    status: str,
    error: str | None,
    *,
    wheel: Path | None,
    **processes: dict,
) -> int:
    print(
        json.dumps(
            {
                "status": status,
                "error": error,
                "wheel": str(wheel) if wheel is not None else None,
                "steps": {
                    name: {
                        "exit_code": process["exit_code"],
                        "timed_out": process["timed_out"],
                        "stdout_truncated": process["stdout_truncated"],
                        "stderr_truncated": process["stderr_truncated"],
                        "stdout_tail": process["stdout"][-2000:],
                        "stderr_tail": process["stderr"][-2000:],
                    }
                    for name, process in processes.items()
                },
            },
            ensure_ascii=True,
            indent=2,
        )
    )
    return 0 if status == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
