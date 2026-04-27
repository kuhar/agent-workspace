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

    # --gh-pr resolves PR metadata up front: defaults base/topic to PR's
    # base/head SHAs, populates session.github, and (unless overridden) gives
    # the session a readable id like `<owner>-<repo>-pr-<n>` so URLs are
    # nicer than the timestamp+hex auto-generated form.
    # base_ref/topic_ref default to None so we can distinguish "user passed
    # the same string as the default" from "user didn't pass anything". This
    # matters for --gh-pr: if the user explicitly passes refs, honor them;
    # otherwise default to the PR's SHAs (or main/HEAD without --gh-pr).
    session_id = args.id
    github = None
    if args.gh_pr:
        from . import gh
        try:
            repo, number = gh.parse_pr_spec(args.gh_pr)
            pr_info = gh.fetch_pr_info(repo, number)
        except (ValueError, gh.GhError) as e:
            print(f"Error: {e}", file=sys.stderr)
            return 1
        base_ref = args.base if args.base is not None else pr_info.base_sha
        topic_ref = args.topic if args.topic is not None else pr_info.head_sha
        if session_id is None:
            owner, repo_name = pr_info.repo.split("/", 1)
            session_id = f"{owner}-{repo_name}-pr-{pr_info.number}"
        github = models.GitHubPR(
            repo=pr_info.repo,
            number=pr_info.number,
            url=pr_info.url,
            head_sha=pr_info.head_sha,
            base_sha=pr_info.base_sha,
            title=pr_info.title,
        )
    else:
        base_ref = args.base if args.base is not None else "main"
        topic_ref = args.topic if args.topic is not None else "HEAD"

    try:
        session, session_dir = sess.create_session(
            workspace=os.path.abspath(args.workspace),
            base_ref=base_ref,
            topic_ref=topic_ref,
            agents=agents,
            personas_dir=personas_dir if personas_dir else None,
            timeout=args.timeout,
            session_dir=args.session,
            session_id=session_id,
            github=github,
        )
    except (RuntimeError, ValueError) as e:
        print(f"Error: {e}", file=sys.stderr)
        return 1

    if not session.diff_stat.strip():
        print("Warning: diff is empty — nothing to review. "
              "Check --base/--topic refs.", file=sys.stderr)

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
        since=args.since,
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
        hdr = f"{'ID':<14} {'Agent':<10} {'Sev':<10} {'File':<30} {'Line':>5}    {'Body'}"
        print(hdr)
        print("-" * len(hdr))
        for c in comments:
            if c.deleted:
                flag = "X"
            elif c.resolved:
                flag = "R"
            elif c.stale:
                flag = "*"
            elif c.edited_at:
                flag = "E"
            else:
                flag = " "
            body = c.body[:60].replace("\n", " ")
            file_col = "[global]" if c.file == sess.GLOBAL_FILE else c.file
            line_col = "" if c.file == sess.GLOBAL_FILE else str(c.line)
            print(f"{c.id:<14} {c.author:<10} {c.severity:<10} {file_col:<30} {line_col:>5} {flag}  {body}")
            if args.show_edits and c.versions:
                for i, v in enumerate(c.versions, 1):
                    vbody = (v.get("body") or "")[:60].replace("\n", " ")
                    print(f"  v{i} ({v.get('edited_by') or 'orig'}): {vbody}")
                print(f"  v{len(c.versions) + 1} (current, edited by {c.edited_by} at {c.edited_at}): {c.body[:60]}")
    return 0


def cmd_edit(args: argparse.Namespace) -> int:
    """Edit an existing comment's body and/or severity, snapshotting prior
    state into Comment.versions. Rewrites the comment's JSONL row in place.
    """
    session_dir = _get_session_dir(args)
    edited_by = _get_author(args)

    if args.body_file:
        try:
            body: str | None = Path(args.body_file).read_text()
        except OSError as e:
            print(f"Error: could not read --body-file: {e}", file=sys.stderr)
            return 1
    elif args.body is not None:
        body = args.body
    else:
        body = None

    severity: str | None = args.severity

    if body is None and severity is None:
        print("Error: at least one of --body, --body-file, --severity required",
              file=sys.stderr)
        return 1

    if not store.edit_comment(session_dir, args.comment_id,
                              body=body, severity=severity, edited_by=edited_by):
        print(f"Error: comment {args.comment_id} not found", file=sys.stderr)
        return 1
    print(f"Edited {args.comment_id}")
    return 0


