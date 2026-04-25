"""CLI dispatcher and all subcommands."""
from __future__ import annotations

import argparse
import dataclasses
import json
import os
import subprocess
import sys
from pathlib import Path

from . import models, polling, session as sess, store


def _get_session_dir(args: argparse.Namespace) -> str:
    """Resolve session directory from --session flag or environment."""
    d = getattr(args, "session", None) or os.environ.get("PEANUT_SESSION")
    if not d:
        print("Error: --session or $PEANUT_SESSION required", file=sys.stderr)
        sys.exit(1)
    if not Path(d).exists():
        print(f"Error: session directory does not exist: {d}", file=sys.stderr)
        sys.exit(1)
    return d


def _get_author(args: argparse.Namespace) -> str:
    """Get author name from --author flag, git config, or GIT_AUTHOR_NAME."""
    author = getattr(args, "author", None)
    if author:
        return author.lower()
    env_name = os.environ.get("GIT_AUTHOR_NAME")
    if env_name:
        return env_name.lower()
    try:
        result = subprocess.run(
            ["git", "config", "user.name"],
            capture_output=True, text=True, timeout=5,
        )
        if result.returncode == 0 and result.stdout.strip():
            return result.stdout.strip().lower()
    except (subprocess.TimeoutExpired, FileNotFoundError):
        pass
    return "unknown"


def _default_personas_dir() -> str:
    """Find the default personas directory."""
    p = Path(__file__).resolve().parent.parent.parent.parent / "skills" / "peanut-gallery-review" / "personas"
    return str(p) if p.exists() else ""


# ── Subcommand handlers ────────────────────────────────────────────

def cmd_init(args: argparse.Namespace) -> int:
    """Create a new review session."""
    agents = None
    if args.agents:
        if args.agents.startswith("["):
            agents = json.loads(args.agents)
        else:
            agents = json.loads(Path(args.agents).read_text())

    personas_dir = args.personas_dir or _default_personas_dir()

    try:
        session, session_dir = sess.create_session(
            workspace=os.path.abspath(args.workspace),
            base_ref=args.base,
            topic_ref=args.topic,
            agents=agents,
            personas_dir=personas_dir if personas_dir else None,
            timeout=args.timeout,
            session_dir=args.session,
        )
    except RuntimeError as e:
        print(f"Error: {e}", file=sys.stderr)
        return 1

    if not session.diff_stat.strip():
        print("Warning: diff is empty — nothing to review. "
              "Check --base/--topic refs.", file=sys.stderr)

    if args.bead:
        try:
            from . import beads
            bead_id = beads.create_review_bead(session_dir)
            session.bead_id = bead_id
            sess.save_session(session_dir, session)
        except Exception as e:
            print(f"Warning: bead creation failed: {e}", file=sys.stderr)

    print(session_dir)
    return 0


def cmd_launch(args: argparse.Namespace) -> int:
    """Spawn all agents."""
    session_dir = _get_session_dir(args)
    from . import launch
    # When --template is omitted, let launch_agents pick per-agent based on
    # each agent's runner (cursor → MCP-preferred, opencode → CLI).
    results = launch.launch_agents(
        session_dir, args.template,
        dry_run=args.dry_run,
        cli_json=getattr(args, "cli_json", None),
    )
    for r in results:
        pid_str = f"pid={r['pid']}" if r["pid"] else "dry-run"
        print(f"  {r['name']}: {pid_str}")
    return 0


