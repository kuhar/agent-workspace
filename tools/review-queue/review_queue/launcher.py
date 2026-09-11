from __future__ import annotations

import asyncio
import json
import os
import re
import signal
import sys
from collections.abc import Callable
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path

from .models import ProjectConfig
from .phases import PhaseChange, PhaseStack
from .util import process_start_ticks


class LauncherError(RuntimeError):
    pass


def failure_details(
    *,
    log_path: Path,
    result_path: Path,
    operation: str,
    exit_code: int,
    repo: str,
    number: int,
    head: str,
    log_offset: int = 0,
    phase: str = "",
) -> tuple[str, str]:
    """Read an optional failure result, falling back to bounded launcher output."""
    try:
        payload = json.loads(result_path.read_text())
    except (OSError, ValueError):
        payload = None
    if isinstance(payload, dict) and all(
        payload.get(key) == value
        for key, value in {
            "protocol": 1,
            "repository": repo,
            "pr": number,
            "requested_head": head,
            "exit_code": exit_code,
        }.items()
    ):
        status, reason = payload.get("status"), payload.get("error")
        if (
            isinstance(status, str)
            and status in {"failed", "ineligible"}
            and isinstance(reason, str)
            and reason.strip()
        ):
            context = f"{phase}: " if phase else ""
            return status, context + " ".join(reason.split())[:1200]

    fallback = (
        f"{phase} failed (exit {exit_code})"
        if phase
        else f"launcher {operation} exited {exit_code}"
    )
    try:
        with log_path.open("rb") as stream:
            stream.seek(0, os.SEEK_END)
            stream.seek(max(log_offset, stream.tell() - 131072))
            output = stream.read().decode(errors="replace")
    except OSError:
        return "failed", fallback
    output = re.sub(r"\x1b\[[0-?]*[ -/]*[@-~]", "", output)
    lines = [line.strip() for line in output.splitlines() if line.strip()]
    errors = [line for line in lines if re.search(r"\berror\b|\bfatal\b", line, re.I)]
    diagnostics = errors or [
        line
        for line in lines
        if re.search(r"\bfailed\b|timed? out|timeout|permission denied", line, re.I)
        and not line.startswith(("FAILED:", "ninja: build stopped:"))
    ]
    # Keep the compiler diagnostic, even when an EXIT trap prints a happy checkout footer.
    details = diagnostics[:3] or lines[-3:]
    summary = "; ".join(line[:350] for line in details)
    return "failed", f"{fallback}: {summary}"[:1200] if summary else fallback


@dataclass(frozen=True, slots=True)
class Protocol:
    version: int
    repository: str
    operations: frozenset[str]


