"""Render a peanut-review session as a single HTML page."""
from __future__ import annotations

import html
import json
from collections import defaultdict
from pathlib import Path

from pygments import highlight as _pyg_highlight
from pygments.formatters import HtmlFormatter
from pygments.lexers import TextLexer, get_lexer_for_filename
from pygments.util import ClassNotFound

from ..models import Comment, Session
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


def _group_comments(comments: list[Comment]) -> dict[tuple[str, int], list[Comment]]:
    """Key each comment by (file, anchor_line).

    For range comments (end_line set and different from line), anchor the thread
    at end_line — where the user's drag ended and their eye is. Single-line
    comments stay anchored at line.
    """
    g: dict[tuple[str, int], list[Comment]] = defaultdict(list)
    for c in comments:
        anchor = c.end_line if (c.end_line is not None and c.end_line != c.line) else c.line
        g[(c.file, anchor)].append(c)
    return g


def _render_comment(c: Comment) -> str:
    classes = ["comment"]
    if c.stale:
        classes.append("stale")
    if c.resolved:
        classes.append("resolved")
    resolve_btn = (
        f'<button data-resolve="{html.escape(c.id)}">Resolve</button>'
        if not c.resolved else ""
    )
    badges = []
    if c.end_line is not None and c.end_line != c.line:
        lo, hi = min(c.line, c.end_line), max(c.line, c.end_line)
        badges.append(f'<span class="round range">L{lo}–L{hi}</span>')
    if c.stale:
        badges.append('<span class="round">stale</span>')
    if c.resolved:
        badges.append('<span class="round">resolved</span>')
    return (
        f'<div class="{" ".join(classes)}" data-cid="{html.escape(c.id)}">'
        f'<div class="comment-meta">'
        f'<span class="author">{html.escape(c.author or "unknown")}</span>'
        f'<span class="sev {html.escape(c.severity)}">{html.escape(c.severity)}</span>'
        f'<span class="round">R{c.round}</span>'
        f'{"".join(badges)}'
        f'{resolve_btn}'
        f'</div>'
        f'<div class="comment-body">{html.escape(c.body)}</div>'
        f'</div>'
    )


def _render_file(fd: FileDiff, comments_at_line: dict[tuple[str, int], list[Comment]]) -> str:
    if fd.binary and not fd.lines:
        return (
            f'<div class="file" data-file="{html.escape(fd.path)}">'
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

        # Append comment thread for comments anchored at this new-file line.
        # Comments are stored with the source-file (new) line number.
        key = (fd.path, dl.new_lineno) if dl.new_lineno is not None else None
        if key and key in comments_at_line:
            thread = (
                f'<div class="comment-thread" data-file="{html.escape(fd.path)}"'
                f' data-line="{dl.new_lineno}">'
                + "".join(_render_comment(c) for c in comments_at_line[key])
                + "</div>"
            )
            rows.append(thread)

    return (
        f'<div class="file" data-file="{html.escape(fd.path)}">'
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


def _render_sidebar(session: Session, comments: list[Comment]) -> str:
    stale_count = sum(1 for c in comments if c.stale)
    resolved = sum(1 for c in comments if c.resolved)
    crit = sum(1 for c in comments if c.severity == "critical")
    agent_rows = "".join(
        f'<li><span>{html.escape(a.name)}</span>'
        f'<span class="v">{html.escape(a.status)}</span></li>'
        for a in session.agents
    )
    return (
        '<aside id="sidebar">'
        '<h3>Session</h3>'
        '<ul>'
        f'<li data-k="state"><span>state</span><span class="v">{html.escape(session.state)}</span></li>'
        f'<li data-k="head"><span>head</span><span class="v mono">{html.escape(session.current_head[:12])}</span></li>'
        f'<li data-k="base"><span>base</span><span class="v mono">{html.escape(session.base_ref)}</span></li>'
        f'<li data-k="total"><span>comments</span><span class="v">{len(comments)}</span></li>'
        f'<li data-k="stale_comments"><span>stale</span><span class="v">{stale_count}</span></li>'
        f'<li data-k="resolved"><span>resolved</span><span class="v">{resolved}</span></li>'
        f'<li data-k="critical"><span>critical</span><span class="v">{crit}</span></li>'
        '</ul>'
        '<h3>Agents</h3>'
        f'<ul>{agent_rows or "<li>(none)</li>"}</ul>'
        '</aside>'
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
) -> str:
    """Build the full HTML page for a session.

    `base_url` is the path prefix the app is mounted under (empty → root).
    """
    comments_at = _group_comments(comments)
    file_html = "".join(_render_file(fd, comments_at) for fd in files)
    sidebar = _render_sidebar(session, comments)

    head_badge = (
        '<span class="badge head state-triage">HEAD shifted</span>'
        if head_shifted else '<span class="badge head"></span>'
    )
    state_class = f"state-{session.state}"

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
    <span class="spacer"></span>
    {head_badge}
    <span class="badge {state_class}">{html.escape(session.state)}</span>
  </header>
  <main>
    {sidebar}
    {file_html}
  </main>
  <script>
    window.PR_BASE_URL = {base_url_js};
    window.PR_SESSION_URL = {session_url_js};
    window.PR_SESSION_ID = {session_id_js};
    {js}
  </script>
</body>
</html>
"""
