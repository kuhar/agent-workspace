from __future__ import annotations

import os
from pathlib import Path

import pytest

from review_queue.launcher import LauncherClient


async def test_log_failure_terminates_and_forgets_supervisor(project, tmp_path: Path) -> None:
    project.launcher.write_text("#!/usr/bin/env bash\necho started\nexec sleep 60\n")
    project.launcher.chmod(0o755)
    client = LauncherClient(grace_seconds=0.1)
    started: list[int] = []

    with pytest.raises(OSError):
        await client.run_logged(
            job_id=7,
            project=project,
            operation="prepare",
            pr_url=f"https://github.com/{project.repo}/pull/7",
            cwd=tmp_path,
            env=os.environ.copy(),
            log_path=Path("/dev/full"),
            on_started=lambda pid, _ticks: started.append(pid),
        )

    assert started
    assert client.processes == {}
    with pytest.raises(ProcessLookupError):
        os.kill(started[0], 0)


async def test_started_callback_failure_terminates_and_forgets_supervisor(
    project, tmp_path: Path
) -> None:
    project.launcher.write_text("#!/usr/bin/env bash\nexec sleep 60\n")
    project.launcher.chmod(0o755)
    client = LauncherClient(grace_seconds=0.1)
    started: list[int] = []

    def fail_after_spawn(pid: int, _ticks: int | None) -> None:
        started.append(pid)
        raise RuntimeError("database unavailable")

    with pytest.raises(RuntimeError, match="database unavailable"):
        await client.run_logged(
            job_id=8,
            project=project,
            operation="prepare",
            pr_url=f"https://github.com/{project.repo}/pull/8",
            cwd=tmp_path,
            env=os.environ.copy(),
            log_path=tmp_path / "job-8.log",
            on_started=fail_after_spawn,
        )

    assert started
    assert client.processes == {}
    with pytest.raises(ProcessLookupError):
        os.kill(started[0], 0)
