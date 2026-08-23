from __future__ import annotations

import asyncio
import json
import os
import shutil
import signal
import sys
from dataclasses import replace
from datetime import timedelta
from pathlib import Path

import pytest

from review_queue.github import GitHubPoll
from review_queue.launcher import LauncherClient, Protocol
from review_queue.models import ProjectConfig, PullRequest, QueueConfig
from review_queue.scheduler import QueueError, Scheduler
from review_queue.util import isoformat, process_start_ticks, utc_now


class FakeLauncher:
    grace_seconds = 1

    def __init__(self):
        self.processes: dict[int, object] = {}

    async def protocol(self, project: ProjectConfig) -> Protocol:
        return Protocol(1, project.repo, frozenset({"prepare", "run", "cleanup-check", "cleanup"}))

    async def run_logged(self, **kwargs: object) -> int:
        on_started = kwargs["on_started"]
        on_started(os.getpid(), None)
        log_path = Path(kwargs["log_path"])
        log_path.parent.mkdir(parents=True, exist_ok=True)
        log_path.write_text(f"{kwargs['operation']}\n")
        if kwargs["operation"] == "run":
            env = kwargs["env"]
            result = Path(env["REVIEW_QUEUE_RESULT"])
            result.write_text(
                json.dumps(
                    {
                        "protocol": 1,
                        "repository": kwargs["project"].repo,
                        "pr": int(str(kwargs["pr_url"]).rsplit("/", 1)[-1]),
                        "requested_head": env["REVIEW_QUEUE_TARGET_HEAD"],
                        "reviewed_head": env["REVIEW_QUEUE_TARGET_HEAD"],
                        "status": "succeeded",
                    }
                )
            )
        return 0

    async def cleanup_check(self, project: ProjectConfig, *, cwd: Path, env: dict[str, str]):
        return {"safe": True, "reason": "safe", "reclaimable_bytes": 1}

    async def cleanup(self, project: ProjectConfig, *, cwd: Path, env: dict[str, str]) -> None:
        shutil.rmtree(env["REVIEW_QUEUE_WRAPPER"])

    def cancel(self, job_id: int) -> bool:
        return True

    async def shutdown(self) -> None:
        return None


class FakeGitHub:
    def __init__(self) -> None:
        self.project: ProjectConfig | None = None

    async def view(self, project: ProjectConfig, _spec: str) -> PullRequest:
        self.project = project
        return record(project)


class MergedGitHub:
    def __init__(self) -> None:
        self.views: list[int] = []

    async def list_project(self, _project: ProjectConfig) -> GitHubPoll:
        return GitHubPoll(records=(), query_numbers=frozenset())

    async def view(self, project: ProjectConfig, spec: str | int) -> PullRequest:
        self.views.append(int(spec))
        return replace(record(project), state="MERGED")


class GapLauncher(FakeLauncher):
    def __init__(self) -> None:
        super().__init__()
        self.preparing = asyncio.Event()
        self.release_prepare = asyncio.Event()
        self.operations: list[str] = []

    async def run_logged(self, **kwargs: object) -> int:
        operation = str(kwargs["operation"])
        self.operations.append(operation)
        kwargs["on_started"](os.getpid(), None)
        if operation == "prepare":
            self.preparing.set()
            await self.release_prepare.wait()
            return 0
        raise AssertionError("cancelled preparation must not start the review")

    def cancel(self, job_id: int) -> bool:
        return False


def record(project: ProjectConfig) -> PullRequest:
    return PullRequest(
        project=project.name,
        repo=project.repo,
        number=12,
        url=f"https://github.com/{project.repo}/pull/12",
        title="Test",
        author="alice",
        head_sha="a" * 40,
        head_ref="users/alice/test",
        updated_at=isoformat(utc_now() - timedelta(minutes=5)),
    )


