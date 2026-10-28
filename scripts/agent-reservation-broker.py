"""Admit sandbox processes to a host-owned CPU reservation without sudo.

The server only moves the authenticated peer; commands execute in the client,
with its original mount namespace, environment, cwd, and file descriptors.
"""
from __future__ import annotations

import argparse
import ctypes
import json
import os
from pathlib import Path
import resource
import select
import signal
import socket
import stat
import struct
import subprocess
import sys
import time

DIRECTORY = Path("/run/agent-cpu-reserve")
SOCKET = DIRECTORY / "broker.sock"
CGROUP = Path("/sys/fs/cgroup")
WORKLOAD_LOCK = Path("/run/agent-reserved-workload.lock")
# Linux UAPI, available since 6.5; Python does not yet expose this constant.
SO_PEERPIDFD = 77
REQUEST = b"join-v1"


def peer_credentials(connection):
    return struct.unpack("3i", connection.getsockopt(
        socket.SOL_SOCKET, socket.SO_PEERCRED, struct.calcsize("3i")))


def verify_broker(connection):
    _, uid, _ = peer_credentials(connection)
    # Rootless Bubblewrap maps only the invoking user. Host root consequently
    # appears as the overflow UID both in SO_PEERCRED and stat results. Anchor
    # trust in the administrator-owned, non-writable directory mounted by the
    # launcher, rather than requiring the translated peer UID to be zero.
    info = DIRECTORY.lstat()
    if (uid == os.geteuid() or uid != info.st_uid or
            not stat.S_ISDIR(info.st_mode) or info.st_mode & 0o022):
        raise RuntimeError("reservation broker is not owned by the host administrator")


def current_cgroup():
    value = Path("/proc/self/cgroup").read_text().strip()
    if not value.startswith("0::/") or "\n" in value:
        raise RuntimeError("a unified cgroup hierarchy is required")
    return CGROUP / value[4:]


def prepare_scope():
    """Keep the scope root empty so each admission can have memory limits."""
    root = current_cgroup()
    session = root / "session"
    session.mkdir()
    (session / "cgroup.procs").write_text(str(os.getpid()))
    (root / "cgroup.subtree_control").write_text("+cpuset +memory +pids")


def effective_limit(source, name):
    limits = []
    while source != CGROUP:
        value = (source / name).read_text().strip()
        if value != "max":
            limits.append(int(value))
        source = source.parent
    return str(min(limits)) if limits else "max"


class Peer:
    """Obtain identity from the socket, never from a caller-supplied PID."""
    def __init__(self, connection, uid):
        self.pid, peer_uid, _ = peer_credentials(connection)
        if peer_uid != uid or uid == 0:
            raise RuntimeError("reservation belongs to another user")
        self.pidfd = connection.getsockopt(socket.SOL_SOCKET, SO_PEERPIDFD)
        self.procfd = None
        try:
            self.check_alive()
            self.procfd = os.open(f"/proc/{self.pid}", os.O_RDONLY | os.O_DIRECTORY)
            self.check_alive()
            status = self.read("status")
            uids = next(line for line in status.splitlines() if line.startswith("Uid:")).split()[1:]
            if {int(value) for value in uids} != {uid}:
                raise RuntimeError("peer credentials changed")
        except BaseException:
            self.close()
            raise

    def read(self, name):
        fd = os.open(name, os.O_RDONLY, dir_fd=self.procfd)
        with os.fdopen(fd) as source:
            return source.read()

    def check_alive(self):
        poller = select.poll()
        poller.register(self.pidfd, select.POLLIN)
        if poller.poll(0):
            raise RuntimeError("requesting process exited")
        signal.pidfd_send_signal(self.pidfd, 0)

    def close(self):
        if self.procfd is not None:
            os.close(self.procfd)
        os.close(self.pidfd)

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()


