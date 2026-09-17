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
from review_queue.launcher import LauncherClient, LauncherError, Protocol
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


@pytest.mark.parametrize("exit_code", [0, 7])
async def test_live_phases_persist_before_process_exit(
    config, project, tmp_path, exit_code, monkeypatch
):
    from review_queue.database import Database

    gate = tmp_path / "continue"
    project.launcher.write_text(
        f"#!{sys.executable}\n"
        + """
import json, os, sys, time
from pathlib import Path
if sys.argv[2] == "prepare":
    print("::group::Setup", flush=True)
    sys.exit(0)
print("source.cpp:1: error: earlier nonblocking diagnostic", flush=True)
print("::group::Build", flush=True)
print("::group::Clang", flush=True)
while not Path(os.environ["TEST_PHASE_GATE"]).exists():
    time.sleep(0.01)
code = int(os.environ["TEST_PHASE_EXIT"])
if code:
    print("source.cpp:12: error: broken build", flush=True)
    print("Final checkout after failure: ready", flush=True)
    sys.exit(code)
print("::endgroup::", flush=True)
print("::endgroup::", flush=True)
Path(os.environ["REVIEW_QUEUE_RESULT"]).write_text(json.dumps({
    "protocol": 1, "repository": "ROCm/rocm-systems", "pr": 12,
    "requested_head": os.environ["REVIEW_QUEUE_TARGET_HEAD"],
    "reviewed_head": os.environ["REVIEW_QUEUE_TARGET_HEAD"], "status": "succeeded",
}))
"""
    )
    monkeypatch.setenv("TEST_PHASE_GATE", str(gate))
    monkeypatch.setenv("TEST_PHASE_EXIT", str(exit_code))
    scheduler = Scheduler(config, launcher=LauncherClient(grace_seconds=0.1))
    scheduler.db.apply_poll(
        project, (record(project),), query_numbers={12}, bootstrap_hours=24, push_quiet_seconds=0
    )
    await scheduler._dispatch_available()
    task = next(iter(scheduler._job_tasks.values()))
    try:

        async def wait_phase():
            while scheduler.db.job_row(1)["phase"] != "Build › Clang":
                if task.done():
                    await task
                    raise AssertionError(dict(scheduler.db.job_row(1)))
                await asyncio.sleep(0.01)

        await asyncio.wait_for(wait_phase(), timeout=10)
        assert not task.done()
        active = scheduler.snapshot().running[0]
        assert active.status == "running"
        assert active.phase == "Build › Clang"
        assert active.phase_started_at
        other = Database(config.state_dir / "queue.sqlite3")
        try:
            assert other.job_row(1)["phase"] == active.phase
            assert [e.phase for e in other.phase_events()] == ["Setup", "Build", "Build › Clang"]
        finally:
            other.close()
    finally:
        gate.touch()
        await asyncio.wait_for(task, timeout=10)
    row = scheduler.db.job_row(1)
    if exit_code:
        assert row["status"] == "failed"
        assert row["phase"] == "Build › Clang"
        assert row["error"].startswith("Build › Clang failed (exit 7)")
        assert "broken build" in row["error"]
        assert "earlier nonblocking" not in row["error"]
    else:
        assert row["status"] == "succeeded"
        assert row["phase"] == ""


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


async def test_manual_mode_dispatch_requires_enqueue_and_pause_still_blocks(config, project):
    scheduler = Scheduler(config, launcher=FakeLauncher(), github=FakeGitHub())
    scheduler.db.apply_poll(
        project, (record(project),), query_numbers={12}, bootstrap_hours=24, push_quiet_seconds=0
    )
    assert scheduler.cycle_mode() == "manual"
    await scheduler._dispatch_available()
    assert not scheduler._job_tasks
    assert not scheduler.db.wrappers()
    assert scheduler.snapshot().dispatch_blocked_reason == "waiting for manual enqueue"
    await scheduler.add_manual("12")
    assert scheduler.snapshot().queue[0].manual_enqueued
    scheduler.toggle_pause()
    await scheduler._dispatch_available()
    assert not scheduler._job_tasks
    scheduler.toggle_pause()
    assert scheduler.snapshot().mode == "manual"
    await scheduler._dispatch_available()
    await next(iter(scheduler._job_tasks.values()))
    assert scheduler.db.connection.execute("SELECT status FROM jobs").fetchone()[0] == "succeeded"


