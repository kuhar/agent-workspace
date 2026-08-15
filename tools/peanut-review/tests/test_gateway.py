"""Integration coverage for the capability-scoped reviewer gateway."""
from __future__ import annotations

import json
import os
import stat
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from peanut_review import gateway, session as sess, store
from peanut_review.models import AgentConfig


def _git(repo: Path, *args: str) -> str:
    return subprocess.check_output(
        ["git", "-C", str(repo), *args], text=True,
    ).strip()


def _make_session(tmp_path: Path, session_id: str = "gateway-test") -> tuple[Path, str]:
    repo = tmp_path / f"repo-{session_id}"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", "-b", "main", str(repo)], check=True)
    _git(repo, "config", "user.name", "Test")
    _git(repo, "config", "user.email", "test@example.com")
    (repo / "code.py").write_text("first = 1\nsecond = 2\n")
    _git(repo, "add", "code.py")
    _git(repo, "commit", "-qm", "base")
    base = _git(repo, "rev-parse", "HEAD")
    (repo / "code.py").write_text("first = 1\nsecond = 3\nthird = 4\n")
    _git(repo, "commit", "-qam", "topic")
    head = _git(repo, "rev-parse", "HEAD")
    root = tmp_path / "reviews"
    session_dir = root / session_id
    sess.create_session(
        workspace=str(repo),
        base_ref=base,
        topic_ref=head,
        agents=[AgentConfig(
            name="remote", model="fake", persona="remote.md", runner="codex",
        ).to_dict()],
        session_dir=str(session_dir),
        session_id=session_id,
    )
    return session_dir, head


@pytest.fixture
def live_gateway(tmp_path: Path):
    session_dir, head = _make_session(tmp_path)
    token = gateway.issue_capability(
        session_dir,
        agent="remote",
        launch_id="launch-one",
        ttl_seconds=300,
    )
    server, thread = gateway.start_test_server([session_dir.parent])
    url = f"http://127.0.0.1:{server.server_port}"
    try:
        yield session_dir, head, token, url, server
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def _remote_cli(
    session_dir: Path,
    url: str,
    token: str,
    *args: str,
    locator: str | None = None,
) -> subprocess.CompletedProcess[str]:
    env = os.environ.copy()
    env.update({
        "PEANUT_SESSION": locator or f"peanut://{session_dir.name}",
        "PEANUT_REVIEW_GATEWAY_URL": url,
        "PEANUT_REVIEW_GATEWAY_TOKEN": token,
        "GIT_AUTHOR_NAME": "spoofed-local-name",
    })
    return subprocess.run(
        [sys.executable, "-m", "peanut_review", *args],
        capture_output=True,
        text=True,
        env=env,
        timeout=10,
    )


def test_remote_cli_full_reviewer_command_matrix(live_gateway, tmp_path: Path):
    session_dir, head, token, url, _server = live_gateway
    body_file = tmp_path / "finding body.md"
    body_file.write_text("Uses `ticks`, $dollars, and unicode: café\n")

    status = _remote_cli(session_dir, url, token, "status")
    assert status.returncode == 0
    assert f"Session:  {session_dir.name}" in status.stdout

    anchored = _remote_cli(
        session_dir, url, token,
        "add-comment", "--file", "code.py", "--line", "2",
        "--severity", "warning", "--body-file", str(body_file),
        "--author", "not-the-capability-agent",
    )
    assert anchored.returncode == 0, anchored.stderr
    assert anchored.stdout.strip().endswith("code.py:2: second = 3")
    anchored_id = store.read_all_comments(session_dir)[0].id
    global_result = _remote_cli(
        session_dir, url, token,
        "add-global-comment", "--severity", "feedback", "--body", "global",
    )
    assert global_result.returncode == 0, global_result.stderr
    assert global_result.stdout.strip().endswith("(global)")
    reply = _remote_cli(
        session_dir, url, token,
        "add-comment", "--reply-to", anchored_id,
        "--severity", "suggestion", "--body", "reply",
    )
    assert reply.returncode == 0, reply.stderr
    assert f"(reply to {anchored_id})" in reply.stdout

    note_file = tmp_path / "test report.md"
    note_file.write_text("tests passed: `x` and $HOME stayed literal\n")
    note = _remote_cli(
        session_dir, url, token, "note", "--file", str(note_file),
    )
    assert note.returncode == 0, note.stderr
    signal_result = _remote_cli(session_dir, url, token, "signal", "round-done")
    assert signal_result.returncode == 0, signal_result.stderr

    comments_result = _remote_cli(
        session_dir, url, token, "comments", "--format", "json",
    )
    rows = json.loads(comments_result.stdout)
    assert [row["author"] for row in rows] == ["remote", "remote", "remote"]
    assert rows[0]["body"] == body_file.read_text()
    assert rows[0]["head_sha"] == head
    assert rows[2]["reply_to"] == anchored_id
    notes_result = _remote_cli(
        session_dir, url, token, "notes", "--format", "json",
    )
    assert json.loads(notes_result.stdout)[0]["body"] == note_file.read_text()
    assert (session_dir / "signals" / "remote.round-done").is_file()


def test_gateway_rejects_bad_anchor_and_non_round_signal(live_gateway):
    session_dir, _head, token, url, _server = live_gateway
    bad = _remote_cli(
        session_dir, url, token,
        "add-comment", "--file", "code.py", "--line", "99", "--body", "bad",
    )
    assert bad.returncode == 1
    assert "line" in bad.stderr.lower()
    bad_signal = _remote_cli(session_dir, url, token, "signal", "next-round")
    assert bad_signal.returncode == 1
    assert "round-done" in bad_signal.stderr
    deleted = _remote_cli(
        session_dir, url, token,
        "comments", "--include-deleted", "--format", "json",
    )
    assert deleted.returncode == 1
    assert "not available" in deleted.stderr
    assert store.read_all_comments(session_dir) == []


