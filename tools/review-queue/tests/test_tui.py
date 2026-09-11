from __future__ import annotations

from datetime import timedelta

from textual.containers import VerticalScroll
from textual.widgets import Button, DataTable, Input, Static

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


async def test_manual_mode_controls_and_enqueue_marker(config, project):
    scheduler = Scheduler(config)
    seed(scheduler, project)
    app = ReviewQueueApp(scheduler)
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.press("m")
        assert app.snapshot.mode == "manual"
        assert scheduler.db.next_ready() is None
        app.query_one("#queue-table", DataTable).focus()
        await pilot.pause()
        assert "press r to enqueue" in str(app.query_one("#detail", Static).render())
        selected = app.selected_job
        await pilot.press("r")
        assert scheduler.db.next_ready().job_id == selected
        assert app._selected_item().manual_enqueued
        assert "▶" in str(app.query_one("#queue-table", DataTable).get_row(str(selected)))
        await pilot.press("space")
        assert app.snapshot.mode == "paused"
        await pilot.press("space")
        assert app.snapshot.mode == "manual"
        await pilot.press("m")
        assert app.snapshot.mode == "paused"
        await pilot.press("m")
        assert app.snapshot.mode == "active"


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
        assert [widget.id for widget in app.query(VerticalScroll)] == ["activity-scroll"]
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


async def test_history_divider_drags_and_releases_mouse(config: QueueConfig) -> None:
    app = ReviewQueueApp(Scheduler(config))
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        divider = app.query_one("#history-divider")
        history = app.query_one("#activity-pane")
        queue = app.query_one("#queue-pane")
        original_height = history.size.height
        start_y = divider.region.y
        await pilot.mouse_down("#history-divider", offset=(4, 0))
        await pilot.hover(offset=(4, start_y - 8))
        await pilot.pause()
        assert history.size.height == original_height + 8
        assert queue.size.height >= 8
        assert app.mouse_captured is divider
        await pilot.mouse_up(offset=(4, start_y - 8))
        await pilot.pause()
        assert app.mouse_captured is None
        await pilot.hover(offset=(4, start_y - 12))
        assert history.size.height == original_height + 8
        assert app.screen.max_scroll_y == 0


async def test_history_resize_limits_keyboard_and_terminal_resize(config: QueueConfig) -> None:
    app = ReviewQueueApp(Scheduler(config))
    async with app.run_test(size=(80, 30)) as pilot:
        await pilot.pause()
        history = app.query_one("#activity-pane")
        original_height = history.size.height
        await pilot.press("alt+up", "alt+up")
        await pilot.pause()
        assert history.size.height == original_height + 2
        await pilot.press("alt+down")
        await pilot.pause()
        assert history.size.height == original_height + 1
        await pilot.mouse_down("#history-divider", offset=(4, 0))
        await pilot.hover(offset=(4, 0))
        await pilot.pause()
        assert app.query_one("#queue-pane").outer_size.height == 8
        await pilot.mouse_up(offset=(4, 0))
        await pilot.resize_terminal(80, 18)
        await pilot.pause()
        assert history.region.bottom < app.size.height
        assert app.screen.max_scroll_y == 0
        await pilot.press("slash")
        await pilot.pause()
        assert history.region.bottom < app.size.height
        assert history.size.height >= 2
        assert app.screen.max_scroll_y == 0
        await pilot.press("escape")
        await pilot.resize_terminal(80, 30)
        await pilot.pause()
        await pilot.mouse_down("#history-divider", offset=(4, 0))
        await pilot.hover(offset=(4, 29))
        await pilot.mouse_up(offset=(4, 29))
        await pilot.pause()
        assert history.size.height == 2


async def test_history_scrolls_older_entries_and_survives_refresh(config: QueueConfig) -> None:
    from textual import events
    from textual.widgets import Static

    app = ReviewQueueApp(Scheduler(config))
    for number in range(50):
        app._record_activity(f"history entry {number}")
    async with app.run_test(size=(100, 35)) as pilot:
        await pilot.pause()
        feed = app.query_one("#activity-feed", Static)
        history = app.query_one("#activity-scroll", VerticalScroll)
        assert history.max_scroll_y > 0
        assert "history entry 0" in str(feed.content)
        await pilot._post_mouse_events(
            [events.MouseScrollDown], widget="#activity-scroll", offset=(3, 1)
        )
        await pilot.pause(0.3)
        assert history.scroll_y > 0
        position = history.scroll_y
        app.refresh_view()
        await pilot.pause()
        assert history.scroll_y == position


async def test_failure_reason_visible_and_full_log_opens(config, project):
    from review_queue.tui import LogModal

    scheduler = Scheduler(config)
    seed(scheduler, project)
    item = scheduler.snapshot().queue[0]
    wrapper = await scheduler._ensure_wrapper(item)
    log = config.state_dir / "failure.log"
    log.write_text("test.cpp:12: error: missing symbol\n== Final checkout ==\nready\n")
    scheduler.db.attach_job(
        item.job_id, wrapper["id"], log_path=log, result_path=config.state_dir / "failure.json"
    )
    scheduler.db.update_job(item.job_id, status="failed", error="Build failed: missing symbol")
    app = ReviewQueueApp(scheduler)
    async with app.run_test(size=(120, 40)) as pilot:
        app.query_one("#review-table", DataTable).focus()
        await pilot.pause()
        assert "missing symbol" in str(app.query_one("#detail", Static).render())
        await pilot.press("l")
        assert isinstance(app.screen, LogModal)
        assert app.screen.reason == "Build failed: missing symbol"
        assert "test.cpp:12: error" in app.screen.log_text
        await pilot.press("escape")
        scheduler.db.update_job(item.job_id, status="ineligible", error="PR is merged")
        app.refresh_view()
        assert "skipped" in str(
            app.query_one("#review-table", DataTable).get_row(str(wrapper["id"]))
        )
        assert "press r to retry" not in str(
            app.query_one("#review-table", DataTable).get_row(str(wrapper["id"]))
        )


async def test_live_phase_elapsed_time_and_short_phase_history(config, project):
    from review_queue.phases import PhaseChange

    scheduler = Scheduler(config)
    seed(scheduler, project)
    item = scheduler.snapshot().queue[0]
    wrapper = await scheduler._ensure_wrapper(item)
    scheduler.db.attach_job(
        item.job_id,
        wrapper["id"],
        log_path=config.state_dir / "run.log",
        result_path=config.state_dir / "run.json",
    )
    scheduler.db.update_job(item.job_id, status="running")
    started = isoformat(utc_now() - timedelta(minutes=2))
    app = ReviewQueueApp(scheduler)
    async with app.run_test(size=(160, 40)) as pilot:
        for name in ["Build", "Build › Configure", "Build › Compile"]:
            scheduler.db.update_phase(item.job_id, PhaseChange(name, started, True))
        app.refresh_view()
        app.query_one("#review-table", DataTable).focus()
        await pilot.pause()
        detail = str(app.query_one("#detail", Static).render())
        assert "running" in detail
        assert "Build › Compile" in detail
        assert "2m in phase" in detail
        history = "\n".join(app.activity)
        assert "Build › Configure" in history  # Entered and left between UI refreshes.
        assert "Build › Compile" in history
        app.refresh_view()
        assert "\n".join(app.activity) == history
        scheduler.db.update_job(item.job_id, status="cancelling")
        app.refresh_view()
        row = str(app.query_one("#review-table", DataTable).get_row(str(wrapper["id"])))
        assert "cancelling" in row
        assert "Compile" in row
