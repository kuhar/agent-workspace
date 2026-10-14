"""Corpus PR preparation, peanut review, and owned queue worktree cleanup."""

from __future__ import annotations

import argparse
import contextlib
import datetime
import fcntl
import json
import os
import re
import shutil
import subprocess
import sys
import uuid
from pathlib import Path

REPOSITORY = "ROCm/rocjitsu-test-corpus"
REPO_DIR = "rocjitsu-test-corpus"
TOOLS = Path(__file__).resolve().parents[1] / "tools" / "peanut-review"
HOME = Path(os.environ.get("CORPUS_HOME", Path(os.environ.get("ROCJITSUS", Path.home() / "rocjitsu")) / "corpus")).resolve()
MAIN = HOME / REPO_DIR
PR_BIN = Path(os.environ.get("PR_BIN", TOOLS / "bin/peanut-review"))
NIGHTLY = "https://rocm.nightlies.amd.com/whl-multi-arch/"
OWNED_ENTRIES = {
    ".review-queue.json",
    ".review-queue.lock",
    ".peanut-review.json",
    ".cursor",
    "AGENTS.md",
    ".envrc",
    "venv",
    ".cache",
    ".pytest_cache",
    ".ruff_cache",
    ".beads",
    "review-session.json",
    REPO_DIR,
}


class ReviewError(RuntimeError):
    def __init__(self, message: str, code: int = 1, status: str = "failed"):
        super().__init__(message)
        self.code = code
        self.status = status


def command(*args, cwd=None, capture=False, env=None):
    result = subprocess.run(
        [str(arg) for arg in args],
        cwd=cwd,
        env=env,
        text=True,
        stdout=subprocess.PIPE if capture else None,
        check=True,
    )
    return result.stdout.strip() if capture else ""


def git(repo, *args):
    return command("git", "-C", repo, *args, capture=True)


def atomic_json(path: Path, value) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    temporary.write_text(json.dumps(value, indent=2) + "\n")
    temporary.replace(path)


@contextlib.contextmanager
def phase(title):
    print(f"::group::{title}", flush=True)
    yield
    print("::endgroup::", flush=True)


@contextlib.contextmanager
def lock(path: Path, *, existing=False):
    with path.open("r+" if existing else "a+") as stream:
        try:
            fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as error:
            raise ReviewError(f"ownership lock is active: {path}") from error
        yield


def pr_number(spec: str) -> int:
    match = re.fullmatch(
        rf"(?:https://github\.com/{REPOSITORY}/pull/|{REPOSITORY}#)?([0-9]+)/?", spec
    )
    if not match or int(match[1]) <= 0:
        raise ReviewError(f"expected a PR number or {REPOSITORY} PR URL: {spec}")
    return int(match[1])


def get_pr(number: int, requested_head: str = ""):
    data = json.loads(
        command(
            "gh",
            "pr",
            "view",
            number,
            "--repo",
            REPOSITORY,
            "--json",
            "number,url,headRefName,headRefOid,baseRefName,baseRefOid,state,isDraft",
            capture=True,
        )
    )
    if data["state"] not in {"OPEN", "CLOSED", "MERGED"} or not isinstance(data["isDraft"], bool):
        raise ReviewError("invalid PR state from GitHub")
    if os.environ.get("REVIEW_QUEUE_MANUAL_INCLUDE") != "1" and (
        data["state"] != "OPEN" or data["isDraft"]
    ):
        raise ReviewError(
            f"PR #{number} is closed, merged, or draft", status="ineligible"
        )
    if requested_head and data["headRefOid"] != requested_head:
        raise ReviewError("PR head changed since it was queued", code=75)
    for key in ("headRefOid", "baseRefOid"):
        if not re.fullmatch(r"[0-9a-f]{40}", data[key]):
            raise ReviewError(f"invalid {key} from GitHub")
    return data


def queue_wrapper() -> Path:
    if os.environ.get("REVIEW_QUEUE_PROTOCOL") != "1":
        raise ReviewError("REVIEW_QUEUE_PROTOCOL=1 is required")
    for name in ("ROOT", "WRAPPER", "OWNER_TOKEN", "TARGET_HEAD"):
        if not os.environ.get(f"REVIEW_QUEUE_{name}"):
            raise ReviewError(f"missing REVIEW_QUEUE_{name}")
    supplied = Path(os.environ["REVIEW_QUEUE_WRAPPER"])
    wrapper = supplied.resolve(strict=True)
    root = Path(os.environ["REVIEW_QUEUE_ROOT"]).resolve(strict=True)
    if supplied.is_symlink() or wrapper.parent != root:
        raise ReviewError("wrapper is not a direct managed-root child")
    marker = wrapper / ".review-queue.json"
    if marker.is_symlink() or (wrapper / ".review-queue.lock").is_symlink():
        raise ReviewError("ownership marker or lock is a symlink")
    data = json.loads(marker.read_text())
    if (
        data.get("protocol") != 1
        or data.get("owner_token") != os.environ["REVIEW_QUEUE_OWNER_TOKEN"]
    ):
        raise ReviewError("ownership marker does not match")
    if data.get("repo") != REPOSITORY:
        raise ReviewError("ownership marker belongs to another repository")
    return wrapper


