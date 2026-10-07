from __future__ import annotations

import asyncio
import re
from collections import deque
from dataclasses import dataclass
from datetime import datetime

from rich.markup import escape
from rich.text import Text
from textual import events
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.screen import ModalScreen
from textual.widgets import Button, DataTable, Footer, Input, Label, Static

from .demo import DemoScheduler
from .models import QueueItem, WrapperView
from .scheduler import Scheduler
from .themes import DARK_PLUS, FAVORITE_THEMES, resolve_theme
from .util import parse_time, utc_now


@dataclass(frozen=True, slots=True)
class PriorityValues:
    project: int
    author: int
    pr: int


class FormModal(ModalScreen[object | None]):
    BINDINGS = [Binding("escape", "cancel", "Cancel")]
    CSS = """
    FormModal { align: center middle; background: $background 65%; }
    FormModal > Vertical {
        width: 68; height: auto; max-height: 24; padding: 1 2;
        border: tall $accent; background: $surface;
    }
    FormModal .modal-title { height: 2; text-style: bold; color: $accent; }
    FormModal .field-label { height: 1; margin-top: 1; color: $text-muted; }
    FormModal Input { height: 3; }
    FormModal .form-error { height: 2; color: $error; }
    FormModal .modal-hint { height: 1; color: $text-muted; }
    FormModal Horizontal { height: 3; align-horizontal: right; margin-top: 1; }
    FormModal Button { margin-left: 1; }
    """

    def action_cancel(self) -> None:
        self.dismiss(None)


class PriorityModal(FormModal):
    def __init__(self, item: QueueItem):
        super().__init__()
        self.item = item

    def compose(self) -> ComposeResult:
        with Vertical():
            yield Label(
                f"Set exact priorities for {self.item.repo}#{self.item.number}",
                classes="modal-title",
            )
            for label, identifier, value in (
                (f"Project ({self.item.project})", "priority-project", self.item.project_priority),
                (f"Author (@{self.item.author})", "priority-author", self.item.author_priority),
                ("Pull request", "priority-pr", self.item.pr_priority),
            ):
                yield Label(label, classes="field-label")
                yield Input(str(value), id=identifier, type="integer")
            yield Static("", id="priority-error", classes="form-error")
            with Horizontal():
                yield Button("Cancel", id="priority-cancel")
                yield Button("Save", id="priority-save", variant="primary")

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "priority-cancel":
            self.dismiss(None)
            return
        try:
            values = PriorityValues(
                project=int(self.query_one("#priority-project", Input).value),
                author=int(self.query_one("#priority-author", Input).value),
                pr=int(self.query_one("#priority-pr", Input).value),
            )
        except ValueError:
            self.query_one("#priority-error", Static).update("All priorities must be integers.")
            return
        self.dismiss(values)


class WatchModal(FormModal):
    def compose(self) -> ComposeResult:
        with Vertical():
            yield Label(
                "Include a pull request outside the project filter",
                classes="modal-title",
            )
            yield Label("PR number or GitHub pull request URL", classes="field-label")
            yield Input(
                placeholder="10583 or https://github.com/ROCm/rocm-systems/pull/10583",
                id="watch-spec",
            )
            yield Static("", id="watch-error", classes="form-error")
            yield Static(
                "[dim]Enter[/] include  ·  [dim]Esc[/] cancel",
                classes="modal-hint",
            )

    def on_input_submitted(self, event: Input.Submitted) -> None:
        if event.input.id != "watch-spec":
            return
        value = event.value.strip()
        if not value:
            self.query_one("#watch-error", Static).update("Enter a PR number or URL.")
            return
        self.dismiss(value)


class ConfirmModal(ModalScreen[bool]):
    BINDINGS = [
        Binding("enter", "accept", "Confirm"),
        Binding("y", "accept", "Confirm", show=False),
        Binding("escape", "reject", "Cancel"),
    ]
    CSS = """
    ConfirmModal { align: center middle; background: $background 65%; }
    ConfirmModal > Vertical {
        width: 68; height: auto; padding: 1 2;
        border: tall $warning; background: $surface;
    }
    ConfirmModal .confirm-message { height: auto; min-height: 3; }
    ConfirmModal .modal-hint { height: 1; color: $text-muted; margin-top: 1; }
    """

    def __init__(self, message: str, *, accept_label: str):
        super().__init__()
        self.message = message
        self.accept_label = accept_label

    def compose(self) -> ComposeResult:
        with Vertical():
            yield Static(self.message, classes="confirm-message")
            yield Static(
                f"[dim]Enter[/] {escape(self.accept_label)}  ·  [dim]Esc[/] cancel",
                classes="modal-hint",
            )

    def action_accept(self) -> None:
        self.dismiss(True)

    def action_reject(self) -> None:
        self.dismiss(False)


class LogModal(ModalScreen[None]):
    BINDINGS = [Binding("escape", "close", "Close")]
    CSS = """
    LogModal { align: center middle; background: $background 65%; }
    LogModal > Vertical {
        width: 90%; height: 85%; padding: 1 2;
        border: tall $accent; background: $surface;
    }
    LogModal Static { height: auto; }
    LogModal VerticalScroll { height: 1fr; }
    """

    def __init__(self, title: str, reason: str, log: str):
        super().__init__()
        self.title_text, self.reason, self.log_text = title, reason, log

    def compose(self) -> ComposeResult:
        with Vertical():
            yield Static(f"{self.title_text} · Esc closes", markup=False)
            with VerticalScroll():
                yield Static(self.reason, markup=False, id="failure-reason")
                yield Static(self.log_text or "No launcher log available.", markup=False)

    def action_close(self) -> None:
        self.dismiss()


