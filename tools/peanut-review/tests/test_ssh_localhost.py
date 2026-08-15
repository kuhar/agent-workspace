"""Real localhost OpenSSH workflow with deterministic reviewer binaries."""
from __future__ import annotations

import getpass
import json
import os
import shlex
import socket
import subprocess
import sys
import time
import uuid
from dataclasses import replace
from pathlib import Path

import pytest

from peanut_review import (
    agent_control,
    gateway,
    launch,
    polling,
    runtime,
    session as sess,
    ssh_transport,
    store,
)
from peanut_review.models import AgentConfig, AgentRole


SSHD = Path("/usr/sbin/sshd")
pytestmark = pytest.mark.skipif(not SSHD.is_file(), reason="local sshd is unavailable")


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _wait_for(predicate, timeout: float = 10.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.05)
    raise AssertionError("timed out waiting for local SSH workflow state")


def _git(repo: Path, *args: str) -> str:
    return subprocess.check_output(
        ["git", "-C", str(repo), *args], text=True,
    ).strip()


def _start_sshd(tmp_path: Path) -> tuple[subprocess.Popen, int, Path]:
    host_key = tmp_path / "host key"
    client_key = tmp_path / "client key"
    for key in (host_key, client_key):
        subprocess.run(
            ["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-f", str(key)],
            check=True,
        )
    authorized = tmp_path / "authorized keys"
    authorized.write_text(client_key.with_suffix(".pub").read_text())
    port = _free_port()
    config = tmp_path / "sshd config"
    config.write_text("\n".join([
        f"Port {port}",
        "ListenAddress 127.0.0.1",
        f'HostKey "{host_key}"',
        f'PidFile "{tmp_path / "sshd.pid"}"',
        f'AuthorizedKeysFile "{authorized}"',
        "PasswordAuthentication no",
        "KbdInteractiveAuthentication no",
        "UsePAM no",
        "StrictModes no",
        "AllowTcpForwarding yes",
        "LogLevel ERROR",
        "Subsystem sftp internal-sftp",
        "",
    ]))
    log = (tmp_path / "sshd.log").open("w")
    daemon = subprocess.Popen(
        [str(SSHD), "-D", "-e", "-f", str(config)],
        stdout=log,
        stderr=subprocess.STDOUT,
        start_new_session=True,
    )

    def listening() -> bool:
        if daemon.poll() is not None:
            raise AssertionError((tmp_path / "sshd.log").read_text())
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.1):
                return True
        except OSError:
            return False

    _wait_for(listening)
    return daemon, port, client_key


def _master_args(
    *,
    port: int,
    client_key: Path,
    control_path: Path,
    remote_gateway_port: int,
    local_gateway_port: int,
) -> list[str]:
    return [
        "ssh", "-N", "-M",
        "-S", str(control_path),
        "-p", str(port),
        "-i", str(client_key),
        "-o", "BatchMode=yes",
        "-o", "IdentitiesOnly=yes",
        "-o", "StrictHostKeyChecking=no",
        "-o", "UserKnownHostsFile=/dev/null",
        "-o", "ExitOnForwardFailure=yes",
        "-R", f"{remote_gateway_port}:127.0.0.1:{local_gateway_port}",
        f"{getpass.getuser()}@127.0.0.1",
    ]


def _start_master(args: list[str], control_path: Path) -> subprocess.Popen:
    master = subprocess.Popen(
        args,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        text=True,
        start_new_session=True,
    )

    def ready() -> bool:
        if master.poll() is not None:
            raise AssertionError(master.stderr.read() if master.stderr else "master failed")
        return control_path.exists()

    _wait_for(ready)
    return master


def _stop_master(control_path: Path, host: str) -> None:
    subprocess.run(
        ["ssh", "-S", str(control_path), "-O", "exit", host],
        capture_output=True,
        text=True,
        timeout=5,
    )


def _make_checkouts(tmp_path: Path) -> tuple[Path, Path, str, str]:
    origin = tmp_path / "origin repo"
    origin.mkdir()
    subprocess.run(["git", "init", "-q", "-b", "main", str(origin)], check=True)
    _git(origin, "config", "user.name", "Test")
    _git(origin, "config", "user.email", "test@example.com")
    (origin / "code.py").write_text("value = 1\nother = 2\n")
    _git(origin, "add", "code.py")
    _git(origin, "commit", "-qm", "base")
    base = _git(origin, "rev-parse", "HEAD")
    (origin / "code.py").write_text("value = 1\nother = 3\n")
    _git(origin, "commit", "-qam", "topic")
    head = _git(origin, "rev-parse", "HEAD")
    local = tmp_path / "local checkout"
    remote = tmp_path / "remote checkout"
    subprocess.run(["git", "clone", "-q", str(origin), str(local)], check=True)
    subprocess.run(["git", "clone", "-q", str(origin), str(remote)], check=True)
    return local, remote, base, head


