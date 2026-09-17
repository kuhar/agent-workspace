from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path

from .models import (
    FailedReview,
    PhaseEvent,
    ProjectConfig,
    ProjectHealth,
    PullRequest,
    QueueItem,
    RunView,
    WrapperView,
)
from .phases import PhaseChange
from .util import isoformat, parse_time, utc_now

SCHEMA = """
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS settings (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS projects (
    name TEXT PRIMARY KEY,
    repo TEXT NOT NULL UNIQUE,
    bootstrap_complete INTEGER NOT NULL DEFAULT 0,
    stale_error TEXT,
    last_poll_at TEXT,
    last_success_at TEXT
);

CREATE TABLE IF NOT EXISTS project_priorities (
    project TEXT PRIMARY KEY REFERENCES projects(name) ON DELETE CASCADE,
    priority INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS authors (
    login TEXT PRIMARY KEY,
    priority INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS pull_requests (
    repo TEXT NOT NULL,
    number INTEGER NOT NULL,
    project TEXT NOT NULL REFERENCES projects(name) ON DELETE CASCADE,
    url TEXT NOT NULL,
    title TEXT NOT NULL,
    author TEXT NOT NULL REFERENCES authors(login),
    head_sha TEXT NOT NULL,
    head_ref TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    state TEXT NOT NULL,
    is_draft INTEGER NOT NULL DEFAULT 0,
    eligible INTEGER NOT NULL DEFAULT 1,
    manual_watch INTEGER NOT NULL DEFAULT 0,
    ignored INTEGER NOT NULL DEFAULT 0,
    pr_priority INTEGER NOT NULL DEFAULT 0,
    discovered_at TEXT NOT NULL,
    last_seen_at TEXT NOT NULL,
    PRIMARY KEY (repo, number)
);

CREATE TABLE IF NOT EXISTS wrappers (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    project TEXT NOT NULL REFERENCES projects(name),
    repo TEXT NOT NULL,
    pr_number INTEGER NOT NULL,
    path TEXT NOT NULL UNIQUE,
    owner_token TEXT NOT NULL,
    state TEXT NOT NULL,
    pinned INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    last_used_at TEXT NOT NULL,
    size_bytes INTEGER NOT NULL DEFAULT 0,
    cleanup_error TEXT,
    head_ref TEXT NOT NULL,
    UNIQUE (repo, pr_number),
    FOREIGN KEY (repo, pr_number) REFERENCES pull_requests(repo, number)
);

CREATE TABLE IF NOT EXISTS jobs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    project TEXT NOT NULL REFERENCES projects(name),
    repo TEXT NOT NULL,
    pr_number INTEGER NOT NULL,
    head_sha TEXT NOT NULL,
    status TEXT NOT NULL,
    source TEXT NOT NULL,
    attempt INTEGER NOT NULL,
    queued_at TEXT NOT NULL,
    quiet_until TEXT,
    started_at TEXT,
    finished_at TEXT,
    wrapper_id INTEGER REFERENCES wrappers(id),
    supervisor_pid INTEGER,
    supervisor_start_ticks INTEGER,
    exit_code INTEGER,
    error TEXT,
    log_path TEXT,
    result_path TEXT,
    UNIQUE (repo, pr_number, head_sha, attempt),
    FOREIGN KEY (repo, pr_number) REFERENCES pull_requests(repo, number)
);

CREATE INDEX IF NOT EXISTS jobs_status_idx ON jobs(status, queued_at);
CREATE INDEX IF NOT EXISTS jobs_pr_idx ON jobs(repo, pr_number, head_sha);
CREATE INDEX IF NOT EXISTS wrappers_lru_idx ON wrappers(state, pinned, last_used_at);

CREATE TABLE IF NOT EXISTS job_phase_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id INTEGER NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,
    phase TEXT NOT NULL,
    started_at TEXT NOT NULL
);
"""


@dataclass(frozen=True, slots=True)
class PollOutcome:
    discovered: int = 0
    changed_heads: int = 0
    queued: int = 0
    left_query: int = 0