def cmd_add_comment(args: argparse.Namespace) -> int:
    """Add a structured comment — either anchored (file+line) or global."""
    session_dir = _get_session_dir(args)
    author = _get_author(args)

    s = sess.load_session(session_dir)
    round_num = args.round if args.round is not None else sess.current_round(s.state)

    # Body source: --body-file > --body (exactly one required). --body-file is
    # preferred for agent-authored comments because the shell eats backticks
    # and $(...) inside double-quoted --body arguments.
    if args.body_file:
        try:
            body = Path(args.body_file).read_text()
        except OSError as e:
            print(f"Error: could not read --body-file: {e}", file=sys.stderr)
            return 1
    elif args.body is not None:
        body = args.body
    else:
        print("Error: --body or --body-file is required", file=sys.stderr)
        return 1

    # Reply mode: --reply-to <id>. Inherits the parent's file/line so the
    # reply renders in the same thread.
    reply_to_arg = getattr(args, "reply_to", None)
    reply_to: str | None = None
    if reply_to_arg:
        all_comments = store.read_all_comments(session_dir)
        reply_to = store.normalize_reply_to(all_comments, reply_to_arg)
        if reply_to is None:
            print(f"Error: --reply-to comment not found: {reply_to_arg}",
                  file=sys.stderr)
            return 1
        if args.file or args.line is not None or args.end_line is not None \
                or getattr(args, "global_", False):
            print("Error: --reply-to cannot be combined with "
                  "--file/--line/--end-line/--global "
                  "(replies inherit the parent's location)", file=sys.stderr)
            return 1
        parent = next(c for c in all_comments if c.id == reply_to)
        file = parent.file
        line = parent.line
        end_line = None
        is_global = (file == sess.GLOBAL_FILE)
        file_lines = None
    else:
        # Global comment mode: --global OR neither --file nor --line given.
        is_global = getattr(args, "global_", False) or (not args.file and args.line is None)
        file_lines: list[str] | None = None
        if is_global:
            if args.file or args.line is not None or args.end_line is not None:
                print("Error: --global cannot be combined with --file/--line/--end-line",
                      file=sys.stderr)
                return 1
            file = sess.GLOBAL_FILE
            line = 0
            end_line = None
        else:
            if not args.file or args.line is None:
                print("Error: --file and --line are required for anchored comments; "
                      "use --global for high-level feedback", file=sys.stderr)
                return 1
            file = args.file
            line = args.line
            end_line = args.end_line
            file_lines, err = sess.validate_comment_location(s.workspace, file, line)
            if err:
                print(f"Error: {err}", file=sys.stderr)
                return 1

    comment = models.Comment(
        author=author,
        file=file,
        line=line,
        end_line=end_line,
        body=body,
        severity=args.severity,
        round=round_num,
        head_sha=s.current_head,
        reply_to=reply_to,
    )

    store.append_comment(session_dir, comment)

    if reply_to:
        print(f"{comment.id} (reply to {reply_to})")
    elif is_global:
        print(f"{comment.id} (global)")
    elif file_lines and line >= 1:
        print(f"{file}:{line}: {file_lines[line - 1]}")
    else:
        print(comment.id)
    return 0


def cmd_add_global_comment(args: argparse.Namespace) -> int:
    """Convenience wrapper: posts a high-level (global) comment."""
    args.file = None
    args.line = None
    args.end_line = None
    args.global_ = True
    return cmd_add_comment(args)


def cmd_comments(args: argparse.Namespace) -> int:
    """List/filter comments."""
    session_dir = _get_session_dir(args)
    comments = store.read_all_comments(session_dir)
    comments = store.filter_comments(
        comments,
        agent=args.agent,
        file=args.file,
        severity=args.severity,
        round_num=args.round,
        unresolved=args.unresolved,
        include_deleted=args.include_deleted,
    )

    if args.format == "json":
        print(json.dumps([json.loads(c.to_json()) for c in comments], indent=2))
    else:
        # Table format
        if not comments:
            print("No comments found.")
            return 0
        hdr = f"{'ID':<14} {'Agent':<10} {'Sev':<10} {'File':<30} {'Line':>5} {'R':>2} {'Body'}"
        print(hdr)
        print("-" * len(hdr))
        for c in comments:
            if c.deleted:
                flag = "X"
            elif c.resolved:
                flag = "R"
            elif c.stale:
                flag = "*"
            else:
                flag = " "
            body = c.body[:60].replace("\n", " ")
            file_col = "[global]" if c.file == sess.GLOBAL_FILE else c.file
            line_col = "" if c.file == sess.GLOBAL_FILE else str(c.line)
            print(f"{c.id:<14} {c.author:<10} {c.severity:<10} {file_col:<30} {line_col:>5} {c.round:>2}{flag} {body}")
    return 0


def cmd_resolve(args: argparse.Namespace) -> int:
    """Resolve a comment."""
    session_dir = _get_session_dir(args)
    by = args.by or _get_author(args)
    if store.resolve_comment(session_dir, args.comment_id, resolved_by=by):
        print(f"Resolved {args.comment_id}")
        return 0
    print(f"Comment {args.comment_id} not found", file=sys.stderr)
    return 1


