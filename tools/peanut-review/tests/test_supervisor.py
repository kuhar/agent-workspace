"""Tests for per-agent supervisor runtime metadata."""
from __future__ import annotations

import json
import os
import tempfile
import textwrap
from pathlib import Path
from unittest.mock import patch

from peanut_review import session as sess
from peanut_review.supervisor import supervise_agent


def _mock_git(workspace, *args):
    if args == ("rev-parse", "HEAD"):
        return "abc123def456"
    if args[0] == "diff" and "--stat" in args:
        return "+1 -0 1 file"
    return ""


def _make_session_dir() -> str:
    sd = os.path.join(tempfile.mkdtemp(prefix="pr-supervisor-"), "session")
    with patch("peanut_review.session._run_git", side_effect=_mock_git):
        sess.create_session(
            workspace=tempfile.mkdtemp(prefix="pr-workspace-"),
            agents=[{"name": "vera", "model": "test-model", "persona": "vera.md"}],
            session_dir=sd,
            timeout=30,
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
    assert meta["supervisor_pid"] == os.getpid()
    loaded = sess.load_session(sd)
    assert loaded.agents[0].status == "done"
    assert loaded.agents[0].pid is not None
    assert loaded.agents[0].pgid is not None
    assert loaded.agents[0].supervisor_pid == os.getpid()


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
    assert meta["termination_signal"] in {"SIGTERM", "SIGKILL"}
    assert sess.load_session(sd).agents[0].status == "timeout"
