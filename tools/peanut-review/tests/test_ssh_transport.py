"""Local validation for SSH preflight and remote process lifecycle."""
from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
from pathlib import Path

import pytest

from peanut_review import gateway, session as sess, ssh_transport, store
from peanut_review.models import AgentConfig, SshTarget


def _git(repo: Path, *args: str) -> str:
    return subprocess.check_output(
        ["git", "-C", str(repo), *args], text=True,
    ).strip()


def _repo(tmp_path: Path) -> tuple[Path, str, str]:
    repo = tmp_path / "remote workspace" / "source repo"
    repo.mkdir(parents=True)
    subprocess.run(["git", "init", "-q", "-b", "main", str(repo)], check=True)
    _git(repo, "config", "user.name", "Test")
    _git(repo, "config", "user.email", "test@example.com")
    (repo / "code.py").write_text("value = 1\n")
    _git(repo, "add", "code.py")
    _git(repo, "commit", "-qm", "base")
    base = _git(repo, "rev-parse", "HEAD")
    (repo / "code.py").write_text("value = 2\n")
    _git(repo, "commit", "-qam", "topic")
    return repo, base, _git(repo, "rev-parse", "HEAD")


def _probe_fixture(tmp_path: Path, monkeypatch):
    repo, base, head = _repo(tmp_path)
    workspace = repo.parent
    build = workspace / "build dir"
    build.mkdir()
    runtime_root = workspace / "runtime"
    runtime_root.mkdir()
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    fake_codex = fake_bin / "codex"
    fake_codex.write_text("#!/bin/sh\nexit 0\n")
    fake_codex.chmod(0o755)
    monkeypatch.setenv("PATH", f"{fake_bin}:{os.environ['PATH']}")

    root = tmp_path / "reviews"
    session_dir = root / "ssh-probe"
    sess.create_session(
        workspace=str(repo),
        base_ref=base,
        topic_ref=head,
        agents=[AgentConfig(
            name="remote", model="fake", persona="remote.md", runner="codex",
        ).to_dict()],
        session_dir=str(session_dir),
        session_id="ssh-probe",
    )
    token = gateway.issue_capability(
        session_dir, agent="remote", launch_id="probe", ttl_seconds=300,
        allowed_ops={"hello"},
    )
    server, thread = gateway.start_test_server([root])
    payload = {
        "protocol": gateway.PROTOCOL_VERSION,
        "session_id": "ssh-probe",
        "agent": "remote",
        "runner": "codex",
        "workspace_root": str(workspace),
        "repo_relative": repo.name,
        "build_roots": [str(build)],
        "runtime_root": str(runtime_root),
        "base_ref": base,
        "topic_ref": head,
        "current_head": head,
        "gateway_url": f"http://127.0.0.1:{server.server_port}",
        "gateway_token": token,
    }
    return session_dir, payload, server, thread


def test_remote_command_requires_configured_master_socket():
    target = SshTarget(
        host="reviewer@example", control_path="/tmp/review control.sock",
        peanut_review_bin="/opt/peanut review/bin/peanut-review",
    )
    command = ssh_transport._internal_remote_command(target, "ssh-probe")
    assert command[:3] == ["ssh", "-S", "/tmp/review control.sock"]
    assert "ProxyCommand=false" in command
    assert "ControlMaster=no" in command
    assert "ControlPersist=no" in command
    assert command[-2] == "reviewer@example"
    assert command[-1] == "'/opt/peanut review/bin/peanut-review' ssh-probe"


def test_remote_probe_validates_checkout_build_runner_and_gateway(tmp_path: Path, monkeypatch):
    _session_dir, payload, server, thread = _probe_fixture(tmp_path, monkeypatch)
    try:
        result = ssh_transport.remote_probe(payload)
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)
    assert result["ok"], result["errors"]
    assert result["head"] == payload["current_head"]
    assert result["runner_binary"] == "codex"


@pytest.mark.parametrize("failure", [
    "head", "dirty", "build", "runner", "protocol", "gateway", "stale",
])
def test_remote_probe_reports_each_pre_model_failure(
    tmp_path: Path, monkeypatch, failure: str,
):
    _session_dir, payload, server, thread = _probe_fixture(tmp_path, monkeypatch)
    repo = Path(payload["workspace_root"]) / payload["repo_relative"]
    if failure == "head":
        payload["current_head"] = "0" * 40
    elif failure == "dirty":
        (repo / "code.py").write_text("dirty = True\n")
    elif failure == "build":
        payload["build_roots"] = [str(tmp_path / "missing")]
    elif failure == "runner":
        payload["runner"] = "cursor"
        monkeypatch.setenv("PATH", "/usr/bin:/bin")
    elif failure == "protocol":
        payload["protocol"] = 999
    elif failure == "gateway":
        payload["gateway_url"] = "http://127.0.0.1:1"
    elif failure == "stale":
        run_dir = (
            Path(payload["runtime_root"]) / payload["session_id"] / "old-launch"
        )
        run_dir.mkdir(parents=True)
        (run_dir / "prompt.md").write_text("sensitive")
    try:
        result = ssh_transport.remote_probe(payload)
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)
    assert not result["ok"]
    joined = "\n".join(result["errors"])
    expected = {
        "head": "does not match pinned head",
        "dirty": "tracked modifications",
        "build": "build root does not exist",
        "runner": "runner executable not found",
        "protocol": "protocol mismatch",
        "gateway": "gateway is unavailable",
        "stale": "recover-ssh",
    }[failure]
    assert expected in joined


