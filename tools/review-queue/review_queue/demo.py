from __future__ import annotations

import asyncio
import hashlib
import re
from contextlib import suppress
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from .models import ProjectHealth, QueueItem, QueueSnapshot, RunView, WrapperView
from .scheduler import QueueError
from .util import isoformat

GIB = 1024**3
DEMO_START = datetime(2026, 8, 18, 9, 0, tzinfo=UTC)
DEMO_SPEEDS = (1.0, 60.0, 300.0, 1800.0)
DEMO_CYCLE_MINUTES = 90.0


@dataclass(frozen=True, slots=True)
class DemoPullRequest:
    key: str
    project: str
    repo: str
    number: int
    title: str
    author: str
    duration_minutes: float
    outcome: str = "success"


@dataclass(frozen=True, slots=True)
class DemoEvent:
    minute: float
    kind: str
    key: str | None = None


@dataclass(slots=True)
class DemoJob:
    job_id: int
    pull: DemoPullRequest
    head_sha: str
    status: str
    attempt: int
    queued_at: datetime
    quiet_until: datetime | None = None
    wrapper_id: int | None = None
    error: str | None = None


@dataclass(slots=True)
class DemoRun:
    job_id: int
    started_at: datetime
    duration_minutes: float
    stage_index: int = -1


@dataclass(slots=True)
class DemoWrapper:
    wrapper_id: int
    pull: DemoPullRequest
    path: str
    state: str
    pinned: bool
    last_used_at: datetime
    size_bytes: int
    cleanup_error: str | None = None


PULLS = {
    "decoder": DemoPullRequest(
        "decoder",
        "rocjitsu",
        "demo/rocjitsu",
        1042,
        "Tighten gfx12 decoder literal handling",
        "alice-amd",
        29,
    ),
    "trace": DemoPullRequest(
        "trace",
        "iree",
        "demo/iree",
        2871,
        "Add dispatch trace correlation IDs",
        "bob-dev",
        24,
        outcome="fail",
    ),
    "combine": DemoPullRequest(
        "combine",
        "llvm",
        "demo/llvm-project",
        220115,
        "AMDGPU combine packed conversion operands",
        "carol",
        21,
    ),
    "hotswap": DemoPullRequest(
        "hotswap",
        "rocjitsu",
        "demo/rocjitsu",
        1043,
        "Reduce HotSwap dispatch latency",
        "dmitri",
        18,
    ),
    "runtime": DemoPullRequest(
        "runtime",
        "runtime",
        "demo/rocr-runtime",
        884,
        "Preserve queue metadata across suspend",
        "alice-amd",
        26,
    ),
    "docs": DemoPullRequest(
        "docs",
        "rocjitsu",
        "demo/rocjitsu",
        1044,
        "Document supported translation targets",
        "erin",
        14,
    ),
    "tests": DemoPullRequest(
        "tests",
        "llvm",
        "demo/llvm-project",
        220116,
        "Exercise wave32 scheduling boundaries",
        "frank",
        20,
    ),
}

TRACE = (
    DemoEvent(0, "arrival", "decoder"),
    DemoEvent(1, "arrival", "trace"),
    DemoEvent(4, "arrival", "combine"),
    DemoEvent(8, "arrival", "hotswap"),
    DemoEvent(12, "push", "decoder"),
    DemoEvent(16, "arrival", "runtime"),
    DemoEvent(21, "stale-on", "runtime"),
    DemoEvent(28, "stale-off", "runtime"),
    DemoEvent(31, "arrival", "docs"),
    DemoEvent(39, "push", "combine"),
    DemoEvent(47, "arrival", "tests"),
    DemoEvent(63, "resubmission", "hotswap"),
)

STAGES = (
    (0.00, "preparing checkout"),
    (0.10, "configuring"),
    (0.22, "building"),
    (0.48, "running tests"),
    (0.62, "reviewers"),
    (0.88, "curator"),
)


