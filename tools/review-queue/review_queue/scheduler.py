from __future__ import annotations

import asyncio
import fcntl
import json
import os
import re
import secrets
import signal
from contextlib import suppress
from pathlib import Path

from .database import Database, PollOutcome
from .github import GitHubClient, GitHubError
from .launcher import LauncherClient, LauncherError, failure_details, launcher_environment
from .models import ProjectConfig, QueueConfig, QueueItem, QueueSnapshot
from .util import (
    atomic_json,
    free_bytes,
    isoformat,
    process_has_token,
    process_start_ticks,
    slugify,
)


class QueueError(RuntimeError):
    pass


class SingleInstanceLock:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.stream = path.open("a+")
        try:
            fcntl.flock(self.stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as error:
            self.stream.close()
            raise QueueError(f"another review-queue process owns {path}") from error
        self.stream.seek(0)
        self.stream.truncate()
        self.stream.write(f"{os.getpid()}\n")
        self.stream.flush()

    def close(self) -> None:
        fcntl.flock(self.stream, fcntl.LOCK_UN)
        self.stream.close()

    def __enter__(self) -> SingleInstanceLock:
        return self

    def __exit__(self, *_args: object) -> None:
        self.close()


class Scheduler:
    def __init__(
        self,
        config: QueueConfig,
        *,
        github: GitHubClient | None = None,
        launcher: LauncherClient | None = None,
    ):
        self.config = config
        self.config.state_dir.mkdir(parents=True, exist_ok=True)
        (self.config.state_dir / "logs").mkdir(exist_ok=True)
        (self.config.state_dir / "results").mkdir(exist_ok=True)
        self.db = Database(self.config.state_dir / "queue.sqlite3")
        self.db.seed(
            config.projects, start_paused=config.start_paused, start_mode=config.start_mode
        )
        self.github = github or GitHubClient()
        self.launcher = launcher or LauncherClient()
        self.projects = {project.name: project for project in config.projects}
        self._background: list[asyncio.Task[object]] = []
        self._job_tasks: dict[int, asyncio.Task[None]] = {}
        self._poll_now = asyncio.Event()
        self._poll_lock = asyncio.Lock()
        self._stopping = False
        self._blocked_reason: str | None = None

    async def start(self) -> None:
        for project in self.config.projects:
            await self.launcher.protocol(project)
            project.wrapper_root.mkdir(parents=True, exist_ok=True)
        await self.reconcile_orphans()
        self._background = [
            asyncio.create_task(self._poll_loop(), name="review-queue-poll"),
            asyncio.create_task(self._dispatch_loop(), name="review-queue-dispatch"),
        ]

    async def stop(self) -> None:
        self._stopping = True
        self._poll_now.set()
        for task in self._background:
            task.cancel()
        await asyncio.gather(*self._background, return_exceptions=True)
        for row in self.db.orphaned_jobs():
            with suppress(ValueError):
                self.db.mark_cancelling(int(row["id"]))
        await self.launcher.shutdown()
        if self._job_tasks:
            try:
                await asyncio.wait_for(
                    asyncio.gather(*self._job_tasks.values(), return_exceptions=True),
                    timeout=self.launcher.grace_seconds + 5,
                )
            except TimeoutError:
                for task in self._job_tasks.values():
                    task.cancel()
        self.db.close()

    async def reconcile_orphans(self) -> None:
        orphans = self.db.orphaned_jobs()
        unidentified = tuple(
            row
            for row in orphans
            if not row["supervisor_pid"]
            or row["supervisor_start_ticks"] is None
            or not row["owner_token"]
        )
        if unidentified:
            for row in unidentified:
                if row["wrapper_id"]:
                    self.db.update_wrapper(
                        int(row["wrapper_id"]),
                        state="stopping",
                        cleanup_error="waiting for an unidentified prior launcher",
                        touch=True,
                    )
            await asyncio.sleep(self.launcher.grace_seconds + 1)

        for row in orphans:
            pid = row["supervisor_pid"]
            expected_ticks = row["supervisor_start_ticks"]
            token = row["owner_token"]
            owned = bool(
                pid
                and expected_ticks is not None
                and process_start_ticks(int(pid)) == int(expected_ticks)
                and token
                and process_has_token(int(pid), str(token))
            )
            if owned:
                stopped = await self._stop_owned_supervisor(
                    int(pid), int(expected_ticks), str(token)
                )
                if not stopped:
                    self.db.update_job(
                        int(row["id"]),
                        status="cancelling",
                        error="queue restart could not stop the prior launcher",
                    )
                    if row["wrapper_id"]:
                        self.db.update_wrapper(
                            int(row["wrapper_id"]),
                            state="cleanup_failed",
                            cleanup_error="prior launcher is still alive",
                            touch=True,
                        )
                    continue
            self.db.update_job(
                int(row["id"]),
                status="interrupted",
                error="queue restarted during an active launcher",
            )
            if row["wrapper_id"]:
                self.db.update_wrapper(int(row["wrapper_id"]), state="idle", touch=True)
        self._reconcile_wrapper_records()

    async def _stop_owned_supervisor(self, pid: int, ticks: int, token: str) -> bool:
        def still_owned() -> bool:
            return process_start_ticks(pid) == ticks and process_has_token(pid, token)

        if not still_owned():
            return True
        with suppress(ProcessLookupError):
            os.kill(pid, signal.SIGTERM)
        deadline = asyncio.get_running_loop().time() + self.launcher.grace_seconds + 5
        while still_owned() and asyncio.get_running_loop().time() < deadline:
            await asyncio.sleep(0.1)
        if not still_owned():
            return True
        with suppress(ProcessLookupError):
            os.kill(pid, signal.SIGKILL)
        deadline = asyncio.get_running_loop().time() + 5
        while still_owned() and asyncio.get_running_loop().time() < deadline:
            await asyncio.sleep(0.1)
        return not still_owned()

    def _reconcile_wrapper_records(self) -> None:
        active_ids = {
            int(row["wrapper_id"])
            for row in self.db.orphaned_jobs()
            if row["wrapper_id"] is not None
        }
        for view in self.db.wrappers():
            row = self.db.wrapper_by_id(view.wrapper_id)
            if row is None:
                continue
            path = Path(row["path"])
            if not path.exists() and not path.is_symlink():
                self.db.remove_wrapper(view.wrapper_id)
                continue
            reason: str | None = None
            if path.is_symlink() or not path.is_dir():
                reason = "managed wrapper path is not a directory"
            else:
                marker = path / ".review-queue.json"
                try:
                    payload = json.loads(marker.read_text())
                except (OSError, json.JSONDecodeError):
                    reason = "managed wrapper ownership marker is unreadable"
                else:
                    if (
                        payload.get("protocol") != 1
                        or payload.get("owner_token") != row["owner_token"]
                    ):
                        reason = "managed wrapper ownership marker does not match"
            if reason:
                self.db.update_wrapper(
                    view.wrapper_id, state="cleanup_failed", cleanup_error=reason, touch=True
                )
            elif view.wrapper_id not in active_ids and view.state in {
                "allocating",
                "preparing",
                "running",
                "cleaning",
            }:
                self.db.update_wrapper(view.wrapper_id, state="idle", touch=True)

    async def _poll_loop(self) -> None:
        while not self._stopping:
            self._poll_now.clear()
            await self.poll_once()
            with suppress(TimeoutError):
                await asyncio.wait_for(self._poll_now.wait(), timeout=self.config.poll_seconds)

    async def poll_once(self) -> dict[str, PollOutcome | Exception]:
        async with self._poll_lock:
            results = await asyncio.gather(
                *(self._poll_project(project) for project in self.config.projects),
                return_exceptions=True,
            )
        return dict(zip((project.name for project in self.config.projects), results, strict=True))

    async def _poll_project(self, project: ProjectConfig) -> PollOutcome:
        try:
            poll = await self.github.list_project(project)
            records = list(poll.records)
            present = set(poll.query_numbers)
            followups = set(self.db.poll_followups(project.name)) - present
            if followups:
                extra = await asyncio.gather(
                    *(self.github.view(project, number) for number in sorted(followups))
                )
                records.extend(extra)
            return self.db.apply_poll(
                project,
                records,
                query_numbers=set(poll.query_numbers),
                bootstrap_hours=self.config.bootstrap_hours,
                push_quiet_seconds=self.config.push_quiet_seconds,
            )
        except Exception as error:
            self.db.mark_poll_failure(project.name, str(error))
            if isinstance(error, (GitHubError, QueueError)):
                raise
            raise GitHubError(str(error)) from error

    def request_refresh(self) -> None:
        self._poll_now.set()

    async def _dispatch_loop(self) -> None:
        while not self._stopping:
            try:
                self.db.promote_debounced()
                await self._dispatch_available()
            except Exception as error:
                self._blocked_reason = f"scheduler: {error}"
            await asyncio.sleep(0.5)

    async def _dispatch_available(self) -> None:
        if self.db.paused():
            self._blocked_reason = "dispatch paused"
            return
        if self.db.running_count() >= self.config.max_running:
            self._blocked_reason = "all review slots are occupied"
            return
        stale_projects = self.db.stale_projects()
        item = self.db.next_ready(excluded_projects=stale_projects)
        if item is None:
            self._blocked_reason = (
                "waiting projects have stale GitHub state"
                if stale_projects and self.db.next_ready() is not None
                else "waiting for manual enqueue"
                if self.db.manual_only()
                else None
            )
            return
        wrapper = await self._ensure_wrapper(item)
        if wrapper is None:
            return
        job_id = item.job_id
        # Wrapper cleanup can await I/O while the user changes the dispatch mode.
        current = self.db.job_row(job_id)
        if self.db.paused() or (
            self.db.manual_only() and current["source"] not in {"manual", "manual-watch"}
        ):
            self._blocked_reason = "dispatch mode changed; waiting"
            return
        self.db.update_job(job_id, status="preparing")
        self.db.update_wrapper(int(wrapper["id"]), state="preparing", touch=True)
        task = asyncio.create_task(self._run_job(job_id), name=f"review-job-{job_id}")
        self._job_tasks[job_id] = task
        task.add_done_callback(lambda _task, value=job_id: self._job_tasks.pop(value, None))
        self._blocked_reason = None

    async def _ensure_wrapper(self, item: QueueItem):
        existing = self.db.wrapper_for_pr(item.repo, item.number)
        if existing is not None:
            return existing

        wrappers = self.db.wrappers()
        if len(wrappers) >= self.config.max_wrappers:
            candidate = next(iter(self.db.wrapper_candidates()), None)
            if candidate is None:
                self._blocked_reason = "wrapper cap reached; no safe idle candidate"
                return None
            if not await self._cleanup_wrapper_row(candidate):
                self._blocked_reason = "wrapper cap reached; cleanup preflight blocked"
                return None

        project = self.projects[item.project]
        largest = max(
            (
                wrapper.size_bytes
                for wrapper in self.db.wrappers()
                if wrapper.project == item.project
            ),
            default=0,
        )
        estimated = max(project.estimated_wrapper_gib * 1024**3, largest)
        required = estimated + self.config.min_free_gib * 1024**3
        available = free_bytes(project.wrapper_root)
        if available < required:
            self._blocked_reason = (
                f"disk guard: {available / 1024**3:.0f} GiB free, "
                f"{required / 1024**3:.0f} GiB required"
            )
            return None

        base = (
            f"pr-{item.number}-{slugify(item.author, limit=24)}-{slugify(item.head_ref, limit=36)}"
        )
        path = project.wrapper_root / base
        suffix = 2
        while path.exists():
            path = project.wrapper_root / f"{base}-{suffix}"
            suffix += 1
        token = secrets.token_hex(24)
        path.mkdir(mode=0o700, parents=True)
        atomic_json(
            path / ".review-queue.json",
            {
                "protocol": 1,
                "owner_token": token,
                "project": item.project,
                "repo": item.repo,
                "pr": item.number,
                "created_at": isoformat(),
            },
        )
        (path / ".review-queue.lock").touch(mode=0o600)
        wrapper_id = self.db.create_wrapper(
            project=item.project,
            repo=item.repo,
            number=item.number,
            path=path,
            token=token,
            head_ref=item.head_ref,
        )
        return self.db.wrapper_by_id(wrapper_id)

    async def _run_job(self, job_id: int) -> None:
        row = self.db.job_row(job_id)
        if row is not None and not row["wrapper_id"]:
            wrapper_row = self.db.wrapper_for_pr(row["repo"], int(row["pr_number"]))
            if wrapper_row is not None:
                log_path = self.config.state_dir / "logs" / f"job-{job_id}.log"
                result_path = self.config.state_dir / "results" / f"job-{job_id}.json"
                self.db.attach_job(
                    job_id,
                    int(wrapper_row["id"]),
                    log_path=log_path,
                    result_path=result_path,
                )
                row = self.db.job_row(job_id)
        if row is None or not row["wrapper_id"]:
            self.db.update_job(job_id, status="failed", error="job has no wrapper")
            return
        project = self.projects[row["project"]]
        wrapper = Path(row["wrapper_path"])
        log_path = self.config.state_dir / "logs" / f"job-{job_id}.log"
        result_path = self.config.state_dir / "results" / f"job-{job_id}.json"
        result_path.unlink(missing_ok=True)
        self.db.attach_job(
            job_id, int(row["wrapper_id"]), log_path=log_path, result_path=result_path
        )
        env = launcher_environment(
            project=project,
            wrapper=wrapper,
            target_head=row["head_sha"],
            result=result_path,
            token=row["owner_token"],
        )

        try:
            lock_stream = (wrapper / ".review-queue.lock").open("r+")
        except OSError as error:
            self.db.update_job(job_id, status="failed", error=f"wrapper lock unavailable: {error}")
            self.db.update_wrapper(
                int(row["wrapper_id"]), state="cleanup_failed", cleanup_error=str(error), touch=True
            )
            return
        try:
            fcntl.flock(lock_stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            lock_stream.close()
            self.db.update_job(job_id, status="failed", error="wrapper ownership lock is busy")
            self.db.update_wrapper(int(row["wrapper_id"]), state="cleanup_failed", touch=True)
            return

        def started(pid: int, ticks: int | None) -> None:
            latest = self.db.job_row(job_id)
            if latest is None:
                self.launcher.cancel(job_id)
                return
            self.db.update_job(
                job_id,
                status=latest["status"],
                supervisor_pid=pid,
                supervisor_start_ticks=ticks,
            )
            if latest["status"] == "cancelling":
                self.launcher.cancel(job_id)

        try:
            prepare_offset = log_path.stat().st_size if log_path.exists() else 0
            prepare_code = await self.launcher.run_logged(
                job_id=job_id,
                project=project,
                operation="prepare",
                pr_url=row["url"],
                cwd=wrapper,
                env=env,
                log_path=log_path,
                on_started=started,
                on_phase=lambda change: self.db.update_phase(job_id, change),
            )
            if prepare_code:
                latest = self.db.job_row(job_id)
                cancelled = latest is not None and latest["status"] == "cancelling"
                status, reason = failure_details(
                    log_path=log_path,
                    result_path=result_path,
                    operation="prepare",
                    exit_code=prepare_code,
                    repo=row["repo"],
                    number=row["pr_number"],
                    head=row["head_sha"],
                    log_offset=max(prepare_offset, latest["phase_log_offset"] if latest else 0),
                    phase=latest["phase"] if latest else "",
                )
                self.db.update_job(
                    job_id,
                    status="cancelled" if cancelled else status,
                    error="cancelled by user" if cancelled else reason,
                    exit_code=prepare_code,
                )
                return

            latest = self.db.job_row(job_id)
            if latest is not None and latest["status"] == "cancelling":
                self.db.update_job(job_id, status="cancelled", error="cancelled by user")
                return
            self.db.update_job(job_id, status="running")
            self.db.update_wrapper(int(row["wrapper_id"]), state="running", touch=True)
            run_offset = log_path.stat().st_size if log_path.exists() else 0
            run_code = await self.launcher.run_logged(
                job_id=job_id,
                project=project,
                operation="run",
                pr_url=row["url"],
                cwd=wrapper,
                env=env,
                log_path=log_path,
                on_started=started,
                on_phase=lambda change: self.db.update_phase(job_id, change),
            )
            latest = self.db.job_row(job_id)
            if latest is not None and latest["status"] == "cancelling":
                self.db.update_job(
                    job_id, status="cancelled", error="cancelled by user", exit_code=run_code
                )
            elif run_code:
                status, reason = failure_details(
                    log_path=log_path,
                    result_path=result_path,
                    operation="run",
                    exit_code=run_code,
                    repo=row["repo"],
                    number=row["pr_number"],
                    head=row["head_sha"],
                    log_offset=max(run_offset, latest["phase_log_offset"] if latest else 0),
                    phase=latest["phase"] if latest else "",
                )
                self.db.update_job(
                    job_id,
                    status=status,
                    error=reason,
                    exit_code=run_code,
                )
            else:
                self._validate_result(result_path, row)
                self.db.update_job(job_id, status="succeeded", exit_code=0)
        except (LauncherError, QueueError, OSError, ValueError, json.JSONDecodeError) as error:
            latest = self.db.job_row(job_id)
            status = "cancelled" if latest and latest["status"] == "cancelling" else "failed"
            self.db.update_job(job_id, status=status, error=str(error))
        finally:
            fcntl.flock(lock_stream, fcntl.LOCK_UN)
            lock_stream.close()
            size = await self._measure(wrapper)
            self.db.update_wrapper(
                int(row["wrapper_id"]), state="idle", size_bytes=size, touch=True
            )
            self.request_refresh()

    @staticmethod
    def _validate_result(path: Path, row: object) -> None:
        if not path.is_file():
            raise QueueError(f"launcher succeeded without result JSON: {path}")
        payload = json.loads(path.read_text())
        expected = {
            "protocol": 1,
            "repository": row["repo"],
            "pr": row["pr_number"],
            "requested_head": row["head_sha"],
            "reviewed_head": row["head_sha"],
            "status": "succeeded",
        }
        for key, value in expected.items():
            if payload.get(key) != value:
                actual = payload.get(key)
                raise QueueError(
                    f"launcher result mismatch for {key}: expected {value!r}, got {actual!r}"
                )

    async def _measure(self, path: Path) -> int:
        if not path.exists():
            return 0
        process = await asyncio.create_subprocess_exec(
            "du", "-sb", "--", str(path), stdout=asyncio.subprocess.PIPE
        )
        stdout, _ = await process.communicate()
        if process.returncode:
            return 0
        try:
            return int(stdout.split(maxsplit=1)[0])
        except (ValueError, IndexError):
            return 0

    async def _cleanup_wrapper_row(self, row: object) -> bool:
        project = self.projects[row["project"]]
        wrapper = Path(row["path"])
        result = self.config.state_dir / "results" / f"cleanup-{row['id']}.json"
        env = launcher_environment(
            project=project,
            wrapper=wrapper,
            target_head="cleanup",
            result=result,
            token=row["owner_token"],
        )
        self.db.update_wrapper(int(row["id"]), state="cleaning", cleanup_error=None)
        try:
            check = await self.launcher.cleanup_check(project, cwd=project.wrapper_root, env=env)
            if not check["safe"]:
                reason = str(check.get("reason") or "cleanup preflight rejected wrapper")
                self.db.update_wrapper(
                    int(row["id"]), state="cleanup_failed", cleanup_error=reason, touch=True
                )
                return False
            await self.launcher.cleanup(project, cwd=project.wrapper_root, env=env)
            self.db.remove_wrapper(int(row["id"]))
            return True
        except (LauncherError, OSError) as error:
            self.db.update_wrapper(
                int(row["id"]), state="cleanup_failed", cleanup_error=str(error), touch=True
            )
            return False

    async def recycle_wrapper(self, wrapper_id: int) -> bool:
        row = self.db.wrapper_by_id(wrapper_id)
        if row is None:
            raise KeyError(f"unknown wrapper: {wrapper_id}")
        if row["pinned"]:
            raise QueueError("wrapper is pinned")
        return await self._cleanup_wrapper_row(row)

    def toggle_pause(self) -> bool:
        paused = not self.db.paused()
        self.db.set_paused(paused)
        return paused

    def cycle_mode(self) -> str:
        modes = ("active", "manual", "paused")
        mode = modes[(modes.index(self.db.mode()) + 1) % len(modes)]
        self.db.set_mode(mode)
        return mode

    def adjust_priority(self, item: QueueItem, delta: int) -> None:
        self.db.adjust_pr_priority(item.repo, item.number, delta)

    def set_priorities(
        self,
        item: QueueItem,
        *,
        project_priority: int,
        author_priority: int,
        pr_priority: int,
    ) -> None:
        self.db.set_priorities(
            project=item.project,
            author=item.author,
            repo=item.repo,
            number=item.number,
            project_priority=project_priority,
            author_priority=author_priority,
            pr_priority=pr_priority,
        )

    async def add_manual(self, spec: str) -> None:
        match = re.search(r"github\.com/([^/]+/[^/]+)/pull/\d+", spec, re.IGNORECASE)
        if match:
            repo = match.group(1).lower()
            project = next(
                (item for item in self.config.projects if item.repo.lower() == repo),
                None,
            )
            if project is None:
                raise QueueError(f"PR repository is not configured: {match.group(1)}")
        elif len(self.config.projects) == 1:
            project = self.config.projects[0]
        else:
            raise QueueError("use a full GitHub PR URL when multiple projects are configured")
        record = await self.github.view(project, spec)
        if record.is_draft or record.state.upper() != "OPEN":
            raise QueueError("manual PR must be open and non-draft")
        self.db.set_manual_watch(record)

    def ignore_pr(self, repo: str, number: int) -> None:
        self.db.ignore_pr(repo, number)

    def retry(self, item: QueueItem) -> int:
        return self.db.enqueue_current(item.repo, item.number, manual=True)

    def retry_wrapper(self, wrapper_id: int) -> int:
        return self.db.enqueue_wrapper_pr(wrapper_id)

    def cancel(self, job_id: int) -> None:
        row = self.db.job_row(job_id)
        if row is None:
            raise KeyError(f"unknown job: {job_id}")
        if row["status"] in {"queued", "debouncing"}:
            self.db.mark_waiting_cancelled(job_id)
            return
        self.db.mark_cancelling(job_id)
        self.launcher.cancel(job_id)

    def toggle_wrapper_pin(self, wrapper_id: int) -> bool:
        row = self.db.wrapper_by_id(wrapper_id)
        if row is None:
            raise KeyError(f"unknown wrapper: {wrapper_id}")
        pinned = not bool(row["pinned"])
        self.db.set_wrapper_pinned(wrapper_id, pinned)
        return pinned

    def snapshot(self) -> QueueSnapshot:
        free = min(
            (free_bytes(project.wrapper_root) for project in self.config.projects), default=0
        )
        return QueueSnapshot(
            paused=self.db.paused(),
            manual_only=self.db.manual_only(),
            dispatch_blocked_reason=self._blocked_reason,
            max_running=self.config.max_running,
            max_wrappers=self.config.max_wrappers,
            free_bytes=free,
            queue=self.db.queue_items(),
            running=self.db.running(),
            wrappers=self.db.wrappers(),
            projects=self.db.project_health(),
            phase_events=self.db.phase_events(),
        )

    def log_tail(self, path: str | None, *, lines: int = 500) -> str:
        if not path:
            return ""
        log = Path(path)
        if not log.is_file():
            return ""
        with log.open(errors="replace") as stream:
            return "".join(stream.readlines()[-lines:])

    def wrapper_log_tail(self, wrapper_id: int, *, lines: int = 500) -> str:
        return self.log_tail(self.db.latest_wrapper_log(wrapper_id), lines=lines)