def check_checkout(repo: Path) -> None:
    if repo.is_symlink() or Path(git(repo, "rev-parse", "--show-toplevel")) != repo:
        raise ReviewError("managed worktree is missing or invalid")
    common = git(repo, "rev-parse", "--path-format=absolute", "--git-common-dir")
    if (
        Path(common).resolve()
        != Path(
            git(MAIN, "rev-parse", "--path-format=absolute", "--git-common-dir")
        ).resolve()
    ):
        raise ReviewError("managed worktree belongs to another repository")
    if git(
        repo,
        "status",
        "--porcelain",
        "--untracked-files=all",
        "--ignore-submodules=none",
    ):
        raise ReviewError("source worktree has tracked or untracked changes")


def check_session_idle(session_dir: Path) -> None:
    # A reviewer may outlive its launcher; completion signals alone are not enough.
    sys.path.insert(0, str(TOOLS))
    from peanut_review import runtime, session

    current = session.load_session(session_dir)
    for agent in current.agents:
        state = runtime.inspect_agent_runtime(session_dir, agent)
        if (
            state["reviewer_live"]
            or state["supervisor_live"]
            or state["process_state"] == "launching"
        ):
            raise ReviewError(f"review agent is still active: {agent.name}")


def cleanup_preflight(wrapper: Path) -> None:
    for entry in wrapper.iterdir():
        if entry.name not in OWNED_ENTRIES:
            raise ReviewError(f"unknown top-level entry: {entry.name}")
    repo = wrapper / REPO_DIR
    if os.path.lexists(repo):
        check_checkout(repo)
    link = wrapper / "review-session.json"
    if link.exists():
        check_session_idle(Path(json.loads(link.read_text())["session"]))


def cleanup(check_only: bool) -> None:
    try:
        wrapper = queue_wrapper()
        with lock(wrapper / ".review-queue.lock", existing=True):
            cleanup_preflight(wrapper)
            if check_only:
                print(json.dumps({"safe": True, "reason": "safe to recycle"}))
                return
            repo = wrapper / REPO_DIR
            if repo.exists():
                command("git", "-C", MAIN, "worktree", "remove", repo)
            # Recheck entries before deleting only the known wrapper assets.
            for entry in wrapper.iterdir():
                if entry.name not in OWNED_ENTRIES or entry.name == REPO_DIR:
                    raise ReviewError(f"unexpected entry during cleanup: {entry.name}")
                if entry.is_dir() and not entry.is_symlink():
                    shutil.rmtree(entry)
                else:
                    entry.unlink()
            wrapper.rmdir()
    except (ReviewError, OSError, ValueError, subprocess.CalledProcessError) as error:
        if not check_only:
            raise
        print(json.dumps({"safe": False, "reason": str(error)}))


def setup_environment(wrapper: Path) -> None:
    venv = wrapper / "venv"
    if not (venv / "bin/python").exists():
        command("uv", "venv", "--python", "3.12", venv)
    command(
        "uv",
        "pip",
        "install",
        "--python",
        venv / "bin/python",
        "--prerelease",
        "allow",
        "--index",
        NIGHTLY,
        "rocm[libraries,devel]",
        "-r",
        wrapper / REPO_DIR / "requirements.txt",
    )