def cmd_delete(args: argparse.Namespace) -> int:
    """Soft-delete a comment. Hidden from default listings and from agents."""
    session_dir = _get_session_dir(args)
    by = args.by or _get_author(args)
    if store.delete_comment(session_dir, args.comment_id, deleted_by=by):
        print(f"Deleted {args.comment_id}")
        return 0
    print(f"Comment {args.comment_id} not found", file=sys.stderr)
    return 1


def cmd_undelete(args: argparse.Namespace) -> int:
    """Restore a soft-deleted comment."""
    session_dir = _get_session_dir(args)
    if store.undelete_comment(session_dir, args.comment_id):
        print(f"Undeleted {args.comment_id}")
        return 0
    print(f"Comment {args.comment_id} not found", file=sys.stderr)
    return 1


def cmd_unresolve(args: argparse.Namespace) -> int:
    """Reopen a previously resolved comment thread."""
    session_dir = _get_session_dir(args)
    if store.unresolve_comment(session_dir, args.comment_id):
        print(f"Unresolved {args.comment_id}")
        return 0
    print(f"Comment {args.comment_id} not found", file=sys.stderr)
    return 1


def cmd_signal(args: argparse.Namespace) -> int:
    """Signal an event (agent name from git config)."""
    session_dir = _get_session_dir(args)
    agent = _get_author(args)
    polling.write_signal(session_dir, agent, args.event)
    print(f"Signaled {agent}.{args.event}")
    return 0


def cmd_wait(args: argparse.Namespace) -> int:
    """Wait for an event signal."""
    session_dir = _get_session_dir(args)
    agent = _get_author(args)
    ok = polling.wait_signal(
        session_dir, agent, args.event,
        timeout=args.timeout, poll_interval=args.poll,
    )
    if ok:
        print(f"Received {agent}.{args.event}")
        return 0
    print(f"Timeout waiting for {agent}.{args.event}", file=sys.stderr)
    return 1


def cmd_wait_all(args: argparse.Namespace) -> int:
    """Wait for all agents to signal an event."""
    session_dir = _get_session_dir(args)
    s = sess.load_session(session_dir)
    agents = [a.name for a in s.agents]
    timed_out = polling.wait_all_signals(
        session_dir, agents, args.event,
        timeout=args.timeout, poll_interval=args.poll,
    )
    if not timed_out:
        print(f"All agents signaled {args.event}")
        return 0
    print(f"Timed out waiting for: {', '.join(timed_out)}", file=sys.stderr)
    return 1


_TRIAGE_DONE_PRIOR_STATES = {
    models.SessionState.ROUND1.value,
    models.SessionState.TRIAGE.value,
}


def cmd_signal_all(args: argparse.Namespace) -> int:
    """Signal all agents with an event."""
    session_dir = _get_session_dir(args)
    s = sess.load_session(session_dir)
    agents = [a.name for a in s.agents]
    polling.signal_all(session_dir, agents, args.event)
    print(f"Signaled {args.event} to {', '.join(agents)}")

    # Triage-done advances the session into Round 2 so any comments agents
    # post next are tagged round=2 (cli.cmd_add_comment defers to
    # session.current_round(state) when --round isn't supplied). Allow the
    # transition from either ROUND1 or TRIAGE — orchestrators sometimes
    # signal before writing triage.json. Skipped if already past Round 2
    # (COMPLETE/ABORTED) so we don't resurrect a closed session.
    if args.event == "triage-done" and s.state in _TRIAGE_DONE_PRIOR_STATES:
        sess.transition_state(session_dir, models.SessionState.ROUND2.value)

    return 0


def cmd_ask(args: argparse.Namespace) -> int:
    """Ask a question and block until answered."""
    session_dir = _get_session_dir(args)
    agent = _get_author(args)
    q = polling.write_question(session_dir, agent, args.question)
    print(f"Asked {q.id}: {q.question}", file=sys.stderr)
    reply = polling.wait_reply(
        session_dir, agent, q.id,
        timeout=args.timeout,
    )
    if reply:
        print(reply.answer)
        return 0
    print("Timeout waiting for reply", file=sys.stderr)
    return 1


def cmd_inbox(args: argparse.Namespace) -> int:
    """Show unanswered questions."""
    session_dir = _get_session_dir(args)
    questions = polling.list_unanswered(session_dir, agent=args.agent)
    if not questions:
        print("No unanswered questions.")
        return 0
    for q in questions:
        print(f"[{q.agent}] {q.id}: {q.question}")
    return 0