def _make_fake_codex(tmp_path: Path) -> tuple[Path, Path]:
    fake_bin = tmp_path / "fake runner bin"
    fake_bin.mkdir()
    fake_codex = fake_bin / "codex"
    fake_codex.write_text(
        "#!" + sys.executable + "\n"
        "import os, subprocess, sys, time\n"
        "if '--model' in sys.argv and sys.argv[sys.argv.index('--model') + 1] == 'slow':\n"
        "    time.sleep(60)\n"
        "    raise SystemExit(0)\n"
        "author=os.environ.get('GIT_AUTHOR_NAME', 'unknown')\n"
        "base=[sys.executable, '-m', 'peanut_review']\n"
        "body=f'finding from {author} with `ticks`, $HOME, and café'\n"
        "calls=[\n"
        " base+['status'],\n"
        " base+['comments','--format','json'],\n"
        " base+['add-comment','--file','code.py','--line','2','--severity','warning','--body',body],\n"
        " base+['note','--message',f'tests passed for {author}'],\n"
        " base+['signal','round-done'],\n"
        "]\n"
        "for command in calls:\n"
        "    result=subprocess.run(command, env=os.environ.copy())\n"
        "    if result.returncode:\n"
        "        raise SystemExit(result.returncode)\n"
    )
    fake_codex.chmod(0o755)
    project_bin = Path(__file__).resolve().parents[1] / "bin" / "peanut-review"
    remote_cli = tmp_path / "remote peanut-review"
    remote_cli.write_text(
        "#!/bin/sh\n"
        f"export PATH={shlex.quote(str(fake_bin))}:\"$PATH\"\n"
        f"exec {shlex.quote(str(project_bin))} \"$@\"\n"
    )
    remote_cli.chmod(0o755)
    return fake_bin, remote_cli


def _wait_supervisors(session_dir: Path, agents: list[str]) -> None:
    def done() -> bool:
        session = sess.load_session(session_dir)
        selected = [agent for agent in session.agents if agent.name in agents]
        return all(
            not runtime.inspect_agent_runtime(session_dir, agent)["supervisor_live"]
            for agent in selected
        )
    _wait_for(done, timeout=20)