class HelpModal(ModalScreen[None]):
    BINDINGS = [
        Binding("escape", "close", "Close"),
        Binding("question_mark", "close", "Close"),
    ]
    CSS = """
    HelpModal { align: center middle; background: $background 65%; }
    HelpModal > Static {
        width: 74; height: 33; padding: 1 2;
        border: tall $accent; background: $surface;
    }
    """
    HELP = """[b]Review queue keys[/b]

[b]↑/↓ or j/k[/b]  select work
[b]Tab[/b]            switch between panes
[b]← / →[/b]        lower / raise selected PR priority by ten
[b]p[/b]            set project / author / PR priorities
[b]t[/b]            cycle Dark+, Monokai, Catppuccin, and Tokyo Night
[b]Space[/b]        pause or resume dispatch
[b]r[/b]            enqueue or retry the current PR head
[b]m[/b]            cycle active / manual / paused mode
[b]x[/b]            cancel selected waiting/running work
[b]P[/b]            pin or unpin selected wrapper
[b]e[/b]            safely recycle selected idle wrapper
[b]n[/b]            include a PR outside the project filter
[b]i[/b]            permanently ignore the selected PR
[b]/[/b]            filter queue
[b]Alt+↑ / Alt+↓[/b]  grow / shrink history (or drag its divider)
[b]l[/b]  show selected review details and launcher log
[b]g[/b]            request an immediate GitHub refresh
[b]?[/b]            close this help
[b]q[/b]            quit; active launchers are stopped

[b]Demo clock[/b]
[b]1 / 2 / 3 / 4[/b]  set 1× / 60× / 300× / 1800× speed
[b]0[/b]              pause or resume simulated time
[b]R[/b]              restart the synthetic trace
[b]g[/b]              inject the next trace event immediately
"""

    def compose(self) -> ComposeResult:
        yield Static(self.HELP)

    def action_close(self) -> None:
        self.dismiss(None)


def _age(value: str | None, *, now: datetime | None = None) -> str:
    if not value:
        return "—"
    seconds = max(0, int(((now or utc_now()) - parse_time(value)).total_seconds()))
    if seconds < 60:
        return f"{seconds}s"
    if seconds < 3600:
        return f"{seconds // 60}m"
    if seconds < 86400:
        return f"{seconds // 3600}h"
    return f"{seconds // 86400}d"


def _size(value: int) -> str:
    if value <= 0:
        return "—"
    return f"{value / 1024**3:.1f} GiB"


def _approvals(item: QueueItem | WrapperView) -> str:
    if not item.approval_viewer:
        return "[dim]Approvals ?[/]"
    mine = any(name.casefold() == item.approval_viewer.casefold() for name in item.approved_by)
    others = sum(name.casefold() != item.approval_viewer.casefold() for name in item.approved_by)
    you = "[b green]You ✓[/]" if mine else "[dim]You –[/]"
    rest = f"[cyan]Others {others}[/]" if others else "[dim]Others 0[/]"
    return f"{you} {rest}"


def _reviews(count: int) -> str:
    return "R" * count if count else "·"


def _phase(status: str, *, width: int) -> str:
    match = re.search(r"(\d{1,3})%", status)
    if match is None:
        return escape(status)
    progress = min(100, int(match.group(1))) / 100
    filled = max(0, min(width, round(progress * width)))
    bar = "█" * filled + "░" * (width - filled)
    return f"[magenta]{bar}[/]  {escape(status)}"


class HistoryLayout(Vertical):
    """Share the remaining screen between the queue and activity history."""

    preferred_height = 4

    def set_history_height(self, height: int) -> None:
        self.preferred_height = max(2, min(height, self._maximum_history_height()))
        self._layout_history()

    def _maximum_history_height(self) -> int:
        available = max(0, self.size.height - 1)  # divider
        queue_minimum = min(8, max(1, available - 2))
        return max(0, available - queue_minimum)

    def _layout_history(self) -> None:
        available = max(0, self.size.height - 1)
        self.query_one("#queue-pane").styles.min_height = min(8, max(1, available - 2))
        self.query_one("#activity-pane").styles.height = min(
            self.preferred_height, self._maximum_history_height()
        )

    def on_resize(self, event: events.Resize) -> None:
        self._layout_history()


class HistoryDivider(Static):
    """Capture the pointer so dragging continues outside the divider row."""

    def __init__(self) -> None:
        super().__init__(id="history-divider")
        self._drag_start: tuple[int, int] | None = None

    def on_mouse_down(self, event: events.MouseDown) -> None:
        if event.button == 1:
            self._drag_start = (
                event.screen_y,
                self.app.query_one("#activity-pane").size.height,
            )
            self.capture_mouse()
            self.add_class("dragging")
            event.prevent_default()
            event.stop()

    def on_mouse_move(self, event: events.MouseMove) -> None:
        if self._drag_start is not None:
            start_y, start_height = self._drag_start
            self.app.query_one(HistoryLayout).set_history_height(
                start_height + start_y - event.screen_y
            )
            event.stop()

    def on_mouse_up(self, event: events.MouseUp) -> None:
        if event.button == 1 and self._drag_start is not None:
            self.release_mouse()
            self._drag_start = None
            self.remove_class("dragging")
            event.stop()

    def on_mouse_release(self, event: events.MouseRelease) -> None:
        self._drag_start = None
        self.remove_class("dragging")


