"""Tests for per-agent supervisor runtime metadata."""
from __future__ import annotations

import json
import os
import tempfile
import textwrap
from pathlib import Path
from unittest.mock import patch

from peanut_review import session as sess
from peanut_review.models import GitHubPR
from peanut_review.supervisor import supervise_agent


def _mock_git(workspace, *args):
    if args == ("rev-parse", "HEAD"):
        return "abc123def456"
    if args[0] == "diff" and "--stat" in args:
        return "+1 -0 1 file"
    return ""


def _make_session_dir(
    *,
    agents: list[dict] | None = None,
    github: GitHubPR | None = None,
    include_curator: bool = False,
) -> str:
    sd = os.path.join(tempfile.mkdtemp(prefix="pr-supervisor-"), "session")
    with patch("peanut_review.session._run_git", side_effect=_mock_git):
        sess.create_session(
            workspace=tempfile.mkdtemp(prefix="pr-workspace-"),
            agents=agents or [
                {"name": "vera", "model": "test-model", "persona": "vera.md"},
            ],
            session_dir=sd,
            timeout=30,
            github=github,
            include_curator=include_curator,
        )
    return sd


def _script(tmp_path: Path, name: str, body: str) -> str:
    path = tmp_path / name
    path.write_text("#!/bin/sh\nset -eu\n" + textwrap.dedent(body))
    path.chmod(0o755)
    return str(path)


def test_supervisor_records_done_when_signal_exists(tmp_path):
    sd = _make_session_dir()
    script = _script(
        tmp_path,
        "ok.sh",
        f"""
        mkdir -p "{sd}/log/vera" "{sd}/signals"
        printf '{{"runner":"test","pid":%s}}\\n' "$$" > "{sd}/log/vera/meta.json"
        date -Iseconds > "{sd}/signals/vera.round-done"
        exec sh -c 'exit 0'
        """,
    )

    rc = supervise_agent(
        session_dir=sd,
        agent_name="vera",
        command=[script],
        timeout=5,
        kill_grace=0.1,
    )

    assert rc == 0
    meta = json.loads((Path(sd) / "log" / "vera" / "meta.json").read_text())
    assert meta["exit_code"] == 0
    assert meta["timed_out"] is False
    assert meta["process_state"] == "exited"
    assert meta["heartbeat_at"]
    assert meta["supervisor_pid"] == os.getpid()
    loaded = sess.load_session(sd)
    assert loaded.agents[0].status == "done"
    assert loaded.agents[0].pid is not None
    assert loaded.agents[0].pgid is not None
    assert loaded.agents[0].supervisor_pid == os.getpid()


def test_supervisor_auto_launches_curator_when_github_reviewers_done(tmp_path):
    sd = _make_session_dir(
        agents=[
            {"name": "vera", "model": "test-model", "persona": "vera.md"},
            {"name": "Curator", "model": "gpt-5.5-high", "role": "curator"},
        ],
        github=GitHubPR(repo="acme/foo", number=42),
        include_curator=True,
    )
    script = _script(
        tmp_path,
        "ok.sh",
        f"""
        mkdir -p "{sd}/signals"
        date -Iseconds > "{sd}/signals/vera.round-done"
        exec sh -c 'exit 0'
        """,
    )

    launched = []

    def fake_launch_curator(session_dir):
        launched.append(str(session_dir))
        return [{"name": "Curator", "supervisor_pid": 12345}]

    with patch("peanut_review.launch.launch_curator", side_effect=fake_launch_curator):
        rc = supervise_agent(
            session_dir=sd,
            agent_name="vera",
            command=[script],
            timeout=5,
            kill_grace=0.1,
        )

    assert rc == 0
    assert launched == [sd]
    assert (Path(sd) / "signals" / "Curator.auto-launching").exists()


def test_supervisor_auto_launches_curator_only_once(tmp_path):
    sd = _make_session_dir(
        agents=[
            {"name": "vera", "model": "test-model", "persona": "vera.md"},
            {"name": "Curator", "model": "gpt-5.5-high", "role": "curator"},
        ],
        github=GitHubPR(repo="acme/foo", number=42),
        include_curator=True,
    )
    signals = Path(sd) / "signals"
    signals.mkdir(exist_ok=True)
    (signals / "Curator.auto-launching").write_text("already launching\n")
    script = _script(
        tmp_path,
        "ok.sh",
        f"""
        date -Iseconds > "{sd}/signals/vera.round-done"
        exec sh -c 'exit 0'
        """,
    )

    with patch("peanut_review.launch.launch_curator") as mocked:
        rc = supervise_agent(
            session_dir=sd,
            agent_name="vera",
            command=[script],
            timeout=5,
            kill_grace=0.1,
        )

    assert rc == 0
    mocked.assert_not_called()


