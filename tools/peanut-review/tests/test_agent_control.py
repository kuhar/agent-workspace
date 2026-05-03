"""Tests for runtime control of launched agents."""
from __future__ import annotations

import os
import signal
import tempfile
from pathlib import Path
from unittest.mock import patch

from peanut_review import agent_control, runtime, session as sess


def _mock_git(workspace, *args):
    if args == ("rev-parse", "HEAD"):
        return "abc123def456"
    if args[0] == "diff" and "--stat" in args:
        return "+1 -0 1 file"
    return ""


def _make_session_dir(agents: list[dict]) -> str:
    sd = os.path.join(tempfile.mkdtemp(prefix="pr-control-"), "session")
    with patch("peanut_review.session._run_git", side_effect=_mock_git):
        sess.create_session(
            workspace=tempfile.mkdtemp(prefix="pr-workspace-"),
            agents=agents,
            session_dir=sd,
            timeout=30,
        )
    return sd


def _record_runtime(
    sd: str,
    agent: str,
    *,
    pid: int,
    pgid: int,
    supervisor_pid: int,
    runner: str = "cursor",
):
    runtime.update_agent_meta(sd, agent, {
        "runner": runner,
        "process_state": "running",
        "pid": pid,
        "pgid": pgid,
        "supervisor_pid": supervisor_pid,
    })
    sess.update_agent_status(
        sd,
        agent,
        "running",
        pid=pid,
        pgid=pgid,
        supervisor_pid=supervisor_pid,
    )


def test_kill_agents_dry_run_targets_all_supported_runners(monkeypatch):
    sd = _make_session_dir([
        {"name": "vera", "model": "m", "persona": "vera.md", "runner": "cursor"},
        {"name": "petra", "model": "m", "persona": "petra.md", "runner": "opencode"},
        {"name": "cleo", "model": "m", "persona": "cleo.md", "runner": "codex"},
    ])
    runners = {"vera": "cursor", "petra": "opencode", "cleo": "codex"}
    for offset, name in enumerate(["vera", "petra", "cleo"], start=1):
        _record_runtime(
            sd,
            name,
            pid=1000 + offset,
            pgid=2000 + offset,
            supervisor_pid=3000 + offset,
            runner=runners[name],
        )

    monkeypatch.setattr("peanut_review.runtime.is_process_live", lambda pid: True)
    monkeypatch.setattr(
        "peanut_review.agent_control._process_matches_agent",
        lambda *args, **kwargs: (True, "matched"),
    )
    monkeypatch.setattr(
        "peanut_review.agent_control._get_pgid",
        lambda pid: 2000 + (pid - 1000),
    )

    results = agent_control.kill_agents(sd, dry_run=True)

    assert [r["name"] for r in results] == ["vera", "petra", "cleo"]
    assert {r["runner"] for r in results} == {"cursor", "opencode", "codex"}
    assert [r["status"] for r in results] == ["dry-run", "dry-run", "dry-run"]
    assert all(r["signals"][0]["target"] == "pgid" for r in results)
    assert all(r["signals"][0]["signal"] == "SIGTERM" for r in results)


def test_kill_agents_rejects_unverified_reviewer(monkeypatch):
    sd = _make_session_dir([
        {"name": "vera", "model": "m", "persona": "vera.md", "runner": "cursor"},
    ])
    _record_runtime(sd, "vera", pid=111, pgid=222, supervisor_pid=333)

    monkeypatch.setattr("peanut_review.runtime.is_process_live", lambda pid: True)
    monkeypatch.setattr(
        "peanut_review.agent_control._process_matches_agent",
        lambda *args, **kwargs: (False, "session mismatch"),
    )

    sent: list[tuple[int, signal.Signals]] = []
    monkeypatch.setattr(
        "peanut_review.agent_control._signal_process_group",
        lambda pgid, sig: sent.append((pgid, sig)),
    )

    [result] = agent_control.kill_agents(sd)

    assert result["status"] == "error"
    assert result["reason"] == "session mismatch"
    assert sent == []


