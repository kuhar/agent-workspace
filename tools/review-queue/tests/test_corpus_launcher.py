from __future__ import annotations

import fcntl
import importlib.util
import json
import subprocess
from pathlib import Path

import pytest

SOURCE = Path(__file__).resolve().parents[3] / "rocjitsu-test-corpus/review.py"
spec = importlib.util.spec_from_file_location("corpus_review", SOURCE)
corpus = importlib.util.module_from_spec(spec)
spec.loader.exec_module(corpus)


@pytest.fixture
def workspace(tmp_path, monkeypatch):
    home = tmp_path / "corpus"
    main = home / corpus.REPO_DIR
    main.mkdir(parents=True)
    subprocess.run(["git", "init", "-b", "develop", main], check=True, capture_output=True)
    (main / "requirements.txt").write_text("pytest\n")
    subprocess.run(["git", "-C", main, "add", "."], check=True)
    subprocess.run(
        [
            "git",
            "-C",
            main,
            "-c",
            "user.name=Test",
            "-c",
            "user.email=test@example.invalid",
            "-c",
            "commit.gpgsign=false",
            "commit",
            "-m",
            "initial",
        ],
        check=True,
        capture_output=True,
    )
    head = corpus.git(main, "rev-parse", "HEAD")
    corpus.git(main, "remote", "add", "origin", str(main))
    corpus.git(main, "update-ref", "refs/pull/43/head", head)
    wrapper = home / "worktrees" / "pr-43"
    wrapper.mkdir(parents=True)
    corpus.atomic_json(
        wrapper / ".review-queue.json",
        {
            "protocol": 1,
            "owner_token": "test-token",
            "repo": corpus.REPOSITORY,
        },
    )
    (wrapper / ".review-queue.lock").touch()
    for key, value in {
        "PROTOCOL": "1",
        "WRAPPER": str(wrapper),
        "ROOT": str(wrapper.parent),
        "OWNER_TOKEN": "test-token",
        "TARGET_HEAD": head,
        "RESULT": str(tmp_path / "results/result.json"),
        "MANUAL_INCLUDE": "0",
    }.items():
        monkeypatch.setenv(f"REVIEW_QUEUE_{key}", value)
    monkeypatch.setattr(corpus, "HOME", home)
    monkeypatch.setattr(corpus, "MAIN", main)
    for name in ("AGENTS.md", ".envrc"):
        (home / name).write_text("test fixture\n")
    corpus.atomic_json(home / ".peanut-review.json", {"agents": []})
    corpus.atomic_json(home / ".cursor/cli.json", {"permissions": {}})
    pr = {
        "number": 43,
        "url": f"https://github.com/{corpus.REPOSITORY}/pull/43",
        "headRefOid": head,
        "baseRefOid": head,
        "state": "OPEN",
        "isDraft": False,
    }
    return wrapper, pr


def test_prepare_and_cleanup_only_owned_worktree(workspace, monkeypatch, capsys):
    wrapper, pr = workspace
    prepared = []
    monkeypatch.setattr(corpus, "setup_environment", prepared.append)
    with corpus.lock(wrapper / ".review-queue.lock", existing=True):
        corpus.prepare(wrapper, pr)
    assert prepared == [wrapper]
    assert corpus.git(wrapper / corpus.REPO_DIR, "rev-parse", "HEAD") == pr["headRefOid"]
    assert (wrapper / ".cursor/cli.json").is_file()
    capsys.readouterr()
    corpus.cleanup(True)
    assert json.loads(capsys.readouterr().out)["safe"] is True
    corpus.cleanup(False)
    assert not wrapper.exists()
    assert corpus.MAIN.is_dir()
    assert corpus.git(corpus.MAIN, "rev-parse", "HEAD") == pr["headRefOid"]
    assert str(wrapper) not in corpus.git(corpus.MAIN, "worktree", "list")


