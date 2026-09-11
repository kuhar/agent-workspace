"""Saved reviews survive linked-worktree recycling and Git garbage collection."""

import json
import os
import subprocess
import sys
import threading
import urllib.request
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from peanut_review import launch
from peanut_review import session as sess
from peanut_review.cli import main
from peanut_review.models import AgentConfig, GitHubPR, Session
from peanut_review.web import app, diff


def git(repo, *args):
    return subprocess.run(
        ["git", "-C", str(repo), *args],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


@pytest.fixture
def review(tmp_path):
    repo = tmp_path / "main"
    repo.mkdir()
    git(repo, "init", "-b", "main")
    git(repo, "config", "user.name", "Test")
    git(repo, "config", "user.email", "test@example.invalid")
    git(repo, "config", "commit.gpgsign", "false")
    (repo / "source.py").write_text("".join(f"x{i} = {i}\n" for i in range(200)))
    git(repo, "add", ".")
    git(repo, "commit", "-m", "base")
    base = git(repo, "rev-parse", "HEAD")
    workspace = tmp_path / "queue" / "pr-1"
    checkout = workspace / "source"
    git(repo, "worktree", "add", "--detach", str(checkout), base)
    source = checkout / "source.py"
    source.write_text(source.read_text().replace("x100 = 100", "x100 = 999"))
    git(checkout, "commit", "-am", "reviewed change")
    sd = tmp_path / "reviews" / "saved"
    s, _ = sess.create_session(
        workspace=str(workspace),
        repo_relative="source",
        base_ref=base,
        session_dir=str(sd),
        session_id="saved",
        github=GitHubPR(repo="test/repo", number=1),
    )
    return repo, checkout, sd, s


def remove_and_prune(repo, checkout):
    git(repo, "worktree", "remove", str(checkout))
    git(repo, "reflog", "expire", "--expire=now", "--all")
    git(repo, "gc", "--prune=now")


def test_saved_review_survives_recycling_and_gc(review):
    repo, checkout, sd, s = review
    assert s.git_common_dir == str(repo / ".git")
    before = diff.parse_diff(sess.review_repo_path(s), s.base_ref, s.topic_ref)
    remove_and_prune(repo, checkout)
    diff.clear_diff_cache()
    loaded = sess.load_session(sd)
    assert sess.repo_path(loaded) == str(checkout)
    assert sess.workspace_head(loaded) is None
    assert (
        diff.parse_diff(sess.review_repo_path(loaded), s.base_ref, s.topic_ref)
        == before
    )
    assert git(repo, "cat-file", "-t", s.current_head) == "commit"


def test_repair_legacy_metadata_preserves_review_identity(review):
    repo, checkout, sd, _ = review
    raw = json.loads((sd / "session.json").read_text())
    raw.pop("git_common_dir")
    (sd / "session.json").write_text(json.dumps(raw))
    git(repo, "worktree", "remove", str(checkout))
    assert main(["--session", str(sd), "retain-git", "--repo", str(repo)]) == 0
    repaired = json.loads((sd / "session.json").read_text())
    assert repaired.pop("git_common_dir") == str(repo / ".git")
    assert repaired == raw
    assert main(["--session", str(sd), "retain-git"]) == 0


def test_repair_missing_commit_leaves_metadata_unchanged(review):
    repo, _, sd, s = review
    s.topic_ref = "f" * 40
    sess.save_session(sd, s)
    before = (sd / "session.json").read_bytes()
    assert main(["--session", str(sd), "retain-git", "--repo", str(repo)]) == 1
    assert (sd / "session.json").read_bytes() == before


def test_sync_retains_previous_and_new_snapshots(review):
    repo, checkout, sd, old = review
    (checkout / "source.py").write_text("new revision\n")
    git(checkout, "commit", "-am", "next revision")
    head = git(checkout, "rev-parse", "HEAD")
    current, changed, _ = sess.sync_session_snapshot(
        sd,
        base_ref=old.base_ref,
        topic_ref=head,
        workspace=str(checkout.parent),
        repo_relative="source",
    )
    assert changed
    remove_and_prune(repo, checkout)
    for oid in (old.base_ref, old.current_head, current.current_head):
        assert git(repo, "cat-file", "-t", oid) == "commit"
    refs = git(
        repo, "for-each-ref", "--format=%(objectname)", "refs/peanut-review/"
    ).splitlines()
    assert set(refs) == {old.base_ref, old.current_head, current.current_head}


def test_legacy_sessions_fall_back_to_execution_repo():
    s = Session(workspace="/workspace", repo_relative="repo")
    assert sess.review_repo_path(s) == "/workspace/repo"


def test_sync_to_replacement_repo_updates_git_storage(review, tmp_path):
    repo, _, sd, old = review
    replacement = tmp_path / "replacement"
    git(repo, "clone", "--no-hardlinks", str(repo), str(replacement))
    git(replacement, "checkout", "--detach", old.current_head)
    current, changed, metadata_changed = sess.sync_session_snapshot(
        sd,
        base_ref=old.base_ref,
        topic_ref=old.current_head,
        workspace=str(replacement),
        repo_relative="",
    )
    assert not changed
    assert metadata_changed
    assert current.git_common_dir == str(replacement / ".git")
    assert sess.repo_path(current) == str(replacement)


def test_retained_git_does_not_allow_launch_in_missing_workspace(review):
    repo, checkout, sd, _ = review
    remove_and_prune(repo, checkout)
    with pytest.raises(ValueError, match="cannot resolve workspace HEAD"):
        launch.launch_agents(str(sd), dry_run=True)


@pytest.mark.parametrize("recycled", [True, False])
def test_curator_restores_pinned_checkout_without_changing_review(review, recycled):
    repo, checkout, sd, original = review
    if recycled:
        remove_and_prune(repo, checkout)
    else:
        git(checkout, "checkout", "--detach", original.base_ref)
        (checkout / "notes.txt").write_text("keep local work\n")
    before = json.loads((sd / "session.json").read_text())
    restored = sess.prepare_curator_workspace(sd)
    assert restored.workspace.startswith(str(sd / "curator-workspaces"))
    assert git(sess.repo_path(restored), "rev-parse", "HEAD") == original.current_head
    assert git(sess.repo_path(restored), "branch", "--show-current") == ""
    after = json.loads((sd / "session.json").read_text())
    for key in ("workspace", "repo_relative"):
        before.pop(key)
        after.pop(key)
    assert after == before
    assert sess.prepare_curator_workspace(sd).workspace == restored.workspace
    if not recycled:
        assert git(checkout, "rev-parse", "HEAD") == original.base_ref
        assert (checkout / "notes.txt").read_text() == "keep local work\n"


def test_curator_restore_refuses_live_reviewers(review):
    from peanut_review import runtime

    repo, checkout, sd, s = review
    s.agents = [AgentConfig(name="Busy", model="test", pid=os.getpid())]
    sess.save_session(sd, s)
    runtime.update_agent_meta(sd, "Busy", {"pid": os.getpid()})
    remove_and_prune(repo, checkout)
    with pytest.raises(ValueError, match="agents are live"):
        sess.prepare_curator_workspace(sd)
    assert sess.load_session(sd).workspace == s.workspace


def test_web_curate_restores_recycled_workspace_and_launches_only_curator(review):
    repo, checkout, sd, s = review
    s.agents = [
        AgentConfig(name="Reviewer", model="test", runner="codex"),
        AgentConfig(name="Curator", model="test", runner="codex", role="curator"),
    ]
    sess.save_session(sd, s)
    remove_and_prune(repo, checkout)
    registry = app.SessionRegistry()
    sid = registry.bind(sd)
    server = app.make_server("127.0.0.1", 0, registry)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        request = urllib.request.Request(
            f"http://127.0.0.1:{server.server_address[1]}/{sid}/api/curator/launch",
            data=b"{}", headers={"Content-Type": "application/json"},
        )
        # Only the model process is stubbed; HTTP, Git recovery, prompts and launch validation run.
        with patch("peanut_review.launch.subprocess", wraps=subprocess) as process_module:
            spawn = process_module.Popen
            spawn.return_value = SimpleNamespace(pid=2147483647)
            with urllib.request.urlopen(request) as response:
                payload = json.load(response)
                assert response.status == 202
            assert [item["name"] for item in payload["results"]] == ["Curator"]
            spawn.assert_called_once()
            restored = sess.load_session(sd)
            assert spawn.call_args.kwargs["cwd"] == restored.workspace
        assert git(sess.repo_path(restored), "rev-parse", "HEAD") == s.current_head
        assert str(Path(restored.workspace) / "repo") in (sd / "prompts/Curator.md").read_text()
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


@pytest.mark.parametrize("failures", [2, 3])
@pytest.mark.parametrize("build_failure", [None, "Configure", "Compile"])
def test_rocjitsu_launcher_uses_half_reviewer_failure_budget(review, tmp_path, failures, build_failure):
    repo, checkout, original_sd, s = review
    sd = original_sd.parent / "launcher-session"
    (checkout / "emulation/rocjitsu").mkdir(parents=True)
    git(repo, "update-ref", "refs/pull/1/head", s.current_head)
    git(checkout, "remote", "add", "origin", str(repo))
    pr = {
        "number": 1, "title": "Test review", "url": "https://github.com/test/repo/pull/1",
        "headRefName": "feature", "headRefOid": s.current_head,
        "baseRefName": "main", "baseRefOid": s.base_ref, "updatedAt": "2026-09-10T00:00:00Z",
    }
    config = checkout.parent / ".peanut-review.json"
    config.write_text(json.dumps({"repoRelative": "source", "reviewAgentTimeoutSeconds": 1}))
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    gh = bin_dir / "gh"
    gh.write_text(f"#!{sys.executable}\nimport os\nprint(os.environ['TEST_PR_JSON'])\n")
    gh.chmod(0o755)
    cmake = bin_dir / "cmake"
    cmake.write_text("#!/bin/sh\n" + 'case "$1" in\n  --preset) phase=Configure ;;\n  --build) phase=Compile ;;\nesac\nif [ "$phase" = "$TEST_BUILD_FAILURE" ]; then\n  echo \'source.cpp:12: error: synthetic compiler failure\' >&2\n  exit 7\nfi\n')
    cmake.chmod(0o755)
    client = bin_dir / "review-client"
    client.write_text(f"#!{sys.executable}\n" + '''
import json, os, sys
from pathlib import Path
from unittest.mock import patch
from peanut_review import session, polling, runtime
from peanut_review.cli import main
from peanut_review.models import GitHubPR
sd = os.environ["TEST_SESSION"]
pr = json.loads(os.environ["TEST_PR_JSON"])
args = sys.argv[1:]
if args[0] == "start":
    if "--dry-run" not in args and not (Path(sd) / "session.json").exists():
        session.create_session(
            workspace=os.environ["REVIEW_PARENT"], repo_relative="source",
            base_ref=pr["baseRefOid"], topic_ref=pr["headRefOid"], session_dir=sd,
            agents=[{"name": f"reviewer{i}", "model": "test"} for i in range(4)]
                + [{"name": "Curator", "model": "test", "role": "curator"}],
            github=GitHubPR(repo="test/repo", number=1, base_sha=pr["baseRefOid"], head_sha=pr["headRefOid"]),
            include_curator=True,
        )
    print("Session:", sd)
    print("Workspace:", os.environ["REVIEW_PARENT"])
    sys.exit(0)
if "launch" in args or "rerun" in args:
    for p in (Path(sd) / "signals").glob("*.round-done"):
        p.unlink()
    for i in range(4):
        if i < int(os.environ["TEST_FAILURES"]):
            runtime.update_agent_meta(sd, f"reviewer{i}", {"exit_code": 1})
        else:
            polling.write_signal(sd, f"reviewer{i}", "round-done")
    sys.exit(0)
def curate(path):
    polling.write_signal(path, "Curator", "round-done")
    return [{"name": "Curator", "supervisor_pid": 12345}]
with patch("peanut_review.launch.launch_curator", side_effect=curate):
    sys.exit(main(args))
''')
    client.chmod(0o755)
    env = os.environ | {
        "PATH": str(bin_dir) + os.pathsep + os.environ["PATH"],
        "TEST_PR_JSON": json.dumps(pr), "TEST_SESSION": str(sd), "TEST_FAILURES": str(failures),
        "TEST_BUILD_FAILURE": build_failure or "", "CMAKE_PRESETS": "default clang-23-tsan",
        "PR_BIN": str(client), "REVIEW_PARENT": str(checkout.parent), "WORKSPACE": str(checkout),
        "ROCJITSU_SOURCE": str(checkout / "emulation/rocjitsu"),
    }
    script = Path(__file__).resolve().parents[3] / "rocjitsu/review-pr.sh"
    for _ in range(2):  # Both initial launch and reuse/rerun must pass the same policy.
        result = subprocess.run(
            [script, "--no-pytest", pr["url"]], env=env,
            text=True, capture_output=True, check=False,
        )
        scopes = []
        entered = []
        for line in result.stdout.splitlines():
            if line.startswith("::group::"):
                scopes.append(line[len("::group::"):])
                entered.append(tuple(scopes))
            elif line == "::endgroup::":
                assert scopes, result.stdout
                scopes.pop()
        if build_failure:
            assert result.returncode == 7, result.stdout + result.stderr
            assert scopes == ["Build", "default", build_failure]
            assert not (sd / "session.json").exists()
            continue
        assert ("Review", "Reviewers") in entered
        if failures == 2:
            assert ("Review", "Curator") in entered
            assert not scopes
        else:
            assert scopes == ["Review", "Reviewers"]
        assert result.returncode == (0 if failures == 2 else 1), result.stdout + result.stderr
        assert "allow up to 2 of 4 reviewers" in result.stdout
        assert (sd / "signals/Curator.round-done").exists() == (failures == 2)


@pytest.mark.parametrize("github_backed", [True, False])
def test_web_diff_fold_comments_and_preview_after_recycling(review, github_backed):
    repo, checkout, sd, s = review
    if not github_backed:
        s.github = None
        sess.save_session(sd, s)
    remove_and_prune(repo, checkout)
    registry = app.SessionRegistry()
    sid = registry.bind(sd)
    server = app.make_server("127.0.0.1", 0, registry)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    url = f"http://127.0.0.1:{server.server_address[1]}/{sid}"
    try:
        with urllib.request.urlopen(url + "/") as response:
            assert response.status == 200
            assert b"source.py" in response.read()
        with urllib.request.urlopen(
            url + "/api/diff/fold?file=source.py&start=0&end=5"
        ) as response:
            lines = json.load(response)["lines"]
            assert len(lines) == 5
            assert lines[0]["content"] == "x0 = 0"
        request = urllib.request.Request(
            url + "/api/comments",
            data=json.dumps(
                {
                    "author": "Human",
                    "file": "source.py",
                    "line": 101,
                    "body": "Check this value",
                }
            ).encode(),
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(request) as response:
            assert response.status == 201
        if github_backed:
            with urllib.request.urlopen(url + "/api/gh/preview") as response:
                preview = json.load(response)
                assert response.status == 200
                assert "Check this value" in json.dumps(preview)
    finally:
        server.shutdown()
        server.server_close()
        thread.join()
