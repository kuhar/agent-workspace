from __future__ import annotations

from dataclasses import replace
from datetime import timedelta
from pathlib import Path

import pytest

from review_queue.database import Database
from review_queue.models import ProjectConfig, PullRequest
from review_queue.util import isoformat, utc_now


def pr(
    project: ProjectConfig,
    number: int,
    *,
    head: str,
    hours_ago: int = 1,
    author: str = "alice",
) -> PullRequest:
    return PullRequest(
        project=project.name,
        repo=project.repo,
        number=number,
        url=f"https://github.com/{project.repo}/pull/{number}",
        title=f"PR {number}",
        author=author,
        head_sha=head,
        head_ref=f"users/{author}/pr-{number}",
        updated_at=isoformat(utc_now() - timedelta(hours=hours_ago)),
    )


def database(tmp_path: Path, project: ProjectConfig) -> Database:
    db = Database(tmp_path / "queue.sqlite3")
    db.seed((project,), start_paused=False)
    return db


def test_manual_mode_holds_automatic_work_and_requires_each_new_head(tmp_path, project):
    db = database(tmp_path, project)
    first = pr(project, 1, head="a" * 40)
    second = pr(project, 2, head="b" * 40)

    def poll(*records):
        db.apply_poll(
            project,
            records,
            query_numbers={r.number for r in records},
            bootstrap_hours=24,
            push_quiet_seconds=0,
        )

    poll(first, second)
    db.set_mode("manual")
    assert len(db.queue_items()) == 2
    assert db.next_ready() is None
    job = db.enqueue_current(first.repo, first.number)
    assert db.next_ready().job_id == job
    assert db.next_ready().manual_enqueued
    assert db.enqueue_current(first.repo, first.number) == job
    assert len(db.queue_items()) == 2

    pushed = replace(first, head_sha="c" * 40)
    poll(pushed, second)
    assert db.next_ready() is None
    assert not any(item.manual_enqueued for item in db.queue_items())
    db.set_manual_watch(pushed)  # Explicit `n` also approves an already discovered head.
    assert db.next_ready().number == first.number
    db.update_job(db.next_ready().job_id, status="succeeded")
    poll(replace(pushed, head_sha="d" * 40), second)
    assert db.next_ready() is None  # Watching future heads is not approval to run them.
    db.set_mode("active")
    assert db.next_ready() is not None


def test_manual_mode_eligibility_restoration_is_not_user_enqueue(tmp_path, project):
    db = database(tmp_path, project)
    item = pr(project, 1, head="a" * 40)
    db.set_mode("manual")
    for record in (item, replace(item, is_draft=True), item):
        db.apply_poll(
            project, (record,), query_numbers={1}, bootstrap_hours=24, push_quiet_seconds=0
        )
    assert len(db.queue_items()) == 1
    assert db.next_ready() is None
    db.ignore_pr(item.repo, item.number)
    assert not db.queue_items()


@pytest.mark.parametrize("initial", ["active", "manual", "paused"])
def test_dispatch_mode_persistence_and_legacy_migration(tmp_path, project, initial):
    path = tmp_path / "queue.sqlite3"
    db = Database(path)
    db.seed((project,), start_paused=False, start_mode=initial)
    assert db.mode() == initial
    db.set_mode("manual")
    db.set_paused(True)
    db.close()
    db = Database(path)
    db.seed((project,), start_paused=False, start_mode="active")
    assert db.mode() == "paused"
    db.set_paused(False)
    assert db.mode() == "manual"
    db.set_mode("active")
    with db.connection:
        db.connection.execute("DELETE FROM settings WHERE key='manual_only'")
    db.seed((project,), start_paused=True, start_mode="manual")
    assert db.mode() == "active"  # Existing legacy selection wins over startup config.