def test_supervisor_stops_process_after_round_done_signal(tmp_path):
    sd = _make_session_dir()
    script = _script(
        tmp_path,
        "wait_after_done.sh",
        f"""
        mkdir -p "{sd}/signals"
        date -Iseconds > "{sd}/signals/vera.round-done"
        sleep 30
        """,
    )

    rc = supervise_agent(
        session_dir=sd,
        agent_name="vera",
        command=[script],
        timeout=5,
        kill_grace=0.1,
        round_done_grace=0.1,
        round_done_poll_interval=0.05,
    )

    assert rc != 0
    meta = json.loads((Path(sd) / "log" / "vera" / "meta.json").read_text())
    assert meta["timed_out"] is False
    assert meta["round_done_observed"] is True
    assert meta["stopped_after_round_done"] is True
    assert meta["process_state"] == "stopped"
    assert meta["termination_signal"] in {"SIGTERM", "SIGKILL"}
    loaded = sess.load_session(sd)
    assert loaded.agents[0].status == "done"


def test_supervisor_ignores_stale_round_done_signal(tmp_path):
    sd = _make_session_dir()
    signals = Path(sd) / "signals"
    signals.mkdir(exist_ok=True)
    (signals / "vera.round-done").write_text("stale\n")
    script = _script(
        tmp_path,
        "ignore_stale_done.sh",
        """
        sleep 30
        """,
    )

    rc = supervise_agent(
        session_dir=sd,
        agent_name="vera",
        command=[script],
        timeout=0.2,
        kill_grace=0.1,
        round_done_grace=0.1,
        round_done_poll_interval=0.05,
    )

    assert rc != 0
    meta = json.loads((Path(sd) / "log" / "vera" / "meta.json").read_text())
    assert meta["timed_out"] is True
    assert meta["round_done_observed"] is False
    assert meta["stopped_after_round_done"] is False
    assert meta["process_state"] == "timeout"


def test_supervisor_records_failed_without_signal(tmp_path):
    sd = _make_session_dir()
    script = _script(
        tmp_path,
        "fail.sh",
        f"""
        mkdir -p "{sd}/log/vera"
        printf '{{"runner":"test","pid":%s}}\\n' "$$" > "{sd}/log/vera/meta.json"
        exec sh -c 'exit 7'
        """,
    )

    rc = supervise_agent(
        session_dir=sd,
        agent_name="vera",
        command=[script],
        timeout=5,
        kill_grace=0.1,
    )

    assert rc == 7
    meta = json.loads((Path(sd) / "log" / "vera" / "meta.json").read_text())
    assert meta["exit_code"] == 7
    assert meta["timed_out"] is False
    assert meta["process_state"] == "failed"
    assert sess.load_session(sd).agents[0].status == "failed"


def test_supervisor_records_cursor_runtime_metadata(tmp_path):
    sd = _make_session_dir()
    cursor_home = str(tmp_path / "cursor-home")
    script = _script(
        tmp_path,
        "cursor-agent-task.sh",
        f"""
        mkdir -p "{sd}/log/vera" "{sd}/signals"
        date -Iseconds > "{sd}/signals/vera.round-done"
        exec sh -c 'exit 0'
        """,
    )

    rc = supervise_agent(
        session_dir=sd,
        agent_name="vera",
        command=[script],
        timeout=5,
        env={
            **os.environ,
            "PEANUT_CURSOR_HOME": cursor_home,
        },
        kill_grace=0.1,
    )

    assert rc == 0
    meta = json.loads((Path(sd) / "log" / "vera" / "meta.json").read_text())
    assert meta["runner"] == "cursor"
    assert meta["cursor_home"] == cursor_home
    assert "mcp_config" not in meta


def test_supervisor_records_shell_style_termination_signal(tmp_path):
    sd = _make_session_dir()
    script = _script(
        tmp_path,
        "termish.sh",
        f"""
        mkdir -p "{sd}/log/vera"
        printf '{{"runner":"test","pid":%s}}\\n' "$$" > "{sd}/log/vera/meta.json"
        exec sh -c 'exit 143'
        """,
    )

    rc = supervise_agent(
        session_dir=sd,
        agent_name="vera",
        command=[script],
        timeout=5,
        kill_grace=0.1,
    )

    assert rc == 143
    meta = json.loads((Path(sd) / "log" / "vera" / "meta.json").read_text())
    assert meta["termination_signal"] == "SIGTERM"
    assert meta["process_state"] == "killed"
    assert meta["start"]
    assert sess.load_session(sd).agents[0].status == "failed"


def test_supervisor_times_out_and_kills_process_group(tmp_path):
    sd = _make_session_dir()
    script = _script(
        tmp_path,
        "sleep.sh",
        f"""
        mkdir -p "{sd}/log/vera"
        printf '{{"runner":"test","pid":%s}}\\n' "$$" > "{sd}/log/vera/meta.json"
        exec sleep 30
        """,
    )

    rc = supervise_agent(
        session_dir=sd,
        agent_name="vera",
        command=[script],
        timeout=0.1,
        kill_grace=0.1,
    )

    meta = json.loads((Path(sd) / "log" / "vera" / "meta.json").read_text())
    assert rc < 0
    assert meta["timed_out"] is True
    assert meta["process_state"] == "timeout"
    assert meta["termination_signal"] in {"SIGTERM", "SIGKILL"}
    assert sess.load_session(sd).agents[0].status == "timeout"