class LauncherClient:
    def __init__(self, *, grace_seconds: int = 30):
        self.grace_seconds = grace_seconds
        self.processes: dict[int, asyncio.subprocess.Process] = {}

    async def _capture(
        self,
        *command: str,
        cwd: Path | None = None,
        env: dict[str, str] | None = None,
        timeout: float = 60,
    ) -> tuple[int, str, str]:
        process = await asyncio.create_subprocess_exec(
            *command,
            cwd=cwd,
            env=env,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=timeout)
        except TimeoutError as error:
            process.kill()
            await process.wait()
            raise LauncherError(f"command timed out: {' '.join(command)}") from error
        return (
            int(process.returncode),
            stdout.decode(errors="replace"),
            stderr.decode(errors="replace"),
        )

    async def protocol(self, project: ProjectConfig) -> Protocol:
        code, stdout, stderr = await self._capture(
            str(project.launcher), "queue", "protocol", timeout=15
        )
        if code:
            raise LauncherError(stderr.strip() or f"protocol probe exited {code}")
        try:
            payload = json.loads(stdout)
            protocol = Protocol(
                version=int(payload["version"]),
                repository=str(payload["repository"]),
                operations=frozenset(str(value) for value in payload["operations"]),
            )
        except (json.JSONDecodeError, KeyError, TypeError, ValueError) as error:
            raise LauncherError(f"invalid launcher protocol: {error}") from error
        required = {"prepare", "run", "cleanup-check", "cleanup"}
        if protocol.version != 1 or protocol.repository != project.repo:
            raise LauncherError(
                f"launcher identity mismatch: version={protocol.version}, "
                f"repo={protocol.repository}"
            )
        if not required <= protocol.operations:
            missing = ", ".join(sorted(required - protocol.operations))
            raise LauncherError(f"launcher is missing operations: {missing}")
        return protocol

    async def run_logged(
        self,
        *,
        job_id: int,
        project: ProjectConfig,
        operation: str,
        pr_url: str,
        cwd: Path,
        env: dict[str, str],
        log_path: Path,
        on_started: Callable[[int, int | None], None],
        on_phase: Callable[[PhaseChange], None] | None = None,
    ) -> int:
        phases = PhaseStack()
        if on_phase:
            on_phase(PhaseChange())
        command = [
            sys.executable,
            "-m",
            "review_queue.job_supervisor",
            "--grace-seconds",
            str(self.grace_seconds),
            "--",
            str(project.launcher),
            "queue",
            operation,
            pr_url,
        ]
        process = await asyncio.create_subprocess_exec(
            *command,
            cwd=cwd,
            env=env,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            start_new_session=True,
        )
        self.processes[job_id] = process
        try:
            on_started(process.pid, process_start_ticks(process.pid))
            log_path.parent.mkdir(parents=True, exist_ok=True)
            with log_path.open("a", encoding="utf-8", errors="replace") as log:
                assert process.stdout is not None
                while line := await process.stdout.readline():
                    text = line.decode(errors="replace")
                    log.write(text)
                    log.flush()
                    change = phases.consume(text, log_offset=log.tell())
                    if change is not None and on_phase:
                        on_phase(change)
            return int(await process.wait())
        except BaseException:
            await self._terminate(process)
            raise
        finally:
            self.processes.pop(job_id, None)

    async def _terminate(self, process: asyncio.subprocess.Process) -> None:
        if process.returncode is not None:
            return
        with suppress(ProcessLookupError):
            process.send_signal(signal.SIGTERM)
        try:
            await asyncio.wait_for(process.wait(), timeout=self.grace_seconds + 5)
        except TimeoutError:
            with suppress(ProcessLookupError):
                process.kill()
            await process.wait()

    async def cleanup_check(
        self, project: ProjectConfig, *, cwd: Path, env: dict[str, str]
    ) -> dict[str, object]:
        code, stdout, stderr = await self._capture(
            str(project.launcher),
            "queue",
            "cleanup",
            "--check",
            cwd=cwd,
            env=env,
            timeout=120,
        )
        if code:
            raise LauncherError(stderr.strip() or stdout.strip() or f"cleanup check exited {code}")
        try:
            payload = json.loads(stdout)
        except json.JSONDecodeError as error:
            raise LauncherError(f"invalid cleanup check JSON: {error}") from error
        if not isinstance(payload, dict) or not isinstance(payload.get("safe"), bool):
            raise LauncherError("cleanup check did not return a boolean safe field")
        return payload

    async def cleanup(self, project: ProjectConfig, *, cwd: Path, env: dict[str, str]) -> None:
        code, stdout, stderr = await self._capture(
            str(project.launcher),
            "queue",
            "cleanup",
            cwd=cwd,
            env=env,
            timeout=600,
        )
        if code:
            raise LauncherError(stderr.strip() or stdout.strip() or f"cleanup exited {code}")

    def cancel(self, job_id: int) -> bool:
        process = self.processes.get(job_id)
        if process is None or process.returncode is not None:
            return False
        process.send_signal(signal.SIGTERM)
        return True

    async def shutdown(self) -> None:
        active = list(self.processes.values())
        if active:
            await asyncio.gather(
                *(self._terminate(process) for process in active), return_exceptions=True
            )


def launcher_environment(
    *, project: ProjectConfig, wrapper: Path, target_head: str, result: Path, token: str
) -> dict[str, str]:
    env = os.environ.copy()
    env.update(
        {
            "REVIEW_QUEUE_PROTOCOL": "1",
            "REVIEW_QUEUE_WRAPPER": str(wrapper),
            "REVIEW_QUEUE_ROOT": str(project.wrapper_root),
            "REVIEW_QUEUE_TARGET_HEAD": target_head,
            "REVIEW_QUEUE_RESULT": str(result),
            "REVIEW_QUEUE_OWNER_TOKEN": token,
        }
    )
    return env