async def test_dispatch_allocates_named_wrapper_and_validates_result(
    config: QueueConfig, project: ProjectConfig
) -> None:
    launcher = FakeLauncher()
    scheduler = Scheduler(config, launcher=launcher)
    scheduler.db.apply_poll(
        project,
        (record(project),),
        query_numbers={12},
        bootstrap_hours=24,
        push_quiet_seconds=0,
    )
    assert not scheduler._poll_now.is_set()
    await scheduler._dispatch_available()
    await next(iter(scheduler._job_tasks.values()))
    wrappers = scheduler.db.wrappers()
    assert len(wrappers) == 1
    assert Path(wrappers[0].path).name.startswith("pr-12-alice-users-alice-test")
    assert scheduler.db.job_row(1)["status"] == "succeeded"
    assert (
        json.loads(Path(scheduler.db.job_row(1)["result_path"]).read_text())["status"]
        == "succeeded"
    )
    assert scheduler._poll_now.is_set()
    wrapper_id = wrappers[0].wrapper_id
    assert await scheduler.recycle_wrapper(wrapper_id)
    assert scheduler.db.wrappers() == ()
    assert scheduler.db.job_row(1)["wrapper_id"] is None


async def test_poll_views_missing_eligible_pr_and_removes_it_when_merged(
    config: QueueConfig, project: ProjectConfig
) -> None:
    github = MergedGitHub()
    scheduler = Scheduler(config, github=github)
    scheduler.db.apply_poll(
        project,
        (record(project),),
        query_numbers={12},
        bootstrap_hours=24,
        push_quiet_seconds=0,
    )
    job_id = scheduler.db.queue_items()[0].job_id

    outcome = await scheduler._poll_project(project)

    assert github.views == [12]
    assert outcome.left_query == 1
    assert scheduler.db.queue_items() == ()
    assert scheduler.db.job_row(job_id)["status"] == "ineligible"
    assert scheduler.db.job_row(job_id)["error"] == "PR merged"


async def test_manual_include_uses_url_repository_with_multiple_projects(
    config: QueueConfig, project: ProjectConfig, tmp_path: Path
) -> None:
    second = replace(
        project,
        name="llvm",
        repo="llvm/llvm-project",
        wrapper_root=tmp_path / "llvm-wrappers",
    )
    github = FakeGitHub()
    scheduler = Scheduler(replace(config, projects=(project, second)), github=github)

    await scheduler.add_manual("https://github.com/llvm/llvm-project/pull/12")
    assert github.project == second
    assert scheduler.snapshot().queue[0].repo == second.repo

    with pytest.raises(QueueError, match="full GitHub PR URL"):
        await scheduler.add_manual("12")


async def test_stale_project_does_not_block_healthy_project(
    config: QueueConfig, project: ProjectConfig, tmp_path: Path
) -> None:
    second = replace(
        project,
        name="llvm",
        repo="llvm/llvm-project",
        wrapper_root=tmp_path / "llvm-wrappers",
    )
    scheduler = Scheduler(replace(config, projects=(project, second)), launcher=FakeLauncher())
    for candidate in (project, second):
        scheduler.db.apply_poll(
            candidate,
            (record(candidate),),
            query_numbers={12},
            bootstrap_hours=24,
            push_quiet_seconds=0,
        )
    scheduler.db.mark_poll_failure(project.name, "temporary failure")

    await scheduler._dispatch_available()
    await next(iter(scheduler._job_tasks.values()))
    assert scheduler.db.wrappers()[0].project == second.name


async def test_cancel_during_prepare_run_gap_is_sticky(
    config: QueueConfig, project: ProjectConfig
) -> None:
    launcher = GapLauncher()
    scheduler = Scheduler(config, launcher=launcher)
    scheduler.db.apply_poll(
        project,
        (record(project),),
        query_numbers={12},
        bootstrap_hours=24,
        push_quiet_seconds=0,
    )
    await scheduler._dispatch_available()
    await launcher.preparing.wait()

    scheduler.cancel(1)
    launcher.release_prepare.set()
    await next(iter(scheduler._job_tasks.values()))

    assert launcher.operations == ["prepare"]
    assert scheduler.db.job_row(1)["status"] == "cancelled"