class WaitingTable(DataTable):
    # Keep priority shortcuts even when extra columns make the table scrollable.
    BINDINGS = [
        Binding("left", "app.lower_priority", "Prio −"),
        Binding("right", "app.raise_priority", "Prio +"),
    ]


class ReviewQueueApp(App[None]):
    TITLE = "Review Queue"
    SUB_TITLE = "foreground scheduler"
    BINDINGS = [
        Binding("up", "cursor_up", "Up", show=False),
        Binding("down", "cursor_down", "Down", show=False),
        Binding("k", "cursor_up", "Up", show=False),
        Binding("j", "cursor_down", "Down", show=False),
        Binding("left", "lower_priority", "Prio −"),
        Binding("right", "raise_priority", "Prio +"),
        Binding("plus", "raise_priority", "+10", show=False),
        Binding("minus", "lower_priority", "-10", show=False),
        Binding("p", "edit_priorities", "Priorities"),
        Binding("t", "cycle_theme", "Theme"),
        Binding("space", "toggle_pause", "Pause"),
        Binding("m", "cycle_mode", "Mode"),
        Binding("r", "retry", "Enqueue"),
        Binding("l", "show_log", "Log"),
        Binding("x", "cancel", "Cancel"),
        Binding("P", "pin_wrapper", "Pin"),
        Binding("e", "evict_wrapper", "Recycle"),
        Binding("n", "watch_pr", "Include"),
        Binding("i", "ignore_pr", "Ignore"),
        Binding("slash", "show_filter", "Filter"),
        Binding("alt+up", "grow_history", "More history", show=False),
        Binding("alt+down", "shrink_history", "Less history", show=False),
        Binding("escape", "clear_filter", "Clear", show=False),
        Binding("g", "refresh_github", "Refresh"),
        Binding("question_mark", "show_help", "Help"),
        Binding("0", "toggle_demo_clock", "Demo clock", show=False),
        Binding("1", "demo_speed_1", "Demo 1×", show=False),
        Binding("2", "demo_speed_2", "Demo 60×", show=False),
        Binding("3", "demo_speed_3", "Demo 300×", show=False),
        Binding("4", "demo_speed_4", "Demo 1800×", show=False),
        Binding("R", "restart_demo", "Restart demo", show=False),
        Binding("q", "quit", "Quit"),
    ]
    CSS = """
    Screen { layout: vertical; background: $background; overflow: hidden; }
    #health-bar {
        height: 3; padding: 0 2; background: $boost;
        border-bottom: solid $primary-background;
    }
    #filter-input { display: none; height: 3; margin: 0 1; border: none; }
    #filter-input.visible { display: block; }
    .panel-title {
        height: 1; padding: 0 2; text-style: bold;
        color: $text-muted; background: $background;
    }
    #reviews-pane {
        height: 3; background: $surface;
        border-bottom: solid $primary-background;
    }
    #review-table { height: 1fr; background: $surface; }
    #queue-pane { height: 1fr; min-height: 8; }
    #queue-table { height: 1fr; }
    HistoryLayout { height: 1fr; overflow: hidden; }
    #history-divider {
        height: 1; border-top: solid $primary-background;
    }
    #history-divider:hover, #history-divider.dragging {
        border-top: solid $accent;
    }
    #activity-pane {
        height: 4; padding: 0 2; background: $panel; overflow: hidden;
    }
    #detail {
        height: 1; overflow: hidden; color: $text;
        text-wrap: nowrap; text-overflow: ellipsis;
    }
    #activity-feed {
        height: auto; color: $text-muted;
        text-wrap: nowrap; text-overflow: ellipsis;
    }
    #activity-scroll { height: 1fr; overflow-x: hidden; }
    DataTable { scrollbar-size-vertical: 0; scrollbar-size-horizontal: 0; }
    DataTable > .datatable--header {
        background: $panel; color: $text-muted; text-style: bold;
    }
    DataTable > .datatable--cursor {
        background: $accent 28%; color: $text; text-style: bold;
    }
    Footer { height: 1; }
    """

    def __init__(
        self,
        scheduler: Scheduler | DemoScheduler,
        *,
        theme_name: str = "catppuccin-mocha",
    ):
        super().__init__()
        self.register_theme(DARK_PLUS)
        resolved_theme = resolve_theme(theme_name)
        if resolved_theme not in self.available_themes:
            choices = ", ".join(sorted(self.available_themes))
            raise ValueError(f"unknown theme {theme_name!r}; available themes: {choices}")
        self.theme = resolved_theme
        self.scheduler = scheduler
        if getattr(scheduler, "is_demo", False):
            self.sub_title = "accelerated synthetic trace"
        self.snapshot = scheduler.snapshot()
        self.selected_job: int | None = None
        self.selected_wrapper: int | None = None
        self.filter_text = ""
        self.compact = False
        self._tables_configured_for: bool | None = None
        self.activity: deque[str] = deque(maxlen=50)
        self._last_phase_event = 0
        self._seen_failure_ids: set[int] = set()
        self._record_activity(
            f"queue ready · {len(self.snapshot.queue)} waiting · "
            f"{len(self.snapshot.running)} active",
            "cyan",
        )

    def compose(self) -> ComposeResult:
        yield Static(id="health-bar")
        yield Input(placeholder="Filter by project, PR, author, or title", id="filter-input")
        with Vertical(id="reviews-pane"):
            yield Static(
                "ACTIVE REVIEWS",
                id="reviews-heading",
                classes="panel-title",
            )
            yield DataTable(id="review-table", cursor_type="row", zebra_stripes=True)
        with HistoryLayout():
            with Vertical(id="queue-pane"):
                yield Static(
                    "WAITING  ·  ←/→ changes selected PR priority",
                    id="queue-heading",
                    classes="panel-title",
                )
                yield WaitingTable(id="queue-table", cursor_type="row", zebra_stripes=True)
            yield HistoryDivider()
            with Vertical(id="activity-pane"):
                yield Static(id="detail")
                with VerticalScroll(id="activity-scroll"):
                    yield Static(id="activity-feed")
        yield Footer()

    def on_mount(self) -> None:
        self._set_compact(self.size.width < 100)
        self._configure_tables()
        self.query_one("#review-table", DataTable).show_header = False
        self.refresh_view()
        self.query_one("#queue-table", DataTable).focus()
        self.set_interval(0.5, self.refresh_view)

    def on_resize(self, event: events.Resize) -> None:
        self._set_compact(event.size.width < 100)
        self._size_review_panel(screen_height=event.size.height)

    def _size_review_panel(self, *, screen_height: int | None = None) -> None:
        if not self.query("#reviews-pane"):
            return
        # Keep room for the health bar, footer, waiting rows, divider and history.
        reserved = 4 + 8 + 1 + 2
        if self.query_one("#filter-input").has_class("visible"):
            reserved += 3
        rows = max(1, len(self.snapshot.wrappers))
        self.query_one("#reviews-pane").styles.height = min(
            rows + 2, max(3, (screen_height or self.size.height) - reserved)
        )

    def _set_compact(self, compact: bool) -> None:
        changed = self.compact != compact
        self.compact = compact
        self.screen.set_class(compact, "narrow")
        if changed and self.is_mounted:
            self._configure_tables()
            self.refresh_view()

    def _configure_tables(self) -> None:
        if self._tables_configured_for == self.compact:
            return
        queue = self.query_one("#queue-table", DataTable)
        reviews = self.query_one("#review-table", DataTable)
        queue.clear(columns=True)
        reviews.clear(columns=True)
        if self.compact:
            for label, width in (
                ("Prio", 6),
                ("R", 5),
                ("Project / PR", 18),
                ("Approvals", 14),
                ("Age", 6),
            ):
                queue.add_column(label, width=width)
            queue.add_column("Title")
            for label, width in (
                ("", 3),
                ("R", 7),
                ("Project / PR", 18),
                ("Approvals", 14),
                ("Progress", 26),
            ):
                reviews.add_column(label, width=width)
            reviews.add_column("Title")
        else:
            for label, width in (
                ("Prio", 6),
                ("Reviewed", 9),
                ("Project / PR", 19),
                ("Approvals", 14),
                ("Author", 15),
                ("Age", 7),
            ):
                queue.add_column(label, width=width)
            queue.add_column("Title")
            for label, width in (
                ("", 3),
                ("Reviewed", 10),
                ("Project / PR", 19),
                ("Approvals", 14),
                ("Author", 15),
                ("Progress", 34),
            ):
                reviews.add_column(label, width=width)
            reviews.add_column("Title")
        self._tables_configured_for = self.compact

    def _record_activity(self, message: str, color: str = "") -> None:
        style = color or "dim"
        self.activity.appendleft(
            f"[dim]{self._now().strftime('%H:%M')}[/]  [{style}]{escape(message)}[/]"
        )

    def _track_activity(self, previous: object, current: object) -> None:
        for event in current.phase_events:
            if event.id > self._last_phase_event:
                self.activity.appendleft(
                    f"[dim]{parse_time(event.started_at).astimezone().strftime('%H:%M')}[/]  "
                    f"[cyan]{escape(f'{event.project}#{event.number} · {event.phase}')}[/]"
                )
                self._last_phase_event = event.id
        old_queue = {item.job_id: item for item in previous.queue}
        new_queue = {item.job_id: item for item in current.queue}
        for job_id, item in new_queue.items():
            if job_id in old_queue:
                continue
            if item.review_count:
                self._record_activity(
                    f"↻ {item.project}#{item.number} returned after author changes · "
                    f"{_reviews(item.review_count)}",
                    "magenta",
                )
            else:
                self._record_activity(f"+ {item.project}#{item.number} joined the queue", "cyan")

        old_runs = {run.job_id: run for run in previous.running}
        new_runs = {run.job_id: run for run in current.running}
        for job_id, run in new_runs.items():
            if job_id not in old_runs:
                self._record_activity(
                    f"▶ {run.project}#{run.number} started review {run.review_count + 1}",
                    "cyan",
                )
        for job_id, run in old_runs.items():
            if job_id in new_runs:
                continue
            wrapper = next(
                (
                    item
                    for item in current.wrappers
                    if item.repo == run.repo and item.number == run.number
                ),
                None,
            )
            if wrapper and wrapper.state == "complete":
                self._record_activity(
                    f"✓ {run.project}#{run.number} completed · {_reviews(wrapper.review_count)}",
                    "green",
                )
            elif (
                wrapper
                and wrapper.state == "failed"
                and not any(failure.job_id == job_id for failure in current.failures)
            ):
                self._record_activity(
                    f"× {run.project}#{run.number} failed · "
                    f"{wrapper.cleanup_error or 'press l for the launcher log'}",
                    "red",
                )
            elif wrapper and wrapper.state == "ineligible":
                self._record_activity(
                    f"– {run.project}#{run.number} skipped · "
                    f"{wrapper.cleanup_error or 'ineligible'}",
                    "yellow",
                )
        for failure in reversed(current.failures):
            if failure.job_id not in self._seen_failure_ids:
                message = f"× {failure.project}#{failure.number} failed · {failure.error}"
                self.activity.appendleft(
                    f"[dim]{parse_time(failure.finished_at).astimezone().strftime('%H:%M')}[/] "
                    f"[red]{escape(message)}[/]"
                )
                self._seen_failure_ids.add(failure.job_id)

    def _filtered_queue(self) -> list[QueueItem]:
        needle = self.filter_text.strip().lower()
        if not needle:
            return list(self.snapshot.queue)
        return [
            item
            for item in self.snapshot.queue
            if needle
            in " ".join(
                (
                    item.project,
                    item.repo,
                    str(item.number),
                    item.author,
                    item.title,
                    item.status,
                )
            ).lower()
        ]

    def refresh_view(self) -> None:
        if not self.query("#health-bar"):
            return  # A queued timer callback can arrive after the screen unmounts.
        next_snapshot = self.scheduler.snapshot()
        self._track_activity(self.snapshot, next_snapshot)
        self.snapshot = next_snapshot
        self._refresh_header()
        self._refresh_queue()
        self._refresh_reviews()
        self._refresh_detail()

    def _now(self) -> datetime:
        now = getattr(self.scheduler, "now", None)
        return now() if callable(now) else utc_now()

    def _refresh_header(self) -> None:
        stale = [health for health in self.snapshot.projects if health.stale]
        demo_status = getattr(self.scheduler, "demo_status", None)
        if callable(demo_status):
            source = f"[b magenta]◆ {escape(demo_status())}[/]"
        else:
            gh = "[red]● gh stale[/]" if stale else "[green]● gh healthy[/]"
            polls = [
                health.last_poll_at for health in self.snapshot.projects if health.last_poll_at
            ]
            last_poll = _age(max(polls), now=self._now()) if polls else "never"
            source = f"[b]{gh}[/]  last poll {last_poll}"
        mode = {
            "paused": "[yellow]PAUSED[/]",
            "manual": "[cyan]MANUAL[/]",
            "active": "[green]ACTIVE[/]",
        }[self.snapshot.mode]
        reason = self.snapshot.dispatch_blocked_reason
        suffix = f"  •  [yellow]{escape(reason)}[/]" if reason else ""
        self.query_one("#health-bar", Static).update(
            f"{source}  •  running "
            f"[b]{len(self.snapshot.running)}/{self.snapshot.max_running}[/]  •  wrappers "
            f"[b]{len(self.snapshot.wrappers)}/{self.snapshot.max_wrappers}[/]  •  free "
            f"[b]{self.snapshot.free_bytes / 1024**3:.0f} GiB[/]  •  "
            f"theme [b]{escape(self.theme)}[/]  •  {mode}{suffix}"
        )

    def _refresh_queue(self) -> None:
        table = self.query_one("#queue-table", DataTable)
        items = self._filtered_queue()
        hidden = len(self.snapshot.queue) - len(items)
        filter_status = f"  ·  {hidden} filtered" if hidden else ""
        hint = (
            "r enqueues selected head · ▶ enqueued"
            if self.snapshot.manual_only
            else "←/→ changes selected PR priority"
        )
        self.query_one("#queue-heading", Static).update(
            f"WAITING  {len(items)}{filter_status}  ·  {hint}"
        )
        ids = {item.job_id for item in items}
        if self.selected_job not in ids:
            self.selected_job = items[0].job_id if items else None
        rows: list[tuple[str, list[str]]] = []
        for item in items:
            reviewed = _reviews(item.review_count)
            if self.snapshot.manual_only and item.manual_enqueued:
                reviewed += " [cyan]▶[/]"
            if item.status == "debouncing":
                reviewed = f"{reviewed} [yellow]…[/]"
            values = [
                f"[b cyan]{item.score:+d}[/]",
                reviewed,
                f"{escape(item.project)}#{item.number}",
                _approvals(item),
            ]
            if not self.compact:
                values.append(escape(item.author))
            values.extend((_age(item.updated_at, now=self._now()), escape(item.title)))
            rows.append((str(item.job_id), values))
        self._sync_table(table, rows, self.selected_job)

    def _sync_table(
        self, table: DataTable, rows: list[tuple[str, list[str]]], selected: int | None
    ) -> None:
        keys = [key for key, _ in rows]
        current_keys = [row.key.value for row in table.ordered_rows]
        with self.batch_update(), table.prevent(DataTable.RowHighlighted):
            if keys != current_keys:
                scroll_x, scroll_y = table.scroll_x, table.scroll_y
                table.clear(columns=False)
                for key, values in rows:
                    table.add_row(*values, key=key)
                if selected is not None and str(selected) in keys:
                    table.move_cursor(row=keys.index(str(selected)), scroll=False)
                # Restore after DataTable has recalculated dimensions and handled
                # its deferred cursor scrolling. Refresh must not move the viewport.
                table.call_after_refresh(
                    table.scroll_to, x=scroll_x, y=scroll_y, animate=False, immediate=True
                )
            else:
                for key, values in rows:
                    previous = table.get_row(key)
                    for column, old, new in zip(
                        table.ordered_columns, previous, values, strict=True
                    ):
                        if old != new:
                            table.update_cell(key, column.key, new, update_width=column.auto_width)

    def _refresh_reviews(self) -> None:
        self._size_review_panel()
        table = self.query_one("#review-table", DataTable)
        running_by_path = {run.wrapper_path: run for run in self.snapshot.running}
        wrappers = sorted(self.snapshot.wrappers, key=lambda wrapper: wrapper.wrapper_id)
        ids = {wrapper.wrapper_id for wrapper in wrappers}
        if self.selected_wrapper not in ids:
            self.selected_wrapper = wrappers[0].wrapper_id if wrappers else None
        rows: list[tuple[str, list[str]]] = []
        active_count = sum(wrapper.path in running_by_path for wrapper in wrappers)
        self.query_one("#reviews-heading", Static).update(
            f"ACTIVE REVIEWS  {active_count}/{self.snapshot.max_running}  ·  "
            f"RETAINED  {len(wrappers)}/{self.snapshot.max_wrappers}"
        )
        for wrapper in wrappers:
            run = running_by_path.get(wrapper.path)
            review_count = run.review_count if run else wrapper.review_count
            if run:
                marker = "[b green]●[/]"
                reviewed = f"{_reviews(review_count)}  [b cyan]#{review_count + 1}[/]"
                # Keep the active step visible in the narrow table; details show the full path.
                phase = _phase(
                    run.phase.rsplit(" › ", 1)[-1] or run.status,
                    width=6 if self.compact else 10,
                )
                if run.phase and run.status == "cancelling":
                    phase = f"[yellow]cancelling[/] · {phase}"
                if run.phase_started_at:
                    phase += f" [dim]{_age(run.phase_started_at, now=self._now())}[/]"
            elif wrapper.state == "interrupted":
                marker = "[yellow]↻[/]"
                reviewed = _reviews(review_count)
                phase = "[yellow]restart pending[/]"
            elif wrapper.state == "ineligible":
                marker = "[yellow]–[/]"
                reviewed = _reviews(review_count)
                phase = "[yellow]skipped[/]"
            elif wrapper.cleanup_error or wrapper.state in {"failed", "cleanup_failed"}:
                marker = "[b red]×[/]"
                phase = f"[red]{escape(wrapper.state)}[/]  ·  press r to retry"
            elif review_count:
                marker = "[b green]✓[/]"
                reviewed = _reviews(review_count)
                phase = f"[green]complete[/]  ·  {_reviews(review_count)}"
            else:
                marker = "[dim]·[/]"
                reviewed = _reviews(review_count)
                phase = escape(wrapper.state)
            if not run and (wrapper.cleanup_error or wrapper.state in {"failed", "cleanup_failed"}):
                reviewed = _reviews(review_count)
            if wrapper.pinned:
                marker = "[b yellow]◆[/]"
            values = [
                marker,
                reviewed,
                f"{escape(wrapper.project)}#{wrapper.number}",
                _approvals(wrapper),
            ]
            if not self.compact:
                values.append(escape(run.author if run else wrapper.author))
            values.extend((phase, escape(run.title if run else wrapper.title)))
            rows.append((str(wrapper.wrapper_id), values))
        self._sync_table(table, rows, self.selected_wrapper)

    def _selected_item(self) -> QueueItem | None:
        return next(
            (item for item in self.snapshot.queue if item.job_id == self.selected_job), None
        )

    def _selected_wrapper(self) -> WrapperView | None:
        return next(
            (
                wrapper
                for wrapper in self.snapshot.wrappers
                if wrapper.wrapper_id == self.selected_wrapper
            ),
            None,
        )

    def _review_focused(self) -> bool:
        return self.query_one("#review-table", DataTable).has_focus

    def _refresh_detail(self) -> None:
        item = None if self._review_focused() else self._selected_item()
        detail = self.query_one("#detail", Static)
        activity = self.query_one("#activity-feed", Static)
        if item is not None:
            reason = "push quiet period" if item.status == "debouncing" else "eligible for dispatch"
            if self.snapshot.manual_only:
                reason = (
                    "manually enqueued" if item.manual_enqueued else "press r to enqueue this head"
                )
            if self.snapshot.paused:
                reason += " · dispatch paused"
            detail.update(
                f"[b]SELECTED[/]  ·  {escape(item.repo)}#{item.number}  "
                f"@{escape(item.author)}  "
                f"[b cyan]score {item.score:+d}[/] = project {item.project_priority:+d} + "
                f"author {item.author_priority:+d} + PR {item.pr_priority:+d}  •  "
                f"reviewed {_reviews(item.review_count)}  •  {reason}  •  "
                f"head {escape(item.head_sha[:12])}"
            )
        else:
            wrapper = self._selected_wrapper()
            if wrapper is None:
                detail.update("[dim]The queue is empty.[/]")
            else:
                active = next(
                    (run for run in self.snapshot.running if run.wrapper_path == wrapper.path),
                    None,
                )
                detail.update(
                    f"[b]SELECTED[/]  ·  {escape(wrapper.repo)}#{wrapper.number}  "
                    f"@{escape(wrapper.author)}  "
                    f"[b]{escape(active.status if active else wrapper.state)}[/]  •  "
                    "reviewed "
                    f"{_reviews(active.review_count if active else wrapper.review_count)}  •  "
                    f"{_size(wrapper.size_bytes)}  •  "
                    f"{'pinned' if wrapper.pinned else 'recyclable when safe'}  •  "
                    f"{escape(wrapper.path)}"
                )
                if active and active.phase:
                    detail.update(
                        f"[b]{escape(wrapper.project)}#{wrapper.number}[/] · "
                        f"{escape(active.status)} · {escape(active.phase)} · "
                        f"{_age(active.phase_started_at, now=self._now())} in phase"
                    )
                if wrapper.cleanup_error:
                    detail.update(
                        Text.assemble(
                            (f"{wrapper.project}#{wrapper.number}", "bold"),
                            " · ",
                            ("l: details/log", "dim"),
                            " · ",
                            wrapper.cleanup_error,
                        )
                    )
        # Match Rich's escaping with its parser; Textual also treats command
        # arguments containing brackets and equals signs as markup.
        activity.update(Text.from_markup("\n".join(self.activity)))

    def action_show_log(self) -> None:
        wrapper = self._selected_wrapper()
        if wrapper is None:
            self.notify("Select a retained review to view its log.")
            return
        active = next(
            (run for run in self.snapshot.running if run.wrapper_path == wrapper.path), None
        )
        phase = active.phase if active else ""
        self.push_screen(
            LogModal(
                f"{wrapper.repo}#{wrapper.number} · {wrapper.state}",
                wrapper.cleanup_error or phase,
                self.scheduler.wrapper_log_tail(wrapper.wrapper_id),
            )
        )

    def action_grow_history(self) -> None:
        self.query_one(HistoryLayout).set_history_height(
            self.query_one("#activity-pane").size.height + 1
        )

    def action_shrink_history(self) -> None:
        self.query_one(HistoryLayout).set_history_height(
            self.query_one("#activity-pane").size.height - 1
        )

    def on_data_table_row_highlighted(self, event: DataTable.RowHighlighted) -> None:
        if event.row_key is None:
            return
        identifier = int(str(event.row_key.value))
        if event.data_table.id == "queue-table":
            self.selected_job = identifier
        elif event.data_table.id == "review-table":
            self.selected_wrapper = identifier
        self._refresh_detail()

    def on_descendant_focus(self, event: events.DescendantFocus) -> None:
        if event.widget.id in {"queue-table", "review-table"} and self.query("#detail"):
            self._refresh_detail()

    def on_input_changed(self, event: Input.Changed) -> None:
        if event.input.id == "filter-input":
            self.filter_text = event.value
            self.refresh_view()

    def on_input_submitted(self, event: Input.Submitted) -> None:
        if event.input.id == "filter-input":
            self.query_one("#queue-table", DataTable).focus()

    def _move(self, delta: int) -> None:
        table = self.query_one(
            "#review-table" if self._review_focused() else "#queue-table",
            DataTable,
        )
        if not table.row_count:
            return
        table.move_cursor(row=max(0, min(table.row_count - 1, table.cursor_row + delta)))
        table.focus()

    def action_cursor_up(self) -> None:
        self._move(-1)

    def action_cursor_down(self) -> None:
        self._move(1)

    def action_raise_priority(self) -> None:
        if self._review_focused():
            return
        item = self._selected_item()
        if item:
            self.scheduler.adjust_priority(item, 10)
            self._record_activity(
                f"↑ {item.project}#{item.number} priority {item.pr_priority + 10:+d}",
                "yellow",
            )
            self.refresh_view()

    def action_lower_priority(self) -> None:
        if self._review_focused():
            return
        item = self._selected_item()
        if item:
            self.scheduler.adjust_priority(item, -10)
            self._record_activity(
                f"↓ {item.project}#{item.number} priority {item.pr_priority - 10:+d}",
                "yellow",
            )
            self.refresh_view()

    def action_edit_priorities(self) -> None:
        if self._review_focused():
            return
        item = self._selected_item()
        if item:
            self.push_screen(PriorityModal(item), self._priorities_edited)

    def _priorities_edited(self, result: object | None) -> None:
        item = self._selected_item()
        if item and isinstance(result, PriorityValues):
            self.scheduler.set_priorities(
                item,
                project_priority=result.project,
                author_priority=result.author,
                pr_priority=result.pr,
            )
            self._record_activity(
                f"↕ {item.project}#{item.number} priorities set · "
                f"project {result.project:+d} · author {result.author:+d} · "
                f"PR {result.pr:+d}",
                "yellow",
            )
            self.refresh_view()

    def action_toggle_pause(self) -> None:
        paused = self.scheduler.toggle_pause()
        self._record_activity(
            "dispatch paused" if paused else f"dispatch resumed · {self.scheduler.snapshot().mode}",
            "yellow" if paused else "green",
        )
        self.refresh_view()

    def action_cycle_mode(self) -> None:
        mode = self.scheduler.cycle_mode()
        self._record_activity(f"dispatch mode · {mode}", "cyan")
        self.refresh_view()

    def action_retry(self) -> None:
        prefer_wrapper = self._review_focused() or self._selected_item() is None
        item = None if prefer_wrapper else self._selected_item()
        wrapper = self._selected_wrapper()
        try:
            if item:
                self.scheduler.retry(item)
            elif prefer_wrapper and wrapper:
                self.scheduler.retry_wrapper(wrapper.wrapper_id)
        except Exception as error:
            self.notify(str(error), severity="error")
        self.refresh_view()

    def action_cancel(self) -> None:
        prefer_wrapper = self._review_focused() or self._selected_item() is None
        item = None if prefer_wrapper else self._selected_item()
        run = None
        if item:
            job_id = item.job_id
            label = f"{item.repo}#{item.number}"
        else:
            wrapper = self._selected_wrapper()
            run = next(
                (
                    candidate
                    for candidate in self.snapshot.running
                    if wrapper and candidate.wrapper_path == wrapper.path
                ),
                None,
            )
            if run is None:
                return
            job_id = run.job_id
            label = f"{run.repo}#{run.number}"
        self.push_screen(
            ConfirmModal(
                f"Cancel {label}? Running work will receive TERM.", accept_label="Cancel review"
            ),
            lambda accepted: self._cancel_confirmed(job_id, accepted),
        )

    def _cancel_confirmed(self, job_id: int, accepted: bool) -> None:
        if not accepted:
            return
        try:
            self.scheduler.cancel(job_id)
        except Exception as error:
            self.notify(str(error), severity="error")
        self.refresh_view()

    def action_pin_wrapper(self) -> None:
        wrapper = self._selected_wrapper()
        if wrapper:
            try:
                pinned = self.scheduler.toggle_wrapper_pin(wrapper.wrapper_id)
                self.notify(f"wrapper {'pinned' if pinned else 'unpinned'}")
            except Exception as error:
                self.notify(str(error), severity="error")
            self.refresh_view()

    def action_evict_wrapper(self) -> None:
        wrapper = self._selected_wrapper()
        if not wrapper:
            return
        self.push_screen(
            ConfirmModal(
                f"Run the safe cleanup preflight and recycle\n{wrapper.path}?",
                accept_label="Recycle wrapper",
            ),
            lambda accepted: self._evict_confirmed(wrapper.wrapper_id, accepted),
        )

    def _evict_confirmed(self, wrapper_id: int, accepted: bool) -> None:
        if accepted:
            self._spawn(self.scheduler.recycle_wrapper(wrapper_id), "wrapper recycled")

    def action_watch_pr(self) -> None:
        self.push_screen(WatchModal(), self._watch_submitted)

    def _watch_submitted(self, result: object | None) -> None:
        if isinstance(result, str):
            self._spawn(self.scheduler.add_manual(result), "PR included and future heads watched")

    def action_ignore_pr(self) -> None:
        item = None if self._review_focused() else self._selected_item()
        wrapper = self._selected_wrapper() if item is None else None
        if item is not None:
            repo, number = item.repo, item.number
        elif wrapper is not None:
            repo, number = wrapper.repo, wrapper.number
        else:
            return
        self.push_screen(
            ConfirmModal(
                f"Permanently ignore {repo}#{number}?\n"
                "Waiting work is removed and future pushed heads stay excluded. "
                "A running review is allowed to finish. Press n to include it again.",
                accept_label="Ignore PR",
            ),
            lambda accepted: self._ignore_confirmed(repo, number, accepted),
        )

    def _ignore_confirmed(self, repo: str, number: int, accepted: bool) -> None:
        if not accepted:
            return
        try:
            self.scheduler.ignore_pr(repo, number)
            self._record_activity(f"− {repo}#{number} permanently ignored", "yellow")
            self.notify(f"{repo}#{number} ignored; press n to include it again")
        except Exception as error:
            self.notify(str(error), severity="error")
        self.selected_job = None
        self.refresh_view()

    def _spawn(self, awaitable: object, success: str) -> None:
        async def run() -> None:
            try:
                await awaitable
                self.notify(success)
            except Exception as error:
                self.notify(str(error), severity="error")
            self.refresh_view()

        asyncio.create_task(run())

    def action_show_filter(self) -> None:
        field = self.query_one("#filter-input", Input)
        field.add_class("visible")
        field.focus()
        self._size_review_panel()

    def action_clear_filter(self) -> None:
        field = self.query_one("#filter-input", Input)
        if field.has_focus or self.filter_text:
            self.filter_text = ""
            field.value = ""
            field.remove_class("visible")
            self.query_one("#queue-table", DataTable).focus()
            self.refresh_view()

    def action_refresh_github(self) -> None:
        self.scheduler.request_refresh()
        if getattr(self.scheduler, "is_demo", False):
            self.notify("next synthetic trace event injected")
        else:
            self.notify("GitHub refresh requested")

    def _set_demo_speed(self, speed: float) -> None:
        setter = getattr(self.scheduler, "set_demo_speed", None)
        if callable(setter):
            setter(speed)
            self.notify(f"demo clock set to {speed:g}×")
            self.refresh_view()

    def action_demo_speed_1(self) -> None:
        self._set_demo_speed(1)

    def action_demo_speed_2(self) -> None:
        self._set_demo_speed(60)

    def action_demo_speed_3(self) -> None:
        self._set_demo_speed(300)

    def action_demo_speed_4(self) -> None:
        self._set_demo_speed(1800)

    def action_toggle_demo_clock(self) -> None:
        toggle = getattr(self.scheduler, "toggle_demo_clock", None)
        if callable(toggle):
            paused = toggle()
            self.notify(f"demo clock {'paused' if paused else 'resumed'}")
            self.refresh_view()

    def action_restart_demo(self) -> None:
        restart = getattr(self.scheduler, "restart_demo", None)
        if callable(restart):
            restart()
            self.selected_job = None
            self.selected_wrapper = None
            self.notify("synthetic trace restarted")
            self.refresh_view()

    def action_cycle_theme(self) -> None:
        current = resolve_theme(self.theme)
        try:
            index = FAVORITE_THEMES.index(current)
        except ValueError:
            index = -1
        self.theme = FAVORITE_THEMES[(index + 1) % len(FAVORITE_THEMES)]
        self.notify(f"theme: {self.theme}")
        self.refresh_view()

    def action_show_help(self) -> None:
        self.push_screen(HelpModal())

    def action_quit(self) -> None:
        if self.scheduler.snapshot().running:
            self.push_screen(
                ConfirmModal(
                    "Stop active reviews and quit? They will retry on restart "
                    "if the PR revision is unchanged.",
                    accept_label="Stop and quit",
                ),
                lambda accepted: self.exit() if accepted else None,
            )
        else:
            self.exit()
