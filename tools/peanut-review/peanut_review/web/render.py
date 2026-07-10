"""Render a peanut-review session as a single HTML page."""
from __future__ import annotations

import html
import json
import re
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

from pygments import highlight as _pyg_highlight
from pygments.formatters import HtmlFormatter
from pygments.lexers import TextLexer, get_lexer_for_filename
from pygments.util import ClassNotFound

from ..models import Comment, Note, Session
from ..session import GLOBAL_FILE
from .diff import DiffLine, FileDiff

ASSETS_DIR = Path(__file__).parent / "assets"

# Visual labels for the keyboard chord prefixes shown in the sidebar
# shortcuts. Must match the corresponding constants in `assets/app.js`
# (PREFIX_LABEL / COMPOSER_PREFIX_LABEL) — change both together.
PREFIX_LABEL = "␣"
COMPOSER_PREFIX_LABEL = f"⌃{PREFIX_LABEL}"

# Peanut-emoji favicon, inlined as an SVG data URI so we ship no binary asset.
_FAVICON_SVG = (
    "<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 64 64'>"
    "<text y='54' font-size='56'>🥜</text>"
    "</svg>"
)
FAVICON_HREF = "data:image/svg+xml;utf8," + _FAVICON_SVG.replace("#", "%23").replace('"', "%22")
THEME_BOOTSTRAP = """<script>
(function () {
  try {
    var theme = localStorage.getItem("pr.theme");
    if (theme === "dark-plus" || theme === "light") {
      document.documentElement.dataset.theme = theme;
    }
  } catch (e) {}
})();
</script>"""
THEME_TOGGLE_BUTTON = (
    '<button id="theme-toggle" class="theme-toggle" type="button" '
    'title="Color theme: system">theme: system</button>'
)

SESSION_STATE_LABELS = {
    "init": "ready",
    "round": "in review",
    "complete": "done",
    "aborted": "aborted",
}

DIFF_CONTEXT_LINES = 32
FOLD_MIN_OMITTED_LINES = 8
MAX_INITIAL_CHANGED_BLOCK_LINES = 100
LARGE_FILE_LINE_THRESHOLD = 2_000
LARGE_FILE_CHANGED_THRESHOLD = 1_500
LARGE_FILE_CONTEXT_LINES = 8
MAX_INITIAL_LARGE_FILE_LINES = 320
MAX_HIGHLIGHT_FILE_LINES = 2_000
MAX_HIGHLIGHT_CHANGED_LINES = 1_500
MAX_HIGHLIGHT_RENDERED_LINES = 1_200
MAX_HIGHLIGHT_RENDERED_BYTES = 200_000
MAX_EMBEDDED_FOLD_LINES = 300
MAX_EMBEDDED_FOLD_BYTES = 120_000