class DemoScheduler:
    """Deterministic, in-memory scheduler used to exercise the production TUI."""

    is_demo = True

    def __init__(
        self,
        *,
        speed: float = 300.0,
        max_running: int = 2,
        max_wrappers: int = 3,
    ):
        if speed <= 0:
            raise ValueError("demo speed must be greater than zero")
        self.max_running = max_running
        self.max_wrappers = max_wrappers
        self._time_scale = float(speed)
        self._clock_paused = False
        self._dispatch_paused = False
        self._task: asyncio.Task[None] | None = None
        self._restart()

    def _restart(self) -> None:
        self._now = DEMO_START
        self._cycle = 0
        self._event_index = 0
        self._next_job_id = 1
        self._next_wrapper_id = 1
        self._jobs: dict[int, DemoJob] = {}
        self._runs: dict[int, DemoRun] = {}
        self._wrappers: dict[int, DemoWrapper] = {}
        self._pulls: dict[tuple[str, int], DemoPullRequest] = {}
        self._pull_versions: dict[tuple[str, int], int] = {}
        self._attempts: dict[tuple[str, int, str], int] = {}
        self._logs: dict[int, list[str]] = {}
        self._latest_job_by_wrapper: dict[int, int] = {}
        self._project_priorities = {
            "rocjitsu": 10,
            "runtime": 5,
            "llvm": 0,
            "iree": -5,
            "manual": 0,
        }
        self._author_priorities: dict[str, int] = {}
        self._pr_priorities: dict[tuple[str, int], int] = {}
        self._stale_projects: set[str] = set()
        self._ignored: set[tuple[str, int]] = set()
        self._last_poll = self._now
        self._process()

    async def start(self) -> None:
        if self._task is None:
            self._task = asyncio.create_task(self._clock_loop(), name="review-queue-demo")

    async def stop(self) -> None:
        if self._task is None:
            return
        self._task.cancel()
        with suppress(asyncio.CancelledError):
            await self._task
        self._task = None

    async def _clock_loop(self) -> None:
        loop = asyncio.get_running_loop()
        previous = loop.time()
        while True:
            await asyncio.sleep(0.1)
            current = loop.time()
            elapsed = current - previous
            previous = current
            if not self._clock_paused:
                self.advance(elapsed * self._time_scale / 60.0)

    def now(self) -> datetime:
        return self._now

    @property
    def time_scale(self) -> float:
        return self._time_scale

    @property
    def clock_paused(self) -> bool:
        return self._clock_paused

    def set_demo_speed(self, speed: float) -> float:
        if speed <= 0:
            raise ValueError("demo speed must be greater than zero")
        self._time_scale = float(speed)
        self._clock_paused = False
        return self._time_scale

    def toggle_demo_clock(self) -> bool:
        self._clock_paused = not self._clock_paused
        return self._clock_paused

    def restart_demo(self) -> None:
        dispatch_paused = self._dispatch_paused
        self._restart()
        self._dispatch_paused = dispatch_paused

    def advance(self, minutes: float) -> None:
        if minutes < 0:
            raise ValueError("demo time cannot move backwards")
        remaining = minutes
        if remaining == 0:
            self._process()
            return
        while remaining > 0:
            step = min(remaining, 0.25)
            self._now += timedelta(minutes=step)
            self._process()
            remaining -= step

    def _process(self) -> None:
        self._update_runs()
        self._emit_due_events()
        self._promote_debounced()
        self._dispatch()

    def _event_time(self, cycle: int, event: DemoEvent) -> datetime:
        return DEMO_START + timedelta(minutes=cycle * DEMO_CYCLE_MINUTES + event.minute)

    def _emit_due_events(self) -> None:
        while True:
            event = TRACE[self._event_index]
            if self._event_time(self._cycle, event) > self._now:
                return
            self._apply_event(event, self._cycle)
            self._event_index += 1
            if self._event_index == len(TRACE):
                self._event_index = 0
                self._cycle += 1

    def _materialize(self, key: str, cycle: int) -> DemoPullRequest:
        template = PULLS[key]
        return DemoPullRequest(
            key=f"{key}-{cycle}",
            project=template.project,
            repo=template.repo,
            number=template.number + cycle * 10_000,
            title=template.title,
            author=template.author,
            duration_minutes=template.duration_minutes,
            outcome=template.outcome,
        )

    def _pull_for_event(self, key: str, cycle: int) -> DemoPullRequest:
        pull = self._materialize(key, cycle)
        return self._pulls.get((pull.repo, pull.number), pull)

    def _apply_event(self, event: DemoEvent, cycle: int) -> None:
        self._last_poll = self._now
        if event.kind == "stale-on":
            if event.key is not None:
                self._stale_projects.add(PULLS[event.key].project)
            return
        if event.kind == "stale-off":
            if event.key is not None:
                self._stale_projects.discard(PULLS[event.key].project)
            return
        if event.key is None:
            return
        pull = self._pull_for_event(event.key, cycle)
        key = (pull.repo, pull.number)
        if event.kind == "arrival":
            self._pulls[key] = pull
            self._pull_versions[key] = 1
            self._enqueue(pull, source="trace arrival")
        elif (
            event.kind in {"push", "resubmission"}
            and key in self._pulls
            and key not in self._ignored
        ):
            self._pull_versions[key] += 1
            for job in self._jobs.values():
                if (
                    job.pull.repo == pull.repo
                    and job.pull.number == pull.number
                    and job.status in {"queued", "debouncing"}
                ):
                    job.status = "superseded"
            self._enqueue(
                pull,
                source=(
                    "author changes submitted; review requested again"
                    if event.kind == "resubmission"
                    else "new push"
                ),
                quiet_until=self._now + timedelta(minutes=4),
            )

    def inject_next_event(self) -> str:
        event = TRACE[self._event_index]
        self._apply_event(event, self._cycle)
        self._event_index += 1
        if self._event_index == len(TRACE):
            self._event_index = 0
            self._cycle += 1
        self._process()
        return event.kind.replace("-", " ")

    def _head(self, pull: DemoPullRequest) -> str:
        version = self._pull_versions[(pull.repo, pull.number)]
        value = f"{pull.repo}#{pull.number}:v{version}".encode()
        return hashlib.sha256(value).hexdigest()[:40]

    def _enqueue(
        self,
        pull: DemoPullRequest,
        *,
        source: str,
        quiet_until: datetime | None = None,
        head_sha: str | None = None,
    ) -> int:
        head = head_sha or self._head(pull)
        attempt_key = (pull.repo, pull.number, head)
        attempt = self._attempts.get(attempt_key, 0) + 1
        self._attempts[attempt_key] = attempt
        job_id = self._next_job_id
        self._next_job_id += 1
        job = DemoJob(
            job_id=job_id,
            pull=pull,
            head_sha=head,
            status="debouncing" if quiet_until else "queued",
            attempt=attempt,
            queued_at=self._now,
            quiet_until=quiet_until,
        )
        self._jobs[job_id] = job
        self._logs[job_id] = [
            f"[{isoformat(self._now)}] {source}: {pull.repo}#{pull.number}",
            f"[{isoformat(self._now)}] head {head[:12]} attempt {attempt}",
        ]
        return job_id

    def _promote_debounced(self) -> None:
        for job in self._jobs.values():
            if job.status == "debouncing" and job.quiet_until <= self._now:
                job.status = "queued"
                self._log(job.job_id, "push quiet period elapsed; ready for dispatch")

    def _score(self, job: DemoJob) -> int:
        pull = job.pull
        return (
            self._project_priorities.get(pull.project, 0)
            + self._author_priorities.get(pull.author, 0)
            + self._pr_priorities.get((pull.repo, pull.number), 0)
        )

    def _review_count(self, pull: DemoPullRequest) -> int:
        return sum(
            job.status == "succeeded"
            for job in self._jobs.values()
            if job.pull.repo == pull.repo and job.pull.number == pull.number
        )

    def _ready_jobs(self) -> list[DemoJob]:
        return sorted(
            (job for job in self._jobs.values() if job.status == "queued"),
            key=lambda job: (-self._score(job), job.queued_at, job.job_id),
        )

    def _dispatch(self) -> None:
        if self._dispatch_paused:
            return
        while len(self._runs) < self.max_running:
            candidate = None
            for job in self._ready_jobs():
                if job.pull.project in self._stale_projects:
                    continue
                wrapper = self._wrapper_for_pull(job.pull)
                if wrapper is None or not self._wrapper_is_active(wrapper.wrapper_id):
                    candidate = job
                    break
            if candidate is None:
                return
            wrapper = self._wrapper_for_pull(candidate.pull)
            if wrapper is None:
                wrapper = self._allocate_wrapper(candidate.pull)
            if wrapper is None:
                return
            candidate.wrapper_id = wrapper.wrapper_id
            candidate.status = "running"
            wrapper.state = "running"
            wrapper.cleanup_error = None
            wrapper.last_used_at = self._now
            self._latest_job_by_wrapper[wrapper.wrapper_id] = candidate.job_id
            duration = candidate.pull.duration_minutes * (0.72 if candidate.attempt > 1 else 1.0)
            self._runs[candidate.job_id] = DemoRun(candidate.job_id, self._now, duration)
            self._log(candidate.job_id, f"allocated {wrapper.path}")
            self._log(candidate.job_id, "review-pr.sh queue prepare")
            self._update_run_stage(candidate, self._runs[candidate.job_id], 0.0)

    def _wrapper_for_pull(self, pull: DemoPullRequest) -> DemoWrapper | None:
        return next(
            (
                wrapper
                for wrapper in self._wrappers.values()
                if wrapper.pull.repo == pull.repo and wrapper.pull.number == pull.number
            ),
            None,
        )

    def _wrapper_is_active(self, wrapper_id: int) -> bool:
        return any(self._jobs[job_id].wrapper_id == wrapper_id for job_id in self._runs)

    def _allocate_wrapper(self, pull: DemoPullRequest) -> DemoWrapper | None:
        if len(self._wrappers) >= self.max_wrappers:
            candidates = sorted(
                (
                    wrapper
                    for wrapper in self._wrappers.values()
                    if not wrapper.pinned and not self._wrapper_is_active(wrapper.wrapper_id)
                ),
                key=lambda wrapper: (
                    wrapper.state == "failed",
                    wrapper.last_used_at,
                    wrapper.wrapper_id,
                ),
            )
            if not candidates:
                return None
            victim = candidates[0]
            del self._wrappers[victim.wrapper_id]
            self._latest_job_by_wrapper.pop(victim.wrapper_id, None)
        wrapper_id = self._next_wrapper_id
        self._next_wrapper_id += 1
        wrapper = DemoWrapper(
            wrapper_id=wrapper_id,
            pull=pull,
            path=f"/demo/review-queue/{pull.project}/pr-{pull.number}-{pull.author}",
            state="idle",
            pinned=False,
            last_used_at=self._now,
            size_bytes=(48 + pull.number % 73) * GIB,
        )
        self._wrappers[wrapper_id] = wrapper
        return wrapper

    def _update_runs(self) -> None:
        for job_id, run in list(self._runs.items()):
            job = self._jobs[job_id]
            elapsed = (self._now - run.started_at).total_seconds() / 60.0
            fraction = min(1.0, elapsed / run.duration_minutes)
            self._update_run_stage(job, run, fraction)
            if job.pull.outcome == "fail" and job.attempt == 1 and fraction >= 0.54:
                self._finish(job, succeeded=False)
            elif fraction >= 1.0:
                self._finish(job, succeeded=True)

    def _update_run_stage(self, job: DemoJob, run: DemoRun, fraction: float) -> None:
        stage_index = max(
            index for index, (threshold, _name) in enumerate(STAGES) if fraction >= threshold
        )
        if stage_index == run.stage_index:
            return
        run.stage_index = stage_index
        stage = STAGES[stage_index][1]
        self._log(job.job_id, f"{stage} ({int(fraction * 100)}%)")

    def _finish(self, job: DemoJob, *, succeeded: bool) -> None:
        self._runs.pop(job.job_id, None)
        wrapper = self._wrappers[job.wrapper_id]
        wrapper.last_used_at = self._now
        if succeeded:
            job.status = "succeeded"
            wrapper.state = "complete"
            wrapper.cleanup_error = None
            self._log(job.job_id, "result: succeeded; exact head verified")
        else:
            job.status = "failed"
            job.error = "synthetic test stage failed"
            wrapper.state = "failed"
            wrapper.cleanup_error = "review failed during tests; press r to retry"
            self._log(job.job_id, "result: failed; synthetic test stage returned 1")

    def _run_status(self, run: DemoRun) -> str:
        elapsed = (self._now - run.started_at).total_seconds() / 60.0
        fraction = min(1.0, elapsed / run.duration_minutes)
        stage = STAGES[max(run.stage_index, 0)][1]
        return f"{stage} {int(fraction * 100):02d}%"

    def _log(self, job_id: int, message: str) -> None:
        self._logs.setdefault(job_id, []).append(f"[{isoformat(self._now)}] {message}")

    def _queue_item(self, job: DemoJob) -> QueueItem:
        pull = job.pull
        wrapper = self._wrapper_for_pull(pull)
        return QueueItem(
            job_id=job.job_id,
            project=pull.project,
            repo=pull.repo,
            number=pull.number,
            url=f"https://example.invalid/{pull.repo}/pull/{pull.number}",
            title=pull.title,
            author=pull.author,
            head_sha=job.head_sha,
            head_ref=f"users/{pull.author}/demo-{pull.number}",
            updated_at=isoformat(job.queued_at),
            status=job.status,
            score=self._score(job),
            project_priority=self._project_priorities.get(pull.project, 0),
            author_priority=self._author_priorities.get(pull.author, 0),
            pr_priority=self._pr_priorities.get((pull.repo, pull.number), 0),
            queued_at=isoformat(job.queued_at),
            quiet_until=isoformat(job.quiet_until) if job.quiet_until else None,
            wrapper_path=wrapper.path if wrapper else None,
            error=job.error,
            review_count=self._review_count(pull),
        )

    def snapshot(self) -> QueueSnapshot:
        waiting = sorted(
            (job for job in self._jobs.values() if job.status in {"queued", "debouncing"}),
            key=lambda job: (
                0 if job.status == "queued" else 1,
                -self._score(job),
                job.queued_at,
                job.job_id,
            ),
        )
        runs = tuple(
            RunView(
                job_id=job_id,
                project=self._jobs[job_id].pull.project,
                repo=self._jobs[job_id].pull.repo,
                number=self._jobs[job_id].pull.number,
                title=self._jobs[job_id].pull.title,
                author=self._jobs[job_id].pull.author,
                head_sha=self._jobs[job_id].head_sha,
                status=self._run_status(run),
                started_at=isoformat(run.started_at),
                wrapper_path=self._wrappers[self._jobs[job_id].wrapper_id].path,
                log_path=f"demo://job/{job_id}",
                error=None,
                review_count=self._review_count(self._jobs[job_id].pull),
            )
            for job_id, run in sorted(self._runs.items())
        )
        wrappers = tuple(
            WrapperView(
                wrapper_id=wrapper.wrapper_id,
                project=wrapper.pull.project,
                repo=wrapper.pull.repo,
                number=wrapper.pull.number,
                path=wrapper.path,
                state=wrapper.state,
                pinned=wrapper.pinned,
                last_used_at=isoformat(wrapper.last_used_at),
                size_bytes=wrapper.size_bytes,
                cleanup_error=wrapper.cleanup_error,
                title=wrapper.pull.title,
                author=wrapper.pull.author,
                review_count=self._review_count(wrapper.pull),
            )
            for wrapper in sorted(
                self._wrappers.values(),
                key=lambda value: (value.last_used_at, value.wrapper_id),
                reverse=True,
            )
        )
        project_names = sorted(self._project_priorities)
        projects = tuple(
            ProjectHealth(
                name=name,
                stale=name in self._stale_projects,
                last_poll_at=isoformat(self._last_poll),
                last_success_at=None
                if name in self._stale_projects
                else isoformat(self._last_poll),
                error="synthetic GitHub outage" if name in self._stale_projects else None,
            )
            for name in project_names
        )
        reason = None
        if self._dispatch_paused:
            reason = "dispatch paused"
        elif waiting and all(job.pull.project in self._stale_projects for job in waiting):
            reason = "waiting projects have stale synthetic GitHub state"
        elif len(self._runs) >= self.max_running:
            reason = "all review slots are occupied"
        elif (
            waiting
            and len(self._wrappers) >= self.max_wrappers
            and not any(
                not wrapper.pinned and not self._wrapper_is_active(wrapper.wrapper_id)
                for wrapper in self._wrappers.values()
            )
        ):
            reason = "wrapper cap reached; unpin an idle wrapper"
        total_size = sum(wrapper.size_bytes for wrapper in self._wrappers.values())
        return QueueSnapshot(
            paused=self._dispatch_paused,
            dispatch_blocked_reason=reason,
            max_running=self.max_running,
            max_wrappers=self.max_wrappers,
            free_bytes=max(0, 900 * GIB - total_size),
            queue=tuple(self._queue_item(job) for job in waiting),
            running=runs,
            wrappers=wrappers,
            projects=projects,
        )

    def demo_status(self) -> str:
        clock = "clock paused" if self._clock_paused else f"{self._time_scale:g}×"
        event = TRACE[self._event_index]
        wait = max(
            0,
            int((self._event_time(self._cycle, event) - self._now).total_seconds() / 60),
        )
        return (
            f"DEMO {self._now.strftime('%a %H:%M')}  •  {clock}  •  "
            f"cycle {self._cycle + 1}  •  next event {wait}m"
        )

    def log_tail(self, path: str | None, *, lines: int = 500) -> str:
        if not path or not path.startswith("demo://job/"):
            return ""
        try:
            job_id = int(path.rsplit("/", 1)[-1])
        except ValueError:
            return ""
        return "\n".join(self._logs.get(job_id, [])[-lines:])

    def wrapper_log_tail(self, wrapper_id: int, *, lines: int = 500) -> str:
        job_id = self._latest_job_by_wrapper.get(wrapper_id)
        return self.log_tail(f"demo://job/{job_id}", lines=lines) if job_id else ""

    def toggle_pause(self) -> bool:
        self._dispatch_paused = not self._dispatch_paused
        if not self._dispatch_paused:
            self._dispatch()
        return self._dispatch_paused

    def adjust_priority(self, item: QueueItem, delta: int) -> None:
        key = (item.repo, item.number)
        self._pr_priorities[key] = self._pr_priorities.get(key, 0) + delta

    def set_priorities(
        self,
        item: QueueItem,
        *,
        project_priority: int,
        author_priority: int,
        pr_priority: int,
    ) -> None:
        self._project_priorities[item.project] = project_priority
        self._author_priorities[item.author] = author_priority
        self._pr_priorities[(item.repo, item.number)] = pr_priority

    async def add_manual(self, spec: str) -> None:
        match = re.search(r"(\d+)(?:/)?$", spec)
        number = int(match.group(1)) if match else 9000 + self._next_job_id
        existing = next((pull for pull in self._pulls.values() if pull.number == number), None)
        if existing is not None:
            self._ignored.discard((existing.repo, existing.number))
            if not any(
                job.pull.repo == existing.repo
                and job.pull.number == existing.number
                and job.status in {"queued", "debouncing", "running"}
                for job in self._jobs.values()
            ):
                self._enqueue(existing, source="manual watch restored")
            self._dispatch()
            return
        repo = "demo/manual"
        while (repo, number) in self._pulls:
            number += 1
        pull = DemoPullRequest(
            key=f"manual-{number}",
            project="manual",
            repo=repo,
            number=number,
            title=f"Manually watched synthetic PR {number}",
            author="you",
            duration_minutes=16,
        )
        self._pulls[(repo, number)] = pull
        self._pull_versions[(repo, number)] = 1
        self._enqueue(pull, source="manual watch")
        self._dispatch()

    def ignore_pr(self, repo: str, number: int) -> None:
        key = (repo, number)
        if key not in self._pulls:
            raise KeyError(f"unknown pull request: {repo}#{number}")
        self._ignored.add(key)
        for job in self._jobs.values():
            if (
                job.pull.repo == repo
                and job.pull.number == number
                and job.status in {"queued", "debouncing"}
            ):
                job.status = "ineligible"
                job.error = "PR permanently excluded in demo"
                self._log(job.job_id, "permanently excluded by user")

    def request_refresh(self) -> None:
        self.inject_next_event()

    def retry(self, item: QueueItem) -> int:
        job = self._jobs[item.job_id]
        if job.status in {"queued", "debouncing"}:
            job.status = "queued"
            job.quiet_until = None
            return job.job_id
        return self._enqueue(job.pull, source="manual retry", head_sha=job.head_sha)

    def retry_wrapper(self, wrapper_id: int) -> int:
        wrapper = self._wrappers.get(wrapper_id)
        if wrapper is None:
            raise KeyError(f"unknown wrapper: {wrapper_id}")
        active = next(
            (
                job.job_id
                for job in self._jobs.values()
                if job.pull.repo == wrapper.pull.repo
                and job.pull.number == wrapper.pull.number
                and job.status in {"queued", "debouncing", "running"}
            ),
            None,
        )
        if active is not None:
            return active
        job_id = self._enqueue(
            wrapper.pull,
            source="manual retry",
            head_sha=self._head(wrapper.pull),
        )
        wrapper.state = "idle"
        wrapper.cleanup_error = None
        self._dispatch()
        return job_id

    def cancel(self, job_id: int) -> None:
        job = self._jobs.get(job_id)
        if job is None:
            raise KeyError(f"unknown job: {job_id}")
        if job.status in {"queued", "debouncing"}:
            job.status = "cancelled"
            job.error = "cancelled in demo"
            self._log(job_id, "cancelled while waiting")
            return
        if job_id not in self._runs:
            raise QueueError("demo job is no longer active")
        self._runs.pop(job_id)
        job.status = "cancelled"
        wrapper = self._wrappers[job.wrapper_id]
        wrapper.state = "idle"
        wrapper.last_used_at = self._now
        self._log(job_id, "cancelled during review")
        self._dispatch()

    def toggle_wrapper_pin(self, wrapper_id: int) -> bool:
        wrapper = self._wrappers.get(wrapper_id)
        if wrapper is None:
            raise KeyError(f"unknown wrapper: {wrapper_id}")
        wrapper.pinned = not wrapper.pinned
        return wrapper.pinned

    async def recycle_wrapper(self, wrapper_id: int) -> bool:
        wrapper = self._wrappers.get(wrapper_id)
        if wrapper is None:
            raise KeyError(f"unknown wrapper: {wrapper_id}")
        if wrapper.pinned:
            raise QueueError("wrapper is pinned")
        if self._wrapper_is_active(wrapper_id):
            raise QueueError("wrapper is active")
        del self._wrappers[wrapper_id]
        self._latest_job_by_wrapper.pop(wrapper_id, None)
        self._dispatch()
        return True
