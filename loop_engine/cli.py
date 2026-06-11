"""CLI for deterministic Loop Engine experiments."""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

from .adapters import OpenAICompatibleJSONClient
from .checkpoint import JsonCheckpointStore
from .demo import build_counter_demo
from .profiles import (
    build_coding_check_loop,
    build_llm_repair_loop,
    build_scripted_repair_loop,
)


def main(argv: list[str] | None = None) -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(prog="mini-codex-loop")
    subparsers = parser.add_subparsers(dest="command", required=True)
    demo = subparsers.add_parser("demo", help="Run the deterministic counter loop.")
    demo.add_argument("--target", type=int, default=3)
    demo.add_argument("--checkpoints", type=Path)
    demo.add_argument("--resume", metavar="RUN_ID")
    check = subparsers.add_parser("check", help="Run one bounded coding verification command.")
    check.add_argument("--workspace", type=Path, default=Path.cwd())
    check.add_argument("--timeout", type=float, default=60.0)
    check.add_argument("--max-output-bytes", type=int, default=64 * 1024)
    check.add_argument("--checkpoints", type=Path)
    check.add_argument("--resume", metavar="RUN_ID")
    check.add_argument("process_command", nargs=argparse.REMAINDER)
    repair = subparsers.add_parser("repair", help="Run a bounded scripted repair loop.")
    repair.add_argument("--workspace", type=Path, default=Path.cwd())
    repair.add_argument("--patch-file", type=Path)
    repair.add_argument("--timeout", type=float, default=60.0)
    repair.add_argument("--max-output-bytes", type=int, default=64 * 1024)
    repair.add_argument("--checkpoints", type=Path)
    repair.add_argument("--resume", metavar="RUN_ID")
    repair.add_argument("process_command", nargs=argparse.REMAINDER)
    llm_repair = subparsers.add_parser(
        "llm-repair",
        help="Run an LLM-planned repair through bounded capabilities.",
    )
    llm_repair.add_argument("--workspace", type=Path, default=Path.cwd())
    llm_repair.add_argument("--goal")
    llm_repair.add_argument("--gateway-url", default="http://127.0.0.1:8000")
    llm_repair.add_argument("--model", default="auto")
    llm_repair.add_argument("--llm-timeout", type=float, default=120.0)
    llm_repair.add_argument("--llm-max-tokens", type=int, default=2048)
    llm_repair.add_argument("--api-key-env", default="LLM_GATEWAY_API_KEY")
    llm_repair.add_argument("--max-iterations", type=int, default=4)
    llm_repair.add_argument("--max-actions", type=int, default=16)
    llm_repair.add_argument("--max-actions-per-plan", type=int, default=5)
    llm_repair.add_argument(
        "--contract-repair-attempts",
        type=int,
        choices=(0, 1),
        default=1,
    )
    llm_repair.add_argument("--timeout", type=float, default=60.0)
    llm_repair.add_argument("--max-output-bytes", type=int, default=64 * 1024)
    llm_repair.add_argument("--checkpoints", type=Path)
    llm_repair.add_argument("--resume", metavar="RUN_ID")
    llm_repair.add_argument("process_command", nargs=argparse.REMAINDER)
    args = parser.parse_args(argv)

    if args.command == "demo":
        engine, definition = build_counter_demo(str(args.checkpoints) if args.checkpoints else None)
        if args.resume:
            if not args.checkpoints:
                parser.error("--resume requires --checkpoints")
            state = engine.resume(JsonCheckpointStore(args.checkpoints).load(args.resume))
        else:
            definition.metadata["target"] = max(1, args.target)
            state = engine.run(definition)
    elif args.command == "check":
        process_command = list(args.process_command)
        if process_command and process_command[0] == "--":
            process_command.pop(0)
        if args.resume:
            if not args.checkpoints:
                parser.error("--resume requires --checkpoints")
            loaded = JsonCheckpointStore(args.checkpoints).load(args.resume)
            metadata = loaded.definition.metadata
            engine, _ = build_coding_check_loop(
                workspace_root=metadata["workspace_root"],
                command=list(metadata["command"]),
                timeout_seconds=float(metadata.get("subprocess_timeout_seconds", args.timeout)),
                max_output_bytes=int(metadata.get("max_output_bytes", args.max_output_bytes)),
                checkpoint_root=args.checkpoints,
            )
            state = engine.resume(loaded)
        elif not process_command:
            parser.error("check requires a process command after --")
        else:
            engine, definition = build_coding_check_loop(
                workspace_root=args.workspace,
                command=process_command,
                timeout_seconds=args.timeout,
                max_output_bytes=args.max_output_bytes,
                checkpoint_root=args.checkpoints,
            )
            state = engine.run(definition)
    elif args.command == "repair":
        process_command = list(args.process_command)
        if process_command and process_command[0] == "--":
            process_command.pop(0)
        if args.resume:
            if not args.checkpoints:
                parser.error("--resume requires --checkpoints")
            loaded = JsonCheckpointStore(args.checkpoints).load(args.resume)
            metadata = loaded.definition.metadata
            if metadata.get("profile") != "scripted_repair":
                parser.error("checkpoint does not contain a scripted repair profile")
            engine, _ = build_scripted_repair_loop(
                workspace_root=metadata["workspace_root"],
                patches=list(metadata["patches"]),
                verification_command=list(metadata["command"]),
                timeout_seconds=float(metadata.get("subprocess_timeout_seconds", args.timeout)),
                max_output_bytes=int(metadata.get("max_output_bytes", args.max_output_bytes)),
                checkpoint_root=args.checkpoints,
            )
            state = engine.resume(loaded)
        else:
            if not args.patch_file:
                parser.error("repair requires --patch-file")
            if not process_command:
                parser.error("repair requires a verification command after --")
            patch_payload = json.loads(args.patch_file.read_text(encoding="utf-8"))
            patches = patch_payload if isinstance(patch_payload, list) else [patch_payload]
            if not all(isinstance(item, dict) for item in patches):
                parser.error("patch file must contain a JSON object or array of objects")
            engine, definition = build_scripted_repair_loop(
                workspace_root=args.workspace,
                patches=patches,
                verification_command=process_command,
                timeout_seconds=args.timeout,
                max_output_bytes=args.max_output_bytes,
                checkpoint_root=args.checkpoints,
            )
            state = engine.run(definition)
    else:
        process_command = list(args.process_command)
        if process_command and process_command[0] == "--":
            process_command.pop(0)
        if args.resume:
            if not args.checkpoints:
                parser.error("--resume requires --checkpoints")
            loaded = JsonCheckpointStore(args.checkpoints).load(args.resume)
            metadata = loaded.definition.metadata
            if metadata.get("profile") != "llm_repair":
                parser.error("checkpoint does not contain an LLM repair profile")
            llm_metadata = dict(metadata.get("llm", {}))
            api_key_env = str(llm_metadata.get("api_key_env", args.api_key_env))
            client = OpenAICompatibleJSONClient(
                base_url=str(llm_metadata.get("gateway_url", args.gateway_url)),
                model=str(llm_metadata.get("model", args.model)),
                timeout_seconds=float(llm_metadata.get("timeout_seconds", args.llm_timeout)),
                max_tokens=int(llm_metadata.get("max_tokens", args.llm_max_tokens)),
                api_key=os.getenv(api_key_env),
            )
            engine, _ = build_llm_repair_loop(
                workspace_root=metadata["workspace_root"],
                goal=str(metadata["goal"]),
                llm_client=client,
                verification_command=list(metadata["command"]),
                max_iterations=int(metadata.get("max_iterations", args.max_iterations)),
                max_actions=int(metadata.get("max_actions", args.max_actions)),
                max_actions_per_plan=int(
                    metadata.get("max_actions_per_plan", args.max_actions_per_plan)
                ),
                contract_repair_attempts=int(
                    metadata.get(
                        "contract_repair_attempts",
                        args.contract_repair_attempts,
                    )
                ),
                timeout_seconds=float(metadata.get("subprocess_timeout_seconds", args.timeout)),
                max_output_bytes=int(metadata.get("max_output_bytes", args.max_output_bytes)),
                checkpoint_root=args.checkpoints,
                llm_metadata=llm_metadata,
            )
            state = engine.resume(loaded)
        else:
            if not args.goal:
                parser.error("llm-repair requires --goal")
            if not process_command:
                parser.error("llm-repair requires a verification command after --")
            llm_metadata = {
                "gateway_url": args.gateway_url,
                "model": args.model,
                "timeout_seconds": args.llm_timeout,
                "max_tokens": args.llm_max_tokens,
                "api_key_env": args.api_key_env,
            }
            client = OpenAICompatibleJSONClient(
                base_url=args.gateway_url,
                model=args.model,
                timeout_seconds=args.llm_timeout,
                max_tokens=args.llm_max_tokens,
                api_key=os.getenv(args.api_key_env),
            )
            engine, definition = build_llm_repair_loop(
                workspace_root=args.workspace,
                goal=args.goal,
                llm_client=client,
                verification_command=process_command,
                max_iterations=args.max_iterations,
                max_actions=args.max_actions,
                max_actions_per_plan=args.max_actions_per_plan,
                contract_repair_attempts=args.contract_repair_attempts,
                timeout_seconds=args.timeout,
                max_output_bytes=args.max_output_bytes,
                checkpoint_root=args.checkpoints,
                llm_metadata=llm_metadata,
            )
            state = engine.run(definition)
    json.dump(state.to_dict(), sys.stdout, ensure_ascii=False, indent=2, default=str)
    sys.stdout.write("\n")
    return 0 if state.status == "completed" else 1