def _require_github(session_dir: str) -> tuple[models.Session, models.GitHubPR] | None:
    """Load the session and require it be GitHub-backed.

    Returns (session, github) on success, or None after printing an error
    so the caller can `return 1` instead of process-exiting.
    """
    s = sess.load_session(session_dir)
    if s.github is None:
        print("Error: this session is not GitHub-backed "
              "(re-init with --gh-pr to enable push/pull)", file=sys.stderr)
        return None
    return s, s.github


def cmd_gh_push(args: argparse.Namespace) -> int:
    """Push local comments to the GitHub PR.

    Delegates to `gh_push.plan_push` + `gh_push.execute_push`. The CLI is just
    a thin wrapper so the web UI's preview/confirm modal can share the exact
    same planning + execution path.
    """
    from . import gh_push as _gh_push
    session_dir = _get_session_dir(args)
    pair = _require_github(session_dir)
    if pair is None:
        return 1
    s, ghpr = pair

    comments = store.read_all_comments(session_dir)
    plan = _gh_push.plan_push(comments)

    if plan.total == 0:
        suffix = f" (skipped {plan.skipped_meta} __meta__)" if plan.skipped_meta else ""
        print(f"Nothing to push{suffix}.")
        return 0

    if args.dry_run:
        for c in plan.new_top:
            kind = "global" if c.file == sess.GLOBAL_FILE else f"{c.file}:{c.line}"
            print(f"[dry-run] {c.id} ({c.severity}) → {kind}")
        for c in plan.new_replies:
            parent_ext = plan.ext_map.get(c.reply_to)
            tag = f"reply→gh#{parent_ext}" if parent_ext else "reply→<parent not pushed>"
            print(f"[dry-run] {c.id} → {tag}")
        for c in plan.edits:
            print(f"[dry-run] {c.id} EDIT → gh#{c.external_id}")
        if plan.skipped_meta:
            print(f"[dry-run] skipped {plan.skipped_meta} __meta__")
        return 0

    result = _gh_push.execute_push(session_dir, s, ghpr, plan)
    for item in result.items:
        if item.error:
            print(f"  {item.id}: FAILED — {item.error}", file=sys.stderr)
        elif item.action == "edit":
            print(f"  {item.id} EDIT → gh#{item.external_id}")
        elif item.action == "reply":
            print(f"  {item.id} → gh#{item.external_id} (reply)")
        else:
            print(f"  {item.id} → gh#{item.external_id}")
    print(result.summary())
    return 0 if result.failed == 0 else 1


def cmd_gh_pull(args: argparse.Namespace) -> int:
    """Fetch GitHub PR comments into the local session.

    Thin wrapper over `gh_pull.pull_comments`; the same path is used by the
    web UI's `/api/gh/pull` endpoint so both surfaces stay in lockstep.
    """
    from . import gh, gh_pull
    session_dir = _get_session_dir(args)
    pair = _require_github(session_dir)
    if pair is None:
        return 1
    s, _ = pair
    try:
        result = gh_pull.pull_comments(session_dir, s, dry_run=args.dry_run)
    except gh.GhError as e:
        print(f"Error: {e}", file=sys.stderr)
        return 1
    prefix = "[dry-run] " if args.dry_run else ""
    print(f"{prefix}{result.summary()}")
    return 0