def test_kill_agents_signals_reviewer_group_and_marks_killed(monkeypatch):
    sd = _make_session_dir([
        {"name": "vera", "model": "m", "persona": "vera.md", "runner": "cursor"},
    ])
    _record_runtime(sd, "vera", pid=111, pgid=222, supervisor_pid=333)
    live = {111: True, 333: True}

    monkeypatch.setattr("peanut_review.runtime.is_process_live", lambda pid: live.get(pid, False))
    monkeypatch.setattr(
        "peanut_review.agent_control._process_matches_agent",
        lambda *args, **kwargs: (True, "matched"),
    )
    monkeypatch.setattr("peanut_review.agent_control._get_pgid", lambda pid: 222)

    sent: list[tuple[int, signal.Signals]] = []

    def fake_killpg(pgid: int, sig: signal.Signals) -> None:
        sent.append((pgid, sig))
        live[111] = False
        live[333] = False

    monkeypatch.setattr("peanut_review.agent_control._signal_process_group", fake_killpg)

    [result] = agent_control.kill_agents(sd, grace_seconds=0)

    assert result["status"] == "killed"
    assert sent == [(222, signal.SIGTERM)]
    meta = runtime.read_agent_meta(sd, "vera")
    assert meta["process_state"] == "killed"
    assert meta["termination_signal"] == "SIGTERM"
    assert sess.load_session(sd).agents[0].status == "failed"


def test_kill_agents_escalates_to_sigkill(monkeypatch):
    sd = _make_session_dir([
        {"name": "vera", "model": "m", "persona": "vera.md", "runner": "cursor"},
    ])
    _record_runtime(sd, "vera", pid=111, pgid=222, supervisor_pid=333)
    live = {111: True, 333: True}

    monkeypatch.setattr("peanut_review.runtime.is_process_live", lambda pid: live.get(pid, False))
    monkeypatch.setattr(
        "peanut_review.agent_control._process_matches_agent",
        lambda *args, **kwargs: (True, "matched"),
    )
    monkeypatch.setattr("peanut_review.agent_control._get_pgid", lambda pid: 222)

    sent: list[tuple[int, signal.Signals]] = []

    def fake_killpg(pgid: int, sig: signal.Signals) -> None:
        sent.append((pgid, sig))
        if sig == signal.SIGKILL:
            live[111] = False
            live[333] = False

    monkeypatch.setattr("peanut_review.agent_control._signal_process_group", fake_killpg)

    [result] = agent_control.kill_agents(sd, grace_seconds=0)

    assert result["status"] == "killed"
    assert sent == [(222, signal.SIGTERM), (222, signal.SIGKILL)]
    assert runtime.read_agent_meta(sd, "vera")["termination_signal"] == "SIGKILL"


def test_kill_agents_terminates_launching_supervisor(monkeypatch):
    sd = _make_session_dir([
        {"name": "vera", "model": "m", "persona": "vera.md", "runner": "cursor"},
    ])
    runtime.update_agent_meta(sd, "vera", {
        "runner": "cursor",
        "process_state": "launching",
        "supervisor_pid": 333,
    })
    sess.update_agent_status(sd, "vera", "running", supervisor_pid=333)
    live = {333: True}

    monkeypatch.setattr("peanut_review.runtime.is_process_live", lambda pid: live.get(pid, False))
    monkeypatch.setattr(
        "peanut_review.agent_control._process_matches_agent",
        lambda *args, **kwargs: (True, "matched"),
    )

    sent: list[tuple[int, signal.Signals]] = []

    def fake_kill(pid: int, sig: signal.Signals) -> None:
        sent.append((pid, sig))
        live[pid] = False

    monkeypatch.setattr("peanut_review.agent_control._signal_process", fake_kill)

    [result] = agent_control.kill_agents(sd, grace_seconds=0)

    assert result["status"] == "killed"
    assert sent == [(333, signal.SIGTERM)]
    assert result["signals"] == [
        {"target": "supervisor", "id": 333, "signal": "SIGTERM"},
    ]


def test_kill_agents_unknown_agent_errors():
    sd = _make_session_dir([
        {"name": "vera", "model": "m", "persona": "vera.md", "runner": "cursor"},
    ])

    try:
        agent_control.kill_agents(sd, agent_names=["irene"])
    except ValueError as e:
        assert "unknown agent" in str(e)
        assert "vera" in str(e)
    else:
        raise AssertionError("expected ValueError")
