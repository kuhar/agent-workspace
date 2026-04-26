"""Render a peanut-review session as a single HTML page."""
from __future__ import annotations

import html
import json
import re
from collections import defaultdict
from pathlib import Path

from pygments import highlight as _pyg_highlight
from pygments.formatters import HtmlFormatter
from pygments.lexers import TextLexer, get_lexer_for_filename
from pygments.util import ClassNotFound

from ..models import Comment, Session
from ..session import GLOBAL_FILE
from .diff import FileDiff

ASSETS_DIR = Path(__file__).parent / "assets"

# Peanut-emoji favicon, inlined as an SVG data URI so we ship no binary asset.
_FAVICON_SVG = (
    "<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 64 64'>"
    "<text y='54' font-size='56'>🥜</text>"
    "</svg>"
)
FAVICON_HREF = "data:image/svg+xml;utf8," + _FAVICON_SVG.replace("#", "%23").replace('"', "%22")


def _lexer_for(path: str):
    try:
        return get_lexer_for_filename(path, stripall=False)
    except ClassNotFound:
        return TextLexer()


def _highlight_file(path: str, lines: list[str]) -> list[str]:
    """Syntax-highlight a file's contents line-by-line.

    We highlight the full file once (for consistent tokenization across
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


def render_inbox_section(transcript: list[dict]) -> str:
    """Bottom-of-page section showing the agent help-channel transcript.

    Read-only: humans/orchestrators reply via the CLI (`peanut-review reply`)
    so the existing `ask`-blocking flow stays the source of truth. Polled by
    the same JS loop as comments and reconciled in place.
    """
    if not transcript:
        body = (
            '<p class="muted">No agent questions yet. When an agent calls '
            '<code>peanut-review ask</code>, the question and the '
            'orchestrator\'s reply appear here.</p>'
        )
    else:
        rows = []
        for entry in transcript:
            agent = html.escape(entry.get("agent", ""))
            qid = html.escape(entry.get("id", ""))
            qts = html.escape(entry.get("timestamp", ""))
            qtext = html.escape(entry.get("question", ""))
            reply = entry.get("reply")
            row = [
                f'<div class="ix-entry" data-qid="{qid}" '
                f'data-key="{agent}/{qid}" '
                f'data-replied="{1 if reply else 0}">',
                f'<div class="ix-q"><span class="ix-meta">'
                f'<span class="agent">{agent}</span>'
                f'<span class="qid mono">{qid}</span>'
                f'<span class="ts mono">{qts}</span>'
                f'</span><pre class="ix-body">{qtext}</pre></div>',
            ]
            if reply:
                ats = html.escape(reply.get("timestamp", ""))
                aby = html.escape(reply.get("answered_by", "orchestrator"))
                atext = html.escape(reply.get("answer", ""))
                row.append(
                    f'<div class="ix-r"><span class="ix-meta">'
                    f'<span class="agent">↳ {aby}</span>'
                    f'<span class="ts mono">{ats}</span>'
                    f'</span><pre class="ix-body">{atext}</pre></div>'
                )
            else:
                row.append('<div class="ix-r pending"><span class="ix-meta">'
                           '<span class="agent">↳ awaiting reply…</span>'
                           '</span></div>')
            row.append('</div>')
            rows.append("".join(row))
        body = "".join(rows)
    return (
        '<section class="inbox-section" id="inbox">'
        '<h2>Agent help inbox</h2>'
        '<p class="hint muted">Babysitting channel for blocked agents '
        '(<code>peanut-review ask</code> / <code>reply</code>). '
        'Read-only here.</p>'
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
    cid = html.escape(c.id)
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
        badges.append('<span class="round">resolved</span>')
    # Replies don't carry their own severity — they inherit the thread's.
    sev_html = (
        ""
        if is_reply
        else f'<span class="sev {html.escape(c.severity)}">{html.escape(c.severity)}</span>'
    )
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
        f'<span class="author">{html.escape(c.author or "unknown")}</span>'
        f'{sev_html}'
        f'{"".join(badges)}'
        f'{edited_html}'
        f'{external_html}'
        f'{"".join(buttons)}'
        f'</div>'
        f'<div class="comment-body">{html.escape(c.body)}</div>'
        f'</div>'
    )


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
    if parent.resolved:
        toggle_btn = f'<button data-unresolve="{pid}">Unresolve</button>'
    else:
        toggle_btn = f'<button data-resolve="{pid}">Resolve</button>'
    actions = (
        f'<div class="thread-actions">'
        f'<button class="reply-btn" data-reply-to="{pid}">Reply</button>'
        f'{toggle_btn}'
        f'</div>'
    )
    cls = "thread"
    if parent.resolved:
        cls += " resolved"
    return f'<div class="{cls}" data-thread-id="{pid}">{parent_html}{replies_html}{actions}</div>'


def _render_file(fd: FileDiff, threads_at_line: dict[tuple[str, int], list[list[Comment]]]) -> str:
    anchor = _file_anchor(fd.path)
    if fd.binary and not fd.lines:
        return (
            f'<div class="file" id="{anchor}" data-file="{html.escape(fd.path)}">'
            f'<div class="file-header">'
            f'<span class="status">[{html.escape(fd.status)}]</span>'
            f'<span class="path">{html.escape(fd.path)}</span>'
            f'<span class="stats">(binary)</span>'
            f'</div></div>'
        )

    # Highlight the final-file view (context + added lines).
    final_contents = [dl.content for dl in fd.lines if dl.kind != "deleted"]
    hl = iter(_highlight_file(fd.path, final_contents))
    rows = []
    for dl in fd.lines:
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
        row = (
            f'<div class="line {dl.kind}">'
            f'<span class="ln old">{old_ln}</span>'
            f'<span class="ln new"{line_attr}>{new_ln}</span>'
            f'<span class="content">{content_html}</span>'
            f'</div>'
        )
        rows.append(row)

        # Append the comment-thread row for threads anchored at this new-file
        # line. Comments are stored with the source-file (new) line number.
        # Multiple top-level threads can share the same line — they render as
        # sibling .thread blocks inside one .comment-thread container.
        key = (fd.path, dl.new_lineno) if dl.new_lineno is not None else None
        if key and key in threads_at_line:
            inner = "".join(_render_thread(t) for t in threads_at_line[key])
            rows.append(
                f'<div class="comment-thread" data-file="{html.escape(fd.path)}"'
                f' data-line="{dl.new_lineno}">{inner}</div>'
            )

    return (
        f'<div class="file" id="{anchor}" data-file="{html.escape(fd.path)}">'
        f'<div class="file-header">'
        f'<span class="status">[{html.escape(fd.status)}]</span>'
        f'<span class="path">{html.escape(fd.path)}</span>'
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

    agent_rows = "".join(
        f'<li><span>{html.escape(a.name)}</span>'
        f'<span class="v">{html.escape(a.status)}</span></li>'
        for a in session.agents
    )
    deleted_row = (
        f'<li data-k="deleted"><span>deleted</span><span class="v">{deleted}</span></li>'
        if deleted else ""
    )
    file_rows = "".join(
        _render_file_row(fd, per_file_unresolved.get(fd.path, 0),
                         per_file_total.get(fd.path, 0))
        for fd in files
    ) or '<li class="muted">(no files)</li>'

    # Inbox jump row: same shape as global-row so it shares the file-row
    # layout/CSS. Pending = unanswered agent questions; total = all entries.
    transcript = inbox_transcript or []
    inbox_total = len(transcript)
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
        'title="Jump to agent help inbox">'
        '<a href="#inbox" class="path-link">'
        '<div class="top-row">'
        '<span class="status s-I">I</span>'
        '<span class="name">Agent inbox</span>'
        f'<span class="counts" id="inbox-counts" data-counts>{inbox_counts_html}</span>'
        '</div>'
        '</a>'
        '</li>'
    )

    return (
        '<aside id="sidebar">'
        f'<h3>Files ({len(files)})</h3>'
        f'<ul class="files">{global_row}{file_rows}{inbox_row}</ul>'
        '<h3>Session</h3>'
        '<ul>'
        f'<li data-k="state"><span>state</span><span class="v">{html.escape(session.state)}</span></li>'
        f'<li data-k="head"><span>head</span><span class="v mono">{html.escape(session.current_head[:12])}</span></li>'
        f'<li data-k="base"><span>base</span><span class="v mono">{html.escape(session.base_ref)}</span></li>'
        f'<li data-k="total"><span>comments</span><span class="v">{len(live)}</span></li>'
        f'<li data-k="stale_comments"><span>stale</span><span class="v">{stale_count}</span></li>'
        f'<li data-k="resolved"><span>resolved</span><span class="v">{resolved}</span></li>'
        f'<li data-k="critical"><span>critical</span><span class="v">{crit}</span></li>'
        f'{deleted_row}'
        '</ul>'
        '<h3>Agents</h3>'
        f'<ul>{agent_rows or "<li>(none)</li>"}</ul>'
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
        '</ul>'
        '<h3>Actions</h3>'
        '<ul class="shortcuts">'
        '<li><span class="keys"><kbd class="prefix">␣</kbd><kbd>r</kbd></span>'
        '<span class="desc">reply</span></li>'
        '<li><span class="keys"><kbd class="prefix">␣</kbd><kbd>e</kbd></span>'
        '<span class="desc">edit</span></li>'
        '<li><span class="keys"><kbd class="prefix">␣</kbd><kbd>R</kbd></span>'
        '<span class="desc">toggle resolved</span></li>'
        '<li><span class="keys"><kbd class="prefix">␣</kbd><kbd>D</kbd></span>'
        '<span class="desc">delete</span></li>'
        '<li><span class="keys"><kbd class="prefix">␣</kbd><kbd>a</kbd></span>'
        '<span class="desc">add global comment</span></li>'
        '<li><span class="keys"><kbd class="prefix">␣</kbd><kbd>g</kbd><kbd>f</kbd></span>'
        '<span class="desc">fetch from GitHub</span></li>'
        '<li><span class="keys"><kbd class="prefix">␣</kbd><kbd>g</kbd><kbd>p</kbd></span>'
        '<span class="desc">push to GitHub</span></li>'
        '</ul>'
        '<h3>Composer</h3>'
        '<ul class="shortcuts">'
        '<li><span class="keys"><kbd>Ctrl</kbd><kbd>↵</kbd></span>'
        '<span class="desc">post / save</span></li>'
        '<li><span class="keys"><kbd>Esc</kbd></span>'
        '<span class="desc">cancel</span></li>'
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
    base = html.escape(s.get("base_ref", ""))
    topic = html.escape(s.get("topic_ref", ""))
    workspace = html.escape(s.get("workspace", ""))
    created = html.escape(s.get("created_at", ""))
    total = s.get("comment_count", 0)
    unresolved = s.get("unresolved_count", 0)
    stale = s.get("stale_count", 0)
    crit = s.get("critical_count", 0)
    agent_count = s.get("agent_count", 0)
    head = html.escape(s.get("current_head", ""))
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
        f'<div class="mono head">{head}</div></td>'
        f'<td><span class="badge state-{state}">{state}</span>'
        f'<div class="sub">{agent_count} agent{"s" if agent_count != 1 else ""}</div></td>'
        f'<td class="mono refs">{base} … {topic}</td>'
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
            '<th>Session</th><th>State</th><th>Base … Topic</th>'
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
  <style>{css}</style>
</head>
<body class="index">
  <header>
    <h1><a href="{index_href}">🥜 peanut-review</a></h1>
    <span class="meta">{len(sessions)} session{"s" if len(sessions) != 1 else ""}</span>
    <span class="meta mono">{roots_str}</span>
    <span class="spacer"></span>
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
    head_shifted: bool = False,
    base_url: str = "",
    inbox_transcript: list[dict] | None = None,
) -> str:
    """Build the full HTML page for a session.

    `base_url` is the path prefix the app is mounted under (empty → root).
    `inbox_transcript` is the agent ask/reply log to render at the bottom;
    None falls back to an empty transcript so tests/non-server callers don't
    have to pass it.
    """
    threads_at = _group_threads_by_anchor(comments)
    transcript = inbox_transcript or []
    file_html = "".join(_render_file(fd, threads_at) for fd in files)
    global_html = _render_global_section(comments)
    inbox_html = render_inbox_section(transcript)
    sidebar = _render_sidebar(session, comments, files, inbox_transcript=transcript)

    head_badge = (
        '<span class="badge head head-shifted">HEAD shifted</span>'
        if head_shifted else '<span class="badge head"></span>'
    )
    state_class = f"state-{session.state}"

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
  <title>peanut-review — {html.escape(session_id)}</title>
  <link rel="icon" href="{FAVICON_HREF}">
  <style>{css}</style>
</head>
<body>
  <header>
    <h1><a href="{index_href}">🥜 peanut-review</a></h1>
    <span class="meta mono">{html.escape(session_id)}</span>
    <span class="meta">{html.escape(session.base_ref)} … {html.escape(session.topic_ref)}</span>
    {gh_link_html}
    <span class="spacer"></span>
    {gh_push_button}
    {head_badge}
    <span class="badge {state_class}">{html.escape(session.state)}</span>
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
