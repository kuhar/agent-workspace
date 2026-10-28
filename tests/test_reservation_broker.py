"""Exercise broker admission and real Unix socket/lock behavior without root."""
import importlib.util
import json
import os
from pathlib import Path
import signal
import socket
import subprocess
import sys
import tempfile
import types
import unittest
from unittest.mock import patch

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
spec = importlib.util.spec_from_file_location("broker", SCRIPTS / "agent-reservation-broker.py")
b = importlib.util.module_from_spec(spec)
spec.loader.exec_module(b)


class PeerTests(unittest.TestCase):
    def test_host_root_authentication_survives_user_namespace_translation(self):
        directory = types.SimpleNamespace(lstat=lambda: types.SimpleNamespace(
            st_uid=65534, st_mode=0o40755))
        with patch.object(b, "DIRECTORY", directory), \
             patch.object(b, "peer_credentials", return_value=(123, 65534, 65534)), \
             patch.object(b.os, "geteuid", return_value=1000):
            b.verify_broker(None)

    def test_writable_socket_directory_is_rejected(self):
        directory = types.SimpleNamespace(lstat=lambda: types.SimpleNamespace(
            st_uid=0, st_mode=0o40777))
        with patch.object(b, "DIRECTORY", directory), \
             patch.object(b, "peer_credentials", return_value=(123, 0, 0)), \
             self.assertRaisesRegex(RuntimeError, "host administrator"):
            b.verify_broker(None)

    def test_identity_comes_from_kernel_credentials(self):
        connection, other = socket.socketpair(socket.AF_UNIX, socket.SOCK_SEQPACKET)
        with connection, other:
            with b.Peer(connection, os.getuid()) as peer:
                self.assertEqual(peer.pid, os.getpid())
                self.assertIn(f"Pid:\t{os.getpid()}\n", peer.read("status"))

    def test_other_user_is_rejected(self):
        left, right = socket.socketpair(socket.AF_UNIX, socket.SOCK_SEQPACKET)
        with left, right, self.assertRaisesRegex(RuntimeError, "another user"):
            b.Peer(left, os.getuid() + 1)

    def test_exited_peer_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            address = str(Path(directory) / "socket")
            with socket.socket(socket.AF_UNIX, socket.SOCK_SEQPACKET) as server:
                server.bind(address)
                server.listen()
                child = subprocess.Popen([sys.executable, "-c",
                    "import socket,sys; s=socket.socket(socket.AF_UNIX,socket.SOCK_SEQPACKET); s.connect(sys.argv[1])", address])
                connection, _ = server.accept()
                child.wait(timeout=5)
                with connection, self.assertRaisesRegex(RuntimeError, "exited"):
                    b.Peer(connection, os.getuid())


class AdmissionTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.mock(b, "CGROUP", self.root)
        source = Path("/proc/self/cgroup").read_text().strip()[4:]
        self.source = self.root / source
        node = self.source
        while node != self.root:
            node.mkdir(parents=True, exist_ok=True)
            for name in ("memory.max", "memory.swap.max", "pids.max"):
                (node / name).write_text("max")
            node = node.parent
        (self.source / "memory.max").write_text("1024")
        (self.source.parent / "memory.max").write_text("512")
        (self.source / "pids.max").write_text("64")
        self.broker = b.Broker.__new__(b.Broker)
        self.broker.groups = set()
        self.broker.config = {"uid": os.getuid(), "gid": os.getgid(),
                              "scope": "agent-reserved-1-2.scope", "cpus": "0-1",
                              "aslr_disabled": True}
        self.target = self.root / "benchmark.slice" / self.broker.config["scope"]
        (self.target / "session").mkdir(parents=True)
        self.broker.h = types.SimpleNamespace(
            systemctl_show=lambda _, name: "active" if name == "ActiveState" else
            "/benchmark.slice/agent-reserved-1-2.scope", parse_cpu_list=lambda _: {0, 1})
        self.affinity = self.mock(b.os, "sched_setaffinity")
        self.mock(b.os, "sched_getaffinity", return_value={0, 1})
        self.priority = self.mock(b.os, "setpriority")
        self.limit = self.mock(b.resource, "prlimit", return_value=(0, 0))

    def mock(self, obj, name, *args, **kwargs):
        patcher = patch.object(obj, name, *args, **kwargs)
        self.addCleanup(patcher.stop)
        return patcher.start()

    def test_join_preserves_ancestor_memory_and_pid_ceilings(self):
        left, right = socket.socketpair(socket.AF_UNIX, socket.SOCK_SEQPACKET)
        with left, right:
            right.sendall(b.REQUEST)
            self.broker.join(left)
            self.assertEqual(json.loads(right.recv(4096)), {"cpus": "0-1", "aslr_disabled": True})
        group, = self.broker.groups
        self.assertEqual((group / "memory.max").read_text(), "512")
        self.assertEqual((group / "pids.max").read_text(), "64")
        self.assertEqual((group / "cgroup.procs").read_text(), str(os.getpid()))
        self.affinity.assert_called_once_with(os.getpid(), {0, 1})
        self.priority.assert_called_once_with(os.PRIO_PROCESS, os.getpid(), 0)
        self.assertEqual(self.limit.call_args.args[2], (20, 20))

    def test_no_pid_or_command_can_be_requested(self):
        left, right = socket.socketpair(socket.AF_UNIX, socket.SOCK_SEQPACKET)
        with left, right:
            right.sendall(b'join-v1 {"pid": 1, "command": "/bin/sh"}')
            with self.assertRaisesRegex(RuntimeError, "unsupported"):
                self.broker.join(left)
        self.assertFalse(self.broker.groups)
        self.affinity.assert_not_called()

    def test_stopping_reservation_rejects_entry(self):
        self.broker.h.systemctl_show = lambda *_: "inactive"
        left, right = socket.socketpair(socket.AF_UNIX, socket.SOCK_SEQPACKET)
        with left, right:
            right.sendall(b.REQUEST)
            with self.assertRaisesRegex(RuntimeError, "stopping"):
                self.broker.join(left)
        self.assertFalse(self.broker.groups)

    def test_empty_groups_are_reaped_but_descendants_keep_group(self):
        self.broker.socket = socket.socket(socket.AF_UNIX, socket.SOCK_SEQPACKET)
        self.addCleanup(self.broker.socket.close)
        self.broker.socket.bind(str(self.root / "socket"))
        self.broker.socket.listen()
        groups = [self.root / f"group-{n}" for n in (0, 1)]
        for n, group in enumerate(groups):
            group.mkdir()
            (group / "cgroup.events").write_text(f"populated {n}\n")
            self.broker.groups.add(group)
        # Real cgroup attribute files vanish with rmdir; ordinary fixture files
        # need removing explicitly before the directory can be removed.
        rmdir = Path.rmdir
        def remove(group):
            (group / "cgroup.events").unlink()
            rmdir(group)
        with patch.object(Path, "rmdir", remove):
            self.broker.serve(.01)
        self.assertFalse(groups[0].exists())
        self.assertEqual(self.broker.groups, {groups[1]})


class ClientTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.address = self.root / "socket"
        self.lock = self.root / "lock"
        self.lock.touch()
        self.server = socket.socket(socket.AF_UNIX, socket.SOCK_SEQPACKET)
        self.addCleanup(self.server.close)
        self.server.bind(str(self.address))
        self.server.listen()
        self.server.settimeout(5)

    def launch(self, command, wait=0, trust=True, **kwargs):
        script = ("import importlib.util,os,sys\n"
                  "s=importlib.util.spec_from_file_location('broker',sys.argv[1]); b=importlib.util.module_from_spec(s); s.loader.exec_module(b)\n"
                  "b.SOCKET=b.Path(sys.argv[2]); b.WORKLOAD_LOCK=b.Path(sys.argv[3])\n")
        if trust:
            # Only the root-credential check is substituted. Socket I/O,
            # flock, exec, process settings, and signal forwarding are real.
            script += "b.verify_broker=lambda _: None\n"
        else:
            script += "b.DIRECTORY=b.SOCKET.parent\n"
        script += "sys.exit(b.join_and_run(sys.argv[5:],int(sys.argv[4])))\n"
        return subprocess.Popen([sys.executable, "-c", script,
            str(SCRIPTS / "agent-reservation-broker.py"), str(self.address), str(self.lock),
            str(wait), *command], text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, **kwargs)

    def admit(self, error=None):
        connection, _ = self.server.accept()
        with connection:
            self.assertEqual(connection.recv(64), b.REQUEST)
            connection.sendall(json.dumps({"error": error} if error else
                {"cpus": "0-1", "aslr_disabled": False}).encode())

    def test_arguments_environment_cwd_stdin_stdout_exit_code(self):
        literal = "space ' $\nvalue"
        code = ("import json,os,pathlib,sys; "
                "print(json.dumps([sys.argv[1],os.environ['BROKER_TEST'],str(pathlib.Path.cwd()),sys.stdin.read(),"
                "os.environ['AGENT_RESERVED_CPUS'],'NoNewPrivs:\\t1' in pathlib.Path('/proc/self/status').read_text()])); "
                "raise SystemExit(7)")
        child = self.launch([sys.executable, "-c", code, literal], cwd=self.root,
                            env=dict(os.environ, BROKER_TEST=literal), stdin=subprocess.PIPE)
        self.admit()
        stdout, stderr = child.communicate("input text", timeout=5)
        self.assertEqual(child.returncode, 7, stderr)
        self.assertEqual(json.loads(stdout), [literal, literal, str(self.root), "input text", "0-1", True])

    def test_unprivileged_server_is_rejected(self):
        child = self.launch(["/usr/bin/true"], trust=False)
        connection, _ = self.server.accept()
        connection.close()
        _, stderr = child.communicate(timeout=5)
        self.assertNotEqual(child.returncode, 0)
        self.assertIn("host administrator", stderr)

    def test_lock_is_held_when_workload_closes_inherited_descriptors(self):
        marker = self.root / "ready"
        first = self.launch([sys.executable, "-c",
            "import os,pathlib,sys,time; os.close(200); pathlib.Path(sys.argv[1]).touch(); time.sleep(30)", str(marker)])
        self.admit()
        try:
            import time
            deadline = time.monotonic() + 5
            while not marker.exists() and time.monotonic() < deadline:
                time.sleep(.01)
            self.assertTrue(marker.exists())
            second = self.launch(["/usr/bin/true"])
            _, stderr = second.communicate(timeout=5)
            self.assertEqual(second.returncode, 75, stderr)
            first.terminate()
            first.communicate(timeout=5)
            self.assertEqual(first.returncode, 128 + signal.SIGTERM)
            third = self.launch(["/usr/bin/true"])
            self.admit()
            _, stderr = third.communicate(timeout=5)
            self.assertEqual(third.returncode, 0, stderr)
        finally:
            if first.poll() is None:
                first.kill()
                first.communicate()

    def test_server_refusal_prevents_command_execution(self):
        marker = self.root / "ran"
        child = self.launch(["/usr/bin/touch", str(marker)])
        self.admit("reservation is stopping")
        _, stderr = child.communicate(timeout=5)
        self.assertNotEqual(child.returncode, 0)
        self.assertIn("reservation is stopping", stderr)
        self.assertFalse(marker.exists())


if __name__ == "__main__":
    unittest.main()