def prepare(wrapper: Path, pr) -> None:
    with phase("Fetch PR"):
        ref = f"refs/remotes/origin/review-queue/{pr['number']}"
        command(
            "git",
            "-C",
            MAIN,
            "fetch",
            "origin",
            f"+pull/{pr['number']}/head:{ref}",
            pr["baseRefOid"],
        )
        if git(MAIN, "rev-parse", ref) != pr["headRefOid"]:
            raise ReviewError("fetched PR head changed", code=75)
    with phase("Prepare workspace"):
        repo = wrapper / REPO_DIR
        if os.path.lexists(repo):
            check_checkout(repo)
            if (wrapper / "review-session.json").exists():
                check_session_idle(
                    Path(
                        json.loads((wrapper / "review-session.json").read_text())[
                            "session"
                        ]
                    )
                )
            command("git", "-C", repo, "checkout", "--detach", pr["headRefOid"])
        else:
            command(
                "git", "-C", MAIN, "worktree", "add", "--detach", repo, pr["headRefOid"]
            )
        for name in (".peanut-review.json", "AGENTS.md", ".envrc"):
            shutil.copy2(HOME / name, wrapper / name)
        (wrapper / ".cursor").mkdir(exist_ok=True)
        shutil.copy2(HOME / ".cursor/cli.json", wrapper / ".cursor/cli.json")
    with phase("Install Python dependencies and TheRock SDK"):
        setup_environment(wrapper)


def review_environment(wrapper: Path):
    env = os.environ.copy()
    venv = wrapper / "venv"
    sdk = command(venv / "bin/rocm-sdk", "path", "--root", capture=True)
    env.update(
        {
            "VIRTUAL_ENV": str(venv),
            "ROCM_PATH": sdk,
            "ROCM_HOME": sdk,
            "HIP_PATH": sdk,
            "CMAKE_OBJDUMP": f"{sdk}/lib/llvm/bin/llvm-objdump",
            "PATH": f"{venv}/bin:{sdk}/bin:{sdk}/lib/llvm/bin:{env.get('PATH', '')}",
        }
    )
    return env


def run_review(wrapper: Path, pr, *, no_launch=False) -> None:
    repo = wrapper / REPO_DIR
    check_checkout(repo)
    if git(repo, "rev-parse", "HEAD") != pr["headRefOid"]:
        raise ReviewError("workspace HEAD differs from requested review head")
    config = json.loads((wrapper / ".peanut-review.json").read_text())
    review_timeout = int(config.get("reviewAgentTimeoutSeconds", 2700))
    if review_timeout <= 0:
        raise ReviewError("reviewAgentTimeoutSeconds must be a positive integer")
    env = review_environment(wrapper)
    python = wrapper / "venv/bin/python"
    with phase("Python tests"):
        command(
            python,
            "-m",
            "pytest",
            "tests",
            "--ignore=tests/test_corpus.py",
            "-q",
            "-o",
            f"cache_dir={wrapper}/.pytest_cache",
            cwd=repo,
            env=env,
        )
    with phase("Collect corpus tests"):
        command(
            python,
            "-m",
            "pytest",
            "--collect-only",
            "-q",
            "tests/test_corpus.py",
            "--target",
            "gfx1250",
            "-o",
            f"cache_dir={wrapper}/.pytest_cache",
            cwd=repo,
            env=env,
        )
    start = [
        PR_BIN,
        "start",
        pr["url"],
        "--config",
        wrapper / ".peanut-review.json",
        "--base",
        pr["baseRefOid"],
        "--topic",
        pr["headRefOid"],
        "--timeout",
        review_timeout,
        "--no-launch",
    ]
    with phase("Prepare review session"):
        preview = command(*start, "--dry-run", capture=True, env=env)
        match = re.search(r"^Session:\s+(.+)$", preview, re.MULTILINE)
        if not match:
            raise ReviewError("could not resolve session from peanut-review dry-run")
        session = Path(match[1])
    with lock(session.with_name(session.name + ".run.lock")):
        existed = (session / "session.json").exists()
        if existed:
            check_session_idle(session)
        command(*start, "--reuse", "--sync", env=env)
        command(PR_BIN, "--session", session, "retain-git", env=env)
        saved = json.loads((session / "session.json").read_text())
        if (
            any(
                saved[key] != value
                for key, value in {
                    "workspace": str(wrapper),
                    "repo_relative": REPO_DIR,
                    "base_ref": pr["baseRefOid"],
                    "topic_ref": pr["headRefOid"],
                    "current_head": pr["headRefOid"],
                }.items()
            )
            or any(
                saved["github"].get(key) != value
                for key, value in {
                    "repo": REPOSITORY,
                    "number": pr["number"],
                    "base_sha": pr["baseRefOid"],
                    "head_sha": pr["headRefOid"],
                }.items()
            )
            or saved["diff_commands"]
            != [f"git diff {pr['baseRefOid']}...{pr['headRefOid']}"]
        ):
            raise ReviewError("peanut session differs from requested snapshot")
        atomic_json(wrapper / "review-session.json", {"session": str(session)})
        print(f"Session: {session}", flush=True)
        if no_launch:
            return
        get_pr(pr["number"], pr["headRefOid"])
        if git(repo, "rev-parse", "HEAD") != pr["headRefOid"]:
            raise ReviewError("workspace HEAD changed before reviewer launch")
        reviewers = [a for a in saved["agents"] if a.get("role") != "curator"]
        with phase("Launch reviewers"):
            args = []
            if existed:
                args = [part for a in reviewers for part in ("--agent", a["name"])]
            command(
                PR_BIN,
                "--session",
                session,
                "rerun" if existed else "launch",
                *args,
                env=env,
            )
        with phase("Review"):
            command(
                PR_BIN,
                "--session",
                session,
                "wait-all",
                "round-done",
                "--timeout",
                review_timeout,
                "--max-reviewer-failures",
                len(reviewers) // 2,
                env=env,
            )
        if git(repo, "rev-parse", "HEAD") != pr["headRefOid"]:
            raise ReviewError("workspace HEAD changed during review")