def _relative_time_label(timestamp: str, *, now: datetime | None = None) -> str:
    try:
        dt = datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
    except ValueError:
        return ""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    if now is None:
        now = datetime.now(timezone.utc)
    elif now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    seconds = int((now - dt).total_seconds())
    if seconds < 45:
        return "just now"
    if seconds < 90:
        return "1 minute ago"
    minutes = seconds // 60
    if minutes < 60:
        return f"{minutes} minutes ago"
    if minutes < 90:
        return "1 hour ago"
    hours = minutes // 60
    if hours < 24:
        return f"{hours} hour{'s' if hours != 1 else ''} ago"
    if hours < 48:
        return "yesterday"
    days = hours // 24
    if days < 30:
        return f"{days} days ago"
    months = max(1, days // 30)
    if days < 365:
        return f"{months} month{'s' if months != 1 else ''} ago"
    years = max(1, days // 365)
    return f"{years} year{'s' if years != 1 else ''} ago"


def _time_tag(timestamp: str, *, extra_class: str = "") -> str:
    if not timestamp:
        return ""
    label = _relative_time_label(timestamp)
    if not label:
        return ""
    ts = html.escape(timestamp)
    classes = "comment-time"
    if extra_class:
        classes += f" {html.escape(extra_class)}"
    return (
        f'<time class="{classes}" datetime="{ts}" '
        f'title="{ts}">{html.escape(label)}</time>'
    )


KILLABLE_PROCESS_STATES = {"launching", "running"}


def _session_state_label(state: str) -> str:
    return SESSION_STATE_LABELS.get(state, state.replace("-", " "))


def _github_change_title(session: Session) -> str:
    if session.github and session.github.title:
        return session.github.title.strip()
    return ""


def _change_label(session: Session) -> str:
    return _github_change_title(session) or f"{session.base_ref} … {session.topic_ref}"


def _github_pr_label(session: Session) -> str:
    if not session.github:
        return ""
    label = session.github.repo
    if session.github.number:
        label = f"{label}#{session.github.number}"
    return label


def _lexer_for(path: str):
    try:
        return get_lexer_for_filename(path, stripall=False)
    except ClassNotFound:
        return TextLexer()


def _highlight_file(path: str, lines: list[str]) -> list[str]:
    """Syntax-highlight a rendered slice of file contents line-by-line.

    We highlight all rendered lines once (for consistent tokenization across
    multi-line constructs like docstrings) and split back into per-line HTML.
    """
    if not lines:
        return []
    full = "\n".join(lines)
    formatter = HtmlFormatter(nowrap=True, classprefix="hl-")
    out = _pyg_highlight(full, _lexer_for(path), formatter)
    out_lines = out.split("\n")
    while len(out_lines) < len(lines):
        out_lines.append("")
    return out_lines[: len(lines)]


_SLUG_RE = re.compile(r"[^a-zA-Z0-9]+")


def _file_anchor(path: str) -> str:
    """HTML id for a file section. Sidebar links jump here."""
    return "f-" + _SLUG_RE.sub("-", path).strip("-")


def _organize_threads(comments: list[Comment]) -> list[list[Comment]]:
    """Group comments into threads: [parent, *replies] each in timestamp order.

    Top-level parents come in timestamp order. Replies attach to their
    `reply_to` parent. Soft-deleted comments are excluded everywhere.
    Orphan replies (parent missing or also deleted) are dropped — there's no
    sensible place to render them.
    """
    live = [c for c in comments if not c.deleted]
    parents = [c for c in live if not c.reply_to]
    parents.sort(key=lambda c: c.timestamp)
    by_parent: dict[str, list[Comment]] = defaultdict(list)
    for c in live:
        if c.reply_to:
            by_parent[c.reply_to].append(c)
    threads: list[list[Comment]] = []
    for p in parents:
        replies = sorted(by_parent.get(p.id, []), key=lambda c: c.timestamp)
        threads.append([p, *replies])
    return threads


def _group_threads_by_anchor(
    comments: list[Comment],
) -> dict[tuple[str, int], list[list[Comment]]]:
    """Group threads by (file, anchor_line) for inline rendering.

    Globals (file=="") are excluded — they render in their own top-level
    section. For range comments (end_line different from line), anchor the
    thread at end_line so the visual lines up with where the drag ended.
    """
    out: dict[tuple[str, int], list[list[Comment]]] = defaultdict(list)
    for thread in _organize_threads(comments):
        parent = thread[0]
        if parent.file == GLOBAL_FILE:
            continue
        anchor = (parent.end_line
                  if (parent.end_line is not None and parent.end_line != parent.line)
                  else parent.line)
        out[(parent.file, anchor)].append(thread)
    return out


def _render_note_entry(note: Note) -> str:
    nid = html.escape(note.id)
    agent = html.escape(note.author or "unknown")
    ts = _time_tag(note.timestamp, extra_class="ix-time")
    body = html.escape(note.body)
    return (
        f'<div class="activity-entry note-entry" data-note-id="{nid}" '
        f'data-key="note/{nid}" data-kind="note">'
        f'<div class="ix-q"><span class="ix-meta">'
        f'<span class="agent">{agent}</span>'
        f'<span class="qid mono">{nid}</span>'
        f'<span class="kind">note</span>'
        f'{ts}'
        f'</span><pre class="ix-body note-body">{body}</pre></div>'
        f'</div>'
    )


def _render_inbox_entry(entry: dict) -> str:
    agent = html.escape(entry.get("agent", ""))
    qid = html.escape(entry.get("id", ""))
    qts = _time_tag(entry.get("timestamp", ""), extra_class="ix-time")
    qtext = html.escape(entry.get("question", ""))
    reply = entry.get("reply")
    row = [
        f'<div class="activity-entry ix-entry" data-qid="{qid}" '
        f'data-key="{agent}/{qid}" '
        f'data-kind="question" data-replied="{1 if reply else 0}">',
        f'<div class="ix-q"><span class="ix-meta">'
        f'<span class="agent">{agent}</span>'
        f'<span class="qid mono">{qid}</span>'
        f'<span class="kind">question</span>'
        f'{qts}'
        f'</span><pre class="ix-body">{qtext}</pre></div>',
    ]
    if reply:
        ats = _time_tag(reply.get("timestamp", ""), extra_class="ix-time")
        aby = html.escape(reply.get("answered_by", "orchestrator"))
        atext = html.escape(reply.get("answer", ""))
        row.append(
            f'<div class="ix-r"><span class="ix-meta">'
            f'<span class="agent">↳ {aby}</span>'
            f'{ats}'
            f'</span><pre class="ix-body">{atext}</pre></div>'
        )
    else:
        row.append('<div class="ix-r pending"><span class="ix-meta">'
                   '<span class="agent">↳ awaiting reply…</span>'
                   '</span></div>')
    row.append('</div>')
    return "".join(row)


def render_inbox_section(transcript: list[dict], notes: list[Note] | None = None) -> str:
    """Bottom-of-page section showing agent activity.

    Notes are read-only free-form activity; questions still use the existing
    ask/reply blocking flow. The browser polls both streams and reconciles
    this list in place.
    """
    notes = notes or []
    rows: list[tuple[str, str]] = []
    rows.extend((n.timestamp, _render_note_entry(n)) for n in notes)
    rows.extend((entry.get("timestamp", ""), _render_inbox_entry(entry))
                for entry in transcript)
    rows.sort(key=lambda pair: pair[0])

    if not rows:
        body = (
            '<p class="muted">No agent activity yet. Agent notes and '
            '<code>peanut-review ask</code> questions appear here.</p>'
        )
    else:
        body = "".join(row for _, row in rows)
    return (
        '<section class="inbox-section" id="inbox">'
        '<h2>Agent activity</h2>'
        '<p class="hint muted">Free-form agent notes plus the Agent help inbox '
        'for blocked agents (<code>peanut-review note</code>, '
        '<code>ask</code> / <code>reply</code>). Read-only here.</p>'
        f'<div class="ix-list" id="inbox-list">{body}</div>'
        '</section>'
    )


def _render_global_section(comments: list[Comment]) -> str:
    """Top-of-page block for high-level (file/line-less) comments.

    Always renders — the "Add Comment" button is reachable even with no
    existing high-level comments.
    """
    threads = [t for t in _organize_threads(comments) if t[0].file == GLOBAL_FILE]
    items = "".join(_render_thread(t) for t in threads)
    return (
        '<section class="global-section" id="global">'
        '<div class="global-header">'
        '<button id="add-global-btn" type="button">Add Comment</button>'
        '<h2>High-level feedback</h2>'
        '</div>'
        f'<div class="global-comments" id="global-comments">{items}</div>'
        '</section>'
    )


def _render_comment(c: Comment, *, is_reply: bool = False) -> str:
    """Render a single comment node.

    Resolve/Unresolve and Reply are thread-level actions (see _render_thread)
    and live below the last comment in the thread, not on each comment.
    Delete stays per-comment because soft-deleting one bad reply shouldn't
    nuke the whole thread.
    """
    classes = ["comment"]
    if is_reply:
        classes.append("reply")
    if c.stale:
        classes.append("stale")
    if c.resolved:
        classes.append("resolved")
    if c.edited_at:
        classes.append("edited")
    if not is_reply:
        classes.append("top-level")
    cid = html.escape(c.id)
    collapse_html = ""
    if not is_reply:
        expanded = not c.resolved
        label = "Collapse thread" if expanded else "Expand thread"
        icon = "▾" if expanded else "▸"
        collapse_html = (
            f'<button type="button" class="thread-collapse" '
            f'data-thread-collapse="{cid}" aria-expanded="{str(expanded).lower()}" '
            f'title="{label}"><span aria-hidden="true">{icon}</span></button>'
        )
    buttons = [
        f'<button data-edit="{cid}">Edit</button>',
        f'<button class="danger" data-delete="{cid}">Delete</button>',
    ]
    badges = []
    if c.end_line is not None and c.end_line != c.line:
        lo, hi = min(c.line, c.end_line), max(c.line, c.end_line)
        badges.append(f'<span class="round range">L{lo}–L{hi}</span>')
    if c.stale:
        badges.append('<span class="round">stale</span>')
    if c.resolved and not is_reply:
        badges.append('<span class="round resolved-badge">resolved</span>')
    # Replies don't carry their own severity — they inherit the thread's.
    sev_html = (
        ""
        if is_reply
        else f'<span class="sev {html.escape(c.severity)}">{html.escape(c.severity)}</span>'
    )
    category_html = ""
    if not is_reply and c.category != "comment":
        label = "approved" if c.category == "approve" else "blocking"
        category_html = (
            f'<span class="category {html.escape(c.category)}">'
            f'{html.escape(label)}</span>'
        )
    time_html = _time_tag(c.timestamp)
    edited_html = ""
    if c.edited_at:
        n = len(c.versions)
        title = f"edited by {c.edited_by or 'unknown'} at {c.edited_at}"
        if n:
            title += f" ({n} prior version{'s' if n != 1 else ''})"
        edited_html = (
            f'<button class="edited-badge" type="button" '
            f'data-history="{cid}" title="{html.escape(title)}">'
            f'edited</button>'
        )
    external_html = ""
    if c.external_url:
        # Tiny "↗ gh" link to view this comment on GitHub. Sits next to the
        # author/edit indicators in comment-meta. New tab so a midstream click
        # doesn't lose the local review state.
        external_html = (
            f'<a class="external-link" href="{html.escape(c.external_url)}" '
            f'target="_blank" rel="noopener" title="View on GitHub">↗ gh</a>'
        )
    return (
        f'<div class="{" ".join(classes)}" data-cid="{cid}">'
        f'<div class="comment-meta">'
        f'{collapse_html}'
        f'<span class="author">{html.escape(c.author or "unknown")}</span>'
        f'{time_html}'
        f'{sev_html}'
        f'{category_html}'
        f'{"".join(badges)}'
        f'{edited_html}'
        f'{external_html}'
        f'{"".join(buttons)}'
        f'</div>'
        f'<div class="comment-body">{html.escape(c.body)}</div>'
        f'</div>'
    )


def _collapsed_summary(reply_count: int) -> str:
    if reply_count == 1:
        return "comment hidden, 1 reply hidden"
    if reply_count > 1:
        return f"comment hidden, {reply_count} replies hidden"
    return "comment hidden"


def _render_thread(thread: list[Comment]) -> str:
    """Render a thread: parent comment, replies (inset), then thread actions.

    `thread` is [parent, *replies] from `_organize_threads`. Reply +
    Resolve/Unresolve sit at the bottom of every thread so they're always
    where you'd look after reading the last reply.
    """
    parent = thread[0]
    replies = thread[1:]
    parent_html = _render_comment(parent)
    replies_html = "".join(_render_comment(r, is_reply=True) for r in replies)
    pid = html.escape(parent.id)
    default_collapsed = "1" if parent.resolved else "0"
    if parent.resolved:
        toggle_btn = f'<button data-unresolve="{pid}">Unresolve</button>'
    else:
        toggle_btn = f'<button data-resolve="{pid}">Resolve</button>'
    reply_btn = (
        "" if parent.file == GLOBAL_FILE
        else f'<button class="reply-btn" data-reply-to="{pid}">Reply</button>'
    )
    actions = (
        f'<div class="thread-actions">'
        f'{reply_btn}'
        f'{toggle_btn}'
        f'</div>'
    )
    cls = "thread"
    if parent.resolved:
        cls += " resolved collapsed"
    summary = (
        '<div class="thread-collapsed-summary" data-collapse-summary>'
        f'{html.escape(_collapsed_summary(len(replies)))}'
        '</div>'
    )
    return (
        f'<div class="{cls}" data-thread-id="{pid}" '
        f'data-default-collapsed="{default_collapsed}">'
        f'{parent_html}{summary}{replies_html}{actions}</div>'
    )


def _thread_anchor_lines(
    path: str,
    threads_at_line: dict[tuple[str, int], list[list[Comment]]],
) -> set[int]:
    return {
        line
        for (thread_path, line) in threads_at_line
        if thread_path == path
    }


def _visible_line_ranges(
    fd: FileDiff,
    threads_at_line: dict[tuple[str, int], list[list[Comment]]],
) -> list[tuple[int, int]]:
    """Return half-open line-index ranges worth rendering for this file.

    Changed rows drive the primary windows. Existing comment anchors also get
    context windows so old review threads remain visible even when they are on
    unchanged lines outside the current diff hunks.
    """
    line_count = len(fd.lines)
    if line_count == 0:
        return []

    changed_anchor_indices = _changed_anchor_indices(fd.lines)
    anchor_indices = set(changed_anchor_indices)
    comment_anchor_indices: set[int] = set()
    comment_lines = _thread_anchor_lines(fd.path, threads_at_line)
    if comment_lines:
        for idx, dl in enumerate(fd.lines):
            if dl.new_lineno in comment_lines:
                comment_anchor_indices.add(idx)
                anchor_indices.add(idx)

    if not anchor_indices:
        return [(0, line_count)]

    if _is_large_file_diff(fd):
        return _large_file_visible_line_ranges(
            line_count, changed_anchor_indices, comment_anchor_indices,
        )

    return _ranges_for_anchors(anchor_indices, line_count, DIFF_CONTEXT_LINES)


def _is_large_file_diff(fd: FileDiff) -> bool:
    return (
        len(fd.lines) > LARGE_FILE_LINE_THRESHOLD
        or fd.additions + fd.deletions > LARGE_FILE_CHANGED_THRESHOLD
    )


def _range_line_count(ranges: list[tuple[int, int]]) -> int:
    return sum(end - start for start, end in ranges)


def _merge_ranges(
    ranges: list[tuple[int, int]],
    line_count: int,
    *,
    expand_edges: bool,
) -> list[tuple[int, int]]:
    if not ranges:
        return []
    ranges = sorted((max(0, s), min(line_count, e)) for s, e in ranges if s < e)
    if not ranges:
        return []

    merged: list[tuple[int, int]] = []
    for start, end in ranges:
        if not merged or start > merged[-1][1] + FOLD_MIN_OMITTED_LINES:
            merged.append((start, end))
            continue
        prev_start, prev_end = merged[-1]
        merged[-1] = (prev_start, max(prev_end, end))

    if expand_edges and merged[0][0] <= FOLD_MIN_OMITTED_LINES:
        merged[0] = (0, merged[0][1])
    if expand_edges and line_count - merged[-1][1] <= FOLD_MIN_OMITTED_LINES:
        merged[-1] = (merged[-1][0], line_count)
    return merged


def _truncate_ranges(
    ranges: list[tuple[int, int]],
    max_lines: int | None,
) -> list[tuple[int, int]]:
    if max_lines is None:
        return ranges
    remaining = max_lines
    out: list[tuple[int, int]] = []
    for start, end in ranges:
        if remaining <= 0:
            break
        length = end - start
        if length <= remaining:
            out.append((start, end))
            remaining -= length
            continue
        out.append((start, start + remaining))
        break
    return out


def _ranges_for_anchors(
    anchor_indices: set[int],
    line_count: int,
    context_lines: int,
    *,
    max_lines: int | None = None,
    expand_edges: bool = True,
) -> list[tuple[int, int]]:
    windows: list[tuple[int, int]] = []
    for idx in sorted(anchor_indices):
        start = max(0, idx - context_lines)
        end = min(line_count, idx + context_lines + 1)
        if start < end:
            windows.append((start, end))
    ranges = _merge_ranges(windows, line_count, expand_edges=expand_edges)
    return _truncate_ranges(ranges, max_lines)


def _large_file_visible_line_ranges(
    line_count: int,
    changed_anchor_indices: set[int],
    comment_anchor_indices: set[int],
) -> list[tuple[int, int]]:
    comment_ranges = _ranges_for_anchors(
        comment_anchor_indices,
        line_count,
        DIFF_CONTEXT_LINES,
        expand_edges=False,
    )
    remaining = max(
        0, MAX_INITIAL_LARGE_FILE_LINES - _range_line_count(comment_ranges),
    )
    changed_ranges = _ranges_for_anchors(
        changed_anchor_indices,
        line_count,
        LARGE_FILE_CONTEXT_LINES,
        max_lines=remaining,
        expand_edges=False,
    )
    ranges = _merge_ranges(
        [*comment_ranges, *changed_ranges],
        line_count,
        expand_edges=False,
    )
    if ranges:
        return ranges
    return [(0, min(line_count, MAX_INITIAL_LARGE_FILE_LINES))]


def _changed_anchor_indices(lines: list[DiffLine]) -> set[int]:
    out: set[int] = set()
    idx = 0
    while idx < len(lines):
        if lines[idx].kind == "context":
            idx += 1
            continue
        start = idx
        while idx < len(lines) and lines[idx].kind != "context":
            idx += 1
        end = idx
        block_len = end - start
        if block_len <= MAX_INITIAL_CHANGED_BLOCK_LINES:
            out.update(range(start, end))
        else:
            out.update(range(start, start + MAX_INITIAL_CHANGED_BLOCK_LINES))
    return out


def _line_span_label(lines: list[int | None]) -> str:
    nums = [n for n in lines if n is not None]
    if not nums:
        return ""
    if nums[0] == nums[-1]:
        return str(nums[0])
    return f"{nums[0]}-{nums[-1]}"


def _fold_payload_json(lines: list[DiffLine]) -> str:
    payload = [
        {
            "kind": dl.kind,
            "old_lineno": dl.old_lineno,
            "new_lineno": dl.new_lineno,
            "content": dl.content,
        }
        for dl in lines
    ]
    return json.dumps(payload, separators=(",", ":")).replace("</", "<\\/")


def _should_embed_fold_payload(lines: list[DiffLine]) -> bool:
    return (
        len(lines) <= MAX_EMBEDDED_FOLD_LINES
        and sum(len(dl.content) for dl in lines) <= MAX_EMBEDDED_FOLD_BYTES
    )


def _line_bounds(lines: list[int | None]) -> tuple[int | None, int | None]:
    nums = [n for n in lines if n is not None]
    if not nums:
        return None, None
    return nums[0], nums[-1]


def _render_fold_gap(
    lines: list[DiffLine],
    fold_id: str,
    *,
    file_path: str,
    start_index: int,
) -> str:
    count = len(lines)
    if count == 0:
        return ""
    kinds = {dl.kind for dl in lines}
    if kinds == {"context"}:
        label_kind = "unchanged"
    elif kinds == {"added"}:
        label_kind = "added"
    elif kinds == {"deleted"}:
        label_kind = "deleted"
    else:
        label_kind = "diff"
    label = f"{count} {label_kind} line{'s' if count != 1 else ''} hidden"
    old_span = _line_span_label([dl.old_lineno for dl in lines])
    new_span = _line_span_label([dl.new_lineno for dl in lines])
    old_start, old_end = _line_bounds([dl.old_lineno for dl in lines])
    new_start, new_end = _line_bounds([dl.new_lineno for dl in lines])
    title_parts = [label]
    if old_span:
        title_parts.append(f"old {old_span}")
    if new_span:
        title_parts.append(f"new {new_span}")
    title = html.escape(" | ".join(title_parts), quote=True)
    fold_id_html = html.escape(fold_id, quote=True)
    file_html = html.escape(file_path, quote=True)
    label_html = html.escape(label)
    end_index = start_index + count
    data_attrs = [
        f'data-folded-lines="{count}"',
        f'data-fold-file="{file_html}"',
        f'data-fold-start-index="{start_index}"',
        f'data-fold-end-index="{end_index}"',
    ]
    if old_start is not None and old_end is not None:
        data_attrs.append(f'data-fold-old-start="{old_start}"')
        data_attrs.append(f'data-fold-old-end="{old_end}"')
    if new_start is not None and new_end is not None:
        data_attrs.append(f'data-fold-new-start="{new_start}"')
        data_attrs.append(f'data-fold-new-end="{new_end}"')
    payload_html = ""
    if _should_embed_fold_payload(lines):
        payload = _fold_payload_json(lines)
        payload_html = (
            f'<script type="application/json" id="fold-data-{fold_id_html}" '
            f'class="fold-payload">{payload}</script>'
        )
    return (
        f'<div class="line fold-gap" {" ".join(data_attrs)} title="{title}">'
        '<span class="ln old fold-marker">...</span>'
        '<span class="ln new fold-marker">...</span>'
        '<span class="content fold-summary">'
        f'<button type="button" class="fold-toggle" '
        f'data-fold-expand="{fold_id_html}">Expand</button>'
        f'<span class="fold-count">{label_html}</span>'
        '</span>'
        '</div>'
        f'{payload_html}'
    )


def _should_highlight_file(fd: FileDiff, final_contents: list[str]) -> bool:
    return (
        len(fd.lines) <= MAX_HIGHLIGHT_FILE_LINES
        and fd.additions + fd.deletions <= MAX_HIGHLIGHT_CHANGED_LINES
        and len(final_contents) <= MAX_HIGHLIGHT_RENDERED_LINES
        and sum(len(line) for line in final_contents) <= MAX_HIGHLIGHT_RENDERED_BYTES
    )


def _render_file(fd: FileDiff, threads_at_line: dict[tuple[str, int], list[list[Comment]]]) -> str:
    anchor = _file_anchor(fd.path)
    path_html = html.escape(fd.path)
    if fd.binary and not fd.lines:
        return (
            f'<div class="file" id="{anchor}" data-file="{path_html}">'
            f'<div class="file-header">'
            f'<span class="status">[{html.escape(fd.status)}]</span>'
            f'<span class="path" title="{path_html}">{path_html}</span>'
            f'<span class="stats">(binary)</span>'
            f'</div></div>'
        )

    ranges = _visible_line_ranges(fd, threads_at_line)
    visible_indices = [
        idx for start, end in ranges for idx in range(start, end)
    ]

    # Highlight only the rendered final-file view (context + added lines).
    final_contents = [
        fd.lines[idx].content
        for idx in visible_indices
        if fd.lines[idx].kind != "deleted"
    ]
    hl = (
        iter(_highlight_file(fd.path, final_contents))
        if _should_highlight_file(fd, final_contents)
        else iter([])
    )
    rows = []
    rendered_until = 0
    fold_index = 0
    for start, end in ranges:
        if start > rendered_until:
            rows.append(_render_fold_gap(
                fd.lines[rendered_until:start],
                f"{anchor}-fold-{fold_index}",
                file_path=fd.path,
                start_index=rendered_until,
            ))
            fold_index += 1
        for idx in range(start, end):
            dl = fd.lines[idx]
            old_ln = dl.old_lineno if dl.old_lineno is not None else ""
            new_ln = dl.new_lineno if dl.new_lineno is not None else ""
            if dl.kind == "deleted":
                content_html = html.escape(dl.content)  # no highlight for deleted
            else:
                content_html = next(hl, html.escape(dl.content))

            line_attr = (
                f' data-line="{dl.new_lineno}"' if dl.new_lineno is not None
                else f' data-line="{dl.old_lineno}"'
            )
            row_attrs = [f'class="line {dl.kind}"']
            if dl.old_lineno is not None:
                row_attrs.append(f'data-old-line="{dl.old_lineno}"')
            if dl.new_lineno is not None:
                row_attrs.append(f'data-new-line="{dl.new_lineno}"')
            row = (
                f'<div {" ".join(row_attrs)}>'
                f'<span class="ln old">{old_ln}</span>'
                f'<span class="ln new"{line_attr}>{new_ln}</span>'
                f'<span class="content">{content_html}</span>'
                f'</div>'
            )
            rows.append(row)

            # Append the comment-thread row for threads anchored at this
            # new-file line. Comments are stored with the source-file (new)
            # line number. Multiple top-level threads can share the same line.
            key = (fd.path, dl.new_lineno) if dl.new_lineno is not None else None
            if key and key in threads_at_line:
                inner = "".join(_render_thread(t) for t in threads_at_line[key])
                rows.append(
                    f'<div class="comment-thread" data-file="{html.escape(fd.path)}"'
                    f' data-line="{dl.new_lineno}">{inner}</div>'
                )
        rendered_until = end
    if rendered_until < len(fd.lines):
        rows.append(_render_fold_gap(
            fd.lines[rendered_until:],
            f"{anchor}-fold-{fold_index}",
            file_path=fd.path,
            start_index=rendered_until,
        ))

    return (
        f'<div class="file" id="{anchor}" data-file="{path_html}">'
        f'<div class="file-header">'
        f'<span class="status">[{html.escape(fd.status)}]</span>'
        f'<span class="path" title="{path_html}">{path_html}</span>'
        f'<span class="stats">'
        f'<span class="add">+{fd.additions}</span> '
        f'<span class="del">-{fd.deletions}</span>'
        f'</span>'
        f'</div>'
        f'<div class="lines">{"".join(rows)}</div>'
        f'</div>'
    )


def _render_sidebar(
    session: Session,
    comments: list[Comment],
    files: list[FileDiff],
    inbox_transcript: list[dict] | None = None,
    notes: list[Note] | None = None,
    agent_runtime: dict[str, dict[str, str]] | None = None,
) -> str:
    # Sidebar counters reflect what's visible (deleted hidden), with a
    # separate "deleted" row so the audit count is still discoverable.
    # Replies don't count toward open/total — a chatty thread shouldn't
    # inflate the "5 open" badge — only top-level comments do.
    live = [c for c in comments if not c.deleted]
    deleted = len(comments) - len(live)
    top_level = [c for c in live if not c.reply_to]
    stale_count = sum(1 for c in top_level if c.stale)
    resolved = sum(1 for c in top_level if c.resolved)
    crit = sum(1 for c in top_level if c.severity == "critical")

    # Per-file counts: unresolved and total live, top-level only. Keyed by
    # file path so the JS poller can update these in place. Globals
    # (file=="") get their own sidebar entry.
    per_file_total: dict[str, int] = defaultdict(int)
    per_file_unresolved: dict[str, int] = defaultdict(int)
    for c in top_level:
        if c.file == GLOBAL_FILE:
            continue
        per_file_total[c.file] += 1
        if not c.resolved:
            per_file_unresolved[c.file] += 1

    global_total = sum(1 for c in top_level if c.file == GLOBAL_FILE)
    global_open = sum(1 for c in top_level if c.file == GLOBAL_FILE and not c.resolved)
    if global_open > 0:
        global_counts = (
            f'<span class="count open">{global_open}</span>'
            f'<span class="count muted">/{global_total}</span>'
        )
    elif global_total > 0:
        global_counts = f'<span class="count muted">{global_total}</span>'
    else:
        global_counts = '<span class="count empty">—</span>'
    global_row = (
        '<li class="file-row global-row" data-global="1" '
        'title="High-level feedback (no file/line anchor)">'
        '<a href="#global" class="path-link">'
        '<div class="top-row">'
        '<span class="status s-G">G</span>'
        '<span class="name">High-level feedback</span>'
        f'<span class="counts" data-counts>{global_counts}</span>'
        '</div>'
        '</a>'
        '</li>'
    )

    runtime_by_agent = agent_runtime or {}
    agent_rows = ""
    any_killable = False
    for a in session.agents:
        info = runtime_by_agent.get(a.name, {})
        process = info.get("process_status", "")
        review = info.get("protocol_status", "")
        status_title = "Overall agent status derived from process and review state"
        status = (
            '<span class="agent-state-field agent-summary" '
            f'title="{html.escape(status_title, quote=True)}">'
            '<span class="agent-state-label">status</span> '
            f'<span class="agent-state-value">{html.escape(a.status)}</span>'
            '</span>'
        )
        detail_fields = []
        if process:
            detail_fields.append(
                '<span class="agent-state-field" '
                'title="Local reviewer process lifecycle">'
                '<span class="agent-state-label">process</span> '
                f'<span class="agent-state-value">{html.escape(process)}</span>'
                '</span>'
            )
        if review:
            detail_fields.append(
                '<span class="agent-state-field" '
                'title="Review protocol state: pending, asking, or done">'
                '<span class="agent-state-label">review</span> '
                f'<span class="agent-state-value">{html.escape(review)}</span>'
                '</span>'
            )
        detail_row = (
            f'<div class="agent-state-row">{"".join(detail_fields)}</div>'
            if detail_fields else ""
        )
        agent_name = html.escape(a.name)
        agent_attr = html.escape(a.name, quote=True)
        model = html.escape(a.model)
        model_attr = html.escape(a.model, quote=True)
        can_kill = process in KILLABLE_PROCESS_STATES
        any_killable = any_killable or can_kill
        kill_button = (
            f'<button type="button" class="agent-kill" data-agent-kill="{agent_attr}" '
            f'title="Stop {agent_attr}">kill</button>'
            if can_kill else ""
        )
        agent_rows += (
            f'<li class="agent-row" data-agent="{agent_attr}">'
            '<div class="agent-main">'
            '<span class="agent-ident">'
            f'<span class="agent-name">{agent_name}</span>'
            f'<span class="agent-model mono" title="{model_attr}">{model}</span>'
            '</span>'
            '<span class="agent-controls">'
            f'{status}'
            f'{kill_button}'
            '</span>'
            '</div>'
            f'{detail_row}'
            '</li>'
        )
    kill_all_hidden = "" if any_killable else " hidden"
    curator_button = (
        '<button id="curator-run-btn" type="button" class="agent-curate" '
        'title="Run comment curator">curate</button>'
    )
    rerun_all_button = (
        '<button id="rerun-all-agents-btn" type="button" '
        'class="agent-rerun-all" title="Rerun all reviewer agents">rerun all</button>'
    )
    deleted_row = (
        f'<li data-k="deleted"><span>deleted</span><span class="v">{deleted}</span></li>'
        if deleted else ""
    )
    pr_row = ""
    if session.github:
        pr_label = html.escape(_github_pr_label(session))
        pr_row = f'<li data-k="pr"><span>pr</span><span class="v mono">{pr_label}</span></li>'
    session_rows = (
        f'<li data-k="head"><span>head</span><span class="v mono">{html.escape(session.current_head[:12])}</span></li>'
        f'<li data-k="base"><span>base</span><span class="v mono">{html.escape(session.base_ref)}</span></li>'
        f'{pr_row}'
    )
    file_rows = "".join(
        _render_file_row(fd, per_file_unresolved.get(fd.path, 0),
                         per_file_total.get(fd.path, 0))
        for fd in files
    ) or '<li class="muted">(no files)</li>'
    total_additions = sum(fd.additions for fd in files)
    total_deletions = sum(fd.deletions for fd in files)
    file_totals = (
        '<span class="file-total-stats" title="Cumulative lines changed">'
        f'<span class="add">+{total_additions}</span> '
        f'<span class="del">-{total_deletions}</span>'
        '</span>'
    )

    # Activity jump row: same shape as global-row so it shares the file-row
    # layout/CSS. Pending = unanswered agent questions; total = notes plus
    # all ask/reply transcript entries.
    transcript = inbox_transcript or []
    note_count = len(notes or [])
    inbox_total = len(transcript) + note_count
    inbox_pending = sum(1 for e in transcript if not e.get("reply"))
    if inbox_pending > 0:
        inbox_counts_html = (
            f'<span class="count open">{inbox_pending}</span>'
            f'<span class="count muted">/{inbox_total}</span>'
        )
    elif inbox_total > 0:
        inbox_counts_html = f'<span class="count muted">{inbox_total}</span>'
    else:
        inbox_counts_html = '<span class="count empty">—</span>'
    inbox_row = (
        '<li class="file-row inbox-row" data-inbox="1" '
        'title="Jump to agent activity">'
        '<a href="#inbox" class="path-link">'
        '<div class="top-row">'
        '<span class="status s-A">A</span>'
        '<span class="name">Agent activity</span>'
        f'<span class="counts" id="inbox-counts" data-counts>{inbox_counts_html}</span>'
        '</div>'
        '</a>'
        '</li>'
    )

    return (
        '<aside id="sidebar">'
        '<div class="sidebar-heading files-heading">'
        f'<h3>Files ({len(files)})</h3>'
        f'{file_totals}'
        '</div>'
        f'<ul class="files">{global_row}{file_rows}{inbox_row}</ul>'
        '<h3>Session</h3>'
        '<ul>'
        f'<li data-k="state"><span>state</span><span class="v">{html.escape(session.state)}</span></li>'
        f'{session_rows}'
        f'<li data-k="total"><span>comments</span><span class="v">{len(live)}</span></li>'
        f'<li data-k="stale_comments"><span>stale</span><span class="v">{stale_count}</span></li>'
        f'<li data-k="resolved"><span>resolved</span><span class="v">{resolved}</span></li>'
        f'<li data-k="critical"><span>critical</span><span class="v">{crit}</span></li>'
        f'{deleted_row}'
        '</ul>'
        '<div class="sidebar-heading agents-heading">'
        '<h3>Agents</h3>'
        '<span class="agent-heading-actions">'
        f'{curator_button}'
        f'{rerun_all_button}'
        '<button id="kill-all-agents-btn" type="button" class="agent-kill-all" '
        f'title="Stop all agents"{kill_all_hidden}>kill all</button>'
        '</span>'
        '</div>'
        f'<ul id="agent-list" class="agent-list">{agent_rows or "<li>(none)</li>"}</ul>'
        '<h3>Navigation</h3>'
        '<ul class="shortcuts">'
        '<li><span class="keys"><kbd>n</kbd><kbd>p</kbd></span>'
        '<span class="desc">next / prev comment</span></li>'
        '<li><span class="keys"><kbd>N</kbd><kbd>P</kbd></span>'
        '<span class="desc">next / prev file</span></li>'
        '<li><span class="keys"><kbd>d</kbd><kbd>u</kbd></span>'
        '<span class="desc">page down / up</span></li>'
        '<li><span class="keys"><kbd>z</kbd></span>'
        '<span class="desc">center focused</span></li>'
        '<li><span class="keys"><kbd>w</kbd></span>'
        '<span class="desc">toggle line wrap</span></li>'
        '</ul>'
        '<h3>Actions</h3>'
        '<ul class="shortcuts">'
        f'<li><span class="keys"><kbd class="prefix">{PREFIX_LABEL}</kbd><kbd>r</kbd></span>'
        '<span class="desc">reply</span></li>'
        f'<li><span class="keys"><kbd class="prefix">{PREFIX_LABEL}</kbd><kbd>e</kbd></span>'
        '<span class="desc">edit</span></li>'
        f'<li><span class="keys"><kbd class="prefix">{PREFIX_LABEL}</kbd><kbd>R</kbd></span>'
        '<span class="desc">toggle resolved</span></li>'
        f'<li><span class="keys"><kbd class="prefix">{PREFIX_LABEL}</kbd><kbd>D</kbd></span>'
        '<span class="desc">delete</span></li>'
        f'<li><span class="keys"><kbd class="prefix">{PREFIX_LABEL}</kbd><kbd>c</kbd><kbd>a</kbd></span>'
        '<span class="desc">add global comment</span></li>'
        f'<li><span class="keys"><kbd class="prefix">{PREFIX_LABEL}</kbd><kbd>c</kbd><kbd>c</kbd></span>'
        '<span class="desc">toggle collapse</span></li>'
        f'<li><span class="keys"><kbd class="prefix">{PREFIX_LABEL}</kbd><kbd>a</kbd><kbd>K</kbd></span>'
        '<span class="desc">kill all agents</span></li>'
        f'<li><span class="keys"><kbd class="prefix">{PREFIX_LABEL}</kbd><kbd>g</kbd><kbd>f</kbd></span>'
        '<span class="desc">fetch from GitHub</span></li>'
        f'<li><span class="keys"><kbd class="prefix">{PREFIX_LABEL}</kbd><kbd>g</kbd><kbd>p</kbd></span>'
        '<span class="desc">push to GitHub</span></li>'
        '</ul>'
        '<h3>Composer</h3>'
        '<ul class="shortcuts">'
        '<li><span class="keys"><kbd>Ctrl</kbd><kbd>↵</kbd></span>'
        '<span class="desc">post / save</span></li>'
        '<li><span class="keys"><kbd>Esc</kbd></span>'
        '<span class="desc">cancel</span></li>'
        f'<li><span class="keys"><kbd class="prefix">{COMPOSER_PREFIX_LABEL}</kbd>'
        '<kbd>c</kbd><kbd>w</kbd><kbd>s</kbd><kbd>n</kbd><kbd>f</kbd></span>'
        '<span class="desc">set severity</span></li>'
        f'<li><span class="keys"><kbd class="prefix">{COMPOSER_PREFIX_LABEL}</kbd><kbd>a</kbd><kbd>b</kbd></span>'
        '<span class="desc">approve / block (global)</span></li>'
        f'<li><span class="keys"><kbd class="prefix">{COMPOSER_PREFIX_LABEL}</kbd><kbd>i</kbd></span>'
        '<span class="desc">insert suggestion (inline only)</span></li>'
        '</ul>'
        '</aside>'
    )


def _render_file_row(fd: FileDiff, unresolved: int, total: int) -> str:
    """One entry in the sidebar's Files list.

    Two lines: filename + counts on top; directory + add/del on the muted
    subline. Full path lands in the title attribute for hover.
    `data-file` lets the live poller update counts without re-rendering.
    """
    anchor = _file_anchor(fd.path)
    name = fd.path.rsplit("/", 1)[-1]
    dirpath = fd.path[: -len(name) - 1] if "/" in fd.path else ""
    status = html.escape(fd.status) if fd.status else ""
    stats = (
        f'<span class="add">+{fd.additions}</span> '
        f'<span class="del">-{fd.deletions}</span>'
    ) if not fd.binary else '<span class="muted">bin</span>'
    if unresolved > 0:
        counts_html = (
            f'<span class="count open">{unresolved}</span>'
            f'<span class="count muted">/{total}</span>'
        )
    elif total > 0:
        counts_html = f'<span class="count muted">{total}</span>'
    else:
        counts_html = '<span class="count empty">—</span>'
    # U+200E (LRM) forces LTR bidi inside an element set to `direction: rtl`
    # so ASCII slashes don't visually flip; the CSS reverses only the overflow
    # direction so the ellipsis lands at the *prefix*, preserving the suffix
    # (the segments nearest the filename — the informative ones).
    dir_html = (
        f'<span class="dir">‎{html.escape(dirpath)}/</span>'
        if dirpath else '<span class="dir"></span>'
    )
    return (
        f'<li class="file-row" data-file="{html.escape(fd.path)}" '
        f'title="{html.escape(fd.path)}">'
        f'<a href="#{anchor}" class="path-link">'
        f'<div class="top-row">'
        f'<span class="status s-{status}">{status}</span>'
        f'<span class="name">{html.escape(name)}</span>'
        f'<span class="counts" data-counts>{counts_html}</span>'
        f'</div>'
        f'<div class="sub-row">'
        f'{dir_html}'
        f'<span class="stats">{stats}</span>'
        f'</div>'
        f'</a>'
        f'</li>'
    )


def _render_session_row(s: dict, base_url: str = "") -> str:
    sid = html.escape(s["id"])
    state = html.escape(s.get("state", ""))
    state_label = html.escape(_session_state_label(s.get("state", "")))
    fallback_change = f"{s.get('base_ref', '')} … {s.get('topic_ref', '')}"
    change = html.escape(s.get("change_label") or fallback_change)
    workspace = html.escape(s.get("workspace", ""))
    created = html.escape(s.get("created_at", ""))
    total = s.get("comment_count", 0)
    unresolved = s.get("unresolved_count", 0)
    stale = s.get("stale_count", 0)
    crit = s.get("critical_count", 0)
    agent_count = s.get("agent_count", 0)
    session_subtitle = html.escape(s.get("session_subtitle") or s.get("current_head", ""))
    counts = (
        f'<span class="n">{total}</span>'
        f'<span class="sub"> total</span>'
        + (f' · <span class="n warn">{unresolved}</span><span class="sub"> open</span>' if unresolved else "")
        + (f' · <span class="n crit">{crit}</span><span class="sub"> crit</span>' if crit else "")
        + (f' · <span class="n muted">{stale}</span><span class="sub"> stale</span>' if stale else "")
    )
    return (
        f'<tr class="session-row state-{state}" data-id="{sid}">'
        f'<td class="id"><a href="{base_url}/{sid}">{sid}</a>'
        f'<div class="mono head">{session_subtitle}</div></td>'
        f'<td><span class="badge state-{state}" '
        f'title="session state: {state}">{state_label}</span>'
        f'<div class="sub">{agent_count} agent{"s" if agent_count != 1 else ""}</div></td>'
        f'<td class="change" title="{change}">{change}</td>'
        f'<td class="mono workspace">{workspace}</td>'
        f'<td class="counts">{counts}</td>'
        f'<td class="mono created">{created}</td>'
        f'</tr>'
    )


def render_index(sessions: list[dict], *, roots: list[str], base_url: str = "") -> str:
    """Session-picker page served at `/`.

    `base_url` is the path prefix the app is mounted under (e.g. `/pr` when
    fronted by `handle_path /pr/*` in caddy). Empty means root-mounted.
    """
    assets = ASSETS_DIR
    css = (assets / "style.css").read_text()
    js = (assets / "index.js").read_text()
    roots_str = " · ".join(html.escape(r) for r in roots) or "(none)"
    base_url_js = json.dumps(base_url)
    if sessions:
        rows = "\n".join(_render_session_row(s, base_url) for s in sessions)
        body = (
            '<table class="sessions">'
            '<thead><tr>'
            '<th>Session</th><th>State</th><th>Change</th>'
            '<th>Workspace</th><th>Comments</th><th>Created</th>'
            '</tr></thead>'
            f'<tbody id="session-rows">{rows}</tbody>'
            '</table>'
        )
    else:
        body = (
            '<div class="empty">'
            f'No review sessions found under <span class="mono">{roots_str}</span>.'
            '<br><br>Create one with:'
            '<pre class="mono">peanut-review init --workspace . --base main --topic HEAD</pre>'
            '</div>'
        )

    index_href = base_url if base_url else "/"
    return f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <title>peanut-review — sessions</title>
  <link rel="icon" href="{FAVICON_HREF}">
  {THEME_BOOTSTRAP}
  <style>{css}</style>
</head>
<body class="index">
  <header>
    <h1><a href="{index_href}">🥜 peanut-review</a></h1>
    <span class="meta">{len(sessions)} session{"s" if len(sessions) != 1 else ""}</span>
    <span class="meta mono">{roots_str}</span>
    <span class="spacer"></span>
    {THEME_TOGGLE_BUTTON}
    <button id="refresh" title="Rescan">Refresh</button>
  </header>
  <main class="index-main">
    {body}
  </main>
  <script>
    window.PR_BASE_URL = {base_url_js};
    {js}
  </script>
</body>
</html>
"""


def render_page(
    session: Session,
    session_id: str,
    files: list[FileDiff],
    comments: list[Comment],
    *,
    notes: list[Note] | None = None,
    head_shifted: bool = False,
    base_url: str = "",
    inbox_transcript: list[dict] | None = None,
    agent_runtime: dict[str, dict[str, str]] | None = None,
) -> str:
    """Build the full HTML page for a session.

    `base_url` is the path prefix the app is mounted under (empty → root).
    `notes` and `inbox_transcript` are the agent activity streams rendered at
    the bottom; None falls back to empty so tests/non-server callers don't
    have to pass them.
    """
    threads_at = _group_threads_by_anchor(comments)
    transcript = inbox_transcript or []
    note_items = notes or []
    file_html = "".join(_render_file(fd, threads_at) for fd in files)
    global_html = _render_global_section(comments)
    inbox_html = render_inbox_section(transcript, note_items)
    sidebar = _render_sidebar(
        session, comments, files,
        inbox_transcript=transcript, notes=note_items,
        agent_runtime=agent_runtime,
    )

    head_badge = (
        '<span class="badge head head-shifted">HEAD shifted</span>'
        if head_shifted else '<span class="badge head"></span>'
    )
    state = html.escape(session.state)
    state_class = f"state-{state}"
    state_label = html.escape(_session_state_label(session.state))
    change_label = _change_label(session)
    change_label_html = html.escape(change_label)
    title_label = change_label if _github_change_title(session) else session_id

    # Full PR URL + push button in the header for gh-backed sessions. URL is
    # rendered verbatim (no truncation) so triple-click → copy works. The
    # button label carries the pending-push count so unfinalized local
    # comments are obvious; disabled when zero.
    if session.github and session.github.url:
        from .. import gh_push as _gh_push
        pending = _gh_push.plan_push(comments).total
        gh_url = html.escape(session.github.url)
        gh_link_html = (
            f'<a class="gh-pr-link mono" href="{gh_url}" '
            f'target="_blank" rel="noopener" '
            f'title="Open PR on GitHub (right-click → Copy link address)">'
            f'{gh_url}</a>'
        )
        if pending > 0:
            label = f'Push to GitHub ({pending} pending)'
            disabled_attr = ""
            cls = "gh-push has-pending"
        else:
            label = 'Push to GitHub (0 pending)'
            disabled_attr = "disabled"
            cls = "gh-push"
        gh_push_button = (
            f'<button id="gh-push-btn" class="{cls}" type="button" '
            f'data-pending="{pending}" {disabled_attr} '
            f'title="Preview and push local comments to GitHub">'
            f'{html.escape(label)}'
            f'</button>'
        )
    else:
        gh_link_html = ""
        gh_push_button = ""

    session_url = f"{base_url}/{session_id}"
    # Escape single quotes for safe JSON in JS
    session_url_js = json.dumps(session_url)
    session_id_js = json.dumps(session_id)
    base_url_js = json.dumps(base_url)

    css = (ASSETS_DIR / "style.css").read_text()
    js = (ASSETS_DIR / "app.js").read_text()

    index_href = base_url if base_url else "/"
    return f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <title>peanut-review — {html.escape(title_label)}</title>
  <link rel="icon" href="{FAVICON_HREF}">
  {THEME_BOOTSTRAP}
  <style>{css}</style>
</head>
<body>
  <header>
    <h1><a href="{index_href}">🥜 peanut-review</a></h1>
    <span class="session-title">
      <span class="meta mono">{html.escape(session_id)}</span>
      <button id="copy-session-id" class="copy-session-id" type="button"
              data-session-id="{html.escape(session_id, quote=True)}"
              title="Copy session name" aria-label="Copy session name">▣</button>
    </span>
    <span class="meta change-title" title="{change_label_html}">{change_label_html}</span>
    {gh_link_html}
    <span class="spacer"></span>
    {THEME_TOGGLE_BUTTON}
    {gh_push_button}
    {head_badge}
    <span class="badge session-state {state_class}" data-session-state="{state}"
          title="session state: {state}">{state_label}</span>
  </header>
  <main>
    {sidebar}
    {global_html}
    {file_html}
    {inbox_html}
  </main>
  <div id="gh-push-modal" class="modal" hidden>
    <div class="modal-backdrop" data-modal-close></div>
    <div class="modal-card" role="dialog" aria-labelledby="gh-push-title">
      <div class="modal-header">
        <h2 id="gh-push-title">Push to GitHub</h2>
        <button class="modal-close" type="button" data-modal-close
                title="Close">×</button>
      </div>
      <div class="modal-body" id="gh-push-body">Loading…</div>
      <div class="modal-footer">
        <button id="gh-push-cancel" type="button" data-modal-close>Cancel</button>
        <button id="gh-push-confirm" type="button" class="primary" disabled>
          Confirm push
        </button>
      </div>
    </div>
  </div>
  <script>
    window.PR_BASE_URL = {base_url_js};
    window.PR_SESSION_URL = {session_url_js};
    window.PR_SESSION_ID = {session_id_js};
    {js}
  </script>
</body>
</html>
"""
