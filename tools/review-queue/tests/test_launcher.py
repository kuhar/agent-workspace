from __future__ import annotations

import os
from pathlib import Path

import pytest

from review_queue.launcher import LauncherClient


async def test_log_failure_terminates_and_forgets_supervisor(project, tmp_path: Path) -> None:
    project.launcher.write_text("#!/usr/bin/env bash\necho started\nexec sleep 60\n")
    project.launcher.chmod(0o755)
    client = LauncherClient(grace_seconds=0.1)
    started: list[int] = []

    with pytest.raises(OSError):
        await client.run_logged(
            job_id=7,
            project=project,
            operation="prepare",
            pr_url=f"https://github.com/{project.repo}/pull/7",
            cwd=tmp_path,
            env=os.environ.copy(),
            log_path=Path("/dev/full"),
            on_started=lambda pid, _ticks: started.append(pid),
        )

    assert started
    assert client.processes == {}
    with pytest.raises(ProcessLookupError):
        os.kill(started[0], 0)


async def test_started_callback_failure_terminates_and_forgets_supervisor(
    project, tmp_path: Path
) -> None:
    project.launcher.write_text("#!/usr/bin/env bash\nexec sleep 60\n")
    project.launcher.chmod(0o755)
    client = LauncherClient(grace_seconds=0.1)
    started: list[int] = []

    def fail_after_spawn(pid: int, _ticks: int | None) -> None:
        started.append(pid)
        raise RuntimeError("database unavailable")

    with pytest.raises(RuntimeError, match="database unavailable"):
        await client.run_logged(
            job_id=8,
            project=project,
            operation="prepare",
            pr_url=f"https://github.com/{project.repo}/pull/8",
            cwd=tmp_path,
            env=os.environ.copy(),
            log_path=tmp_path / "job-8.log",
            on_started=fail_after_spawn,
        )

    assert started
    assert client.processes == {}
    with pytest.raises(ProcessLookupError):
        os.kill(started[0], 0)


@pytest.mark.parametrize(
    "state,draft,reason",
    [
        ("MERGED", False, "merged"),
        ("CLOSED", False, "closed"),
        ("OPEN", True, "a draft"),
    ],
)
@pytest.mark.parametrize("operation", ["prepare", "run"])
def test_rocjitsu_adapter_reports_ineligible_before_setup(
    tmp_path, state, draft, reason, operation
):
    import json
    import subprocess

    from review_queue.launcher import failure_details

    adapter = Path(__file__).resolve().parents[3] / "rocjitsu/review-pr.sh"
    root = tmp_path / "wrappers"
    wrapper = root / "pr-12"
    wrapper.mkdir(parents=True)
    (wrapper / ".review-queue.json").write_text(json.dumps({"protocol": 1, "owner_token": "test"}))
    bindir = tmp_path / "bin"
    bindir.mkdir()
    gh = bindir / "gh"
    gh.write_text('#!/bin/sh\nprintf "%s\\n" "$TEST_PR_JSON"\n')
    gh.chmod(0o755)
    result = tmp_path / "result.json"
    env = os.environ | {
        "PATH": str(bindir) + os.pathsep + os.environ["PATH"],
        "REVIEW_QUEUE_PROTOCOL": "1",
        "REVIEW_QUEUE_OWNER_TOKEN": "test",
        "REVIEW_QUEUE_WRAPPER": str(wrapper),
        "REVIEW_QUEUE_ROOT": str(root),
        "REVIEW_QUEUE_TARGET_HEAD": "a" * 40,
        "REVIEW_QUEUE_RESULT": str(result),
        "TEST_PR_JSON": json.dumps(
            {
                "number": 12,
                "state": state,
                "isDraft": draft,
                "headRefOid": "b" * 40,
                "url": "https://github.com/test/repo/pull/12",
            }
        ),
    }
    run = subprocess.run(
        [adapter, "queue", operation, "test/repo#12"], env=env, capture_output=True, text=True
    )
    assert run.returncode == 1, run.stdout + run.stderr
    log = tmp_path / "log"
    log.write_text(run.stdout + run.stderr)
    assert failure_details(
        log_path=log,
        result_path=result,
        operation=operation,
        exit_code=1,
        repo="test/repo",
        number=12,
        head="a" * 40,
    ) == ("ineligible", f"PR #12 is {reason}")
    assert not (wrapper / "rocm-systems").exists()


def test_failure_diagnostics_survive_checkout_footer_and_ignore_prepare_output(tmp_path):
    from review_queue.launcher import failure_details

    log = tmp_path / "log"
    prepare = "error: old preparation diagnostic\n"
    log.write_text(
        prepare + "== Build ==\npreset: clang-23-tsan\n"
        "test.cpp:12: error: missing symbol\n"
        "preset: clang-23-tsan (build failed)\n"
        "== Final checkout ==\nbranch: pr-12\nhead: abc\nready: test/repo#12\n"
    )
    status, reason = failure_details(
        log_path=log,
        result_path=tmp_path / "missing.json",
        operation="run",
        exit_code=1,
        repo="test/repo",
        number=12,
        head="a" * 40,
        log_offset=len(prepare),
        phase="Build › clang-23-tsan › Compile",
    )
    assert status == "failed"
    assert "test.cpp:12: error: missing symbol" in reason
    assert "clang-23-tsan" in reason
    assert "old preparation" not in reason
    assert "ready:" not in reason


@pytest.mark.parametrize("payload", ["not json", "[]", '{"status":"ineligible","error":"merged"}'])
def test_invalid_failure_result_does_not_hide_launcher_failure(tmp_path, payload):
    from review_queue.launcher import failure_details

    result = tmp_path / "result.json"
    result.write_text(payload)
    status, reason = failure_details(
        log_path=tmp_path / "missing.log",
        result_path=result,
        operation="prepare",
        exit_code=1,
        repo="test/repo",
        number=12,
        head="a" * 40,
    )
    assert (status, reason) == ("failed", "launcher prepare exited 1")