class Database:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.path = path
        self.connection = sqlite3.connect(path, timeout=30)
        self.connection.row_factory = sqlite3.Row
        self.connection.execute("PRAGMA journal_mode = WAL")
        self.connection.execute("PRAGMA synchronous = NORMAL")
        self.connection.executescript(SCHEMA)
        columns = {
            row["name"]
            for row in self.connection.execute("PRAGMA table_info(pull_requests)").fetchall()
        }
        if "ignored" not in columns:
            self.connection.execute(
                "ALTER TABLE pull_requests ADD COLUMN ignored INTEGER NOT NULL DEFAULT 0"
            )
        self.connection.commit()
        for name, declaration in (
            ("path_filter_key", "TEXT NOT NULL DEFAULT ''"),
            ("path_filter_passed", "INTEGER NOT NULL DEFAULT 0"),
            ("approval_viewer", "TEXT NOT NULL DEFAULT ''"),
            ("approved_by", "TEXT NOT NULL DEFAULT '[]'"),
        ):
            if name not in columns:
                self.connection.execute(
                    f"ALTER TABLE pull_requests ADD COLUMN {name} {declaration}"
                )
        self.connection.commit()
        self._path_filters: dict[str, str] = {}
        job_columns = {row["name"] for row in self.connection.execute("PRAGMA table_info(jobs)")}
        for name, declaration in (
            ("phase", "TEXT NOT NULL DEFAULT ''"),
            ("phase_started_at", "TEXT"),
            ("phase_log_offset", "INTEGER NOT NULL DEFAULT 0"),
            ("resume_on_restart", "INTEGER NOT NULL DEFAULT 0"),
        ):
            if name not in job_columns:
                self.connection.execute(f"ALTER TABLE jobs ADD COLUMN {name} {declaration}")
        self.connection.commit()

    def update_phase(self, job_id: int, change: PhaseChange) -> None:
        with self.connection:
            self.connection.execute(
                "UPDATE jobs SET phase = ?, phase_started_at = ?, phase_log_offset = ? "
                "WHERE id = ?",
                (change.phase, change.started_at, change.log_offset, job_id),
            )
            if change.entered:
                self.connection.execute(
                    "INSERT INTO job_phase_events(job_id, phase, started_at) VALUES (?, ?, ?)",
                    (job_id, change.phase, change.started_at),
                )

    def phase_events(self) -> tuple[PhaseEvent, ...]:
        rows = self.connection.execute(
            "SELECT e.id, j.project, j.pr_number, e.phase, e.started_at "
            "FROM job_phase_events e JOIN jobs j ON j.id = e.job_id "
            "ORDER BY e.id DESC LIMIT 50"
        ).fetchall()
        return tuple(
            PhaseEvent(row["id"], row["project"], row["pr_number"], row["phase"], row["started_at"])
            for row in reversed(rows)
        )

    def close(self) -> None:
        self.connection.close()

    def seed(
        self,
        projects: Iterable[ProjectConfig],
        *,
        start_paused: bool,
        start_mode: str | None = None,
    ) -> None:
        with self.connection:
            existing_mode = self.connection.execute(
                "SELECT 1 FROM settings WHERE key = 'paused'"
            ).fetchone()
            self.connection.execute(
                "INSERT OR IGNORE INTO settings(key, value) VALUES ('paused', ?)",
                ("1" if (start_mode == "paused" if start_mode else start_paused) else "0",),
            )
            self.connection.execute(
                "INSERT OR IGNORE INTO settings(key, value) VALUES ('manual_only', ?)",
                ("1" if start_mode == "manual" and existing_mode is None else "0",),
            )
            for project in projects:
                self._path_filters[project.name] = project.path_filter_key
                if project.include_paths:
                    self.connection.execute(
                        "UPDATE pull_requests SET eligible = 0, path_filter_passed = 0 "
                        "WHERE project = ? AND path_filter_key != ?",
                        (project.name, project.path_filter_key),
                    )
                existing = self.connection.execute(
                    "SELECT repo FROM projects WHERE name = ?", (project.name,)
                ).fetchone()
                if existing is not None and existing["repo"] != project.repo:
                    raise ValueError(
                        f"project {project.name} repository changed from "
                        f"{existing['repo']} to {project.repo}; use a new project name "
                        "or reset the state database"
                    )
                self.connection.execute(
                    "INSERT OR IGNORE INTO projects(name, repo) VALUES (?, ?)",
                    (project.name, project.repo),
                )
                self.connection.execute(
                    "INSERT OR IGNORE INTO project_priorities(project, priority) VALUES (?, ?)",
                    (project.name, project.priority),
                )

    def paused(self) -> bool:
        row = self.connection.execute("SELECT value FROM settings WHERE key = 'paused'").fetchone()
        return row is not None and row["value"] == "1"

    def set_paused(self, paused: bool) -> None:
        with self.connection:
            self.connection.execute(
                "INSERT INTO settings(key, value) VALUES ('paused', ?) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                ("1" if paused else "0",),
            )

    def manual_only(self) -> bool:
        row = self.connection.execute(
            "SELECT value FROM settings WHERE key = 'manual_only'"
        ).fetchone()
        return row is not None and row["value"] == "1"

    def mode(self) -> str:
        return "paused" if self.paused() else "manual" if self.manual_only() else "active"

    def set_mode(self, mode: str) -> None:
        if mode not in {"active", "manual", "paused"}:
            raise ValueError("mode must be active, manual, or paused")
        with self.connection:
            self.connection.execute(
                "INSERT INTO settings(key, value) VALUES ('paused', ?) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                ("1" if mode == "paused" else "0",),
            )
            if mode != "paused":
                self.connection.execute(
                    "INSERT INTO settings(key, value) VALUES ('manual_only', ?) "
                    "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                    ("1" if mode == "manual" else "0",),
                )

    def mark_poll_failure(self, project: str, error: str) -> None:
        with self.connection:
            self.connection.execute(
                "UPDATE projects SET stale_error = ?, last_poll_at = ? WHERE name = ?",
                (error, isoformat(), project),
            )

    def project_health(self) -> tuple[ProjectHealth, ...]:
        rows = self.connection.execute(
            "SELECT name, stale_error, last_poll_at, last_success_at FROM projects ORDER BY name"
        ).fetchall()
        return tuple(
            ProjectHealth(
                name=row["name"],
                stale=row["stale_error"] is not None,
                last_poll_at=row["last_poll_at"],
                last_success_at=row["last_success_at"],
                error=row["stale_error"],
            )
            for row in rows
        )

    def any_stale(self) -> bool:
        row = self.connection.execute(
            "SELECT 1 FROM projects WHERE stale_error IS NOT NULL LIMIT 1"
        ).fetchone()
        return row is not None

    def stale_projects(self) -> frozenset[str]:
        rows = self.connection.execute(
            "SELECT name FROM projects WHERE stale_error IS NOT NULL"
        ).fetchall()
        return frozenset(str(row["name"]) for row in rows)

    def manual_watches(self, project: str) -> tuple[int, ...]:
        rows = self.connection.execute(
            "SELECT number FROM pull_requests WHERE project = ? AND manual_watch = 1",
            (project,),
        ).fetchall()
        return tuple(int(row["number"]) for row in rows)

    def poll_followups(self, project: str) -> tuple[int, ...]:
        rows = self.connection.execute(
            "SELECT number FROM pull_requests "
            "WHERE project = ? AND (eligible = 1 OR manual_watch = 1 OR EXISTS ("
            "SELECT 1 FROM wrappers w WHERE w.repo = pull_requests.repo "
            "AND w.pr_number = pull_requests.number))",
            (project,),
        ).fetchall()
        return tuple(int(row["number"]) for row in rows)

    def apply_poll(
        self,
        project: ProjectConfig,
        records: Iterable[PullRequest],
        *,
        query_numbers: set[int],
        bootstrap_hours: int,
        push_quiet_seconds: int,
    ) -> PollOutcome:
        now = utc_now()
        now_text = isoformat(now)
        cutoff = now - timedelta(hours=bootstrap_hours)
        project_row = self.connection.execute(
            "SELECT bootstrap_complete FROM projects WHERE name = ?", (project.name,)
        ).fetchone()
        if project_row is None:
            raise KeyError(f"unknown project: {project.name}")
        bootstrapped = bool(project_row["bootstrap_complete"])
        seen: set[int] = set()
        discovered = changed = queued = left = 0

        with self.connection:
            for record in records:
                seen.add(record.number)
                self.connection.execute(
                    "INSERT OR IGNORE INTO authors(login, priority) VALUES (?, 0)",
                    (record.author,),
                )
                existing = self.connection.execute(
                    "SELECT head_sha, manual_watch, ignored, eligible FROM pull_requests "
                    "WHERE repo = ? AND number = ?",
                    (record.repo, record.number),
                ).fetchone()
                is_open = record.state.upper() == "OPEN" and not record.is_draft
                manual_watch = bool(existing and existing["manual_watch"])
                keep_manual_watch = manual_watch and record.state.upper() == "OPEN"
                ignored = bool(existing and existing["ignored"])
                paths_match = not project.include_paths or (
                    record.path_filter_key == project.path_filter_key and record.path_filter_passed
                )
                eligible = (
                    is_open
                    and not ignored
                    and paths_match
                    and (record.number in query_numbers or manual_watch)
                )

                if existing is None:
                    discovered += 1
                    self.connection.execute(
                        """
                        INSERT INTO pull_requests(
                            repo, number, project, url, title, author, head_sha, head_ref,
                            updated_at, state, is_draft, eligible, manual_watch,
                            discovered_at, last_seen_at, path_filter_key, path_filter_passed
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0, ?, ?, ?, ?)
                        """,
                        (
                            record.repo,
                            record.number,
                            project.name,
                            record.url,
                            record.title,
                            record.author,
                            record.head_sha,
                            record.head_ref,
                            record.updated_at,
                            record.state.upper(),
                            int(record.is_draft),
                            int(eligible),
                            now_text,
                            now_text,
                            record.path_filter_key,
                            int(record.path_filter_passed),
                        ),
                    )
                    should_queue = eligible and (
                        bootstrapped or parse_time(record.updated_at) >= cutoff
                    )
                    if should_queue:
                        queued += int(
                            self._enqueue_locked(
                                record.repo,
                                record.number,
                                record.head_sha,
                                project.name,
                                source="automatic",
                                quiet_until=self._quiet_until(
                                    record.updated_at, now_text, push_quiet_seconds
                                ),
                                manual=False,
                            )
                        )
                else:
                    head_changed = existing["head_sha"] != record.head_sha
                    became_eligible = eligible and not bool(existing["eligible"])
                    became_ineligible = not eligible and bool(existing["eligible"])
                    changed += int(head_changed)
                    left += int(became_ineligible)
                    self.connection.execute(
                        """
                        UPDATE pull_requests
                        SET url = ?, title = ?, author = ?, head_sha = ?, head_ref = ?,
                            updated_at = ?, state = ?, is_draft = ?, eligible = ?,
                            manual_watch = ?, last_seen_at = ?,
                            path_filter_key = ?, path_filter_passed = ?
                        WHERE repo = ? AND number = ?
                        """,
                        (
                            record.url,
                            record.title,
                            record.author,
                            record.head_sha,
                            record.head_ref,
                            record.updated_at,
                            record.state.upper(),
                            int(record.is_draft),
                            int(eligible),
                            int(keep_manual_watch),
                            now_text,
                            record.path_filter_key,
                            int(record.path_filter_passed),
                            record.repo,
                            record.number,
                        ),
                    )
                    if not eligible:
                        if record.state.upper() == "MERGED":
                            reason = "PR merged"
                        elif record.state.upper() != "OPEN":
                            reason = "PR closed"
                        elif record.is_draft:
                            reason = "PR became draft"
                        elif not paths_match:
                            reason = "PR does not touch configured include_paths"
                        else:
                            reason = "PR left configured query"
                        self.connection.execute(
                            """
                            UPDATE jobs SET status = 'ineligible', finished_at = ?, error = ?
                            WHERE repo = ? AND pr_number = ?
                              AND status IN ('debouncing', 'queued')
                            """,
                            (now_text, reason, record.repo, record.number),
                        )
                    if head_changed:
                        self.connection.execute(
                            """
                            UPDATE jobs SET status = 'superseded', finished_at = ?,
                                error = 'newer PR head observed'
                            WHERE repo = ? AND pr_number = ?
                              AND head_sha != ? AND status IN ('debouncing', 'queued')
                            """,
                            (now_text, record.repo, record.number, record.head_sha),
                        )
                        if eligible:
                            queued += int(
                                self._enqueue_locked(
                                    record.repo,
                                    record.number,
                                    record.head_sha,
                                    project.name,
                                    source="automatic",
                                    quiet_until=self._quiet_until(
                                        record.updated_at, now_text, push_quiet_seconds
                                    ),
                                    manual=False,
                                )
                            )
                    elif became_eligible:
                        queued += int(
                            self._enqueue_locked(
                                record.repo,
                                record.number,
                                record.head_sha,
                                project.name,
                                source="eligibility-restored",
                                quiet_until=None,
                                manual=True,
                            )
                        )

                self._store_approvals(record)

            missing_rows = self.connection.execute(
                """
                SELECT repo, number FROM pull_requests
                WHERE project = ? AND manual_watch = 0 AND eligible = 1
                """,
                (project.name,),
            ).fetchall()
            missing = [row for row in missing_rows if int(row["number"]) not in seen]
            for row in missing:
                self.connection.execute(
                    "UPDATE pull_requests SET eligible = 0 WHERE repo = ? AND number = ?",
                    (row["repo"], row["number"]),
                )
                self.connection.execute(
                    """
                    UPDATE jobs SET status = 'ineligible', finished_at = ?,
                        error = 'PR left configured query'
                    WHERE repo = ? AND pr_number = ?
                      AND status IN ('debouncing', 'queued')
                    """,
                    (now_text, row["repo"], row["number"]),
                )

            self.connection.execute(
                """
                UPDATE projects
                SET bootstrap_complete = 1, stale_error = NULL,
                    last_poll_at = ?, last_success_at = ?
                WHERE name = ?
                """,
                (now_text, now_text, project.name),
            )
        return PollOutcome(discovered, changed, queued, left + len(missing))

    @staticmethod
    def _quiet_until(updated_at: str, now_text: str, quiet_seconds: int) -> str | None:
        if quiet_seconds == 0:
            return None
        candidate = parse_time(updated_at) + timedelta(seconds=quiet_seconds)
        if candidate <= parse_time(now_text):
            return None
        return isoformat(candidate)

    def _enqueue_locked(
        self,
        repo: str,
        number: int,
        head_sha: str,
        project: str,
        *,
        source: str,
        quiet_until: str | None,
        manual: bool,
    ) -> bool:
        active = self.connection.execute(
            """
            SELECT id FROM jobs
            WHERE repo = ? AND pr_number = ? AND head_sha = ?
              AND status IN ('debouncing', 'queued', 'preparing', 'running', 'cancelling')
            LIMIT 1
            """,
            (repo, number, head_sha),
        ).fetchone()
        if active is not None:
            if source in {"manual", "manual-watch"}:
                self._mark_manually_enqueued(int(active["id"]), source)
            return False
        previous = self.connection.execute(
            "SELECT COALESCE(MAX(attempt), 0) AS attempt FROM jobs "
            "WHERE repo = ? AND pr_number = ? AND head_sha = ?",
            (repo, number, head_sha),
        ).fetchone()
        attempt = int(previous["attempt"])
        if attempt and not manual:
            return False
        attempt += 1
        status = "debouncing" if quiet_until else "queued"
        self.connection.execute(
            """
            INSERT INTO jobs(project, repo, pr_number, head_sha, status, source,
                             attempt, queued_at, quiet_until)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (project, repo, number, head_sha, status, source, attempt, isoformat(), quiet_until),
        )
        return True

    def _store_approvals(self, record: PullRequest) -> None:
        self.connection.execute(
            "UPDATE pull_requests SET approval_viewer = ?, approved_by = ? "
            "WHERE repo = ? AND number = ?",
            (record.approval_viewer, json.dumps(record.approved_by), record.repo, record.number),
        )

    def _mark_manually_enqueued(self, job_id: int, source: str = "manual") -> None:
        self.connection.execute(
            "UPDATE jobs SET source = ?, status = 'queued', quiet_until = NULL "
            "WHERE id = ? AND status IN ('queued', 'debouncing')",
            (source, job_id),
        )

    def enqueue_current(self, repo: str, number: int, *, manual: bool = True) -> int:
        row = self.connection.execute(
            "SELECT project, head_sha, state, is_draft, path_filter_key, path_filter_passed "
            "FROM pull_requests "
            "WHERE repo = ? AND number = ?",
            (repo, number),
        ).fetchone()
        if row is None:
            raise KeyError(f"unknown pull request: {repo}#{number}")
        if row["state"] != "OPEN" or bool(row["is_draft"]):
            raise ValueError(f"pull request is not open for review: {repo}#{number}")
        required_paths = self._path_filters.get(row["project"], "")
        if required_paths and (
            row["path_filter_key"] != required_paths or not row["path_filter_passed"]
        ):
            raise ValueError(f"PR does not touch configured include_paths: {repo}#{number}")
        with self.connection:
            active = self.connection.execute(
                """
                SELECT id FROM jobs
                WHERE repo = ? AND pr_number = ? AND head_sha = ?
                  AND status IN ('debouncing', 'queued', 'preparing', 'running', 'cancelling')
                ORDER BY id DESC LIMIT 1
                """,
                (repo, number, row["head_sha"]),
            ).fetchone()
            if active is not None:
                if manual:
                    self._mark_manually_enqueued(int(active["id"]))
                return int(active["id"])
            self.connection.execute(
                "UPDATE pull_requests SET eligible = 1, ignored = 0 WHERE repo = ? AND number = ?",
                (repo, number),
            )
            self._enqueue_locked(
                repo,
                number,
                row["head_sha"],
                row["project"],
                source="manual",
                quiet_until=None,
                manual=manual,
            )
            job = self.connection.execute("SELECT last_insert_rowid() AS id").fetchone()
        return int(job["id"])

    def enqueue_wrapper_pr(self, wrapper_id: int) -> int:
        row = self.connection.execute(
            "SELECT repo, pr_number FROM wrappers WHERE id = ?", (wrapper_id,)
        ).fetchone()
        if row is None:
            raise KeyError(f"unknown wrapper: {wrapper_id}")
        return self.enqueue_current(row["repo"], int(row["pr_number"]), manual=True)

    def set_manual_watch(self, record: PullRequest, *, enqueue: bool = True) -> None:
        if record.state.upper() != "OPEN" or record.is_draft:
            raise ValueError(
                f"manual pull request must be open and non-draft: {record.repo}#{record.number}"
            )
        required_paths = self._path_filters.get(record.project, "")
        if required_paths and (
            record.path_filter_key != required_paths or not record.path_filter_passed
        ):
            raise ValueError(
                f"PR does not touch configured include_paths: {record.repo}#{record.number}"
            )
        now = isoformat()
        with self.connection:
            self.connection.execute(
                "INSERT OR IGNORE INTO authors(login, priority) VALUES (?, 0)",
                (record.author,),
            )
            self.connection.execute(
                """
                INSERT INTO pull_requests(
                    repo, number, project, url, title, author, head_sha, head_ref,
                    updated_at, state, is_draft, eligible, manual_watch, ignored,
                    discovered_at, last_seen_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1, 1, 0, ?, ?)
                ON CONFLICT(repo, number) DO UPDATE SET
                    url = excluded.url, title = excluded.title, author = excluded.author,
                    head_sha = excluded.head_sha, head_ref = excluded.head_ref,
                    updated_at = excluded.updated_at, state = excluded.state,
                    is_draft = excluded.is_draft, eligible = 1, manual_watch = 1,
                    ignored = 0,
                    last_seen_at = excluded.last_seen_at
                """,
                (
                    record.repo,
                    record.number,
                    record.project,
                    record.url,
                    record.title,
                    record.author,
                    record.head_sha,
                    record.head_ref,
                    record.updated_at,
                    record.state.upper(),
                    int(record.is_draft),
                    now,
                    now,
                ),
            )
            self.connection.execute(
                "UPDATE pull_requests SET path_filter_key = ?, path_filter_passed = ? "
                "WHERE repo = ? AND number = ?",
                (
                    record.path_filter_key,
                    int(record.path_filter_passed),
                    record.repo,
                    record.number,
                ),
            )
            self._store_approvals(record)
            if enqueue:
                self.connection.execute(
                    """
                    UPDATE jobs SET status = 'superseded', finished_at = ?,
                        error = 'newer PR head observed during manual inclusion'
                    WHERE repo = ? AND pr_number = ? AND head_sha != ?
                      AND status IN ('debouncing', 'queued')
                    """,
                    (now, record.repo, record.number, record.head_sha),
                )
                self._enqueue_locked(
                    record.repo,
                    record.number,
                    record.head_sha,
                    record.project,
                    source="manual-watch",
                    quiet_until=None,
                    manual=True,
                )

    def ignore_pr(self, repo: str, number: int) -> None:
        now = isoformat()
        with self.connection:
            cursor = self.connection.execute(
                "UPDATE pull_requests "
                "SET eligible = 0, manual_watch = 0, ignored = 1 "
                "WHERE repo = ? AND number = ?",
                (repo, number),
            )
            if not cursor.rowcount:
                raise KeyError(f"unknown pull request: {repo}#{number}")
            self.connection.execute(
                """
                UPDATE jobs SET status = 'ineligible', finished_at = ?,
                    error = 'PR permanently excluded by user'
                WHERE repo = ? AND pr_number = ?
                  AND status IN ('debouncing', 'queued')
                """,
                (now, repo, number),
            )

    def promote_debounced(self) -> int:
        now = isoformat()
        with self.connection:
            cursor = self.connection.execute(
                """
                UPDATE jobs SET status = 'queued'
                WHERE status = 'debouncing' AND quiet_until <= ?
                """,
                (now,),
            )
        return cursor.rowcount

    def _queue_rows(self, *, only_ready: bool = False, limit: int | None = None):
        if only_ready:
            where = """
                j.status = 'queued'
                AND NOT EXISTS (
                    SELECT 1 FROM jobs active
                    WHERE active.repo = j.repo
                      AND active.pr_number = j.pr_number
                      AND active.status IN ('preparing', 'running', 'cancelling')
                )
            """
            if self.manual_only():
                where += " AND j.source IN ('manual', 'manual-watch')"
        else:
            where = "j.status IN ('queued', 'debouncing')"
        sql = f"""
            SELECT j.id AS job_id, j.project, j.repo, j.pr_number AS number,
                   pr.url, pr.title, pr.author, j.head_sha, pr.head_ref, pr.updated_at,
                   pr.approval_viewer, pr.approved_by,
                   j.status, j.queued_at, j.quiet_until, j.error, j.source,
                   COALESCE(pp.priority, 0) AS project_priority,
                   COALESCE(a.priority, 0) AS author_priority,
                   pr.pr_priority,
                   COALESCE(pp.priority, 0) + COALESCE(a.priority, 0) + pr.pr_priority AS score,
                   w.path AS wrapper_path,
                   (SELECT COUNT(*) FROM jobs reviewed
                    WHERE reviewed.repo = j.repo
                      AND reviewed.pr_number = j.pr_number
                      AND reviewed.status = 'succeeded') AS review_count
            FROM jobs j
            JOIN pull_requests pr ON pr.repo = j.repo AND pr.number = j.pr_number
            LEFT JOIN project_priorities pp ON pp.project = j.project
            LEFT JOIN authors a ON a.login = pr.author
            LEFT JOIN wrappers w ON w.repo = j.repo AND w.pr_number = j.pr_number
            WHERE {where} AND pr.eligible = 1
            ORDER BY CASE j.status WHEN 'queued' THEN 0 ELSE 1 END,
                     score DESC, j.queued_at ASC, j.repo ASC, j.pr_number ASC
        """
        params: tuple[object, ...] = ()
        if limit is not None:
            sql += " LIMIT ?"
            params = (limit,)
        return self.connection.execute(sql, params).fetchall()

    @staticmethod
    def _queue_item(row: sqlite3.Row) -> QueueItem:
        return QueueItem(
            job_id=row["job_id"],
            project=row["project"],
            repo=row["repo"],
            number=row["number"],
            url=row["url"],
            title=row["title"],
            author=row["author"],
            head_sha=row["head_sha"],
            head_ref=row["head_ref"],
            updated_at=row["updated_at"],
            status=row["status"],
            score=row["score"],
            project_priority=row["project_priority"],
            author_priority=row["author_priority"],
            pr_priority=row["pr_priority"],
            queued_at=row["queued_at"],
            quiet_until=row["quiet_until"],
            wrapper_path=row["wrapper_path"],
            error=row["error"],
            review_count=row["review_count"],
            manual_enqueued=row["source"] in {"manual", "manual-watch"},
            approval_viewer=row["approval_viewer"],
            approved_by=tuple(json.loads(row["approved_by"])),
        )

    def queue_items(self) -> tuple[QueueItem, ...]:
        return tuple(self._queue_item(row) for row in self._queue_rows())

    def next_ready(self, *, excluded_projects: frozenset[str] = frozenset()) -> QueueItem | None:
        rows = self._queue_rows(only_ready=True)
        row = next((item for item in rows if item["project"] not in excluded_projects), None)
        return self._queue_item(row) if row else None

    def running(self) -> tuple[RunView, ...]:
        rows = self.connection.execute(
            """
            SELECT j.id AS job_id, j.project, j.repo, j.pr_number AS number,
                   pr.title, pr.author, j.head_sha, j.status, j.started_at,
                   w.path AS wrapper_path, j.log_path, j.error, j.phase, j.phase_started_at,
                   (SELECT COUNT(*) FROM jobs reviewed
                    WHERE reviewed.repo = j.repo
                      AND reviewed.pr_number = j.pr_number
                      AND reviewed.status = 'succeeded') AS review_count
            FROM jobs j
            JOIN pull_requests pr ON pr.repo = j.repo AND pr.number = j.pr_number
            LEFT JOIN wrappers w ON w.id = j.wrapper_id
            WHERE j.status IN ('preparing', 'running', 'cancelling')
            ORDER BY j.started_at, j.id
            """
        ).fetchall()
        return tuple(
            RunView(
                job_id=row["job_id"],
                project=row["project"],
                repo=row["repo"],
                number=row["number"],
                title=row["title"],
                author=row["author"],
                head_sha=row["head_sha"],
                status=row["status"],
                started_at=row["started_at"],
                wrapper_path=row["wrapper_path"],
                log_path=row["log_path"],
                error=row["error"],
                review_count=row["review_count"],
                phase=row["phase"],
                phase_started_at=row["phase_started_at"],
            )
            for row in rows
        )

    def wrappers(self) -> tuple[WrapperView, ...]:
        rows = self.connection.execute(
            """
            SELECT w.id, w.project, w.repo, w.pr_number, w.path,
                   CASE latest.status
                       WHEN 'succeeded' THEN 'complete'
                       WHEN 'failed' THEN 'failed'
                       WHEN 'ineligible' THEN 'ineligible'
                       WHEN 'interrupted' THEN 'interrupted'
                       ELSE w.state
                   END AS display_state,
                   w.pinned, w.last_used_at, w.size_bytes,
                   COALESCE(w.cleanup_error, latest.error) AS display_error,
                   pr.title, pr.author, pr.approval_viewer, pr.approved_by,
                   (SELECT COUNT(*) FROM jobs reviewed
                    WHERE reviewed.repo = w.repo
                      AND reviewed.pr_number = w.pr_number
                      AND reviewed.status = 'succeeded') AS review_count
            FROM wrappers w
            JOIN pull_requests pr ON pr.repo = w.repo AND pr.number = w.pr_number
            LEFT JOIN jobs latest ON latest.id = (
                SELECT MAX(candidate.id) FROM jobs candidate
                WHERE candidate.wrapper_id = w.id
            )
            ORDER BY w.last_used_at DESC, w.id DESC
            """
        ).fetchall()
        return tuple(
            WrapperView(
                wrapper_id=row["id"],
                project=row["project"],
                repo=row["repo"],
                number=row["pr_number"],
                path=row["path"],
                state=row["display_state"],
                pinned=bool(row["pinned"]),
                last_used_at=row["last_used_at"],
                size_bytes=row["size_bytes"],
                cleanup_error=row["display_error"],
                title=row["title"],
                author=row["author"],
                review_count=row["review_count"],
                approval_viewer=row["approval_viewer"],
                approved_by=tuple(json.loads(row["approved_by"])),
            )
            for row in rows
        )

    def wrapper_for_pr(self, repo: str, number: int) -> sqlite3.Row | None:
        return self.connection.execute(
            "SELECT * FROM wrappers WHERE repo = ? AND pr_number = ?", (repo, number)
        ).fetchone()

    def wrapper_by_id(self, wrapper_id: int) -> sqlite3.Row | None:
        return self.connection.execute(
            "SELECT * FROM wrappers WHERE id = ?", (wrapper_id,)
        ).fetchone()

    def recent_failures(self) -> tuple[FailedReview, ...]:
        rows = self.connection.execute(
            """
            SELECT id, project, pr_number, finished_at, error, log_path
            FROM jobs WHERE status = 'failed'
            ORDER BY finished_at DESC, id DESC LIMIT 50
            """
        ).fetchall()
        return tuple(
            FailedReview(
                job_id=row["id"],
                project=row["project"],
                number=row["pr_number"],
                finished_at=row["finished_at"],
                error=row["error"] or "review failed",
                log_path=row["log_path"],
            )
            for row in rows
        )

    def wrapper_candidates(self, *, failed_only: bool = False) -> tuple[sqlite3.Row, ...]:
        rows = self.connection.execute(
            """
            SELECT w.*, pr.eligible, pr.state AS pr_state
            FROM wrappers w
            JOIN pull_requests pr ON pr.repo = w.repo AND pr.number = w.pr_number
            WHERE w.pinned = 0
              AND w.state NOT IN ('preparing', 'running', 'cleaning')
              AND NOT EXISTS (
                  SELECT 1 FROM jobs j WHERE j.wrapper_id = w.id
                    AND j.status IN ('debouncing', 'queued', 'preparing', 'running', 'cancelling')
              )
              AND (NOT ? OR (
                  (SELECT status FROM jobs WHERE wrapper_id = w.id ORDER BY id DESC LIMIT 1)
                      = 'failed'
                  AND NOT EXISTS (
                      SELECT 1 FROM jobs j WHERE j.repo = w.repo AND j.pr_number = w.pr_number
                        AND j.status IN (
                            'debouncing', 'queued', 'preparing', 'running', 'cancelling'
                        )
                  )
              ))
            ORDER BY CASE
                         WHEN pr.state IN ('CLOSED', 'MERGED') THEN 0
                         WHEN pr.eligible = 0 THEN 1
                         ELSE 2
                     END,
                     w.last_used_at ASC, w.id ASC
            """,
            (failed_only,),
        ).fetchall()
        return tuple(rows)

    def create_wrapper(
        self,
        *,
        project: str,
        repo: str,
        number: int,
        path: Path,
        token: str,
        head_ref: str,
    ) -> int:
        now = isoformat()
        with self.connection:
            cursor = self.connection.execute(
                """
                INSERT INTO wrappers(project, repo, pr_number, path, owner_token,
                                     state, created_at, last_used_at, head_ref)
                VALUES (?, ?, ?, ?, ?, 'allocating', ?, ?, ?)
                """,
                (project, repo, number, str(path), token, now, now, head_ref),
            )
        return int(cursor.lastrowid)

    def remove_wrapper(self, wrapper_id: int) -> None:
        with self.connection:
            self.connection.execute(
                "UPDATE jobs SET wrapper_id = NULL WHERE wrapper_id = ?", (wrapper_id,)
            )
            self.connection.execute("DELETE FROM wrappers WHERE id = ?", (wrapper_id,))

    def update_wrapper(
        self,
        wrapper_id: int,
        *,
        state: str | None = None,
        size_bytes: int | None = None,
        cleanup_error: str | None = None,
        touch: bool = False,
    ) -> None:
        assignments: list[str] = []
        values: list[object] = []
        if state is not None:
            assignments.append("state = ?")
            values.append(state)
        if size_bytes is not None:
            assignments.append("size_bytes = ?")
            values.append(size_bytes)
        if cleanup_error is not None or state in {"idle", "preparing", "running", "cleaning"}:
            assignments.append("cleanup_error = ?")
            values.append(cleanup_error)
        if touch:
            assignments.append("last_used_at = ?")
            values.append(isoformat())
        if not assignments:
            return
        values.append(wrapper_id)
        with self.connection:
            self.connection.execute(
                f"UPDATE wrappers SET {', '.join(assignments)} WHERE id = ?", values
            )

    def set_wrapper_pinned(self, wrapper_id: int, pinned: bool) -> None:
        with self.connection:
            self.connection.execute(
                "UPDATE wrappers SET pinned = ? WHERE id = ?", (int(pinned), wrapper_id)
            )

    def attach_job(
        self, job_id: int, wrapper_id: int, *, log_path: Path, result_path: Path
    ) -> None:
        with self.connection:
            self.connection.execute(
                """
                UPDATE jobs SET wrapper_id = ?, log_path = ?, result_path = ?
                WHERE id = ?
                """,
                (wrapper_id, str(log_path), str(result_path), job_id),
            )

    def update_job(
        self,
        job_id: int,
        *,
        status: str,
        error: str | None = None,
        exit_code: int | None = None,
        supervisor_pid: int | None = None,
        supervisor_start_ticks: int | None = None,
    ) -> None:
        if status == "cancelled":
            row = self.connection.execute(
                "SELECT resume_on_restart FROM jobs WHERE id = ?", (job_id,)
            ).fetchone()
            if row is not None and row["resume_on_restart"]:
                status = "interrupted"
                error = "queue stopped; review will retry after restart"
        terminal = status in {
            "succeeded",
            "failed",
            "cancelled",
            "interrupted",
            "superseded",
            "ineligible",
        }
        assignments = ["status = ?", "error = ?"]
        values: list[object] = [status, error]
        if status in {"preparing", "running"}:
            assignments.append("started_at = COALESCE(started_at, ?)")
            values.append(isoformat())
        if terminal:
            assignments.append("finished_at = ?")
            values.append(isoformat())
        if exit_code is not None:
            assignments.append("exit_code = ?")
            values.append(exit_code)
        if supervisor_pid is not None:
            assignments.append("supervisor_pid = ?")
            values.append(supervisor_pid)
        if supervisor_start_ticks is not None:
            assignments.append("supervisor_start_ticks = ?")
            values.append(supervisor_start_ticks)
        values.append(job_id)
        with self.connection:
            self.connection.execute(
                f"UPDATE jobs SET {', '.join(assignments)} WHERE id = ?", values
            )

    def job_row(self, job_id: int) -> sqlite3.Row | None:
        return self.connection.execute(
            """
            SELECT j.*, pr.url, pr.title, pr.author, pr.head_ref,
                   w.path AS wrapper_path, w.owner_token
            FROM jobs j
            JOIN pull_requests pr ON pr.repo = j.repo AND pr.number = j.pr_number
            LEFT JOIN wrappers w ON w.id = j.wrapper_id
            WHERE j.id = ?
            """,
            (job_id,),
        ).fetchone()

    def orphaned_jobs(self) -> tuple[sqlite3.Row, ...]:
        return tuple(
            self.connection.execute(
                """
                SELECT j.*, w.owner_token
                FROM jobs j LEFT JOIN wrappers w ON w.id = j.wrapper_id
                WHERE j.status IN ('preparing', 'running', 'cancelling')
                """
            ).fetchall()
        )

    def running_count(self) -> int:
        row = self.connection.execute(
            "SELECT COUNT(*) AS count FROM jobs "
            "WHERE status IN ('preparing', 'running', 'cancelling')"
        ).fetchone()
        return int(row["count"])

    def set_priorities(
        self,
        *,
        project: str,
        author: str,
        repo: str,
        number: int,
        project_priority: int,
        author_priority: int,
        pr_priority: int,
    ) -> None:
        with self.connection:
            self.connection.execute(
                "UPDATE project_priorities SET priority = ? WHERE project = ?",
                (project_priority, project),
            )
            self.connection.execute(
                "UPDATE authors SET priority = ? WHERE login = ?",
                (author_priority, author),
            )
            self.connection.execute(
                "UPDATE pull_requests SET pr_priority = ? WHERE repo = ? AND number = ?",
                (pr_priority, repo, number),
            )

    def adjust_pr_priority(self, repo: str, number: int, delta: int) -> None:
        with self.connection:
            cursor = self.connection.execute(
                "UPDATE pull_requests SET pr_priority = pr_priority + ? "
                "WHERE repo = ? AND number = ?",
                (delta, repo, number),
            )
        if cursor.rowcount != 1:
            raise KeyError(f"unknown pull request: {repo}#{number}")

    def mark_cancelling(self, job_id: int, *, resume_on_restart: bool = False) -> None:
        with self.connection:
            cursor = self.connection.execute(
                "UPDATE jobs SET status = 'cancelling', resume_on_restart = ? "
                "WHERE id = ? AND status IN ('preparing', 'running')",
                (resume_on_restart, job_id),
            )
        if cursor.rowcount != 1:
            raise ValueError(f"job {job_id} is not running")

    def resume_interrupted_jobs(self, project: str) -> int:
        """Retry previously authorized revisions after a successful GitHub poll."""
        resumed = 0
        with self.connection:
            rows = self.connection.execute(
                """
                SELECT j.*, pr.head_sha AS current_head, pr.eligible, pr.ignored,
                       pr.state AS pr_state, pr.is_draft, pr.path_filter_key,
                       pr.path_filter_passed
                FROM jobs j JOIN pull_requests pr
                  ON pr.repo = j.repo AND pr.number = j.pr_number
                WHERE j.project = ? AND j.status = 'interrupted' AND j.resume_on_restart = 1
                ORDER BY j.id
                """,
                (project,),
            ).fetchall()
            for row in rows:
                required_paths = self._path_filters.get(project, "")
                if (
                    row["head_sha"] == row["current_head"]
                    and row["eligible"]
                    and not row["ignored"]
                    and row["pr_state"] == "OPEN"
                    and not row["is_draft"]
                    and (
                        not required_paths
                        or (row["path_filter_key"] == required_paths and row["path_filter_passed"])
                    )
                ):
                    resumed += self._enqueue_locked(
                        row["repo"],
                        row["pr_number"],
                        row["head_sha"],
                        project,
                        source="manual",
                        quiet_until=None,
                        manual=True,
                    )
                self.connection.execute(
                    "UPDATE jobs SET resume_on_restart = 0 WHERE id = ?", (row["id"],)
                )
        return resumed

    def mark_waiting_cancelled(self, job_id: int) -> None:
        with self.connection:
            cursor = self.connection.execute(
                """
                UPDATE jobs SET status = 'cancelled', finished_at = ?, error = 'cancelled by user'
                WHERE id = ? AND status IN ('debouncing', 'queued')
                """,
                (isoformat(), job_id),
            )
        if cursor.rowcount != 1:
            raise ValueError(f"job {job_id} is not waiting")

    def latest_wrapper_log(self, wrapper_id: int) -> str | None:
        row = self.connection.execute(
            """
            SELECT log_path FROM jobs
            WHERE wrapper_id = ? AND log_path IS NOT NULL
            ORDER BY id DESC LIMIT 1
            """,
            (wrapper_id,),
        ).fetchone()
        return row["log_path"] if row is not None else None