async def test_switching_to_manual_during_wrapper_preparation_holds_job(
    config, project, monkeypatch
):
    scheduler = Scheduler(config, launcher=FakeLauncher())
    scheduler.db.apply_poll(
        project, (record(project),), query_numbers={12}, bootstrap_hours=24, push_quiet_seconds=0
    )
    prepare = scheduler._ensure_wrapper

    async def switch_mode(item):
        wrapper = await prepare(item)
        scheduler.db.set_mode("manual")
        return wrapper

    monkeypatch.setattr(scheduler, "_ensure_wrapper", switch_mode)
    await scheduler._dispatch_available()
    assert not scheduler._job_tasks
    assert scheduler.snapshot().queue[0].status == "queued"
    scheduler.retry(scheduler.snapshot().queue[0])
    await scheduler._dispatch_available()
    await next(iter(scheduler._job_tasks.values()))


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


@pytest.fixture
async def failed_review(config, project):
    class BuildFailure(FakeLauncher):
        async def run_logged(self, **kwargs):
            if kwargs["operation"] == "run":
                Path(kwargs["log_path"]).write_text("mold: error: undefined symbol: missing\n")
                return 1
            return await super().run_logged(**kwargs)

    scheduler = Scheduler(config, launcher=BuildFailure())
    scheduler.db.apply_poll(
        project, (record(project),), query_numbers={12}, bootstrap_hours=24, push_quiet_seconds=0
    )
    await scheduler._dispatch_available()
    await next(iter(scheduler._job_tasks.values()))
    assert scheduler.db.job_row(1)["status"] == "failed"
    yield scheduler
    scheduler.db.close()


async def test_failed_review_automatically_recycles_and_keeps_failure_log(failed_review):
    scheduler = failed_review
    wrapper = scheduler.db.wrappers()[0]
    scheduler.db.set_mode("manual")
    await scheduler._dispatch_available()
    assert scheduler.db.wrappers() == ()
    assert not Path(wrapper.path).exists()
    assert scheduler.db.job_row(1)["wrapper_id"] is None
    failure = scheduler.snapshot().failures[0]
    assert failure.number == 12
    assert "undefined symbol" in failure.error
    assert "undefined symbol" in Path(failure.log_path).read_text()
    assert scheduler.db.queue_items() == ()


@pytest.mark.parametrize("protection", ["pinned", "unsafe", "retry", "active"])
async def test_failed_workspace_cleanup_preserves_protected_work(
    failed_review, protection, monkeypatch
):
    scheduler = failed_review
    wrapper = scheduler.db.wrappers()[0]
    if protection == "pinned":
        scheduler.db.set_wrapper_pinned(wrapper.wrapper_id, True)
    elif protection == "unsafe":

        async def unsafe(*args, **kwargs):
            return {"safe": False, "reason": "source changes"}

        monkeypatch.setattr(scheduler.launcher, "cleanup_check", unsafe)
    elif protection == "retry":
        scheduler.retry_wrapper(wrapper.wrapper_id)
    else:
        scheduler.db.update_wrapper(wrapper.wrapper_id, state="running")
    await scheduler._recycle_failed_wrappers()
    assert Path(wrapper.path).exists()
    assert scheduler.db.wrapper_by_id(wrapper.wrapper_id) is not None
    assert scheduler.snapshot().failures[0].job_id == 1


@pytest.fixture
async def full_wrapper_pool(config, project):
    scheduler = Scheduler(replace(config, max_wrappers=2), launcher=FakeLauncher())
    scheduler.db.apply_poll(
        project,
        tuple(replace(record(project), number=number) for number in (12, 13, 14)),
        query_numbers={12, 13, 14},
        bootstrap_hours=24,
        push_quiet_seconds=0,
    )
    items = sorted(scheduler.db.queue_items(), key=lambda item: item.number)
    wrappers = []
    for item in items[:2]:
        wrapper = await scheduler._ensure_wrapper(item)
        scheduler.db.update_wrapper(wrapper["id"], state="idle")
        scheduler.db.update_job(item.job_id, status="succeeded")
        wrappers.append(scheduler.db.wrapper_by_id(wrapper["id"]))
    yield scheduler, wrappers, items[2]
    scheduler.db.close()