def test_capability_is_hashed_private_scoped_and_revocable(live_gateway, tmp_path: Path):
    session_dir, _head, token, url, _server = live_gateway
    path = gateway._capability_path(session_dir, "launch-one")
    record = json.loads(path.read_text())
    assert token not in path.read_text()
    assert record["agent"] == "remote"
    assert record["session_id"] == session_dir.name
    assert stat.S_IMODE(path.stat().st_mode) == 0o600

    other_dir, _ = _make_session(tmp_path, "other-session")
    cross = _remote_cli(
        session_dir, url, token, "status", locator=f"peanut://{other_dir.name}",
    )
    assert cross.returncode == 1
    assert "capability" in cross.stderr

    assert gateway.revoke_capability(session_dir, "launch-one")
    replay = _remote_cli(session_dir, url, token, "status")
    assert replay.returncode == 1
    assert "revoked" in replay.stderr


def test_expired_and_operation_scoped_capabilities(live_gateway):
    session_dir, _head, _token, url, _server = live_gateway
    read_token = gateway.issue_capability(
        session_dir,
        agent="remote",
        launch_id="read-only",
        ttl_seconds=300,
        allowed_ops={"hello", "comments:read"},
    )
    assert _remote_cli(
        session_dir, url, read_token, "comments", "--format", "json",
    ).returncode == 0
    denied = _remote_cli(
        session_dir, url, read_token, "add-global-comment", "--body", "no",
    )
    assert denied.returncode == 1
    assert "allowed" in denied.stderr

    expired = gateway.issue_capability(
        session_dir,
        agent="remote",
        launch_id="expired",
        ttl_seconds=300,
    )
    path = gateway._capability_path(session_dir, "expired")
    record = json.loads(path.read_text())
    record["expires_at"] = 0
    path.write_text(json.dumps(record))
    result = _remote_cli(session_dir, url, expired, "status")
    assert result.returncode == 1
    assert "expired" in result.stderr


def test_remote_locator_rejects_privileged_and_missing_gateway(tmp_path: Path):
    env = os.environ.copy()
    env["PEANUT_SESSION"] = "peanut://remote"
    env.pop("PEANUT_REVIEW_GATEWAY_URL", None)
    env.pop("PEANUT_REVIEW_GATEWAY_TOKEN", None)
    missing = subprocess.run(
        [sys.executable, "-m", "peanut_review", "status"],
        env=env, capture_output=True, text=True,
    )
    assert missing.returncode == 1
    assert "require" in missing.stderr
    privileged = subprocess.run(
        [sys.executable, "-m", "peanut_review", "gh-push", "--dry-run"],
        env=env, capture_output=True, text=True,
    )
    assert privileged.returncode == 1
    assert "reviewer capability" in privileged.stderr


def test_gateway_concurrent_writes_remain_valid_jsonl(live_gateway):
    session_dir, _head, token, url, _server = live_gateway
    client = gateway.GatewayClient(url, token, session_dir.name)
    with ThreadPoolExecutor(max_workers=12) as pool:
        results = list(pool.map(
            lambda index: client.request(
                "POST", "notes", payload={"body": f"report-{index}"},
            ),
            range(40),
        ))
    assert len(results) == 40
    notes = store.read_all_notes(session_dir)
    assert len(notes) == 40
    assert {note.body for note in notes} == {f"report-{index}" for index in range(40)}
    for line in (session_dir / "notes" / "remote.jsonl").read_text().splitlines():
        json.loads(line)


def test_gateway_restart_preserves_live_capability(tmp_path: Path):
    session_dir, _head = _make_session(tmp_path)
    token = gateway.issue_capability(
        session_dir, agent="remote", launch_id="restart", ttl_seconds=300,
    )
    first, thread = gateway.start_test_server([session_dir.parent])
    first.shutdown()
    first.server_close()
    thread.join(timeout=2)
    second, thread2 = gateway.start_test_server([session_dir.parent])
    try:
        client = gateway.GatewayClient(
            f"http://127.0.0.1:{second.server_port}", token, session_dir.name,
        )
        assert client.request("GET", "hello")["launch_id"] == "restart"
    finally:
        second.shutdown()
        second.server_close()
        thread2.join(timeout=2)


def test_gateway_bounds_and_unavailable_errors(live_gateway):
    session_dir, _head, token, url, server = live_gateway
    client = gateway.GatewayClient(url, token, session_dir.name)
    with pytest.raises(gateway.GatewayError, match="exceeds"):
        client.request(
            "POST", "notes", payload={"body": "x" * (gateway.MAX_REQUEST_BYTES + 1)},
        )
    server.shutdown()
    with pytest.raises(gateway.GatewayError, match="unavailable"):
        client.request("GET", "hello")


@pytest.mark.parametrize("host", ["0.0.0.0", "::1"])
def test_gateway_refuses_unsupported_listener(tmp_path: Path, host: str):
    with pytest.raises(ValueError, match="loopback"):
        gateway.serve([tmp_path], host=host, port=0)


@pytest.mark.parametrize("host", ["127.0.0.1", "localhost"])
def test_gateway_binds_every_accepted_loopback_form(tmp_path: Path, host: str):
    server = gateway._GatewayServer((host, 0), [tmp_path.resolve()])
    try:
        assert server.server_port > 0
    finally:
        server.server_close()