def test_bootstrap_only_queues_recent_then_watches_head_changes(
    tmp_path: Path, project: ProjectConfig
) -> None:
    db = database(tmp_path, project)
    recent = pr(project, 1, head="a" * 40)
    old = pr(project, 2, head="b" * 40, hours_ago=48)
    outcome = db.apply_poll(
        project,
        (recent, old),
        query_numbers={1, 2},
        bootstrap_hours=24,
        push_quiet_seconds=0,
    )
    assert outcome.queued == 1
    assert [item.number for item in db.queue_items()] == [1]

    changed = pr(project, 2, head="c" * 40, hours_ago=0)
    outcome = db.apply_poll(
        project,
        (recent, changed),
        query_numbers={1, 2},
        bootstrap_hours=24,
        push_quiet_seconds=0,
    )
    assert outcome.changed_heads == 1
    assert {item.number for item in db.queue_items()} == {1, 2}


def test_pushes_coalesce_and_running_head_is_not_preempted(
    tmp_path: Path, project: ProjectConfig
) -> None:
    db = database(tmp_path, project)
    first = pr(project, 9, head="1" * 40)
    db.apply_poll(
        project,
        (first,),
        query_numbers={9},
        bootstrap_hours=24,
        push_quiet_seconds=0,
    )
    first_job = db.queue_items()[0]
    db.update_job(first_job.job_id, status="running")

    second = pr(project, 9, head="2" * 40, hours_ago=0)
    third = pr(project, 9, head="3" * 40, hours_ago=0)
    for record in (second, third):
        db.apply_poll(
            project,
            (record,),
            query_numbers={9},
            bootstrap_hours=24,
            push_quiet_seconds=0,
        )
    waiting = db.queue_items()
    assert len(waiting) == 1
    assert waiting[0].head_sha == "3" * 40
    assert db.job_row(first_job.job_id)["status"] == "running"


def test_priority_changes_rebalance_waiting_queue_without_running_preemption(
    tmp_path: Path, project: ProjectConfig
) -> None:
    db = database(tmp_path, project)
    one = pr(project, 1, head="a" * 40, author="alice")
    two = pr(project, 2, head="b" * 40, author="bob")
    db.apply_poll(
        project,
        (one, two),
        query_numbers={1, 2},
        bootstrap_hours=24,
        push_quiet_seconds=0,
    )
    assert [item.number for item in db.queue_items()] == [1, 2]
    selected = next(item for item in db.queue_items() if item.number == 2)
    db.set_priorities(
        project=selected.project,
        author=selected.author,
        repo=selected.repo,
        number=selected.number,
        project_priority=3,
        author_priority=20,
        pr_priority=7,
    )
    ordered = db.queue_items()
    assert ordered[0].number == 2
    assert ordered[0].score == 30


def test_leaving_query_cancels_waiting_but_manual_retry_is_allowed(
    tmp_path: Path, project: ProjectConfig
) -> None:
    db = database(tmp_path, project)
    record = pr(project, 4, head="d" * 40)
    db.apply_poll(
        project,
        (record,),
        query_numbers={4},
        bootstrap_hours=24,
        push_quiet_seconds=0,
    )
    db.apply_poll(
        project,
        (),
        query_numbers=set(),
        bootstrap_hours=24,
        push_quiet_seconds=0,
    )
    assert db.queue_items() == ()
    retry = db.enqueue_current(project.repo, 4)
    assert db.job_row(retry)["attempt"] == 2


def test_merged_manual_pr_leaves_queue_and_watch_set(
    tmp_path: Path, project: ProjectConfig
) -> None:
    db = database(tmp_path, project)
    opened = pr(project, 18, head="e" * 40)
    db.set_manual_watch(opened)
    job_id = db.queue_items()[0].job_id

    outcome = db.apply_poll(
        project,
        (replace(opened, state="MERGED"),),
        query_numbers=set(),
        bootstrap_hours=24,
        push_quiet_seconds=0,
    )

    assert outcome.left_query == 1
    assert db.queue_items() == ()
    assert db.manual_watches(project.name) == ()
    assert db.job_row(job_id)["status"] == "ineligible"
    assert db.job_row(job_id)["error"] == "PR merged"
    with pytest.raises(ValueError, match="not open"):
        db.enqueue_current(project.repo, 18)