@pytest.mark.parametrize("failure", ["unsafe", "check-error", "cleanup-error"])
async def test_failed_cleanup_backs_off_without_reordering(full_wrapper_pool, monkeypatch, failure):
    scheduler, wrappers, item = full_wrapper_pool
    now = 100.0
    monkeypatch.setattr("review_queue.scheduler.time.monotonic", lambda: now)
    checks = []

    async def check(project, *, cwd, env):
        checks.append(env["REVIEW_QUEUE_WRAPPER"])
        if failure == "check-error":
            raise LauncherError("check failed")
        return {"safe": failure == "cleanup-error", "reason": "unknown file"}

    async def cleanup(project, *, cwd, env):
        raise OSError("cleanup failed")

    monkeypatch.setattr(scheduler.launcher, "cleanup_check", check)
    monkeypatch.setattr(scheduler.launcher, "cleanup", cleanup)
    assert await scheduler._ensure_wrapper(item) is None
    assert checks == [row["path"] for row in wrappers]
    for _ in range(5):
        assert await scheduler._ensure_wrapper(item) is None
    assert len(checks) == 2
    now += 60
    assert await scheduler._ensure_wrapper(item) is None
    assert len(checks) == 4
    candidates = scheduler.db.wrapper_candidates()
    assert [row["id"] for row in candidates] == [row["id"] for row in wrappers]
    assert [row["last_used_at"] for row in candidates] == [row["last_used_at"] for row in wrappers]
    assert all(row["state"] == "cleanup_failed" for row in candidates)
    assert "retry every 60s" in scheduler.snapshot().dispatch_blocked_reason
    # Explicit recycling can immediately retry after the user fixes the blocker.
    monkeypatch.setattr(scheduler.launcher, "cleanup_check", FakeLauncher().cleanup_check)
    monkeypatch.setattr(scheduler.launcher, "cleanup", FakeLauncher().cleanup)
    assert await scheduler.recycle_wrapper(wrappers[0]["id"])
    assert not Path(wrappers[0]["path"]).exists()


@pytest.mark.parametrize("state", ["idle", "preparing", "running", "cleaning"])
async def test_wrapper_reuse_clears_previous_cleanup_error(full_wrapper_pool, state):
    scheduler, wrappers, _ = full_wrapper_pool
    wrapper_id = wrappers[0]["id"]
    scheduler.db.update_wrapper(wrapper_id, state="cleanup_failed", cleanup_error="unknown file")
    scheduler.db.update_wrapper(wrapper_id, state=state)
    assert scheduler.db.wrapper_by_id(wrapper_id)["cleanup_error"] is None


async def test_cleanup_tries_next_candidate_when_oldest_is_blocked(full_wrapper_pool, monkeypatch):
    scheduler, wrappers, item = full_wrapper_pool
    checks = []

    async def check(project, *, cwd, env):
        path = env["REVIEW_QUEUE_WRAPPER"]
        checks.append(path)
        return {"safe": path != wrappers[0]["path"], "reason": "unknown file"}

    monkeypatch.setattr(scheduler.launcher, "cleanup_check", check)
    allocated = await scheduler._ensure_wrapper(item)
    assert allocated["pr_number"] == item.number
    assert checks == [row["path"] for row in wrappers]
    assert Path(wrappers[0]["path"]).exists()
    assert not Path(wrappers[1]["path"]).exists()
    assert len(scheduler.db.wrappers()) == 2


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


@pytest.mark.parametrize("operation", ["prepare", "run"])
@pytest.mark.parametrize("explicit_cancel", [False, True])
async def test_exit_then_restart_recovers_active_review_once(
    config, project, operation, explicit_cancel, monkeypatch
):
    project.launcher.write_text(
        f"#!{sys.executable}\n"
        "import os, sys, time\n"
        "if sys.argv[2] == os.environ['TEST_BLOCK_OPERATION']:\n"
        "    print('::group::Waiting', flush=True)\n"
        "    time.sleep(60)\n"
    )
    monkeypatch.setenv("TEST_BLOCK_OPERATION", operation)
    scheduler = Scheduler(config, launcher=LauncherClient(grace_seconds=0.1))
    scheduler.db.apply_poll(
        project, (record(project),), query_numbers={12}, bootstrap_hours=24, push_quiet_seconds=0
    )
    scheduler.db.set_mode("manual")
    scheduler.db.enqueue_current(project.repo, 12)
    await scheduler._dispatch_available()
    task = next(iter(scheduler._job_tasks.values()))

    async def wait_started():
        while scheduler.db.job_row(1)["phase"] != "Waiting":
            if task.done():
                await task
                raise AssertionError(dict(scheduler.db.job_row(1)))
            await asyncio.sleep(0.01)

    try:
        await asyncio.wait_for(wait_started(), timeout=10)
        if explicit_cancel:
            scheduler.cancel(1)
    finally:
        await scheduler.stop()
    assert task.done()

    class FreshGitHub(FakeGitHub):
        async def list_project(self, project):
            return GitHubPoll(records=(record(project),), query_numbers=frozenset({12}))

    restarted = Scheduler(config, launcher=FakeLauncher(), github=FreshGitHub())
    try:
        assert restarted.db.job_row(1)["status"] == (
            "cancelled" if explicit_cancel else "interrupted"
        )
        await restarted.reconcile_orphans()
        assert restarted.db.queue_items() == ()  # Wait for fresh GitHub validation.
        await restarted.poll_once()
        await restarted.poll_once()
        items = restarted.db.queue_items()
        assert len(items) == (0 if explicit_cancel else 1)
        if items:
            assert items[0].head_sha == "a" * 40
            assert items[0].manual_enqueued
            assert restarted.db.next_ready().job_id == items[0].job_id
            assert restarted.db.job_row(items[0].job_id)["attempt"] == 2
    finally:
        restarted.db.close()


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