@pytest.mark.parametrize("blocker", ["tracked", "untracked", "unknown", "token", "lock", "repo"])
def test_cleanup_preserves_blocked_workspaces(workspace, monkeypatch, capsys, blocker):
    wrapper, pr = workspace
    monkeypatch.setattr(corpus, "setup_environment", lambda _: None)
    corpus.prepare(wrapper, pr)
    repo = wrapper / corpus.REPO_DIR
    if blocker == "tracked":
        (repo / "requirements.txt").write_text("preserve changes")
    elif blocker == "untracked":
        (repo / "local.txt").write_text("preserve changes")
    elif blocker == "unknown":
        (wrapper / "reviewer-draft.md").write_text("preserve notes")
    elif blocker == "token":
        monkeypatch.setenv("REVIEW_QUEUE_OWNER_TOKEN", "wrong")
    elif blocker == "repo":
        monkeypatch.setattr(corpus, "MAIN", wrapper)
    with (wrapper / ".review-queue.lock").open("r+") as stream:
        if blocker == "lock":
            fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
        capsys.readouterr()
        corpus.cleanup(True)
        assert json.loads(capsys.readouterr().out)["safe"] is False
        with pytest.raises((corpus.ReviewError, subprocess.CalledProcessError)):
            corpus.cleanup(False)
    assert repo.is_dir()


def test_cleanup_does_not_follow_symlink_or_accept_live_review(workspace, monkeypatch):
    wrapper, _ = workspace
    target = corpus.HOME / "saved-data"
    target.mkdir()
    (target / "keep").write_text("keep")
    (wrapper / "venv").symlink_to(target, target_is_directory=True)
    corpus.atomic_json(wrapper / "review-session.json", {"session": "session-path"})

    def active(_):
        raise corpus.ReviewError("review agent is still active")

    monkeypatch.setattr(corpus, "check_session_idle", active)
    with pytest.raises(corpus.ReviewError, match="active"):
        corpus.cleanup(False)
    assert wrapper.exists()
    monkeypatch.setattr(corpus, "check_session_idle", lambda _: None)
    corpus.cleanup(False)
    assert (target / "keep").read_text() == "keep"


@pytest.mark.parametrize(
    "state,draft,code",
    [("OPEN", False, 75), ("CLOSED", False, 1), ("MERGED", False, 1), ("OPEN", True, 1)],
)
@pytest.mark.parametrize("manual_include", [False, True])
def test_pr_validation_rejects_changed_or_ineligible_head(
    workspace, monkeypatch, state, draft, code, manual_include
):
    _, pr = workspace
    pr.update(state=state, isDraft=draft)
    if manual_include:
        monkeypatch.setenv("REVIEW_QUEUE_MANUAL_INCLUDE", "1")
        code = 75
    monkeypatch.setattr(corpus, "command", lambda *args, **kwargs: json.dumps(pr))
    if manual_include or code == 75:
        assert corpus.get_pr(43, pr["headRefOid"]) == pr
    with pytest.raises(corpus.ReviewError) as failure:
        corpus.get_pr(43, "a" * 40)
    assert failure.value.code == code
    assert failure.value.status == ("failed" if code == 75 else "ineligible")


@pytest.mark.parametrize("state,draft", [("UNKNOWN", False), ("OPEN", "false")])
def test_manual_include_still_rejects_invalid_pr_state(workspace, monkeypatch, state, draft):
    _, pr = workspace
    pr.update(state=state, isDraft=draft)
    monkeypatch.setenv("REVIEW_QUEUE_MANUAL_INCLUDE", "1")
    monkeypatch.setattr(corpus, "command", lambda *args, **kwargs: json.dumps(pr))
    with pytest.raises(corpus.ReviewError, match="invalid PR state"):
        corpus.get_pr(43)


@pytest.mark.parametrize("state,draft", [("CLOSED", False), ("MERGED", False), ("OPEN", True)])
def test_standalone_manual_review_allows_inactive_pr(workspace, monkeypatch, state, draft):
    _, pr = workspace
    pr.update(state=state, isDraft=draft)
    monkeypatch.setattr(corpus, "command", lambda *args, **kwargs: json.dumps(pr))
    operations = []

    def execute(operation, spec, **kwargs):
        assert corpus.get_pr(43, pr["headRefOid"]) == pr
        operations.append(operation)
        return 0

    monkeypatch.setattr(corpus, "execute", execute)
    assert corpus.main(["43", "--no-launch"]) == 0
    assert operations == ["prepare", "run"]


def test_prepare_failure_writes_queue_result(workspace, monkeypatch):
    wrapper, _ = workspace

    def changed(*_):
        raise corpus.ReviewError("head moved", code=75)

    monkeypatch.setattr(corpus, "get_pr", changed)
    assert corpus.execute("prepare", "43") == 75
    result = json.loads((corpus.HOME.parent / "results/result.json").read_text())
    assert result["status"] == "failed"
    assert result["exit_code"] == 75
    assert result["error"] == "head moved"
    assert result["repository"] == corpus.REPOSITORY
    assert not (wrapper / corpus.REPO_DIR).exists()


