"""SSH-specific preflight, transport, and remote reviewer lifecycle."""
from __future__ import annotations

import argparse
import json
import os
import shlex
import shutil
import signal
import stat
import subprocess
import sys
import time
import uuid
from pathlib import Path
from typing import Any

from . import gateway, launch, runtime, session as sess, validation
from .models import AgentConfig, SshTarget

MAX_CONTROL_MESSAGE_BYTES = 2 * 1024 * 1024
REMOTE_GATEWAY_POLL_SECONDS = 1.0
REMOTE_GATEWAY_FAILURE_LIMIT = 3
RUNNER_BINARIES = {
    "cursor": "cursor-agent",
    "opencode": "opencode",
    "codex": "codex",
}


class SshTransportError(ValueError):
    """A persistent-SSH preflight or lifecycle failure."""


def _read_json_stdin() -> dict[str, Any]:
    raw = sys.stdin.buffer.read(MAX_CONTROL_MESSAGE_BYTES + 1)
    if len(raw) > MAX_CONTROL_MESSAGE_BYTES:
        raise SshTransportError("SSH control payload is too large")
    try:
        value = json.loads(raw)
    except ValueError as exc:
        raise SshTransportError("SSH control payload is not valid JSON") from exc
    if not isinstance(value, dict):
        raise SshTransportError("SSH control payload must be a JSON object")
    return value


def _remote_command(target: SshTarget, command: str) -> list[str]:
    """Build a command that can only reuse the configured master socket.

    ProxyCommand=false makes a missing or unusable socket fail closed instead
    of opening a new network connection.
    """
    return [
        "ssh",
        "-S", target.control_path,
        "-o", "BatchMode=yes",
        "-o", "ControlMaster=no",
        "-o", "ControlPersist=no",
        "-o", "ProxyCommand=false",
        "-o", "RequestTTY=no",
        "-T", "-a", "-x",
        target.host,
        command,
    ]


def _internal_remote_command(target: SshTarget, subcommand: str) -> list[str]:
    return _remote_command(
        target,
        shlex.join([target.peanut_review_bin, subcommand]),
    )


def check_control_master(target: SshTarget, timeout: float = 5.0) -> None:
    control_path = Path(target.control_path)
    try:
        control_stat = control_path.stat()
    except OSError as exc:
        raise SshTransportError(
            f"persistent SSH control socket is unavailable: {control_path}: {exc}"
        ) from exc
    if not stat.S_ISSOCK(control_stat.st_mode):
        raise SshTransportError(
            f"persistent SSH control path is not a socket: {control_path}"
        )
    if hasattr(os, "getuid") and control_stat.st_uid != os.getuid():
        raise SshTransportError(
            f"persistent SSH control socket is not owned by the current user: {control_path}"
        )
    command = [
        "ssh", "-S", target.control_path,
        "-o", "BatchMode=yes",
        "-O", "check", target.host,
    ]
    try:
        result = subprocess.run(command, capture_output=True, text=True, timeout=timeout)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise SshTransportError(
            f"could not check persistent SSH connection for {target.host}: {exc}"
        ) from exc
    if result.returncode != 0:
        detail = result.stderr.strip() or result.stdout.strip() or f"exit {result.returncode}"
        raise SshTransportError(
            f"persistent SSH connection is unavailable for {target.host}: {detail}"
        )