def cmd_gh_push_verdict(args: argparse.Namespace) -> int:
    """Submit `result.json` as a GitHub PR review (verdict).

    Maps decision: approve → APPROVE, request-changes → REQUEST_CHANGES.
    Refuses if no result.json exists or the verdict has already been
    submitted (Verdict.external_review_id set) unless --force is passed.
    """
    from . import gh
    session_dir = _get_session_dir(args)
    pair = _require_github(session_dir)
    if pair is None:
        return 1
    _, ghpr = pair

    result_path = Path(session_dir) / "result.json"
    if not result_path.exists():
        print(f"Error: no result.json — record a verdict first with "
              f"`peanut-review verdict --approve|--request-changes`",
              file=sys.stderr)
        return 1
    v = models.Verdict.from_json(result_path.read_text())

    if v.external_review_id and not args.force:
        print(f"Already submitted (review {v.external_review_id}). "
              f"Use --force to re-submit.", file=sys.stderr)
        return 1

    event_map = {
        "approve": "APPROVE",
        "request-changes": "REQUEST_CHANGES",
        "comment": "COMMENT",
    }
    event = event_map.get(v.decision)
    if event is None:
        print(f"Error: unknown verdict decision {v.decision!r} "
              f"(expected approve, request-changes, or comment)",
              file=sys.stderr)
        return 1

    if args.dry_run:
        body_preview = v.body[:80].replace("\n", " ")
        print(f"[dry-run] {event} on PR {ghpr.repo}#{ghpr.number}"
              + (f' — "{body_preview}…"' if v.body else ""))
        return 0

    try:
        resp = gh.post_pr_review(
            ghpr.repo, ghpr.number, event=event, body=v.body,
        )
    except gh.GhError as e:
        print(f"Error: {e}", file=sys.stderr)
        return 1

    v.external_review_id = str(resp["id"])
    v.external_review_url = resp.get("html_url", "")
    result_path.write_text(v.to_json() + "\n")
    print(f"Submitted {event} → review {v.external_review_id}")
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


def _clear_signals_matching(session_dir: str, suffixes: list[str]) -> None:
    """Remove signals/{*}.{suffix} files for each suffix.

    Used when advancing rounds so stale `next-round` and `round-done` files
    from prior rounds don't auto-satisfy a fresh wait in the next round.
    """
    sigs = Path(session_dir) / "signals"
    if not sigs.is_dir():
        return
    for path in sigs.iterdir():
        if not path.is_file():
            continue
        for suffix in suffixes:
            if path.name.endswith(f".{suffix}"):
                try:
                    path.unlink()
                except FileNotFoundError:
                    pass
                break


def cmd_signal_all(args: argparse.Namespace) -> int:
    """Signal all agents with an event.

    Special-case `next-round`: clears any stale `next-round` / `round-done`
    signal files so a fresh `wait next-round` in the new pass doesn't
    auto-satisfy on a leftover file, and lifts the session out of INIT into
    ROUND on first call. No-op for sessions that are already COMPLETE or
    ABORTED.
    """
    session_dir = _get_session_dir(args)
    s = sess.load_session(session_dir)
    agents = [a.name for a in s.agents]

    if args.event == "next-round":
        if s.state in (models.SessionState.COMPLETE.value,
                       models.SessionState.ABORTED.value):
            print(f"Cannot signal next-round: session state is {s.state}",
                  file=sys.stderr)
            return 1
        _clear_signals_matching(session_dir, ["next-round", "round-done"])
        if s.state == models.SessionState.INIT.value:
            sess.transition_state(session_dir, models.SessionState.ROUND.value)

    polling.signal_all(session_dir, agents, args.event)
    print(f"Signaled {args.event} to {', '.join(agents)}")
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