def test_remote_run_uses_same_cli_and_cleans_staged_secrets(tmp_path: Path, monkeypatch):
    session_dir, payload, server, thread = _probe_fixture(tmp_path, monkeypatch)
    token = gateway.issue_capability(
        session_dir, agent="remote", launch_id="run-one", ttl_seconds=300,
    )
    fake_wrapper = tmp_path / "fake runner.py"
    fake_wrapper.write_text(
        "#!" + sys.executable + "\n"
        "import os, subprocess, sys\n"
        "base=[sys.executable, '-m', 'peanut_review']\n"
        "calls=[\n"
        " base+['status'],\n"
        " base+['add-global-comment','--severity','warning','--body','remote finding'],\n"
        " base+['note','--message','tests passed'],\n"
        " base+['signal','round-done'],\n"
        "]\n"
        "raise SystemExit(next((p.returncode for c in calls if (p:=subprocess.run(c, env=os.environ.copy())).returncode), 0))\n"
    )
    fake_wrapper.chmod(0o755)
    monkeypatch.setattr(
        ssh_transport.launch, "_find_launcher_script", lambda _runner: str(fake_wrapper),
    )
    run_payload = {
        **payload,
        "launch_id": "run-one",
        "model": "fake",
        "reasoning_effort": "",
        "fast_mode": False,
        "timeout": 30,
        "gateway_token": token,
        "prompt": "prompt with `ticks` and $dollars",
        "persona": "internal persona",
    }
    try:
        assert ssh_transport.remote_run(run_payload) == 0
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)
    comments = store.read_all_comments(session_dir)
    assert [(comment.author, comment.body) for comment in comments] == [
        ("remote", "remote finding"),
    ]
    assert store.read_all_notes(session_dir)[0].body == "tests passed"
    assert (session_dir / "signals" / "remote.round-done").is_file()
    run_dir = Path(payload["runtime_root"]) / payload["session_id"] / "run-one"
    assert not (run_dir / "prompt.md").exists()
    assert not (run_dir / "persona.md").exists()
    assert not (run_dir / "process.json").exists()


def test_remote_stop_checks_process_start_identity(tmp_path: Path):
    runtime_root = tmp_path / "runtime"
    run_dir = runtime_root / "session" / "launch"
    run_dir.mkdir(parents=True)
    proc = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(60)"],
        start_new_session=True,
    )
    identity = {
        "pid": proc.pid,
        "pgid": proc.pid,
        "start_ticks": ssh_transport._proc_start_ticks(proc.pid) + 1,
        "launch_id": "launch",
        "agent": "remote",
    }
    (run_dir / "process.json").write_text(json.dumps(identity))
    with pytest.raises(ssh_transport.SshTransportError, match="identity changed"):
        ssh_transport.remote_stop({
            "runtime_root": str(runtime_root),
            "session_id": "session",
            "launch_id": "launch",
        })
    assert proc.poll() is None
    identity["start_ticks"] -= 1
    (run_dir / "process.json").write_text(json.dumps(identity))
    result = ssh_transport.remote_stop({
        "runtime_root": str(runtime_root),
        "session_id": "session",
        "launch_id": "launch",
        "grace_seconds": 1,
    })
    proc.wait(timeout=5)
    assert result["ok"] and result["stopped"]


def test_remote_recover_removes_stale_sensitive_files(tmp_path: Path):
    run_dir = tmp_path / "runtime" / "session" / "launch"
    run_dir.mkdir(parents=True)
    (run_dir / "prompt.md").write_text("prompt")
    (run_dir / "persona.md").write_text("persona")
    result = ssh_transport.remote_recover({
        "runtime_root": str(tmp_path / "runtime"),
        "session_id": "session",
        "agent": "remote",
    })
    assert result["ok"]
    assert set(result["recovered"][0]["removed"]) == {"prompt.md", "persona.md"}
    assert not (run_dir / "prompt.md").exists()