def _run_control_request(
    target: SshTarget,
    subcommand: str,
    payload: dict[str, Any],
    *,
    timeout: float,
) -> dict[str, Any]:
    command = _internal_remote_command(target, subcommand)
    try:
        result = subprocess.run(
            command,
            input=json.dumps(payload),
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise SshTransportError(f"remote {subcommand} failed: {exc}") from exc
    if result.returncode != 0:
        detail = result.stderr.strip() or result.stdout.strip() or f"exit {result.returncode}"
        raise SshTransportError(f"remote {subcommand} failed: {detail}")
    try:
        response = json.loads(result.stdout)
    except ValueError as exc:
        raise SshTransportError(
            f"remote {subcommand} returned an invalid response"
        ) from exc
    if not isinstance(response, dict) or not response.get("ok"):
        errors = response.get("errors", []) if isinstance(response, dict) else []
        raise SshTransportError(
            f"remote {subcommand} rejected target: " + "; ".join(map(str, errors))
        )
    return response


def preflight_agent(
    session_dir: str | Path,
    agent: AgentConfig,
    target: SshTarget,
) -> dict[str, Any]:
    """Validate the existing channel and remote checkout before model spend."""
    check_control_master(target)
    local_session = sess.load_session(session_dir)
    launch_id = f"preflight-{uuid.uuid4().hex}"
    token = gateway.issue_capability(
        session_dir,
        agent=agent.name,
        launch_id=launch_id,
        ttl_seconds=60,
        allowed_ops={"hello"},
    )
    try:
        return _run_control_request(
            target,
            "ssh-probe",
            {
                "protocol": gateway.PROTOCOL_VERSION,
                "session_id": local_session.id,
                "agent": agent.name,
                "runner": agent.runner,
                "workspace_root": target.workspace_root,
                "repo_relative": target.repo_relative,
                "build_roots": target.build_roots,
                "runtime_root": target.runtime_root,
                "base_ref": local_session.base_ref,
                "topic_ref": local_session.topic_ref,
                "current_head": local_session.current_head,
                "gateway_url": target.gateway_url,
                "gateway_token": token,
            },
            timeout=20,
        )
    finally:
        gateway.revoke_capability(session_dir, launch_id)


def stop_remote_launch(
    session_dir: str | Path,
    agent_name: str,
    launch_id: str,
    *,
    grace_seconds: float = 3,
) -> dict[str, Any]:
    """Idempotently stop one identity-checked remote launch."""
    session = sess.load_session(session_dir)
    agent = next((item for item in session.agents if item.name == agent_name), None)
    if agent is None or not agent.ssh_target:
        raise SshTransportError(f"SSH agent not found: {agent_name}")
    target = session.ssh_targets[agent.ssh_target]
    check_control_master(target)
    return _run_control_request(
        target,
        "ssh-stop",
        {
            "session_id": session.id,
            "launch_id": launch_id,
            "runtime_root": target.runtime_root,
            "grace_seconds": grace_seconds,
        },
        timeout=max(grace_seconds * 2 + 3, 5),
    )


def _git(repo: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True,
        text=True,
        timeout=10,
    )


def remote_probe(payload: dict[str, Any]) -> dict[str, Any]:
    errors: list[str] = []
    if payload.get("protocol") != gateway.PROTOCOL_VERSION:
        errors.append(
            f"protocol mismatch: local={gateway.PROTOCOL_VERSION} "
            f"requested={payload.get('protocol')}"
        )
    workspace = Path(str(payload.get("workspace_root", "")))
    repo_relative = str(payload.get("repo_relative") or ".")
    relative_path = Path(repo_relative)
    if relative_path.is_absolute() or ".." in relative_path.parts:
        errors.append("repo_relative must stay under workspace_root")
    repo = workspace if repo_relative in {"", "."} else workspace / relative_path
    if not workspace.is_dir():
        errors.append(f"workspace does not exist: {workspace}")
    elif not os.access(workspace, os.R_OK | os.W_OK | os.X_OK):
        errors.append(f"workspace is not readable and writable: {workspace}")
    if not repo.is_dir():
        errors.append(f"repository does not exist: {repo}")

    for raw_build in payload.get("build_roots", []):
        build = Path(str(raw_build))
        if not build.is_dir():
            errors.append(f"build root does not exist: {build}")
        elif not os.access(build, os.R_OK | os.W_OK | os.X_OK):
            errors.append(f"build root is not readable and writable: {build}")

    runtime_root = Path(str(payload.get("runtime_root", "")))
    runtime_parent = runtime_root if runtime_root.exists() else runtime_root.parent
    if not runtime_root.is_absolute():
        errors.append("runtime root must be absolute")
    elif not runtime_parent.is_dir() or not os.access(runtime_parent, os.W_OK | os.X_OK):
        errors.append(f"runtime root parent is not writable: {runtime_parent}")
    session_runtime = runtime_root / str(payload.get("session_id", ""))
    if session_runtime.is_dir():
        agent_name = str(payload.get("agent", ""))
        for run_dir in session_runtime.iterdir():
            if not run_dir.is_dir():
                continue
            identity_path = run_dir / "process.json"
            owner_path = run_dir / "owner.json"
            sensitive = [run_dir / "prompt.md", run_dir / "persona.md"]
            owner: dict[str, Any] = {}
            try:
                owner = json.loads(owner_path.read_text())
            except (FileNotFoundError, ValueError):
                pass
            if owner and owner.get("agent") != agent_name:
                continue
            if identity_path.is_file():
                try:
                    identity = json.loads(identity_path.read_text())
                    if identity.get("agent") == agent_name:
                        pid = int(identity["pid"])
                        live = (
                            runtime.is_process_live(pid)
                            and _proc_start_ticks(pid) == int(identity["start_ticks"])
                            and os.getpgid(pid) == int(identity["pgid"])
                        )
                        state = "active" if live else "stale"
                        errors.append(
                            f"{state} remote launch {run_dir.name} requires "
                            f"local recover-ssh --agent {agent_name}"
                        )
                except (OSError, ValueError, KeyError, ProcessLookupError):
                    errors.append(
                        f"unverifiable remote launch {run_dir.name} requires "
                        f"local recover-ssh --agent {agent_name}"
                    )
            elif any(path.exists() for path in sensitive):
                errors.append(
                    f"stale remote launch files in {run_dir.name} require "
                    f"local recover-ssh --agent {agent_name}"
                )

    runner = str(payload.get("runner", ""))
    runner_binary = RUNNER_BINARIES.get(runner)
    if runner_binary is None:
        errors.append(f"unsupported runner: {runner}")
    elif shutil.which(runner_binary) is None:
        errors.append(f"runner executable not found: {runner_binary}")
    if runner == "cursor" and workspace.is_dir():
        try:
            validation.validate_cursor_cli_json(workspace)
        except validation.ValidationError as exc:
            errors.append(str(exc))

    head = ""
    if repo.is_dir():
        head_result = _git(repo, "rev-parse", "--verify", "HEAD^{commit}")
        if head_result.returncode != 0:
            errors.append(f"could not resolve remote HEAD: {head_result.stderr.strip()}")
        else:
            head = head_result.stdout.strip()
            if head != payload.get("current_head"):
                errors.append(
                    f"remote HEAD {head[:12]} does not match pinned head "
                    f"{str(payload.get('current_head', ''))[:12]}"
                )
        for label in ("base_ref", "topic_ref"):
            ref = str(payload.get(label, ""))
            result = _git(repo, "cat-file", "-e", f"{ref}^{{commit}}")
            if result.returncode != 0:
                errors.append(f"remote repository lacks pinned {label}: {ref}")
        status = _git(repo, "status", "--porcelain", "--untracked-files=no")
        if status.returncode != 0:
            errors.append(f"could not inspect remote worktree: {status.stderr.strip()}")
        elif status.stdout.strip():
            errors.append("remote worktree has staged or tracked modifications")

    gateway_url = str(payload.get("gateway_url", ""))
    gateway_token = str(payload.get("gateway_token", ""))
    try:
        hello = gateway.GatewayClient(
            gateway_url,
            gateway_token,
            str(payload.get("session_id", "")),
            timeout=5,
        ).request("GET", "hello")
        if hello.get("protocol") != gateway.PROTOCOL_VERSION:
            errors.append("reviewer gateway protocol mismatch")
        if hello.get("agent") != payload.get("agent"):
            errors.append("reviewer gateway capability agent mismatch")
    except gateway.GatewayError as exc:
        errors.append(f"reviewer gateway is unavailable: {exc}")

    return {
        "ok": not errors,
        "errors": errors,
        "protocol": gateway.PROTOCOL_VERSION,
        "workspace": str(workspace),
        "repo": str(repo),
        "head": head,
        "runner": runner,
        "runner_binary": runner_binary,
    }


def _proc_start_ticks(pid: int) -> int:
    fields = (Path("/proc") / str(pid) / "stat").read_text().split()
    return int(fields[21])


def _write_private_json(path: Path, value: dict[str, Any]) -> None:
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        os.write(fd, (json.dumps(value, separators=(",", ":")) + "\n").encode())
        os.fsync(fd)
    finally:
        os.close(fd)


def _remote_run_dir(payload: dict[str, Any]) -> Path:
    session_id = str(payload["session_id"])
    launch_id = str(payload["launch_id"])
    for label, value in (("session_id", session_id), ("launch_id", launch_id)):
        if not value or any(ch not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_-" for ch in value):
            raise SshTransportError(f"invalid {label}")
    return Path(str(payload["runtime_root"])) / session_id / launch_id


def _remote_wrapper_command(payload: dict[str, Any], run_dir: Path) -> list[str]:
    runner = str(payload["runner"])
    wrapper = launch._find_launcher_script(runner)
    command = [
        wrapper,
        "--model", str(payload["model"]),
        "--workspace", str(payload["workspace_root"]),
        "--output-dir", str(run_dir / "log"),
        "--name", str(payload["agent"]),
        "--timeout", str(payload["timeout"]),
        "--prompt-file", str(run_dir / "prompt.md"),
    ]
    if runner == "codex":
        command += [
            "--sandbox", "danger-full-access",
            "--add-dir", str(run_dir),
            "--add-dir", "/tmp",
        ]
        if payload.get("reasoning_effort"):
            command += ["--reasoning-effort", str(payload["reasoning_effort"])]
        command.append("--fast-mode" if payload.get("fast_mode") is True else "--no-fast-mode")
    return command


def remote_run(payload: dict[str, Any]) -> int:
    """Run one reviewer remotely and guarantee group cleanup on termination."""
    run_dir = _remote_run_dir(payload)
    run_dir.mkdir(parents=True, mode=0o700, exist_ok=False)
    run_dir.chmod(0o700)
    owner_path = run_dir / "owner.json"
    _write_private_json(owner_path, {
        "agent": payload["agent"],
        "launch_id": payload["launch_id"],
    })
    prompt_path = run_dir / "prompt.md"
    persona_path = run_dir / "persona.md"
    prompt_path.write_text(str(payload["prompt"]))
    persona_path.write_text(str(payload["persona"]))
    prompt_path.chmod(0o600)
    persona_path.chmod(0o600)
    (run_dir / "log").mkdir(mode=0o700)

    env = os.environ.copy()
    env.update({
        "GIT_AUTHOR_NAME": str(payload["agent"]),
        "GIT_AUTHOR_EMAIL": f"{payload['agent']}@peanut-review.local",
        "GIT_COMMITTER_NAME": str(payload["agent"]),
        "GIT_COMMITTER_EMAIL": f"{payload['agent']}@peanut-review.local",
        "PEANUT_SESSION": f"peanut://{payload['session_id']}",
        "PEANUT_REVIEW_GATEWAY_URL": str(payload["gateway_url"]),
        "PEANUT_REVIEW_GATEWAY_TOKEN": str(payload["gateway_token"]),
    })
    try:
        command = _remote_wrapper_command(payload, run_dir)
    except Exception:
        for path in (owner_path, prompt_path, persona_path):
            try:
                path.unlink()
            except FileNotFoundError:
                pass
        raise
    proc: subprocess.Popen | None = None

    def terminate_child(_signum=None, _frame=None) -> None:
        if proc is not None and proc.poll() is None:
            try:
                os.killpg(proc.pid, signal.SIGTERM)
            except ProcessLookupError:
                pass

    for sig in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP):
        signal.signal(sig, terminate_child)
    try:
        proc = subprocess.Popen(
            command,
            cwd=str(payload["workspace_root"]),
            env=env,
            start_new_session=True,
        )
        identity = {
            "pid": proc.pid,
            "pgid": proc.pid,
            "start_ticks": _proc_start_ticks(proc.pid),
            "launch_id": payload["launch_id"],
            "agent": payload["agent"],
        }
        _write_private_json(run_dir / "process.json", identity)
        gateway_client = gateway.GatewayClient(
            str(payload["gateway_url"]),
            str(payload["gateway_token"]),
            str(payload["session_id"]),
            timeout=REMOTE_GATEWAY_POLL_SECONDS,
        )
        gateway_failures = 0
        while True:
            try:
                return proc.wait(timeout=REMOTE_GATEWAY_POLL_SECONDS)
            except subprocess.TimeoutExpired:
                try:
                    gateway_client.request("GET", "hello")
                    gateway_failures = 0
                except gateway.GatewayError:
                    gateway_failures += 1
                    if gateway_failures >= REMOTE_GATEWAY_FAILURE_LIMIT:
                        terminate_child()
                        try:
                            return proc.wait(timeout=5)
                        except subprocess.TimeoutExpired:
                            try:
                                os.killpg(proc.pid, signal.SIGKILL)
                            except ProcessLookupError:
                                pass
                            return proc.wait()
    finally:
        terminate_child()
        if proc is not None and proc.poll() is None:
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                try:
                    os.killpg(proc.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                proc.wait()
        for path in (run_dir / "process.json", owner_path, prompt_path, persona_path):
            try:
                path.unlink()
            except FileNotFoundError:
                pass


def remote_stop(payload: dict[str, Any]) -> dict[str, Any]:
    run_dir = _remote_run_dir(payload)
    identity_path = run_dir / "process.json"
    try:
        identity = json.loads(identity_path.read_text())
    except FileNotFoundError:
        return {"ok": True, "stopped": False, "reason": "not running"}
    pid = int(identity["pid"])
    if identity.get("launch_id") != payload.get("launch_id"):
        raise SshTransportError("remote launch identity mismatch")
    try:
        current_start = _proc_start_ticks(pid)
        current_pgid = os.getpgid(pid)
    except (FileNotFoundError, ProcessLookupError):
        return {"ok": True, "stopped": False, "reason": "not running"}
    if current_start != int(identity["start_ticks"]) or current_pgid != int(identity["pgid"]):
        raise SshTransportError("remote process identity changed; refusing to signal")
    os.killpg(current_pgid, signal.SIGTERM)
    deadline = time.monotonic() + float(payload.get("grace_seconds", 5))
    while time.monotonic() < deadline:
        if not runtime.is_process_live(pid):
            return {"ok": True, "stopped": True, "signal": "SIGTERM"}
        time.sleep(0.05)
    try:
        if _proc_start_ticks(pid) != int(identity["start_ticks"]):
            raise SshTransportError("remote process identity changed before SIGKILL")
        os.killpg(current_pgid, signal.SIGKILL)
    except (FileNotFoundError, ProcessLookupError):
        pass
    return {"ok": True, "stopped": True, "signal": "SIGKILL"}


def remote_recover(payload: dict[str, Any]) -> dict[str, Any]:
    runtime_root = Path(str(payload["runtime_root"]))
    session_id = str(payload["session_id"])
    agent_name = str(payload["agent"])
    session_runtime = runtime_root / session_id
    recovered: list[dict[str, Any]] = []
    if not session_runtime.is_dir():
        return {"ok": True, "recovered": recovered}
    for run_dir in sorted(session_runtime.iterdir()):
        identity_path = run_dir / "process.json"
        if not run_dir.is_dir():
            continue
        owner: dict[str, Any] = {}
        try:
            owner = json.loads((run_dir / "owner.json").read_text())
        except (FileNotFoundError, ValueError):
            pass
        if owner and owner.get("agent") != agent_name:
            continue
        identity: dict[str, Any] = {}
        try:
            identity = json.loads(identity_path.read_text())
        except (FileNotFoundError, ValueError):
            pass
        if identity and identity.get("agent") != agent_name:
            continue
        sensitive_paths = [run_dir / "prompt.md", run_dir / "persona.md"]
        if not identity and not any(path.exists() for path in sensitive_paths):
            continue
        if identity:
            result = remote_stop({
                "runtime_root": str(runtime_root),
                "session_id": session_id,
                "launch_id": run_dir.name,
                "grace_seconds": payload.get("grace_seconds", 3),
            })
        else:
            result = {"ok": True, "stopped": False, "reason": "no identity"}
        removed: list[str] = []
        for name in ("process.json", "owner.json", "prompt.md", "persona.md"):
            try:
                (run_dir / name).unlink()
                removed.append(name)
            except FileNotFoundError:
                pass
        recovered.append({"launch_id": run_dir.name, "removed": removed, **result})
    return {"ok": True, "recovered": recovered}


def recover_agent(
    session_dir: str | Path,
    agent_name: str,
    *,
    grace_seconds: float = 3,
) -> dict[str, Any]:
    session = sess.load_session(session_dir)
    agent = next((item for item in session.agents if item.name == agent_name), None)
    if agent is None or not agent.ssh_target:
        raise SshTransportError(f"SSH agent not found: {agent_name}")
    target = session.ssh_targets[agent.ssh_target]
    check_control_master(target)
    result = _run_control_request(
        target,
        "ssh-recover",
        {
            "runtime_root": target.runtime_root,
            "session_id": session.id,
            "agent": agent.name,
            "grace_seconds": grace_seconds,
        },
        timeout=max(grace_seconds * 4 + 5, 10),
    )
    runtime.update_agent_meta(session_dir, agent.name, {
        "remote_process_state": "stopped",
        "ssh_channel_state": "closed",
        "ssh_cleanup_required": False,
        "ssh_recovered_at": time.time(),
    })
    return result


def _transport_payload(
    session_dir: Path,
    agent: AgentConfig,
    target: SshTarget,
    launch_id: str,
    token: str,
    prompt_path: Path,
) -> dict[str, Any]:
    session = sess.load_session(session_dir)
    persona_path = session_dir / "personas" / agent.persona
    return {
        "protocol": gateway.PROTOCOL_VERSION,
        "session_id": session.id,
        "agent": agent.name,
        "launch_id": launch_id,
        "runner": agent.runner,
        "model": agent.model,
        "reasoning_effort": agent.reasoning_effort,
        "fast_mode": agent.fast_mode,
        "timeout": session.timeout,
        "workspace_root": target.workspace_root,
        "runtime_root": target.runtime_root,
        "gateway_url": target.gateway_url,
        "gateway_token": token,
        "prompt": prompt_path.read_text(),
        "persona": persona_path.read_text() if persona_path.is_file() else "",
    }


def run_transport(args: argparse.Namespace) -> int:
    session_dir = Path(args.session)
    session = sess.load_session(session_dir)
    agent = next((item for item in session.agents if item.name == args.agent), None)
    if agent is None or not agent.ssh_target:
        print("Error: SSH agent configuration not found", file=sys.stderr)
        return 1
    target = session.ssh_targets[agent.ssh_target]
    token = os.environ.pop("PEANUT_REVIEW_GATEWAY_TOKEN", "")
    if not token:
        print("Error: reviewer gateway capability is missing", file=sys.stderr)
        return 1
    payload = _transport_payload(
        session_dir, agent, target, args.launch_id, token, Path(args.prompt_file)
    )
    child: subprocess.Popen | None = None
    stop_requested = False

    def stop_remote(_signum=None, _frame=None) -> None:
        nonlocal stop_requested
        if stop_requested:
            return
        stop_requested = True
        try:
            stop_remote_launch(
                session_dir, agent.name, args.launch_id, grace_seconds=3,
            )
        except SshTransportError:
            pass

    for sig in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP):
        signal.signal(sig, stop_remote)
    runtime.update_agent_meta(session_dir, agent.name, {
        "transport": "ssh",
        "ssh_target": agent.ssh_target,
        "ssh_host": target.host,
        "ssh_control_path": target.control_path,
        "ssh_launch_id": args.launch_id,
        "ssh_channel_state": "connecting",
        "remote_process_state": "starting",
    })
    try:
        child = subprocess.Popen(
            _internal_remote_command(target, "ssh-run"),
            stdin=subprocess.PIPE,
        )
        runtime.update_agent_meta(session_dir, agent.name, {
            "ssh_channel_state": "connected",
            "remote_process_state": "running",
        })
        assert child.stdin is not None
        child.stdin.write(json.dumps(payload).encode())
        child.stdin.close()
        return child.wait()
    finally:
        if child is not None and child.poll() is None:
            stop_remote()
            child.terminate()
            try:
                child.wait(timeout=5)
            except subprocess.TimeoutExpired:
                child.kill()
                child.wait()
        runtime.update_agent_meta(session_dir, agent.name, {
            "ssh_channel_state": "closed",
            "remote_process_state": "stopped",
        })
        gateway.revoke_capability(session_dir, args.launch_id)


def main_probe() -> int:
    try:
        result = remote_probe(_read_json_stdin())
        print(json.dumps(result, separators=(",", ":")))
        return 0 if result["ok"] else 1
    except (OSError, SshTransportError) as exc:
        print(json.dumps({"ok": False, "errors": [str(exc)]}, separators=(",", ":")))
        return 1


def main_run() -> int:
    try:
        return remote_run(_read_json_stdin())
    except (OSError, KeyError, ValueError, SshTransportError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1


def main_stop() -> int:
    try:
        print(json.dumps(remote_stop(_read_json_stdin()), separators=(",", ":")))
        return 0
    except (OSError, KeyError, ValueError, SshTransportError) as exc:
        print(json.dumps({"ok": False, "errors": [str(exc)]}, separators=(",", ":")))
        return 1


def main_recover() -> int:
    try:
        print(json.dumps(remote_recover(_read_json_stdin()), separators=(",", ":")))
        return 0
    except (OSError, KeyError, ValueError, SshTransportError) as exc:
        print(json.dumps({"ok": False, "errors": [str(exc)]}, separators=(",", ":")))
        return 1