def cmd_reply(args: argparse.Namespace) -> int:
    """Reply to an agent's question."""
    session_dir = _get_session_dir(args)
    polling.write_reply(session_dir, args.agent, args.id, args.answer)
    print(f"Replied to {args.agent}/{args.id}")
    return 0


def cmd_triage(args: argparse.Namespace) -> int:
    """Write triage.json from applied/dismissed decisions."""
    session_dir = _get_session_dir(args)
    s = sess.load_session(session_dir)

    decisions = []
    if args.applied:
        for d in json.loads(args.applied):
            decisions.append(models.TriageDecision(
                comment_id=d["comment_id"],
                action=models.TriageAction.APPLIED.value,
                description=d.get("description", ""),
            ))
    if args.dismissed:
        for d in json.loads(args.dismissed):
            decisions.append(models.TriageDecision(
                comment_id=d["comment_id"],
                action=models.TriageAction.DISMISSED.value,
                rebuttal=d.get("rebuttal", ""),
            ))

    triage = models.Triage(
        original_head=s.original_head,
        triage_commit=args.commit or "",
        fix_diff_commands=[f"git diff {s.original_head}..{args.commit}"] if args.commit else [],
        decisions=decisions,
    )

    triage_path = Path(session_dir) / "triage.json"
    triage_path.write_text(triage.to_json() + "\n")

    # Transition state
    sess.transition_state(session_dir, models.SessionState.TRIAGE.value)
    print(f"Wrote triage.json with {len(decisions)} decisions")
    return 0


def cmd_verdict(args: argparse.Namespace) -> int:
    """Record final verdict."""
    session_dir = _get_session_dir(args)
    s = sess.load_session(session_dir)

    decision = "approve" if args.approve else "request-changes"
    comments = [c for c in store.read_all_comments(session_dir) if not c.deleted]

    # Summary per agent
    agents_summary = []
    for agent_cfg in s.agents:
        ac = [c for c in comments if c.author == agent_cfg.name]
        agents_summary.append({
            "agent": agent_cfg.name,
            "total": len(ac),
            "critical": sum(1 for c in ac if c.severity == "critical"),
            "resolved": sum(1 for c in ac if c.resolved),
        })

    v = models.Verdict(
        decision=decision,
        body=args.body or "",
        agents_summary=agents_summary,
    )

    result_path = Path(session_dir) / "result.json"
    result_path.write_text(v.to_json() + "\n")

    sess.transition_state(session_dir, models.SessionState.COMPLETE.value)

    if args.update_bead:
        try:
            from . import beads
            beads.update_with_verdict(session_dir, decision, args.body or "")
        except Exception as e:
            print(f"Warning: bead update failed: {e}", file=sys.stderr)

    print(f"Verdict: {decision}")
    return 0


def cmd_migrate(args: argparse.Namespace) -> int:
    """Update session for a new HEAD commit, mark comments stale."""
    session_dir = _get_session_dir(args)
    s = sess.load_session(session_dir)

    new_head = args.new_head
    if not new_head:
        result = subprocess.run(
            ["git", "-C", s.workspace, "rev-parse", "HEAD"],
            capture_output=True, text=True, timeout=10,
        )
        if result.returncode != 0:
            print(f"Error: git rev-parse failed: {result.stderr.strip()}", file=sys.stderr)
            return 1
        new_head = result.stdout.strip()

    if new_head == s.current_head:
        print("HEAD unchanged, nothing to migrate")
        return 0

    count = store.mark_stale(session_dir)
    s.current_head = new_head
    sess.save_session(session_dir, s)
    print(f"Migrated to {new_head[:12]}, marked {count} comments stale")
    return 0