def test_draft_manual_pr_remains_watched_until_it_returns(
    tmp_path: Path, project: ProjectConfig
) -> None:
    db = database(tmp_path, project)
    opened = pr(project, 19, head="f" * 40)
    db.set_manual_watch(opened)

    db.apply_poll(
        project,
        (replace(opened, is_draft=True),),
        query_numbers=set(),
        bootstrap_hours=24,
        push_quiet_seconds=0,
    )
    assert db.queue_items() == ()
    assert db.manual_watches(project.name) == (19,)

    outcome = db.apply_poll(
        project,
        (opened,),
        query_numbers=set(),
        bootstrap_hours=24,
        push_quiet_seconds=0,
    )
    assert outcome.queued == 1
    assert db.queue_items()[0].number == 19


def test_new_head_reports_completed_review_count(tmp_path: Path, project: ProjectConfig) -> None:
    db = database(tmp_path, project)
    first = pr(project, 12, head="a" * 40)
    db.apply_poll(
        project,
        (first,),
        query_numbers={12},
        bootstrap_hours=24,
        push_quiet_seconds=0,
    )
    db.update_job(db.queue_items()[0].job_id, status="succeeded")

    returned = pr(project, 12, head="b" * 40, hours_ago=0)
    db.apply_poll(
        project,
        (returned,),
        query_numbers={12},
        bootstrap_hours=24,
        push_quiet_seconds=0,
    )
    assert db.queue_items()[0].review_count == 1


def test_permanent_ignore_suppresses_pushes_until_manual_inclusion(
    tmp_path: Path, project: ProjectConfig
) -> None:
    db = database(tmp_path, project)
    first = pr(project, 13, head="a" * 40)
    db.apply_poll(
        project,
        (first,),
        query_numbers={13},
        bootstrap_hours=24,
        push_quiet_seconds=0,
    )
    db.ignore_pr(project.repo, 13)
    assert db.queue_items() == ()

    pushed = pr(project, 13, head="b" * 40, hours_ago=0)
    db.apply_poll(
        project,
        (pushed,),
        query_numbers={13},
        bootstrap_hours=24,
        push_quiet_seconds=0,
    )
    assert db.queue_items() == ()

    db.set_manual_watch(pushed)
    assert db.queue_items()[0].head_sha == "b" * 40


def test_same_head_is_requeued_when_it_returns_to_query(
    tmp_path: Path, project: ProjectConfig
) -> None:
    db = database(tmp_path, project)
    record = pr(project, 14, head="a" * 40)
    db.apply_poll(
        project,
        (record,),
        query_numbers={14},
        bootstrap_hours=24,
        push_quiet_seconds=0,
    )
    first_job = db.queue_items()[0].job_id
    db.update_job(first_job, status="succeeded")
    db.apply_poll(
        project,
        (),
        query_numbers=set(),
        bootstrap_hours=24,
        push_quiet_seconds=0,
    )

    outcome = db.apply_poll(
        project,
        (record,),
        query_numbers={14},
        bootstrap_hours=24,
        push_quiet_seconds=0,
    )
    assert outcome.queued == 1
    assert db.queue_items()[0].job_id != first_job


def test_manual_inclusion_requeues_ignored_same_head_once(
    tmp_path: Path, project: ProjectConfig
) -> None:
    db = database(tmp_path, project)
    record = pr(project, 15, head="b" * 40)
    db.apply_poll(
        project,
        (record,),
        query_numbers={15},
        bootstrap_hours=24,
        push_quiet_seconds=0,
    )
    db.ignore_pr(project.repo, 15)
    db.set_manual_watch(record)
    queued = db.queue_items()
    assert len(queued) == 1
    assert db.job_row(queued[0].job_id)["attempt"] == 2

    db.set_manual_watch(record)
    assert len(db.queue_items()) == 1


def test_manual_inclusion_supersedes_waiting_older_head(
    tmp_path: Path, project: ProjectConfig
) -> None:
    db = database(tmp_path, project)
    first = pr(project, 17, head="e" * 40)
    db.apply_poll(
        project,
        (first,),
        query_numbers={17},
        bootstrap_hours=24,
        push_quiet_seconds=0,
    )
    first_job = db.queue_items()[0].job_id
    pushed = pr(project, 17, head="f" * 40, hours_ago=0)

    db.set_manual_watch(pushed)

    queued = db.queue_items()
    assert [item.head_sha for item in queued] == ["f" * 40]
    assert db.job_row(first_job)["status"] == "superseded"


