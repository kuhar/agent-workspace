"""Per-agent process supervisor used by `peanut-review launch`."""
from __future__ import annotations

import argparse
import json
import os
import signal
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

from . import runtime
from .models import AgentStatus
from .session import update_agent_status

HEARTBEAT_INTERVAL_SECONDS = 30.0
ROUND_DONE_POLL_INTERVAL_SECONDS = 1.0
ROUND_DONE_GRACE_SECONDS = 5.0


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


def _signal_name(signum: int | None) -> str | None:
    if signum is None:
        return None
    try:
        return signal.Signals(signum).name
    except ValueError:
        return f"SIG{signum}"


def _termination_signal_from_return_code(return_code: int) -> str | None:
    if return_code < 0:
        return _signal_name(-return_code)
    # Many wrappers/runtimes report signal death using the conventional
    # shell code 128+signal instead of a negative subprocess return code.
    if return_code > 128:
        return _signal_name(return_code - 128)
    return None


def _runner_from_command(command: list[str]) -> str | None:
    if not command:
        return None
    name = Path(command[0]).name
    if name == "cursor-agent-task.sh":
        return "cursor"
    if name == "opencode-agent-task.sh":
        return "opencode"
    if name == "codex-agent-task.sh":
        return "codex"
    return None


def _get_pgid(pid: int) -> int | None:
    try:
        return os.getpgid(pid)
    except ProcessLookupError:
        return pid
    except OSError:
        return None


def _terminate_group(pgid: int | None, sig: signal.Signals) -> bool:
    if pgid is None:
        return False
    try:
        os.killpg(pgid, sig)
    except ProcessLookupError:
        return False
    return True


def _round_done_signal_mtime_ns(session_dir: Path, agent_name: str) -> int | None:
    path = session_dir / "signals" / f"{agent_name}.{runtime.ROUND_DONE_EVENT}"
    try:
        return path.stat().st_mtime_ns
    except FileNotFoundError:
        return None


def _has_new_round_done_signal(
    session_dir: Path,
    agent_name: str,
    initial_mtime_ns: int | None,
) -> bool:
    current_mtime_ns = _round_done_signal_mtime_ns(session_dir, agent_name)
    return current_mtime_ns is not None and current_mtime_ns != initial_mtime_ns


def _record_runtime_heartbeat(
    *,
    session_dir: Path,
    agent_name: str,
    runner: str | None,
    pid: int,
    pgid: int | None,
    runner_meta: dict[str, str],
    extra: dict[str, object] | None = None,
) -> None:
    now = _now_iso()
    updates: dict[str, object] = {
        "runner": runner,
        "pid": pid,
        "pgid": pgid,
        "supervisor_pid": os.getpid(),
        "process_state": runtime.PROCESS_RUNNING,
        "heartbeat_at": now,
        **runner_meta,
    }
    if extra:
        updates.update(extra)
    runtime.update_agent_meta(session_dir, agent_name, updates)


def _runner_env_meta(env: dict[str, str]) -> dict[str, str]:
    meta = {}
    cursor_home = env.get("PEANUT_CURSOR_HOME")
    if cursor_home:
        meta["cursor_home"] = cursor_home
    return meta


def _final_status(session_dir: str | Path, agent_name: str) -> str:
    from .session import load_session

    session = load_session(session_dir)
    for agent in session.agents:
        if agent.name == agent_name:
            return runtime.derive_agent_status(session_dir, agent)
    return AgentStatus.FAILED.value


