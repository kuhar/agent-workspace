"""Tests for session lifecycle."""
import json
import os
import tempfile
from pathlib import Path
from unittest.mock import patch

from peanut_review.models import AgentConfig, Session, SessionState
from peanut_review.session import (
    create_session,
    discover_session,
    load_session,
    save_session,
    transition_state,
    update_agent_status,
)


def _mock_git(workspace, *args):
    """Mock git calls for testing."""
    if args == ("rev-parse", "HEAD"):
        return "abc123def456"
    if args[0] == "diff" and "--stat" in args:
        return "+42 -10 3 files"
    return ""


@patch("peanut_review.session._run_git", side_effect=_mock_git)
def test_create_session(mock_git):
    sd = tempfile.mkdtemp(prefix="pr-test-")
    session_dir = os.path.join(sd, "session")
    s, _ = create_session(
        workspace="/tmp/fakerepo",
        base_ref="main",
        agents=[
            {"name": "vera", "model": "opus-4.6-thinking", "persona": "vera.md"},
            {"name": "felix", "model": "sonnet-4.6", "persona": "felix.md"},
        ],
        session_dir=session_dir,
    )

    assert s.workspace == "/tmp/fakerepo"
    assert s.base_ref == "main"
    assert len(s.agents) == 2
    assert s.agents[0].name == "vera"
    assert s.state == SessionState.INIT.value

    # Directory structure created
    assert (Path(session_dir) / "comments").is_dir()
    assert (Path(session_dir) / "signals").is_dir()
    assert (Path(session_dir) / "messages").is_dir()
    assert (Path(session_dir) / "prompts").is_dir()
    assert (Path(session_dir) / "log").is_dir()
    assert (Path(session_dir) / "session.json").is_file()


@patch("peanut_review.session._run_git", side_effect=_mock_git)
def test_load_session(mock_git):
    sd = tempfile.mkdtemp(prefix="pr-test-")
    session_dir = os.path.join(sd, "session")
    create_session(workspace="/tmp/repo", session_dir=session_dir)

    loaded = load_session(session_dir)
    assert loaded.workspace == "/tmp/repo"
    assert loaded.state == SessionState.INIT.value


@patch("peanut_review.session._run_git", side_effect=_mock_git)
def test_state_transitions(mock_git):
    sd = tempfile.mkdtemp(prefix="pr-test-")
    session_dir = os.path.join(sd, "session")
    create_session(workspace="/tmp/repo", session_dir=session_dir)

    s = transition_state(session_dir, SessionState.ROUND1.value)
    assert s.state == "round1"

    s = transition_state(session_dir, SessionState.TRIAGE.value)
    assert s.state == "triage"

    s = transition_state(session_dir, SessionState.ROUND2.value)
    assert s.state == "round2"

    s = transition_state(session_dir, SessionState.COMPLETE.value)
    assert s.state == "complete"


@patch("peanut_review.session._run_git", side_effect=_mock_git)
def test_update_agent_status(mock_git):
    sd = tempfile.mkdtemp(prefix="pr-test-")
    session_dir = os.path.join(sd, "session")
    create_session(
        workspace="/tmp/repo",
        agents=[{"name": "vera", "model": "opus-4.6-thinking", "persona": "vera.md"}],
        session_dir=session_dir,
    )

    s = update_agent_status(session_dir, "vera", "running", pid=12345)
    assert s.agents[0].status == "running"
    assert s.agents[0].pid == 12345

    # Persisted
    loaded = load_session(session_dir)
    assert loaded.agents[0].pid == 12345


@patch("peanut_review.session._run_git", side_effect=_mock_git)
def test_persona_copying(mock_git):
    personas_src = tempfile.mkdtemp(prefix="pr-personas-")
    (Path(personas_src) / "vera.md").write_text("---\nname: vera\n---\nTest persona")
    (Path(personas_src) / "felix.md").write_text("---\nname: felix\n---\nTest persona 2")

    sd = tempfile.mkdtemp(prefix="pr-test-")
    session_dir = os.path.join(sd, "session")
    create_session(
        workspace="/tmp/repo",
        personas_dir=personas_src,
        session_dir=session_dir,
    )

    assert (Path(session_dir) / "personas" / "vera.md").exists()
    assert (Path(session_dir) / "personas" / "felix.md").exists()


def test_discover_session_env():
    with patch.dict(os.environ, {"PEANUT_SESSION": "/tmp/test-session"}):
        assert discover_session() == "/tmp/test-session"


def test_discover_session_marker():
    d = tempfile.mkdtemp(prefix="pr-test-")
    (Path(d) / ".peanut-session").write_text("/tmp/my-session\n")
    with patch.dict(os.environ, {}, clear=False):
        os.environ.pop("PEANUT_SESSION", None)
        assert discover_session(d) == "/tmp/my-session"


def test_session_json_roundtrip():
    s = Session(
        id="test-123",
        workspace="/repo",
        agents=[
            AgentConfig(name="vera", model="opus", persona="vera.md"),
        ],
        state="round1",
    )
    text = s.to_json()
    s2 = Session.from_json(text)
    assert s2.id == "test-123"
    assert len(s2.agents) == 1
    assert s2.agents[0].name == "vera"
    assert s2.state == "round1"