def test_new_head_waits_while_same_pr_is_under_review(
    tmp_path: Path, project: ProjectConfig
) -> None:
    db = database(tmp_path, project)
    first = pr(project, 16, head="c" * 40)
    db.apply_poll(
        project,
        (first,),
        query_numbers={16},
        bootstrap_hours=24,
        push_quiet_seconds=0,
    )
    first_job = db.queue_items()[0].job_id
    db.update_job(first_job, status="running")
    pushed = pr(project, 16, head="d" * 40, hours_ago=0)
    db.apply_poll(
        project,
        (pushed,),
        query_numbers={16},
        bootstrap_hours=24,
        push_quiet_seconds=0,
    )

    assert db.next_ready() is None
    db.update_job(first_job, status="succeeded")
    assert db.next_ready().head_sha == "d" * 40


def test_seed_rejects_project_repository_identity_change(
    tmp_path: Path, project: ProjectConfig
) -> None:
    db = database(tmp_path, project)
    changed = replace(project, repo="ROCm/different")
    with pytest.raises(ValueError, match="repository changed"):
        db.seed((changed,), start_paused=False)


def test_path_filter_blocks_automatic_manual_and_retry_until_matching_push(tmp_path, project):
    project = replace(project, include_paths=("emulation/",))
    db = database(tmp_path, project)
    outside = replace(pr(project, 42, head="a" * 40), path_filter_key=project.path_filter_key)
    db.apply_poll(project, [outside], query_numbers={42}, bootstrap_hours=24, push_quiet_seconds=0)
    assert not db.queue_items()
    with pytest.raises(ValueError, match="include_paths"):
        db.set_manual_watch(outside)
    with pytest.raises(ValueError, match="include_paths"):
        db.enqueue_current(project.repo, 42)
    included = replace(outside, head_sha="b" * 40, path_filter_passed=True)
    db.apply_poll(project, [included], query_numbers={42}, bootstrap_hours=24, push_quiet_seconds=0)
    assert db.next_ready().head_sha == included.head_sha
    db.set_manual_watch(included)
    outside_again = replace(outside, head_sha="c" * 40)
    db.apply_poll(
        project, [outside_again], query_numbers=set(), bootstrap_hours=24, push_quiet_seconds=0
    )
    assert not db.queue_items()
    assert db.manual_watches(project.name) == (42,)
    assert db.connection.execute("SELECT ignored FROM pull_requests").fetchone()[0] == 0
    assert all(row[0] != "queued" for row in db.connection.execute("SELECT status FROM jobs"))
    with pytest.raises(ValueError, match="include_paths"):
        db.enqueue_current(project.repo, 42)
    included_again = replace(included, head_sha="d" * 40)
    db.apply_poll(
        project, [included_again], query_numbers=set(), bootstrap_hours=24, push_quiet_seconds=0
    )
    assert db.next_ready().head_sha == included_again.head_sha


def test_enabling_or_changing_path_filter_holds_old_queue_until_rechecked(tmp_path, project):
    db = database(tmp_path, project)
    record = pr(project, 42, head="a" * 40)
    db.apply_poll(project, [record], query_numbers={42}, bootstrap_hours=24, push_quiet_seconds=0)
    assert db.next_ready()
    db.close()
    required = replace(project, include_paths=("emulation/",))
    db = database(tmp_path, required)
    assert db.next_ready() is None
    with pytest.raises(ValueError, match="include_paths"):
        db.enqueue_current(project.repo, 42)
    checked = replace(record, path_filter_key=required.path_filter_key, path_filter_passed=True)
    db.apply_poll(required, [checked], query_numbers={42}, bootstrap_hours=24, push_quiet_seconds=0)
    assert db.next_ready()
    db.close()
    db = database(tmp_path, replace(project, include_paths=("other/",)))
    assert db.next_ready() is None
    with pytest.raises(ValueError, match="include_paths"):
        db.set_manual_watch(checked)