async def test_shutdown_does_not_finish_an_unresolved_prior_supervisor(config, project):
    from review_queue.database import Database

    scheduler = Scheduler(config, launcher=FakeLauncher())
    scheduler.db.apply_poll(
        project, (record(project),), query_numbers={12}, bootstrap_hours=24, push_quiet_seconds=0
    )
    scheduler.db.update_job(1, status="running")
    scheduler.db.mark_cancelling(1, resume_on_restart=True)
    await scheduler.stop()
    db = Database(config.state_dir / "queue.sqlite3")
    try:
        assert db.job_row(1)["status"] == "cancelling"
        assert db.resume_interrupted_jobs(project.name) == 0
    finally:
        db.close()


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


async def test_path_check_failure_holds_dispatch_and_manual_inclusion(config, project):
    from review_queue.github import GitHubError

    project = replace(project, include_paths=("emulation/",))
    config = replace(config, projects=(project,))
    included = replace(
        record(project), path_filter_key=project.path_filter_key, path_filter_passed=True
    )

    class FailingFiles:
        async def list_project(self, _project):
            raise GitHubError("incomplete PR file list")

        async def view(self, _project, _spec):
            return replace(included, path_filter_passed=False)

    scheduler = Scheduler(config, github=FailingFiles(), launcher=FakeLauncher())
    scheduler.db.apply_poll(
        project, [included], query_numbers={12}, bootstrap_hours=24, push_quiet_seconds=0
    )
    with pytest.raises(GitHubError, match="incomplete"):
        await scheduler._poll_project(project)
    await scheduler._dispatch_available()
    assert not scheduler._job_tasks
    assert scheduler.db.stale_projects() == frozenset({project.name})
    with pytest.raises(ValueError, match="include_paths"):
        await scheduler.add_manual("12")


@pytest.mark.parametrize("operation", ["prepare", "run"])
@pytest.mark.parametrize("ineligible", [True, False])
async def test_launcher_failure_reason_and_terminal_status(config, project, operation, ineligible):
    class FailedLauncher(FakeLauncher):
        async def run_logged(self, **kwargs):
            if kwargs["operation"] != operation:
                return await super().run_logged(**kwargs)
            kwargs["on_started"](os.getpid(), None)
            with Path(kwargs["log_path"]).open("a") as log:
                log.write("test.cpp:12: error: missing symbol\n")
            if ineligible:
                Path(kwargs["env"]["REVIEW_QUEUE_RESULT"]).write_text(
                    json.dumps(
                        {
                            "protocol": 1,
                            "repository": project.repo,
                            "pr": 12,
                            "requested_head": "a" * 40,
                            "exit_code": 1,
                            "status": "ineligible",
                            "error": "PR #12 is merged",
                        }
                    )
                )
            return 1

    scheduler = Scheduler(config, launcher=FailedLauncher())
    scheduler.db.apply_poll(
        project, (record(project),), query_numbers={12}, bootstrap_hours=24, push_quiet_seconds=0
    )
    await scheduler._dispatch_available()
    await next(iter(scheduler._job_tasks.values()))
    row = scheduler.db.job_row(1)
    assert row["status"] == ("ineligible" if ineligible else "failed")
    assert row["finished_at"]
    assert row["exit_code"] == 1
    assert ("PR #12 is merged" if ineligible else "missing symbol") in row["error"]
    assert scheduler.snapshot().wrappers[0].state == row["status"]
    assert scheduler.snapshot().wrappers[0].cleanup_error == row["error"]
    assert scheduler._poll_now.is_set()