def test_real_controlmaster_reverse_gateway_mixed_review_and_recovery(
    tmp_path: Path, monkeypatch,
):
    local, remote, base, head = _make_checkouts(tmp_path)
    local_build = local / "build local"
    remote_build = remote / "build remote"
    local_build.mkdir()
    remote_build.mkdir()
    fake_bin, remote_cli = _make_fake_codex(tmp_path)
    monkeypatch.setenv("PATH", f"{fake_bin}:{os.environ['PATH']}")
    personas = tmp_path / "personas"
    personas.mkdir()
    (personas / "local.md").write_text("local reviewer")
    (personas / "remote.md").write_text("internal documentation reviewer")

    sshd, ssh_port, client_key = _start_sshd(tmp_path)
    review_root = tmp_path / "reviews"
    session_dir = review_root / "localhost-ssh"
    gateway_server, gateway_thread = gateway.start_test_server([review_root])
    control_path = Path(f"/tmp/peanut-ssh-{uuid.uuid4().hex[:10]}.sock")
    remote_gateway_port = _free_port()
    host = f"{getpass.getuser()}@127.0.0.1"
    master_args = _master_args(
        port=ssh_port,
        client_key=client_key,
        control_path=control_path,
        remote_gateway_port=remote_gateway_port,
        local_gateway_port=gateway_server.server_port,
    )
    master = _start_master(master_args, control_path)
    try:
        session, _ = sess.create_session(
            workspace=str(local),
            base_ref=base,
            topic_ref=head,
            agents=[
                AgentConfig(
                    name="local", model="fake", persona="local.md", runner="codex",
                ).to_dict(),
                AgentConfig(
                    name="remote", model="fake", persona="remote.md", runner="codex",
                    ssh_target="localhost",
                ).to_dict(),
                AgentConfig(
                    name="Curator", model="fake", role=AgentRole.CURATOR.value,
                    runner="codex",
                ).to_dict(),
            ],
            ssh_targets={
                "localhost": {
                    "host": host,
                    "controlPath": str(control_path),
                    "gatewayUrl": f"http://127.0.0.1:{remote_gateway_port}",
                    "workspaceRoot": str(remote),
                    "repoRelative": ".",
                    "buildRoots": [str(remote_build)],
                    "peanutReviewBin": str(remote_cli),
                    "runtimeRoot": str(tmp_path / "remote runtime"),
                },
            },
            personas_dir=str(personas),
            timeout=30,
            session_dir=str(session_dir),
            session_id="localhost-ssh",
        )
        assert Path(session.workspace).resolve() != remote.resolve()
        assert (Path(session.workspace) / ".git").is_dir()
        assert (remote / ".git").is_dir()

        results = launch.launch_agents(session_dir)
        assert [result.get("transport", "local") for result in results] == [
            "local", "ssh",
        ]
        assert polling.wait_all_signals(
            session_dir, ["local", "remote"], "round-done",
            timeout=20, poll_interval=0.05,
        ) == []
        _wait_supervisors(session_dir, ["local", "remote"])

        comments = store.read_all_comments(session_dir)
        assert {comment.author for comment in comments} == {"local", "remote"}
        assert all("`ticks`, $HOME, and café" in comment.body for comment in comments)
        assert {note.author for note in store.read_all_notes(session_dir)} == {
            "local", "remote",
        }
        remote_meta = runtime.read_agent_meta(session_dir, "remote")
        assert remote_meta["transport"] == "ssh"
        assert remote_meta["ssh_channel_state"] == "closed"
        assert remote_meta["remote_process_state"] == "stopped"

        curator_result = launch.launch_curator(session_dir)
        assert curator_result[0].get("transport") is None
        assert polling.wait_signal(
            session_dir, "Curator", "round-done", timeout=20, poll_interval=0.05,
        )
        _wait_supervisors(session_dir, ["Curator"])

        target = sess.load_session(session_dir).ssh_targets["localhost"]
        remote_agent = next(
            agent for agent in sess.load_session(session_dir).agents
            if agent.name == "remote"
        )

        for changed_target, message in [
            (replace(target, workspace_root=str(tmp_path / "missing workspace")), "workspace does not exist"),
            (replace(target, repo_relative="missing repo"), "repository does not exist"),
            (replace(target, build_roots=[str(tmp_path / "missing build")]), "build root does not exist"),
            (replace(target, gateway_url="http://127.0.0.1:1"), "gateway is unavailable"),
            (replace(target, peanut_review_bin="/no/such/peanut-review"), "ssh-probe failed"),
        ]:
            with pytest.raises(ValueError, match=message):
                ssh_transport.preflight_agent(session_dir, remote_agent, changed_target)

        remote_build.chmod(0o555)
        try:
            with pytest.raises(ValueError, match="not readable and writable"):
                ssh_transport.preflight_agent(session_dir, remote_agent, target)
        finally:
            remote_build.chmod(0o755)

        subprocess.run(
            ["git", "-C", str(remote), "checkout", "-q", "--detach", base],
            check=True,
        )
        try:
            with pytest.raises(ValueError, match="does not match pinned head"):
                ssh_transport.preflight_agent(session_dir, remote_agent, target)
        finally:
            subprocess.run(
                ["git", "-C", str(remote), "checkout", "-q", "--detach", head],
                check=True,
            )

        saved_session = sess.load_session(session_dir)
        saved_base = saved_session.base_ref
        saved_session.base_ref = "f" * 40
        sess.save_session(session_dir, saved_session)
        try:
            with pytest.raises(ValueError, match="lacks pinned base_ref"):
                ssh_transport.preflight_agent(session_dir, remote_agent, target)
        finally:
            saved_session.base_ref = saved_base
            sess.save_session(session_dir, saved_session)

        incompatible_cli = tmp_path / "incompatible peanut-review"
        incompatible_cli.write_text(
            "#!/bin/sh\n"
            "python3 -c 'import sys; sys.stdin.read()'\n"
            "printf '%s\\n' '{\"ok\":false,\"errors\":[\"protocol mismatch\"]}'\n"
        )
        incompatible_cli.chmod(0o755)
        with pytest.raises(ValueError, match="protocol mismatch"):
            ssh_transport.preflight_agent(
                session_dir, remote_agent,
                replace(target, peanut_review_bin=str(incompatible_cli)),
            )

        project_bin = Path(__file__).resolve().parents[1] / "bin" / "peanut-review"
        no_runner_cli = tmp_path / "no runner peanut-review"
        no_runner_cli.write_text(
            "#!/bin/sh\n"
            "export PATH=/usr/bin:/bin\n"
            f"exec {shlex.quote(str(project_bin))} \"$@\"\n"
        )
        no_runner_cli.chmod(0o755)
        with pytest.raises(ValueError, match="runner executable not found"):
            ssh_transport.preflight_agent(
                session_dir, remote_agent,
                replace(target, peanut_review_bin=str(no_runner_cli)),
            )

        cursor_agent = replace(remote_agent, runner="cursor")
        with pytest.raises(ValueError, match="Cursor CLI config validation failed"):
            ssh_transport.preflight_agent(session_dir, cursor_agent, target)

        (remote / "code.py").write_text("dirty = True\n")
        with pytest.raises(ValueError, match="tracked modifications"):
            ssh_transport.preflight_agent(session_dir, remote_agent, target)
        subprocess.run(
            ["git", "-C", str(remote), "restore", "code.py"], check=True,
        )

        current = sess.load_session(session_dir)
        remote_agent = next(agent for agent in current.agents if agent.name == "remote")
        remote_agent.model = "slow"
        sess.save_session(session_dir, current)
        launch.rerun_agents(session_dir, agent_names=["remote"])
        process_root = Path(target.runtime_root) / session.id

        def process_identity() -> Path | None:
            matches = list(process_root.glob("*/process.json"))
            return matches[0] if matches else None

        _wait_for(lambda: process_identity() is not None)
        identity_path = process_identity()
        assert identity_path is not None
        identity = json.loads(identity_path.read_text())
        killed = agent_control.kill_agents(
            session_dir, agent_names=["remote"], grace_seconds=3,
        )
        assert killed[0]["status"] == "killed"
        _wait_for(lambda: not runtime.is_process_live(identity["pid"]))
        assert not identity_path.exists()

        timed_session = sess.load_session(session_dir)
        timed_session.timeout = 2
        sess.save_session(session_dir, timed_session)
        launch.rerun_agents(session_dir, agent_names=["remote"])
        _wait_for(lambda: process_identity() is not None)
        timed_identity = json.loads(process_identity().read_text())
        _wait_supervisors(session_dir, ["remote"])
        timed_meta = runtime.read_agent_meta(session_dir, "remote")
        assert timed_meta["timed_out"] is True
        _wait_for(lambda: not runtime.is_process_live(timed_identity["pid"]))
        assert not list(process_root.glob("*/process.json"))

        loss_session = sess.load_session(session_dir)
        loss_session.timeout = 30
        sess.save_session(session_dir, loss_session)
        launch.rerun_agents(session_dir, agent_names=["remote"])
        _wait_for(lambda: process_identity() is not None)
        uncertain_identity = json.loads(process_identity().read_text())
        _stop_master(control_path, host)
        master.wait(timeout=10)
        with pytest.raises(ValueError, match="control socket is unavailable"):
            ssh_transport.preflight_agent(session_dir, remote_agent, target)
        _wait_supervisors(session_dir, ["remote"])
        _wait_for(
            lambda: not runtime.is_process_live(uncertain_identity["pid"]),
            timeout=10,
        )
        master = _start_master(master_args, control_path)
        recovered = ssh_transport.recover_agent(session_dir, "remote")
        assert recovered["ok"]
        assert not list(process_root.glob("*/process.json"))

        capability_dir = session_dir / "runtime" / "gateway" / "capabilities"
        records = [json.loads(path.read_text()) for path in capability_dir.glob("*.json")]
        assert records and all(record["revoked"] for record in records)
    finally:
        if master.poll() is None:
            _stop_master(control_path, host)
            try:
                master.wait(timeout=5)
            except subprocess.TimeoutExpired:
                master.terminate()
                master.wait(timeout=5)
        gateway_server.shutdown()
        gateway_server.server_close()
        gateway_thread.join(timeout=2)
        if sshd.poll() is None:
            sshd.terminate()
            try:
                sshd.wait(timeout=5)
            except subprocess.TimeoutExpired:
                sshd.kill()
                sshd.wait(timeout=5)