class Broker:
    def __init__(self, config, h):
        h.require_root()
        self.config, self.h = config, h
        self.groups = set()
        DIRECTORY.mkdir(mode=0o755, exist_ok=True)
        info = DIRECTORY.lstat()
        if not stat.S_ISDIR(info.st_mode) or info.st_uid != 0 or info.st_mode & 0o022:
            raise RuntimeError("broker directory must be root-owned without other write access")
        # The host reservation lock is already held. A prior guard may have
        # been killed before it could unlink its socket.
        SOCKET.unlink(missing_ok=True)
        self.socket = socket.socket(socket.AF_UNIX, socket.SOCK_SEQPACKET)
        try:
            self.socket.bind(str(SOCKET))
            SOCKET.chmod(0o600)
            os.chown(SOCKET, config["uid"], config["gid"])
            self.socket.listen(16)
        except BaseException:
            self.close()
            raise

    def join(self, connection):
        with Peer(connection, self.config["uid"]) as peer:
            if connection.recv(64) != REQUEST:
                raise RuntimeError("unsupported reservation request")
            control_group = f"/benchmark.slice/{self.config['scope']}"
            if self.h.systemctl_show(self.config["scope"], "ActiveState") != "active":
                raise RuntimeError("reservation is not ready or is stopping")
            if self.h.systemctl_show(self.config["scope"], "ControlGroup") != control_group:
                raise RuntimeError("unexpected reservation cgroup")
            root = CGROUP / control_group[1:]
            if not (root / "session").is_dir():
                raise RuntimeError("reservation is not ready")
            value = peer.read("cgroup").strip()
            if not value.startswith("0::/") or "\n" in value:
                raise RuntimeError("peer must use the host cgroup namespace")
            source = CGROUP / value[4:]
            # Kernel-generated cgroup paths are read through the pinned proc
            # directory. Snapshot ceilings before leaving the source hierarchy.
            limits = {name: effective_limit(source, name)
                      for name in ("memory.max", "memory.swap.max", "pids.max")}
            peer.check_alive()
            target = root / f"workload-{peer.pid}-{time.monotonic_ns()}"
            target.mkdir()
            self.groups.add(target)
            for name, limit in limits.items():
                (target / name).write_text(limit)
            (target / "memory.oom.group").write_text("1")
            peer.check_alive()
            (target / "cgroup.procs").write_text(str(peer.pid))
            cpus = self.h.parse_cpu_list(self.config["cpus"])
            peer.check_alive()
            os.sched_setaffinity(peer.pid, cpus)
            os.setpriority(os.PRIO_PROCESS, peer.pid, 0)
            soft, hard = resource.prlimit(peer.pid, resource.RLIMIT_NICE)
            resource.prlimit(peer.pid, resource.RLIMIT_NICE, tuple(
                value if value == resource.RLIM_INFINITY else max(20, value)
                for value in (soft, hard)))
            peer.check_alive()
            if os.sched_getaffinity(peer.pid) != cpus:
                raise RuntimeError("failed to enter the reserved CPU set")
            connection.sendall(json.dumps({"cpus": self.config["cpus"],
                                           "aslr_disabled": self.config["aslr_disabled"]}).encode())

    def serve(self, timeout=1):
        self.socket.settimeout(timeout)
        try:
            connection, _ = self.socket.accept()
        except socket.timeout:
            connection = None
        if connection is not None:
            with connection:
                connection.settimeout(1)
                try:
                    self.join(connection)
                except (OSError, RuntimeError, ValueError) as exc:
                    try:
                        connection.sendall(json.dumps({"error": str(exc)}).encode())
                    except OSError:
                        pass
        # Empty cgroups do not consume PIDs, but must not accumulate across a
        # long shared session. Descendants keep populated=1 after exec exits.
        for group in self.groups.copy():
            try:
                if "populated 0" in (group / "cgroup.events").read_text().splitlines():
                    group.rmdir()
                    self.groups.remove(group)
            except FileNotFoundError:
                self.groups.remove(group)

    def close(self):
        self.socket.close()
        SOCKET.unlink(missing_ok=True)


def join_and_run(command, wait):
    # Take the same kernel lock as already-reserved callers before admission.
    # Keep it across exec, so the whole command tree shares it with nested runs.
    fd = os.open(WORKLOAD_LOCK, os.O_RDONLY)
    os.dup2(fd, 200, inheritable=True)
    if fd != 200:
        os.close(fd)
    result = subprocess.run(
        ["/usr/bin/flock", "--exclusive", "--wait", str(wait), "200"], pass_fds=(200,))
    if result.returncode:
        print("Error: another benchmark command holds the workload lock", file=sys.stderr)
        return 75
    with socket.socket(socket.AF_UNIX, socket.SOCK_SEQPACKET) as connection:
        connection.settimeout(10)
        connection.connect(str(SOCKET))
        verify_broker(connection)
        connection.sendall(REQUEST)
        response = json.loads(connection.recv(4096))
    if "error" in response:
        raise RuntimeError(response["error"])
    # These process settings cannot be inherited from the host session when
    # joining from a different agent, so apply them in the client before exec.
    libc = ctypes.CDLL(None, use_errno=True)
    if libc.prctl(38, 1, 0, 0, 0) != 0:
        raise OSError(ctypes.get_errno(), "cannot set NoNewPrivs")
    if response["aslr_disabled"]:
        libc.personality.argtypes = [ctypes.c_ulong]
        previous = libc.personality(0xffffffff)
        if previous == -1 or libc.personality(previous | 0x40000) == -1:
            raise OSError(ctypes.get_errno(), "cannot disable address randomization")
    os.environ["AGENT_RESERVED_CPUS"] = response["cpus"]
    os.environ["AGENT_RESERVED_WORKLOAD_LOCK_PID"] = str(os.getpid())
    # Keep a supervisor holding the lock even if a program closes inherited
    # descriptors. Nested wrappers find its PID and reuse that same lock.
    workload = subprocess.Popen(command, pass_fds=(200,))
    def forward(signum, _frame):
        if workload.poll() is None:
            workload.send_signal(signum)
    for signum in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP):
        signal.signal(signum, forward)
    code = workload.wait()
    return code if code >= 0 else 128 - code


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--wait-lock", type=int, default=0)
    parser.add_argument("command", nargs=argparse.REMAINDER)
    args = parser.parse_args()
    if args.command and args.command[0] == "--":
        args.command = args.command[1:]
    if not args.command or not 0 <= args.wait_lock <= 86400:
        parser.error("supply a command and a lock wait between 0 and 86400 seconds")
    try:
        return join_and_run(args.command, args.wait_lock)
    except (OSError, RuntimeError, ValueError) as exc:
        print(f"Error: cannot join host reservation: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
