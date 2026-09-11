from __future__ import annotations

from textual.widgets import Button, DataTable, Static

from review_queue.cli import _parser
from review_queue.demo import DEMO_START, DemoScheduler
from review_queue.tui import ConfirmModal, ReviewQueueApp


def test_demo_trace_arrivals_push_failure_and_wrapper_cap() -> None:
    demo = DemoScheduler(speed=300)
    assert [run.number for run in demo.snapshot().running] == [1042]

    demo.advance(15)
    snapshot = demo.snapshot()
    pushed = next(item for item in snapshot.queue if item.number == 1042)
    assert pushed.status == "debouncing"
    assert any(
        wrapper.number == 2871 and wrapper.state == "failed" for wrapper in snapshot.wrappers
    )

    demo.advance(83)
    snapshot = demo.snapshot()
    assert len(snapshot.wrappers) <= snapshot.max_wrappers == 3
    assert any(
        wrapper.number == 2871 and wrapper.state == "failed" for wrapper in snapshot.wrappers
    )
    assert any(item.number >= 10_000 for item in snapshot.queue) or any(
        run.number >= 10_000 for run in snapshot.running
    )


def test_demo_manual_mode_holds_discovery_and_starts_only_selected_head():
    demo = DemoScheduler(speed=300)
    running = demo.snapshot().running
    assert demo.cycle_mode() == "manual"
    assert demo.snapshot().running == running
    demo.advance(next(iter(demo._runs.values())).duration_minutes + 1)
    assert not demo.snapshot().running
    assert demo.snapshot().queue
    selected = demo.snapshot().queue[0]
    demo.retry(selected)
    demo.advance(0)
    assert [run.job_id for run in demo.snapshot().running] == [selected.job_id]
    demo.toggle_pause()
    demo.toggle_pause()
    assert demo.snapshot().mode == "manual"


def test_demo_dispatch_pause_priorities_and_failed_retry() -> None:
    demo = DemoScheduler(speed=300)
    demo.advance(16)
    demo.toggle_pause()
    snapshot = demo.snapshot()
    assert snapshot.paused
    assert len(snapshot.queue) >= 3

    selected = min(snapshot.queue, key=lambda item: item.score)
    demo.set_priorities(
        selected,
        project_priority=40,
        author_priority=30,
        pr_priority=20,
    )
    assert demo.snapshot().queue[0].job_id == selected.job_id

    for run in tuple(demo.snapshot().running):
        demo.cancel(run.job_id)
    failed = next(wrapper for wrapper in demo.snapshot().wrappers if wrapper.number == 2871)
    retry_id = demo.retry_wrapper(failed.wrapper_id)
    retry = next(item for item in demo.snapshot().queue if item.job_id == retry_id)
    demo.set_priorities(
        retry,
        project_priority=100,
        author_priority=0,
        pr_priority=100,
    )
    demo.toggle_pause()
    demo.advance(20)
    retried = next(wrapper for wrapper in demo.snapshot().wrappers if wrapper.number == 2871)
    assert retried.state == "complete"
    assert "succeeded" in demo.wrapper_log_tail(retried.wrapper_id)


def test_demo_stale_project_does_not_block_healthy_projects() -> None:
    demo = DemoScheduler(speed=300)
    demo.toggle_pause()
    demo.advance(22)
    assert "runtime" in demo._stale_projects
    for run in tuple(demo.snapshot().running):
        demo.cancel(run.job_id)

    demo.toggle_pause()
    demo.advance(1)
    assert any(run.project != "runtime" for run in demo.snapshot().running)


def test_demo_clock_controls_and_restart() -> None:
    demo = DemoScheduler(speed=300)
    demo.advance(10)
    assert demo.now() > DEMO_START
    assert demo.set_demo_speed(1800) == 1800
    assert demo.toggle_demo_clock() is True
    assert "clock paused" in demo.demo_status()
    demo.restart_demo()
    assert demo.now() == DEMO_START
    assert demo.time_scale == 1800


async def test_demo_uses_production_tui_and_rebalances() -> None:
    demo = DemoScheduler(speed=300)
    demo.toggle_pause()
    demo.advance(6)
    app = ReviewQueueApp(demo)
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        table = app.query_one("#queue-table", DataTable)
        assert table.row_count >= 2
        assert "DEMO" in str(app.query_one("#health-bar", Static).render())
        selected = app.selected_job
        old_score = app._selected_item().score
        await pilot.press("right")
        await pilot.press("4")
        await pilot.press("0")
        await pilot.pause()
        assert app.selected_job == selected
        assert app._selected_item().score == old_score + 10
        assert demo.time_scale == 1800
        assert demo.clock_paused


async def test_wrapper_pane_targets_failed_review_while_queue_is_nonempty() -> None:
    demo = DemoScheduler(speed=300)
    demo.advance(16)
    demo.toggle_pause()
    failed = next(wrapper for wrapper in demo.snapshot().wrappers if wrapper.state == "failed")
    assert demo.snapshot().queue

    app = ReviewQueueApp(demo)
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        table = app.query_one("#review-table", DataTable)
        app.selected_wrapper = failed.wrapper_id
        table.move_cursor(row=table.get_row_index(str(failed.wrapper_id)))
        table.focus()
        await pilot.pause()
        app.refresh_view()
        assert "failed" in str(app.query_one("#detail", Static).render())
        await pilot.press("r")
        await pilot.pause()
        assert any(item.number == failed.number for item in demo.snapshot().queue)


async def test_selected_layout_shows_return_event_and_second_review() -> None:
    demo = DemoScheduler(speed=300)
    app = ReviewQueueApp(demo, theme_name="monokai")
    async with app.run_test(size=(80, 30)) as pilot:
        demo.advance(64)
        app.refresh_view()
        await pilot.pause()
        assert any("returned after author changes" in line for line in app.activity)

        demo.advance(8)
        app.refresh_view()
        await pilot.pause()
        wrapper = next(item for item in app.snapshot.wrappers if item.number == 1043)
        row = app.query_one("#review-table", DataTable).get_row(str(wrapper.wrapper_id))
        rendered = " ".join(str(cell) for cell in row)
        assert "R" in rendered
        assert "#2" in rendered


async def test_stop_and_quit_confirmation_is_keyboard_only() -> None:
    demo = DemoScheduler(speed=300)
    app = ReviewQueueApp(demo)
    async with app.run_test(size=(80, 30)) as pilot:
        await pilot.press("q")
        await pilot.pause()
        assert isinstance(app.screen, ConfirmModal)
        assert not list(app.screen.query(Button))
        await pilot.press("escape")
        await pilot.pause()
        assert not isinstance(app.screen, ConfirmModal)


def test_author_changes_return_a_reviewed_pr_to_the_queue() -> None:
    demo = DemoScheduler(speed=300)
    demo.advance(62)
    demo.toggle_pause()
    demo.advance(2)
    returned = next(item for item in demo.snapshot().queue if item.number == 1043)
    assert returned.review_count == 1
    assert returned.status == "debouncing"


def test_demo_cli_options() -> None:
    args = _parser().parse_args(["--demo", "--demo-speed", "1800", "--theme", "monokai"])
    assert args.demo
    assert args.demo_speed == 1800
    assert args.theme == "monokai"