async def test_owned_orphan_is_stopped_before_reconciliation(config: QueueConfig) -> None:
    launcher = FakeLauncher()
    launcher.grace_seconds = 0.1
    scheduler = Scheduler(config, launcher=launcher)
    token = "orphan-owner-token"
    environment = os.environ | {"REVIEW_QUEUE_OWNER_TOKEN": token}
    process = await asyncio.create_subprocess_exec(
        sys.executable,
        "-c",
        "import time; time.sleep(60)",
        env=environment,
    )
    ticks = process_start_ticks(process.pid)
    assert ticks is not None

    assert await scheduler._stop_owned_supervisor(process.pid, ticks, token)
    await process.wait()
    assert process.returncode is not None


async def test_cancel_during_supervisor_registration_is_delivered(
    config: QueueConfig,
    project: ProjectConfig,
    monkeypatch,
) -> None:
    project.launcher.write_text("#!/usr/bin/env bash\nexec sleep 60\n")
    project.launcher.chmod(0o755)
    scheduler = Scheduler(config, launcher=LauncherClient(grace_seconds=0.1))
    scheduler.db.apply_poll(
        project,
        (record(project),),
        query_numbers={12},
        bootstrap_hours=24,
        push_quiet_seconds=0,
    )
    real_create = asyncio.create_subprocess_exec
    spawning = asyncio.Event()
    finish_spawn = asyncio.Event()

    async def delayed_create(*args, **kwargs):
        spawning.set()
        await finish_spawn.wait()
        return await real_create(*args, **kwargs)

    monkeypatch.setattr(asyncio, "create_subprocess_exec", delayed_create)
    await scheduler._dispatch_available()
    task = next(iter(scheduler._job_tasks.values()))
    await spawning.wait()

    scheduler.cancel(1)
    finish_spawn.set()
    await task

    assert scheduler.db.job_row(1)["status"] == "cancelled"
    assert scheduler.launcher.processes == {}


async def test_reconcile_waits_for_unidentified_supervisor_shutdown(
    config: QueueConfig,
    project: ProjectConfig,
    tmp_path: Path,
) -> None:
    launcher = FakeLauncher()
    launcher.grace_seconds = 0.1
    scheduler = Scheduler(config, launcher=launcher)
    scheduler.db.apply_poll(
        project,
        (record(project),),
        query_numbers={12},
        bootstrap_hours=24,
        push_quiet_seconds=0,
    )
    item = scheduler.db.queue_items()[0]
    wrapper = await scheduler._ensure_wrapper(item)
    assert wrapper is not None
    wrapper_id = int(wrapper["id"])
    scheduler.db.attach_job(
        item.job_id,
        wrapper_id,
        log_path=config.state_dir / "logs/orphan.log",
        result_path=config.state_dir / "results/orphan.json",
    )
    scheduler.db.update_job(item.job_id, status="running")
    scheduler.db.update_wrapper(wrapper_id, state="running")

    child_pid_file = tmp_path / "orphan-child.pid"
    token = str(wrapper["owner_token"])
    environment = os.environ | {"REVIEW_QUEUE_OWNER_TOKEN": token}
    child_program = (
        "import os, pathlib, signal, time; "
        f"pathlib.Path({str(child_pid_file)!r}).write_text(str(os.getpid())); "
        "signal.signal(signal.SIGTERM, signal.SIG_IGN); time.sleep(60)"
    )
    supervisor = await asyncio.create_subprocess_exec(
        sys.executable,
        "-m",
        "review_queue.job_supervisor",
        "--grace-seconds",
        "0.1",
        "--",
        sys.executable,
        "-c",
        child_program,
        env=environment,
    )
    for _ in range(100):
        if child_pid_file.exists():
            break
        await asyncio.sleep(0.01)
    assert child_pid_file.exists()
    child_pid = int(child_pid_file.read_text())
    supervisor.send_signal(signal.SIGTERM)

    await scheduler.reconcile_orphans()
    await supervisor.wait()

    assert scheduler.db.job_row(item.job_id)["status"] == "interrupted"
    assert scheduler.db.wrapper_by_id(wrapper_id)["state"] == "idle"
    with pytest.raises(ProcessLookupError):
        os.kill(child_pid, 0)
