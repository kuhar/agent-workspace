#!/usr/bin/python3
"""Test an already-running shared host reservation without sudo or host changes.

Start it in another terminal with:
  agent-reserved-run --profile stable --cores 24 --session -- bash
This smoke test leaves that session running.
"""
import ctypes
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import time

WRAPPER = "/usr/local/bin/agent-reserved-run"


def call(argv, expected=0, **kwargs):
    result = subprocess.run(argv, text=True, capture_output=True, timeout=15, **kwargs)
    assert result.returncode == expected, (argv, result.returncode, result.stdout, result.stderr)
    return result


def wait_for(path):
    deadline = time.monotonic() + 5
    while not path.exists() and time.monotonic() < deadline:
        time.sleep(.02)
    assert path.exists(), f"missing readiness marker: {path}"


def main():
    assert Path("/run/agent-cpu-reserve/broker.sock").is_socket()
    assert "/benchmark.slice/" not in Path("/proc/self/cgroup").read_text()
    # Descendants cannot acquire privileges, even if the old sudoers entry is
    # still installed. Start at the lowered priority commonly used by agents.
    assert ctypes.CDLL(None).prctl(38, 1, 0, 0, 0) == 0
    os.nice(10)
    probe = call([WRAPPER, "/usr/bin/python3", "-c", """
import json,os,pathlib
assert os.getpriority(os.PRIO_PROCESS,0)==0
assert 'NoNewPrivs:\\t1' in pathlib.Path('/proc/self/status').read_text()
assert int(pathlib.Path('/proc/self/personality').read_text(),16)&0x40000
print(json.dumps({'uid':os.getuid(),'cpus':sorted(os.sched_getaffinity(0)),
                  'cgroup':pathlib.Path('/proc/self/cgroup').read_text().strip()}))
"""])
    metadata = json.loads(probe.stdout)
    assert metadata["uid"] == os.getuid()
    assert "/workload-" in metadata["cgroup"]
    print(json.dumps({"host_entry": metadata}), flush=True)

    with tempfile.TemporaryDirectory(prefix="agent-broker-smoke-") as directory:
        root = Path(directory)
        (root / "marker").touch()
        mount = "/broker-smoke-only"
        assert not Path(mount).exists()
        sandbox = ["/usr/bin/bwrap", "--ro-bind", "/usr", "/usr", "--symlink", "usr/bin", "/bin",
                   "--symlink", "usr/lib", "/lib", "--symlink", "usr/lib64", "/lib64",
                   "--ro-bind", "/etc", "/etc", "--ro-bind", "/sys", "/sys", "--proc", "/proc",
                   "--dev", "/dev", "--dir", "/run", "--ro-bind", "/run/agent-cpu-reserve", "/run/agent-cpu-reserve",
                   "--ro-bind", "/run/agent-reserved-workload.lock", "/run/agent-reserved-workload.lock",
                   "--bind", directory, mount, "--chdir", mount, "--"]
        source = Path(__file__).resolve().parents[2] / "scripts" / "agent-reservation-broker.py"
        literal = "argument with ' spaces $ and\nnewline"
        code = """
import json,os,pathlib,resource,subprocess,sys
assert pathlib.Path('marker').exists() and pathlib.Path.cwd()==pathlib.Path('/broker-smoke-only')
assert sys.argv[1]==os.environ['BROKER_TEST']
assert sys.stdin.read()=='stdin from the sandbox'
assert 'NoNewPrivs:\\t1' in pathlib.Path('/proc/self/status').read_text()
assert os.getpriority(os.PRIO_PROCESS,0)==0
assert int(pathlib.Path('/proc/self/personality').read_text(),16)&0x40000
cgroup=pathlib.Path('/proc/self/cgroup').read_text().strip()[3:]
group=pathlib.Path('/sys/fs/cgroup'+cgroup)
assert (group/'memory.max').read_text().strip()=='268435456'
assert (group/'pids.max').read_text().strip()=='64'
os.nice(10)
subprocess.run(['/usr/local/bin/agent-reserved-run','/usr/bin/python3','-c',
 'import os; assert os.getpriority(os.PRIO_PROCESS,0)==0'],check=True)
print(json.dumps({'sandbox_entry':cgroup,'memory_max':268435456,'pids_max':64,'nested_lock':True}))
raise SystemExit(7)
"""
        scoped = ["/usr/bin/systemd-run", "--user", "--scope", "--quiet", "--collect",
                  "--property=MemoryMax=256M", "--property=TasksMax=64", "--"]
        result = call(scoped + sandbox + [WRAPPER, "/usr/bin/python3", "-c", code, literal],
                      expected=7, input="stdin from the sandbox", env=dict(os.environ, BROKER_TEST=literal))
        print(result.stdout.strip(), flush=True)

        # Exercise the launcher's real mounts and source overlays without
        # starting a model or touching an agent's credentials/configuration.
        launcher = source.with_name("agent-sandbox")
        harness = root / "codex-probe"
        harness_code = """
import json,os,pathlib
assert os.getpriority(os.PRIO_PROCESS,0)==0
cg=pathlib.Path('/proc/self/cgroup').read_text().strip()[3:]
assert '/workload-' in cg
assert pathlib.Path('/sys/fs/cgroup'+cg+'/memory.max').read_text().strip()=='1073741824'
print(json.dumps({'agent_sandbox_entry':cg,'source_memory_max':1073741824}))
"""
        harness.write_text("#!/usr/bin/python3\nimport os\nos.execv(" + repr(WRAPPER) + ", " +
                           repr([WRAPPER, "/usr/bin/python3", "-c", harness_code]) + ")\n")
        harness.chmod(0o755)
        launched = call([str(launcher), "codex", "--memory-limit-gb", "1", directory],
                        env=dict(os.environ, CODEX_BIN=str(harness)))
        print(launched.stdout.splitlines()[-1], flush=True)

        # Two independent mount namespaces contend on the same host lock.
        first_code = "import os,pathlib,time; pathlib.Path('ready').write_text(str(os.getppid())); time.sleep(30)"
        first = subprocess.Popen(sandbox + [WRAPPER, "/usr/bin/python3", "-c", first_code],
                                 text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        try:
            wait_for(root / "ready")
            call(sandbox + [WRAPPER, "/usr/bin/true"], expected=75)
            second = subprocess.Popen(sandbox + [WRAPPER, "--wait-lock", "5", "--", "/usr/bin/true"],
                                      text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            time.sleep(.2)
            assert second.poll() is None
            # Bubblewrap's outer monitor is separate from the wrapper. Signal
            # the wrapper supervisor, which forwards termination to its child.
            os.kill(int((root / "ready").read_text()), signal.SIGTERM)
            stdout, stderr = first.communicate(timeout=5)
            assert first.returncode == 143, (first.returncode, stdout, stderr)
            stdout, stderr = second.communicate(timeout=8)
            assert second.returncode == 0, (second.returncode, stdout, stderr)
        finally:
            if first.poll() is None:
                if (root / "ready").exists():
                    try:
                        os.kill(int((root / "ready").read_text()), signal.SIGTERM)
                    except ProcessLookupError:
                        pass
                first.kill()
                first.communicate()
            if 'second' in locals() and second.poll() is None:
                second.kill()
                second.communicate()
        print(json.dumps({"cross_sandbox_lock": True, "lock_wait": True, "signal_forwarding": True}), flush=True)

    scope = Path("/sys/fs/cgroup" + metadata["cgroup"][3:]).parent
    deadline = time.monotonic() + 5
    while list(scope.glob("workload-*")) and time.monotonic() < deadline:
        time.sleep(.1)
    assert not list(scope.glob("workload-*")), "completed workload cgroups were not reaped"
    assert Path("/run/agent-cpu-reserve/broker.sock").is_socket()
    print(json.dumps({"workload_cleanup": True, "host_session_running": True}), flush=True)


if __name__ == "__main__":
    main()