def execute(operation: str, spec: str, *, no_launch=False) -> int:
    wrapper = queue_wrapper()
    requested = os.environ["REVIEW_QUEUE_TARGET_HEAD"]
    number = pr_number(spec)
    started = datetime.datetime.now(datetime.timezone.utc).isoformat()
    code, status, error = 0, "succeeded", ""
    try:
        # The scheduler holds the wrapper lock across both operations. Standalone
        # invocation takes the same lock in main; reacquiring it here deadlocks.
        with phase("Check PR"):
            pr = get_pr(number, requested)
        if operation == "prepare":
            prepare(wrapper, pr)
        else:
            run_review(wrapper, pr, no_launch=no_launch)
    except (ReviewError, OSError, ValueError, subprocess.CalledProcessError) as failure:
        code = getattr(failure, "code", getattr(failure, "returncode", 1))
        status = getattr(failure, "status", "failed")
        error = str(failure)
        print(f"Error: {error}", file=sys.stderr, flush=True)
    result = os.environ.get("REVIEW_QUEUE_RESULT")
    if result:
        head = ""
        if (wrapper / REPO_DIR).exists():
            head = git(wrapper / REPO_DIR, "rev-parse", "HEAD")
        atomic_json(
            Path(result),
            {
                "protocol": 1,
                "repository": REPOSITORY,
                "pr": number,
                "requested_head": requested,
                "reviewed_head": head,
                "status": status,
                "exit_code": code,
                "error": error,
                "started_at": started,
                "finished_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
            },
        )
    return code


def main(argv=None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    if argv == ["queue", "protocol"]:
        print(
            json.dumps(
                {
                    "version": 1,
                    "repository": REPOSITORY,
                    "operations": ["prepare", "run", "cleanup-check", "cleanup"],
                }
            )
        )
        return 0
    if argv in (["queue", "cleanup"], ["queue", "cleanup", "--check"]):
        cleanup("--check" in argv)
        return 0
    if len(argv) == 3 and argv[:2] in (["queue", "prepare"], ["queue", "run"]):
        return execute(argv[1], argv[2])
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("pr", help=f"PR number or URL in {REPOSITORY}")
    parser.add_argument(
        "--no-launch",
        action="store_true",
        help="prepare, validate, and create session only",
    )
    args = parser.parse_args(argv)
    number = pr_number(args.pr)
    os.environ["REVIEW_QUEUE_MANUAL_INCLUDE"] = "1"
    pr = get_pr(number)
    root = HOME / "worktrees"
    wrapper = root / f"manual-pr-{number}"
    root.mkdir(parents=True, exist_ok=True)
    if not wrapper.exists():
        wrapper.mkdir()
        atomic_json(
            wrapper / ".review-queue.json",
            {
                "protocol": 1,
                "owner_token": uuid.uuid4().hex,
                "repo": REPOSITORY,
            },
        )
        (wrapper / ".review-queue.lock").touch()
    token = json.loads((wrapper / ".review-queue.json").read_text())["owner_token"]
    os.environ.update(
        {
            "REVIEW_QUEUE_PROTOCOL": "1",
            "REVIEW_QUEUE_WRAPPER": str(wrapper),
            "REVIEW_QUEUE_ROOT": str(root),
            "REVIEW_QUEUE_OWNER_TOKEN": token,
            "REVIEW_QUEUE_TARGET_HEAD": pr["headRefOid"],
        }
    )
    with lock(wrapper / ".review-queue.lock", existing=True):
        code = execute("prepare", args.pr)
        return code or execute("run", args.pr, no_launch=args.no_launch)


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (ReviewError, OSError, ValueError, subprocess.CalledProcessError) as failure:
        print(f"Error: {failure}", file=sys.stderr)
        raise SystemExit(getattr(failure, "code", 1))