def cmd_verdict(args: argparse.Namespace) -> int:
    """Record final verdict."""
    session_dir = _get_session_dir(args)
    s = sess.load_session(session_dir)

    if args.approve:
        decision = "approve"
    elif args.request_changes:
        decision = "request-changes"
    else:
        decision = "comment"
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
    sp.add_argument("--base", default=None,
                    help="Base ref (default: main, or PR baseRefOid with --gh-pr)")
    sp.add_argument("--topic", default=None,
                    help="Topic ref (default: HEAD, or PR headRefOid with --gh-pr)")
    sp.add_argument("--agents", help="Agent config JSON array (inline or file path)")
    sp.add_argument("--personas-dir", help="Source dir for persona files")
    sp.add_argument("--timeout", type=int, default=1200, help="Agent timeout (default: 1200)")
    sp.add_argument("--id", default=None, metavar="SLUG",
                    help="Override the auto-generated session id "
                         "([A-Za-z0-9_-], must not be 'api'). Becomes the URL "
                         "path segment for the web UI.")
    sp.add_argument("--gh-pr", default=None, metavar="OWNER/REPO#N",
                    help="Back this session with a GitHub PR. Accepts "
                         "owner/repo#N, owner/repo/pull/N, or a github.com URL. "
                         "Defaults --base/--topic to the PR's base/head SHAs "
                         "and --id to <owner>-<repo>-pr-<N> when not given. "
                         "The workspace must already be a local checkout.")

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
                    choices=["critical", "warning", "suggestion", "nit", "feedback"],
                    help="Severity (default: suggestion). Use `feedback` "
                         "for non-actionable observations (questions, FYI, "
                         "praise) — not as a fallback for unsure findings.")
    sp.add_argument("--author", help="Author name (default: git config user.name)")

    # add-global-comment (convenience wrapper around `add-comment --global`)
    sp = sub.add_parser("add-global-comment",
                        help="Add a high-level comment not tied to any file/line")
    sp.add_argument("--body", help="Comment text (watch for shell-eaten backticks — prefer --body-file)")
    sp.add_argument("--body-file", help="Read comment text from FILE (safer for bodies with backticks or $ chars)")
    sp.add_argument("--severity", default="suggestion",
                    choices=["critical", "warning", "suggestion", "nit", "feedback"],
                    help="Severity (default: suggestion). Use `feedback` "
                         "for non-actionable observations (questions, FYI, "
                         "praise) — not as a fallback for unsure findings.")
    sp.add_argument("--author", help="Author name (default: git config user.name)")

    # comments
    sp = sub.add_parser("comments", help="List/filter comments")
    sp.add_argument("--agent", help="Filter by agent")
    sp.add_argument("--file", help="Filter by file")
    sp.add_argument("--severity", help="Filter by severity")
    sp.add_argument("--since", metavar="ID",
                    help="Return only comments posted after the comment with "
                         "this id (use to poll for new activity since the "
                         "last time you read)")
    sp.add_argument("--unresolved", action="store_true", help="Only unresolved")
    sp.add_argument("--include-deleted", action="store_true",
                    help="Include soft-deleted comments (hidden by default)")
    sp.add_argument("--show-edits", action="store_true",
                    help="In table mode, expand each comment's full edit "
                         "history below it (JSON mode always includes it)")
    sp.add_argument("--format", default="table", choices=["json", "table"],
                    help="Output format (default: table)")

    # gh-push
    sp = sub.add_parser("gh-push",
                        help="Push local comments to the GitHub PR (anchored + global only; replies/edits in stage 2B)")
    sp.add_argument("--dry-run", action="store_true",
                    help="Print what would be pushed without calling gh")

    # gh-pull
    sp = sub.add_parser("gh-pull",
                        help="Fetch new comments + edits from the GitHub PR into the local session")
    sp.add_argument("--dry-run", action="store_true",
                    help="Print what would be pulled without writing locally")

    # gh-push-verdict
    sp = sub.add_parser("gh-push-verdict",
                        help="Submit result.json as a GitHub PR review (approve/request-changes)")
    sp.add_argument("--dry-run", action="store_true",
                    help="Print the planned event without submitting")
    sp.add_argument("--force", action="store_true",
                    help="Re-submit even if a prior review id is already recorded")

    # edit
    sp = sub.add_parser("edit",
                        help="Rewrite a comment's body/severity, keeping the prior version in history")
    sp.add_argument("comment_id", help="Comment ID to edit")
    sp.add_argument("--body", help="New comment text (watch for shell-eaten backticks — prefer --body-file)")
    sp.add_argument("--body-file", help="Read new comment text from FILE")
    sp.add_argument("--severity", default=None,
                    choices=["critical", "warning", "suggestion", "nit", "feedback"],
                    help="New severity (omit to keep current)")
    sp.add_argument("--author", help="Editor name (default: git config user.name)")

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
    sp.add_argument("event", help="Event name (e.g. round-done)")

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

    # verdict
    sp = sub.add_parser("verdict", help="Record final verdict")
    grp = sp.add_mutually_exclusive_group(required=True)
    grp.add_argument("--approve", action="store_true")
    grp.add_argument("--request-changes", dest="request_changes",
                     action="store_true")
    grp.add_argument("--comment", action="store_true",
                     help="Submit review comments without approving or "
                          "blocking — required for self-owned PRs since "
                          "GitHub forbids approve/request-changes on your "
                          "own PR")
    sp.add_argument("--body", help="Verdict body text")

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
        "gh-push": cmd_gh_push,
        "gh-pull": cmd_gh_pull,
        "gh-push-verdict": cmd_gh_push_verdict,
        "edit": cmd_edit,
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