def cmd_status(args: argparse.Namespace) -> int:
    """Show session status."""
    session_dir = _get_session_dir(args)
    s = sess.load_session(session_dir)
    sess.refresh_agent_statuses(session_dir, s)

    print(f"Session:  {s.id}")
    print(f"State:    {s.state}")
    print(f"Base:     {s.base_ref}")
    print(f"Head:     {s.current_head[:12]}")
    if s.original_head != s.current_head:
        print(f"Original: {s.original_head[:12]}")
    print(f"Workspace: {s.workspace}")
    if s.bead_id:
        print(f"Bead:     {s.bead_id}")
    print()

    # Agents
    print("Agents:")
    for a in s.agents:
        pid = f" (pid {a.pid})" if a.pid else ""
        print(f"  {a.name:<12} {a.status:<10} {a.model}{pid}")

    # Comment counts — deleted comments are hidden from the total but
    # surfaced separately for transparency.
    all_comments = store.read_all_comments(session_dir)
    live = [c for c in all_comments if not c.deleted]
    deleted = len(all_comments) - len(live)
    if all_comments:
        print()
        parts = [
            f"{len(live)} total",
            f"{sum(1 for c in live if c.severity == 'critical')} critical",
            f"{sum(1 for c in live if c.resolved)} resolved",
            f"{sum(1 for c in live if c.stale)} stale",
        ]
        if deleted:
            parts.append(f"{deleted} deleted")
        print("Comments: " + ", ".join(parts))

    # Signals
    signals_dir = Path(session_dir) / "signals"
    if signals_dir.exists():
        sigs = sorted(f.name for f in signals_dir.iterdir() if f.is_file())
        if sigs:
            print()
            print(f"Signals: {', '.join(sigs)}")

    # Unanswered questions
    questions = polling.list_unanswered(session_dir)
    if questions:
        print()
        print(f"Unanswered questions: {len(questions)}")
        for q in questions:
            print(f"  [{q.agent}] {q.id}: {q.question[:60]}")

    return 0


def _resolve_serve_roots(args: argparse.Namespace) -> tuple[list[Path], str | None]:
    """Pick review roots for serve/stop, plus an optional extra session to bind.

    Precedence: explicit --root wins; otherwise infer from $PEANUT_SESSION's
    parent; otherwise the default `/tmp/peanut-review/`.
    """
    from .web import app as web_app
    roots: list[Path] = []
    if getattr(args, "root", None):
        roots = [Path(r) for r in args.root]

    extra_session: str | None = None
    session_env = getattr(args, "session", None) or os.environ.get("PEANUT_SESSION")
    if session_env:
        sd = Path(session_env)
        if sd.is_dir():
            extra_session = str(sd)
            if not roots:
                roots = [sd.parent]

    if not roots:
        roots = [web_app.DEFAULT_ROOT]

    for r in roots:
        r.mkdir(parents=True, exist_ok=True)
    return roots, extra_session


def cmd_serve(args: argparse.Namespace) -> int:
    """Start the multi-session web UI."""
    from .web import app as web_app
    roots, extra_session = _resolve_serve_roots(args)
    extras = [extra_session] if extra_session else []
    try:
        web_app.serve(
            roots, host=args.host, port=args.port, extra_sessions=extras,
            base_url=args.base_url or "",
        )
    except (RuntimeError, ValueError) as e:
        print(f"Error: {e}", file=sys.stderr)
        return 1
    return 0


def cmd_stop(args: argparse.Namespace) -> int:
    """Stop the web UI server running at <root>/web.pid."""
    from .web import app as web_app
    roots, _ = _resolve_serve_roots(args)
    try:
        payload = web_app.stop(roots[0], timeout=args.timeout)
    except RuntimeError as e:
        print(f"Error: {e}", file=sys.stderr)
        return 1
    url = payload.get("url") or f"pid {payload.get('pid')}"
    print(f"Stopped {url}")
    return 0


def cmd_archive(args: argparse.Namespace) -> int:
    """Export comments to git notes for peanut-review archival."""
    session_dir = _get_session_dir(args)
    s = sess.load_session(session_dir)
    comments = store.read_all_comments(session_dir)

    ref = args.ref or "refs/notes/peanut-review"

    for c in comments:
        note_data = json.dumps(dataclasses.asdict(c), indent=2)
        subprocess.run(
            ["git", "-C", s.workspace, "notes", "--ref", ref,
             "append", "-m", note_data, s.original_head],
            capture_output=True, text=True, timeout=10,
        )

    print(f"Archived {len(comments)} comments to {ref}")
    return 0


# ── Parser construction ────────────────────────────────────────────

