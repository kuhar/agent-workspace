from __future__ import annotations

import argparse
import asyncio
import json
import shutil
import sys
from dataclasses import asdict
from pathlib import Path

from .config import load_config, write_default_config, xdg_config_path
from .demo import DemoScheduler
from .scheduler import QueueError, Scheduler, SingleInstanceLock
from .tui import ReviewQueueApp
from .util import free_bytes


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="review-queue",
        description="Persistent priority queue for project-owned PR review launchers",
    )
    parser.add_argument("--config", type=Path, default=None)
    parser.add_argument("--state-dir", type=Path, default=None)
    parser.add_argument(
        "--theme",
        default=None,
        help="Textual theme (for example dark+, monokai, or catppuccin-mocha)",
    )
    parser.add_argument(
        "--demo",
        action="store_true",
        help="run an isolated accelerated synthetic PR trace",
    )
    parser.add_argument(
        "--demo-speed",
        type=float,
        default=300.0,
        metavar="MULTIPLIER",
        help="initial synthetic clock multiplier (default: 300)",
    )
    subcommands = parser.add_subparsers(dest="command")
    initialize = subcommands.add_parser("init", help="write the default configuration")
    initialize.add_argument("--force", action="store_true")
    subcommands.add_parser("doctor", help="validate configuration and launcher protocols")
    poll = subcommands.add_parser("poll", help="run one GitHub discovery pass")
    poll.add_argument("--json", action="store_true")
    state = subcommands.add_parser("state", help="print the persistent queue snapshot")
    state.add_argument("--json", action="store_true")
    watch = subcommands.add_parser("watch", help="watch and enqueue one PR")
    watch.add_argument("pr")
    subcommands.add_parser("pause", help="pause automatic dispatch")
    subcommands.add_parser("resume", help="resume automatic dispatch")
    return parser


async def _doctor(scheduler: Scheduler) -> int:
    problems: list[str] = []
    if shutil.which("gh") is None:
        problems.append("gh is not installed")
    for project in scheduler.config.projects:
        if not project.launcher.is_file() or not project.launcher.stat().st_mode & 0o111:
            problems.append(f"launcher is not executable: {project.launcher}")
            continue
        try:
            protocol = await scheduler.launcher.protocol(project)
            print(
                f"ok launcher {project.name}: protocol {protocol.version}, "
                f"repository {protocol.repository}"
            )
        except Exception as error:
            problems.append(f"launcher {project.name}: {error}")
        project.wrapper_root.mkdir(parents=True, exist_ok=True)
        print(
            f"ok storage {project.name}: {project.wrapper_root} "
            f"({free_bytes(project.wrapper_root) / 1024**3:.0f} GiB free)"
        )
    if problems:
        for problem in problems:
            print(f"error: {problem}", file=sys.stderr)
        return 1
    print(f"ok state: {scheduler.config.state_dir}")
    print("doctor passed")
    return 0


async def _run(args: argparse.Namespace) -> int:
    if args.demo:
        if args.command is not None:
            raise ValueError("--demo does not accept a subcommand")
        demo = DemoScheduler(speed=args.demo_speed)
        await demo.start()
        app = ReviewQueueApp(demo, theme_name=args.theme or "catppuccin-mocha")
        try:
            await app.run_async()
        finally:
            await demo.stop()
        return 0

    if args.command == "init":
        destination = args.config or xdg_config_path()
        path = write_default_config(destination, force=args.force)
        print(path)
        return 0

    config = load_config(args.config, state_dir=args.state_dir)
    scheduler = Scheduler(config)
    lock = (
        SingleInstanceLock(config.state_dir / "process.lock")
        if args.command in {None, "poll"}
        else None
    )
    scheduler_started = False
    try:
        if args.command == "doctor":
            return await _doctor(scheduler)
        if args.command == "poll":
            await scheduler.reconcile_orphans()
            results = await scheduler.poll_once()
            if args.json:
                payload = {
                    name: asdict(result)
                    if not isinstance(result, Exception)
                    else {"error": str(result)}
                    for name, result in results.items()
                }
                print(json.dumps(payload, indent=2, sort_keys=True))
            else:
                for name, result in results.items():
                    print(f"{name}: {result}")
            return int(any(isinstance(result, Exception) for result in results.values()))
        if args.command == "state":
            snapshot = scheduler.snapshot()
            if args.json:
                print(json.dumps(asdict(snapshot), indent=2, sort_keys=True))
            else:
                print(
                    f"paused={snapshot.paused} queued={len(snapshot.queue)} "
                    f"running={len(snapshot.running)} wrappers={len(snapshot.wrappers)}"
                )
                for item in snapshot.queue:
                    print(
                        f"{item.score:+5d} {item.status:10s} "
                        f"{item.project}#{item.number} @{item.author} {item.head_sha[:12]}"
                    )
            return 0
        if args.command == "watch":
            await scheduler.add_manual(args.pr)
            print(f"watching {args.pr}")
            return 0
        if args.command in {"pause", "resume"}:
            scheduler.db.set_paused(args.command == "pause")
            print(args.command + "d")
            return 0

        await scheduler.start()
        scheduler_started = True
        app = ReviewQueueApp(scheduler, theme_name=args.theme or config.theme)
        try:
            await app.run_async()
        finally:
            await scheduler.stop()
        return 0
    finally:
        if lock is not None:
            lock.close()
        if not scheduler_started:
            scheduler.db.close()


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        return asyncio.run(_run(args))
    except (FileNotFoundError, FileExistsError, ValueError, QueueError) as error:
        print(f"review-queue: {error}", file=sys.stderr)
        return 2
