from __future__ import annotations

from datetime import timedelta

from textual.containers import VerticalScroll
from textual.widgets import Button, DataTable, Input

from review_queue.models import ProjectConfig, PullRequest, QueueConfig
from review_queue.scheduler import Scheduler
from review_queue.tui import ConfirmModal, PriorityModal, ReviewQueueApp, WatchModal
from review_queue.util import isoformat, utc_now


def seed(scheduler: Scheduler, project: ProjectConfig) -> None:
    records = tuple(
        PullRequest(
            project=project.name,
            repo=project.repo,
            number=number,
            url=f"https://github.com/{project.repo}/pull/{number}",
            title=f"Review {number}",
            author=author,
            head_sha=str(number) * 40,
            head_ref=f"users/{author}/review-{number}",
            updated_at=isoformat(utc_now() - timedelta(minutes=number)),
        )
        for number, author in ((1, "alice"), (2, "bob"))
    )
    scheduler.db.apply_poll(
        project,
        records,
        query_numbers={1, 2},
        bootstrap_hours=24,
        push_quiet_seconds=0,
    )


async def test_headless_queue_rebalances_without_losing_selection(
    config: QueueConfig, project: ProjectConfig
) -> None:
    scheduler = Scheduler(config)
    seed(scheduler, project)
    app = ReviewQueueApp(scheduler)
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        table = app.query_one("#queue-table", DataTable)
        assert table.row_count == 2
        assert not list(app.query(VerticalScroll))
        assert app.screen.max_scroll_y == 0
        assert "State" not in {str(column.label) for column in table.columns.values()}
        selected = app.selected_job
        assert selected is not None
        score = app._selected_item().score
        await pilot.press("right")
        await pilot.pause()
        assert app.selected_job == selected
        assert app._selected_item().score == score + 10
        await pilot.press("left")
        await pilot.pause()
        assert app._selected_item().score == score
        await pilot.press("space")
        assert scheduler.db.paused()


async def test_priority_modal_and_compact_layout(
    config: QueueConfig, project: ProjectConfig
) -> None:
    scheduler = Scheduler(config)
    seed(scheduler, project)
    app = ReviewQueueApp(scheduler)
    async with app.run_test(size=(80, 30)) as pilot:
        await pilot.pause()
        assert app.compact
        assert app.screen.max_scroll_y == 0
        assert app.query_one("#review-table", DataTable).styles.scrollbar_size_vertical == 0
        await pilot.press("p")
        assert isinstance(app.screen, PriorityModal)
        app.screen.query_one("#priority-project", Input).value = "5"
        app.screen.query_one("#priority-author", Input).value = "7"
        app.screen.query_one("#priority-pr", Input).value = "11"
        await pilot.click("#priority-save")
        await pilot.pause()
        assert app._selected_item().score == 23


async def test_theme_alias_and_cycle(config: QueueConfig, project: ProjectConfig) -> None:
    scheduler = Scheduler(config)
    seed(scheduler, project)
    app = ReviewQueueApp(scheduler, theme_name="dark+")
    async with app.run_test(size=(80, 30)) as pilot:
        await pilot.pause()
        assert app.theme == "dark-plus"
        await pilot.press("t")
        await pilot.pause()
        assert app.theme == "monokai"


async def test_ignore_removes_pr_and_future_heads(
    config: QueueConfig, project: ProjectConfig
) -> None:
    scheduler = Scheduler(config)
    seed(scheduler, project)
    app = ReviewQueueApp(scheduler)
    async with app.run_test(size=(80, 30)) as pilot:
        await pilot.pause()
        item = app._selected_item()
        assert item is not None
        app._ignore_confirmed(item.repo, item.number, True)
        await pilot.pause()
        assert all(queued.number != item.number for queued in scheduler.snapshot().queue)
        assert "permanently ignored" in "\n".join(app.activity)


async def test_include_dialog_is_keyboard_only(config: QueueConfig, project: ProjectConfig) -> None:
    scheduler = Scheduler(config)
    app = ReviewQueueApp(scheduler)
    async with app.run_test(size=(80, 30)) as pilot:
        await pilot.press("n")
        await pilot.pause()
        assert isinstance(app.screen, WatchModal)
        assert not list(app.screen.query(Button))
        assert app.screen.query_one("#watch-spec", Input).has_focus
        await pilot.press("escape")


async def test_quit_confirmation_is_keyboard_only(
    config: QueueConfig, project: ProjectConfig
) -> None:
    scheduler = Scheduler(config)
    seed(scheduler, project)
    scheduler.db.update_job(scheduler.snapshot().queue[0].job_id, status="running")
    app = ReviewQueueApp(scheduler)
    async with app.run_test(size=(80, 30)) as pilot:
        await pilot.press("q")
        await pilot.pause()
        assert isinstance(app.screen, ConfirmModal)
        assert not list(app.screen.query(Button))
        await pilot.press("escape")