def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="peanut-review", description="Structured multi-agent code review CLI")
    p.add_argument("--session", metavar="DIR", help="Session directory (or $PEANUT_SESSION)")
    sub = p.add_subparsers(dest="command")

    # init
    sp = sub.add_parser("init", help="Create a new review session")
    sp.add_argument("--workspace", required=True, help="Repository path")
    sp.add_argument("--base", default="main", help="Base ref (default: main)")
    sp.add_argument("--topic", default="HEAD", help="Topic ref (default: HEAD)")
    sp.add_argument("--agents", help="Agent config JSON array (inline or file path)")
    sp.add_argument("--personas-dir", help="Source dir for persona files")
    sp.add_argument("--timeout", type=int, default=1200, help="Agent timeout (default: 1200)")
    sp.add_argument("--bead", action="store_true", help="Create a br bead")

    # launch
    sp = sub.add_parser("launch", help="Spawn all agents")
    sp.add_argument("--dry-run", action="store_true", help="Print commands only")
    sp.add_argument("--template", help="Agent prompt template path")
    sp.add_argument("--cli-json", help="Path to cli.json for agent permissions")

    # add-comment
    sp = sub.add_parser("add-comment",
                        help="Add a structured comment (anchored or global)")
    sp.add_argument("--file", default=None,
                    help="Relative file path (omit + use --global for high-level feedback)")
    sp.add_argument("--line", type=int, default=None,
                    help="Line number (omit + use --global for high-level feedback)")
    sp.add_argument("--end-line", type=int, default=None, help="End line number")
    sp.add_argument("--global", dest="global_", action="store_true",
                    help="Post a high-level comment with no file/line anchor")
    sp.add_argument("--reply-to", dest="reply_to", default=None, metavar="ID",
                    help="Post as a reply to an existing comment thread "
                         "(file/line are inherited from the parent)")
    sp.add_argument("--body", help="Comment text (watch for shell-eaten backticks — prefer --body-file)")
    sp.add_argument("--body-file", help="Read comment text from FILE (safer for bodies with backticks or $ chars)")
    sp.add_argument("--severity", default="suggestion",
                    choices=["critical", "warning", "suggestion", "nit"],
                    help="Severity (default: suggestion)")
    sp.add_argument("--author", help="Author name (default: git config user.name)")
    sp.add_argument("--round", type=int, default=None, help="Round number (default: auto)")

    # add-global-comment (convenience wrapper around `add-comment --global`)
    sp = sub.add_parser("add-global-comment",
                        help="Add a high-level comment not tied to any file/line")
    sp.add_argument("--body", help="Comment text (watch for shell-eaten backticks — prefer --body-file)")
    sp.add_argument("--body-file", help="Read comment text from FILE (safer for bodies with backticks or $ chars)")
    sp.add_argument("--severity", default="suggestion",
                    choices=["critical", "warning", "suggestion", "nit"],
                    help="Severity (default: suggestion)")
    sp.add_argument("--author", help="Author name (default: git config user.name)")
    sp.add_argument("--round", type=int, default=None, help="Round number (default: auto)")

    # comments
    sp = sub.add_parser("comments", help="List/filter comments")
    sp.add_argument("--agent", help="Filter by agent")
    sp.add_argument("--file", help="Filter by file")
    sp.add_argument("--severity", help="Filter by severity")
    sp.add_argument("--round", type=int, help="Filter by round")
    sp.add_argument("--unresolved", action="store_true", help="Only unresolved")
    sp.add_argument("--include-deleted", action="store_true",
                    help="Include soft-deleted comments (hidden by default)")
    sp.add_argument("--format", default="table", choices=["json", "table"],
                    help="Output format (default: table)")

    # resolve
    sp = sub.add_parser("resolve", help="Resolve a comment")
    sp.add_argument("comment_id", help="Comment ID to resolve")
    sp.add_argument("--by", help="Resolved by (default: git config user.name)")

    # unresolve
    sp = sub.add_parser("unresolve", help="Reopen a resolved comment thread")
    sp.add_argument("comment_id", help="Comment ID to reopen")

    # delete
    sp = sub.add_parser("delete", help="Soft-delete a comment (hides it from default views)")
    sp.add_argument("comment_id", help="Comment ID to delete")
    sp.add_argument("--by", help="Deleted by (default: git config user.name)")

    # undelete
    sp = sub.add_parser("undelete", help="Restore a soft-deleted comment")
    sp.add_argument("comment_id", help="Comment ID to restore")

    # signal
    sp = sub.add_parser("signal", help="Signal an event")
    sp.add_argument("event", help="Event name (e.g. round1-done)")

    # wait
    sp = sub.add_parser("wait", help="Wait for a signal")
    sp.add_argument("event", help="Event name")
    sp.add_argument("--timeout", type=int, default=600, help="Timeout seconds (default: 600)")
    sp.add_argument("--poll", type=float, default=2.0, help="Poll interval (default: 2)")

    # wait-all
    sp = sub.add_parser("wait-all", help="Wait for all agents to signal")
    sp.add_argument("event", help="Event name")
    sp.add_argument("--timeout", type=int, default=600, help="Timeout seconds (default: 600)")
    sp.add_argument("--poll", type=float, default=2.0, help="Poll interval (default: 2)")

    # signal-all
    sp = sub.add_parser("signal-all", help="Signal all agents")
    sp.add_argument("event", help="Event name")

    # ask
    sp = sub.add_parser("ask", help="Ask a question (blocks until reply)")
    sp.add_argument("question", help="Question text")
    sp.add_argument("--timeout", type=int, default=600, help="Timeout seconds (default: 600)")

    # inbox
    sp = sub.add_parser("inbox", help="Show unanswered questions")
    sp.add_argument("--agent", help="Filter by agent")

    # reply
    sp = sub.add_parser("reply", help="Reply to a question")
    sp.add_argument("--agent", required=True, help="Agent name")
    sp.add_argument("--id", required=True, help="Question ID")
    sp.add_argument("answer", help="Answer text")

    # triage
    sp = sub.add_parser("triage", help="Write triage decisions")
    sp.add_argument("--applied", help="JSON array of applied decisions")
    sp.add_argument("--dismissed", help="JSON array of dismissed decisions")
    sp.add_argument("--commit", help="Triage fix commit SHA")

    # verdict
    sp = sub.add_parser("verdict", help="Record final verdict")
    grp = sp.add_mutually_exclusive_group(required=True)
    grp.add_argument("--approve", action="store_true")
    grp.add_argument("--request-changes", action="store_true")
    sp.add_argument("--body", help="Verdict body text")
    sp.add_argument("--update-bead", action="store_true", help="Update bead with verdict")

    # migrate
    sp = sub.add_parser("migrate", help="Update HEAD, mark comments stale")
    sp.add_argument("--new-head", help="New HEAD SHA (default: current HEAD)")

    # status
    sp = sub.add_parser("status", help="Show session status")

    # archive
    sp = sub.add_parser("archive", help="Export comments to git notes")
    sp.add_argument("--ref", help="Git notes ref (default: refs/notes/peanut-review)")

    # serve
    sp = sub.add_parser("serve", help="Start the multi-session web UI")
    sp.add_argument("--host", default="127.0.0.1", help="Bind host (default: 127.0.0.1)")
    sp.add_argument("--port", type=int, default=0,
                    help="Bind port (0 = OS-assigned, default)")
    sp.add_argument("--root", action="append", metavar="PATH",
                    help="Review root to scan for sessions (repeatable). "
                         "Defaults to $PEANUT_SESSION's parent or /tmp/peanut-review/")
    sp.add_argument("--base-url", metavar="PATH",
                    help="Path prefix this server is mounted under when fronted "
                         "by a reverse proxy (e.g. '/pr'). The router assumes "
                         "the upstream already strips it (e.g. caddy handle_path).")

    # stop
    sp = sub.add_parser("stop", help="Stop the multi-session web UI")
    sp.add_argument("--timeout", type=float, default=5.0,
                    help="Seconds to wait for graceful shutdown before SIGKILL (default: 5)")
    sp.add_argument("--root", action="append", metavar="PATH",
                    help="Review root whose server to stop (same default as serve)")

    return p


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    if not args.command:
        parser.print_help()
        return 1

    handler = {
        "init": cmd_init,
        "launch": cmd_launch,
        "add-comment": cmd_add_comment,
        "add-global-comment": cmd_add_global_comment,
        "comments": cmd_comments,
        "resolve": cmd_resolve,
        "unresolve": cmd_unresolve,
        "delete": cmd_delete,
        "undelete": cmd_undelete,
        "signal": cmd_signal,
        "wait": cmd_wait,
        "wait-all": cmd_wait_all,
        "signal-all": cmd_signal_all,
        "ask": cmd_ask,
        "inbox": cmd_inbox,
        "reply": cmd_reply,
        "triage": cmd_triage,
        "verdict": cmd_verdict,
        "migrate": cmd_migrate,
        "status": cmd_status,
        "archive": cmd_archive,
        "serve": cmd_serve,
        "stop": cmd_stop,
    }.get(args.command)

    if handler is None:
        parser.print_help()
        return 1

    return handler(args)
