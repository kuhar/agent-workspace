from __future__ import annotations

import asyncio
import json
import os
import signal
import sys
from collections.abc import Callable
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path

from .models import ProjectConfig
from .util import process_start_ticks


class LauncherError(RuntimeError):
    pass


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
    ) -> int:
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