def test_prepare_uses_lock_already_held_by_queue(workspace, monkeypatch):
    wrapper, pr = workspace
    monkeypatch.setattr(corpus, "setup_environment", lambda _: None)
    monkeypatch.setattr(corpus, "get_pr", lambda *_: pr)
    with corpus.lock(wrapper / ".review-queue.lock", existing=True):
        assert corpus.execute("prepare", "43") == 0
    assert corpus.git(wrapper / corpus.REPO_DIR, "rev-parse", "HEAD") == pr["headRefOid"]


def test_rejects_other_repositories_and_symlink_wrappers(workspace, monkeypatch):
    wrapper, _ = workspace
    with pytest.raises(corpus.ReviewError):
        corpus.pr_number("ROCm/rocm-systems#43")
    alias = wrapper.parent / "alias"
    alias.symlink_to(wrapper, target_is_directory=True)
    monkeypatch.setenv("REVIEW_QUEUE_WRAPPER", str(alias))
    with pytest.raises(corpus.ReviewError, match="direct managed-root child"):
        corpus.queue_wrapper()


@pytest.mark.parametrize("existing,no_launch", [(False, False), (True, False), (False, True)])
def test_review_runs_checks_retains_git_and_waits_with_failure_budget(
    workspace, monkeypatch, existing, no_launch
):
    wrapper, pr = workspace
    monkeypatch.setattr(corpus, "setup_environment", lambda _: None)
    corpus.prepare(wrapper, pr)
    monkeypatch.setattr(corpus, "review_environment", lambda _: {})
    monkeypatch.setattr(corpus, "check_session_idle", lambda _: None)
    monkeypatch.setattr(corpus, "get_pr", lambda *_: pr)
    session = corpus.HOME / "sessions/test-review"
    session.parent.mkdir()
    saved = {
        "workspace": str(wrapper),
        "repo_relative": corpus.REPO_DIR,
        "base_ref": pr["baseRefOid"],
        "topic_ref": pr["headRefOid"],
        "current_head": pr["headRefOid"],
        "github": {
            "repo": corpus.REPOSITORY,
            "number": pr["number"],
            "head_sha": pr["headRefOid"],
            "base_sha": pr["baseRefOid"],
        },
        "diff_commands": [f"git diff {pr['baseRefOid']}...{pr['headRefOid']}"],
        "agents": [{"name": name} for name in ("Vera", "Felix", "Petra", "Soren")]
        + [{"name": "Curator", "role": "curator"}],
    }
    if existing:
        corpus.atomic_json(session / "session.json", saved)
    original = corpus.command
    calls = []

    def command(*args, **kwargs):
        args = [str(arg) for arg in args]
        if args[0] == "git":
            return original(*args, **kwargs)
        calls.append(args)
        if "start" in args:
            if "--dry-run" in args:
                return f"Session: {session}"
            corpus.atomic_json(session / "session.json", saved)
        return ""

    monkeypatch.setattr(corpus, "command", command)
    corpus.run_review(wrapper, pr, no_launch=no_launch)
    assert "--ignore=tests/test_corpus.py" in calls[0]
    assert "--collect-only" in calls[1]
    assert any("retain-git" in call for call in calls)
    assert json.loads((wrapper / "review-session.json").read_text())["session"] == str(session)
    launch = [call for call in calls if "launch" in call or "rerun" in call]
    wait = [call for call in calls if "wait-all" in call]
    assert len(launch) == len(wait) == int(not no_launch)
    if not no_launch:
        assert wait[0][-2:] == ["--max-reviewer-failures", "2"]
        if existing:
            assert "rerun" in launch[0]
            assert "Curator" not in launch[0]
            assert launch[0].count("--agent") == 4


def test_test_failure_stops_before_session_or_reviewer_launch(workspace, monkeypatch):
    wrapper, pr = workspace
    monkeypatch.setattr(corpus, "setup_environment", lambda _: None)
    corpus.prepare(wrapper, pr)
    monkeypatch.setattr(corpus, "review_environment", lambda _: {})
    monkeypatch.setattr(corpus, "get_pr", lambda *_: pr)
    original = corpus.command

    def command(*args, **kwargs):
        if str(args[0]) == "git":
            return original(*args, **kwargs)
        assert "pytest" in args
        raise subprocess.CalledProcessError(1, args)

    monkeypatch.setattr(corpus, "command", command)
    assert corpus.execute("run", "43") == 1
    assert not (wrapper / "review-session.json").exists()
