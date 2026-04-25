"""GitHub PR integration via the `gh` CLI.

Every call shells out to `gh` (or `$PEANUT_REVIEW_GH_BIN` for tests). The
caller's existing `gh auth` is reused; we never touch tokens. Push/pull
primitives pass JSON bodies via stdin (`gh api --input -`) so multi-line
bodies, backticks, and shell metacharacters travel verbatim.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
from dataclasses import dataclass


GH_BIN_ENV = "PEANUT_REVIEW_GH_BIN"

# Spec parser: accepts `owner/repo#123`, `owner/repo/pull/123`, and
# `https://github.com/owner/repo/pull/123` (plus `http://` and trailing /).
_SPEC_RE = re.compile(
    r"^(?:https?://[^/]+/)?"
    r"(?P<owner>[^/\s#]+)/(?P<repo>[^/\s#]+?)"
    r"(?:#|/pull/|/pulls/)(?P<num>\d+)/?$"
)


def _gh_bin() -> str:
    return os.environ.get(GH_BIN_ENV) or shutil.which("gh") or "gh"


class GhError(RuntimeError):
    """Raised when a `gh` invocation fails. Carries stderr for diagnosis."""

    def __init__(self, cmd: list[str], rc: int, stderr: str) -> None:
        super().__init__(
            f"{' '.join(cmd[:3])}... failed (rc={rc}): {stderr.strip()}"
        )
        self.cmd = cmd
        self.rc = rc
        self.stderr = stderr


def parse_pr_spec(spec: str) -> tuple[str, int]:
    """Return (`owner/repo`, pr_number). Raises ValueError on bad input."""
    m = _SPEC_RE.match(spec.strip())
    if not m:
        raise ValueError(
            f"invalid PR spec: {spec!r} "
            f"(expected owner/repo#N, owner/repo/pull/N, or a github.com URL)"
        )
    return f"{m['owner']}/{m['repo']}", int(m["num"])


def _run(args: list[str], *, input: str | None = None,
         timeout: int = 60) -> str:
    """Invoke `gh` and return stdout. Raises GhError on non-zero exit."""
    cmd = [_gh_bin(), *args]
    res = subprocess.run(
        cmd, input=input, capture_output=True, text=True, timeout=timeout,
    )
    if res.returncode != 0:
        raise GhError(cmd, res.returncode, res.stderr)
    return res.stdout


def _api(endpoint: str, *, method: str = "GET",
         payload: dict | None = None,
         paginate: bool = False) -> str:
    args = ["api", endpoint]
    if method != "GET":
        args += ["-X", method]
    if paginate:
        args.append("--paginate")
    if payload is not None:
        args += ["--input", "-"]
        return _run(args, input=json.dumps(payload))
    return _run(args)


@dataclass
class PRInfo:
    repo: str
    number: int
    url: str
    title: str
    head_sha: str
    base_sha: str


def fetch_pr_info(repo: str, number: int) -> PRInfo:
    out = _run([
        "pr", "view", str(number),
        "--repo", repo,
        "--json", "number,headRefOid,baseRefOid,url,title",
    ])
    d = json.loads(out)
    return PRInfo(
        repo=repo,
        number=int(d["number"]),
        url=d["url"],
        title=d["title"],
        head_sha=d["headRefOid"],
        base_sha=d["baseRefOid"],
    )


def fetch_review_comments(repo: str, number: int) -> list[dict]:
    """Inline (line-anchored) review comments. Paginated."""
    raw = _api(f"repos/{repo}/pulls/{number}/comments", paginate=True)
    return _parse_paginated(raw)


def fetch_issue_comments(repo: str, number: int) -> list[dict]:
    """PR-level (issue) comments. Paginated."""
    raw = _api(f"repos/{repo}/issues/{number}/comments", paginate=True)
    return _parse_paginated(raw)


def _parse_paginated(raw: str) -> list[dict]:
    """`gh api --paginate` concatenates JSON arrays back-to-back as
    `][`. Split and merge. Empty result returns []."""
    raw = raw.strip()
    if not raw:
        return []
    # Sequential JSON arrays from paginate: `[...][...]` → `[...,...]`.
    merged = "[" + raw[1:-1].replace("][", ",") + "]" if raw.startswith("[") else raw
    parsed = json.loads(merged)
    return parsed if isinstance(parsed, list) else [parsed]


def post_review_comment(
    repo: str,
    number: int,
    *,
    body: str,
    commit_id: str,
    path: str,
    line: int,
    side: str = "RIGHT",
    start_line: int | None = None,
) -> dict:
    """POST an inline review comment. Returns the created comment dict
    (id, html_url, etc.). `commit_id` must be a SHA the PR knows about
    — usually `Session.current_head`.
    """
    payload: dict = {
        "body": body,
        "commit_id": commit_id,
        "path": path,
        "line": line,
        "side": side,
    }
    if start_line is not None and start_line != line:
        payload["start_line"] = start_line
        payload["start_side"] = side
    out = _api(
        f"repos/{repo}/pulls/{number}/comments",
        method="POST", payload=payload,
    )
    return json.loads(out)


def post_issue_comment(repo: str, number: int, *, body: str) -> dict:
    """POST a PR-level (top-of-PR) comment. Returns the created comment dict."""
    out = _api(
        f"repos/{repo}/issues/{number}/comments",
        method="POST", payload={"body": body},
    )
    return json.loads(out)