def _postprocess_codex_output(session_dir: str | Path, agent_name: str) -> None:
    """Recover output.md from stream.jsonl if codex exited before writing it."""
    meta = runtime.read_agent_meta(session_dir, agent_name)
    if meta.get("runner") != "codex":
        return
    log_dir = runtime.agent_log_dir(session_dir, agent_name)
    output_file = log_dir / "output.md"
    stream_file = log_dir / "stream.jsonl"
    if output_file.exists() and output_file.stat().st_size > 0:
        return
    if not stream_file.exists() or stream_file.stat().st_size == 0:
        return

    messages: list[str] = []
    for line in stream_file.read_text(errors="replace").splitlines():
        try:
            event = json.loads(line)
        except ValueError:
            continue
        if (
            event.get("type") == "item.completed"
            and event.get("item", {}).get("type") == "agent_message"
        ):
            text = event.get("item", {}).get("text")
            if isinstance(text, str):
                messages.append(text)
    if messages:
        output_file.write_text("\n".join(messages) + "\n")


def supervise_agent(
    *,
    session_dir: str | Path,
    agent_name: str,
    command: list[str],
    timeout: float,
    cwd: str | Path | None = None,
    env: dict[str, str] | None = None,
    kill_grace: float = 10.0,
    round_done_grace: float = ROUND_DONE_GRACE_SECONDS,
    round_done_poll_interval: float = ROUND_DONE_POLL_INTERVAL_SECONDS,
) -> int:
    """Run one reviewer wrapper, enforce timeout, and persist runtime metadata."""
    sdir = Path(session_dir)
    child_env = dict(os.environ if env is None else env)
    child_env["PEANUT_SUPERVISOR_PID"] = str(os.getpid())
    runner = _runner_from_command(command)
    runner_meta = _runner_env_meta(child_env)
    now = _now_iso()
    initial_round_done_mtime_ns = _round_done_signal_mtime_ns(sdir, agent_name)

    runtime.update_agent_meta(
        sdir,
        agent_name,
        {
            "runner": runner,
            "supervisor_pid": os.getpid(),
            "supervisor_start": now,
            "process_state": runtime.PROCESS_LAUNCHING,
            "heartbeat_at": now,
            "command": command,
            **runner_meta,
        },
    )
    update_agent_status(
        sdir,
        agent_name,
        AgentStatus.RUNNING.value,
        supervisor_pid=os.getpid(),
    )

    try:
        proc = subprocess.Popen(
            command,
            cwd=str(cwd) if cwd is not None else None,
            env=child_env,
            start_new_session=True,
        )
    except OSError as e:
        now = _now_iso()
        runtime.update_agent_meta(
            sdir,
            agent_name,
            {
                "runner": runner,
                "end": now,
                "exit_code": 127,
                "timed_out": False,
                "process_state": runtime.PROCESS_FAILED,
                "heartbeat_at": now,
                "error": str(e),
                **runner_meta,
            },
        )
        update_agent_status(sdir, agent_name, AgentStatus.FAILED.value)
        return 127

    pgid = _get_pgid(proc.pid)
    reviewer_start = _now_iso()
    runtime.update_agent_meta(
        sdir,
        agent_name,
        {
            "runner": runner,
            "pid": proc.pid,
            "pgid": pgid,
            "supervisor_pid": os.getpid(),
            "command": command,
            "start": reviewer_start,
            "process_state": runtime.PROCESS_RUNNING,
            "heartbeat_at": reviewer_start,
            **runner_meta,
        },
    )
    update_agent_status(
        sdir,
        agent_name,
        AgentStatus.RUNNING.value,
        pid=proc.pid,
        pgid=pgid,
        supervisor_pid=os.getpid(),
    )

    timed_out = False
    round_done_observed = False
    stopped_after_round_done = False
    termination_signal: str | None = None
    try:
        deadline = time.monotonic() + timeout
        next_heartbeat = time.monotonic() + HEARTBEAT_INTERVAL_SECONDS
        poll_interval = max(round_done_poll_interval, 0.01)
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise subprocess.TimeoutExpired(command, timeout)
            try:
                return_code = proc.wait(
                    timeout=min(poll_interval, remaining)
                )
                break
            except subprocess.TimeoutExpired:
                if time.monotonic() >= deadline:
                    raise
                if _has_new_round_done_signal(
                    sdir,
                    agent_name,
                    initial_round_done_mtime_ns,
                ):
                    round_done_observed = True
                    _record_runtime_heartbeat(
                        session_dir=sdir,
                        agent_name=agent_name,
                        runner=runner,
                        pid=proc.pid,
                        pgid=pgid,
                        runner_meta=runner_meta,
                        extra={
                            "round_done_observed_at": _now_iso(),
                            "completion_signal": runtime.ROUND_DONE_EVENT,
                        },
                    )
                    try:
                        return_code = proc.wait(timeout=max(round_done_grace, 0.0))
                    except subprocess.TimeoutExpired:
                        if _terminate_group(pgid, signal.SIGTERM):
                            termination_signal = signal.SIGTERM.name
                            stopped_after_round_done = True
                        try:
                            return_code = proc.wait(timeout=kill_grace)
                        except subprocess.TimeoutExpired:
                            if _terminate_group(pgid, signal.SIGKILL):
                                termination_signal = signal.SIGKILL.name
                                stopped_after_round_done = True
                            return_code = proc.wait()
                    break
                now_mono = time.monotonic()
                if now_mono >= next_heartbeat:
                    _record_runtime_heartbeat(
                        session_dir=sdir,
                        agent_name=agent_name,
                        runner=runner,
                        pid=proc.pid,
                        pgid=pgid,
                        runner_meta=runner_meta,
                    )
                    next_heartbeat = now_mono + HEARTBEAT_INTERVAL_SECONDS
    except subprocess.TimeoutExpired:
        timed_out = True
        if _terminate_group(pgid, signal.SIGTERM):
            termination_signal = signal.SIGTERM.name
        try:
            return_code = proc.wait(timeout=kill_grace)
        except subprocess.TimeoutExpired:
            if _terminate_group(pgid, signal.SIGKILL):
                termination_signal = signal.SIGKILL.name
            return_code = proc.wait()

    if termination_signal is None:
        termination_signal = _termination_signal_from_return_code(return_code)

    _postprocess_codex_output(sdir, agent_name)
    process_state = runtime.process_state_from_exit(
        return_code,
        timed_out=timed_out,
        termination_signal=termination_signal,
    )
    if stopped_after_round_done:
        process_state = runtime.PROCESS_STOPPED
    now = _now_iso()
    runtime.update_agent_meta(
        sdir,
        agent_name,
        {
            "runner": runner,
            "pid": proc.pid,
            "pgid": pgid,
            "supervisor_pid": os.getpid(),
            "command": command,
            "start": reviewer_start,
            "end": now,
            "exit_code": return_code,
            "timed_out": timed_out,
            "round_done_observed": round_done_observed,
            "stopped_after_round_done": stopped_after_round_done,
            "termination_signal": termination_signal,
            "process_state": process_state,
            "heartbeat_at": now,
            **runner_meta,
        },
    )
    update_agent_status(
        sdir,
        agent_name,
        _final_status(sdir, agent_name),
        pid=proc.pid,
        pgid=pgid,
        supervisor_pid=os.getpid(),
    )
    return return_code


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Supervise one peanut-review agent")
    parser.add_argument("--session", required=True, help="Session directory")
    parser.add_argument("--agent", required=True, help="Agent name")
    parser.add_argument("--timeout", type=float, required=True, help="Timeout seconds")
    parser.add_argument("--cwd", help="Working directory for the wrapper")
    parser.add_argument("--kill-grace", type=float, default=10.0,
                        help="Seconds between SIGTERM and SIGKILL")
    parser.add_argument("command", nargs=argparse.REMAINDER,
                        help="Wrapper command after --")
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    command = args.command
    if command and command[0] == "--":
        command = command[1:]
    if not command:
        parser.error("wrapper command required after --")
    return supervise_agent(
        session_dir=args.session,
        agent_name=args.agent,
        command=command,
        timeout=args.timeout,
        cwd=args.cwd,
        kill_grace=args.kill_grace,
    )


if __name__ == "__main__":
    sys.exit(main())
