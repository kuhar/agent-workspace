"""Opt-in, workload-independent benchmark mode for agent-cpu-reserve.

The root controller never executes workload code as root. An immutable copy in
/run owns a transient systemd guard; ExecStopPost also restores after SIGKILL.
"""
from __future__ import annotations

import argparse
import datetime
import fcntl
import hashlib
import json
import os
import pathlib
import platform
import pwd
import shutil
import signal
import socket
import subprocess
import tempfile
import time

CPU = pathlib.Path("/sys/devices/system/cpu")
RUN = pathlib.Path("/run")
POWER_UNITS = ("power-profiles-daemon.service", "ananicy-cpp.service")
TIMERS = (
    "snapper-cleanup.timer", "plocate-updatedb.timer", "man-db.timer",
    "fstrim.timer", "logrotate.timer", "cachyos-rate-mirrors.timer",
    "apt-daily.timer", "apt-daily-upgrade.timer",
)
MAINTENANCE = tuple(u.replace(".timer", ".service") for u in TIMERS)
FREQUENCY_FIELDS = ("boost", "scaling_max_freq", "scaling_min_freq", "scaling_governor",
                    "energy_performance_preference")


def read(path):
    return pathlib.Path(path).read_text().strip()


def command(h, *argv, check=True):
    return subprocess.run(argv, check=check, text=True, stdout=subprocess.PIPE,
                          stderr=subprocess.PIPE, env=h.SAFE_ROOT_ENV)


def atomic(path, state):
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(state, indent=2) + "\n")
    temporary.chmod(0o600)
    temporary.replace(path)


def selection(args, h):
    groups = h.physical_core_groups()
    if args.cpus:
        try:
            selected = h.parse_cpu_list(args.cpus)
        except ValueError:
            h.fail("invalid CPU list")
        if not selected or not selected <= h.online_cpus():
            h.fail("selected CPUs must be nonempty and online")
        cores = [g for g in groups if selected & set(g[2])]
        if any(len(selected & set(g[2])) != 1 for g in cores):
            h.fail("--cpus must select exactly one SMT thread per physical core")
    else:
        if args.cores <= 0:
            h.fail("core count must be positive")
        cores, _ = h.select_cores(args.cores)
        selected = {min(g[2]) for g in cores}
    if args.reserve_cpus:
        try:
            pool = h.parse_cpu_list(args.reserve_cpus)
        except ValueError:
            h.fail("invalid reservation CPU list")
        if not pool or not pool <= h.online_cpus():
            h.fail("reservation CPUs must be nonempty and online")
        cores = [g for g in groups if pool & set(g[2])]
        if any(len(pool & set(g[2])) != 1 for g in cores):
            h.fail("--reserve-cpus must name one SMT thread per reserved physical core")
        if not selected <= {c for g in cores for c in g[2]}:
            h.fail("workload CPUs must belong to the reserved physical cores")
    reserved = {c for g in cores for c in g[2]}
    if not h.online_cpus() - reserved:
        h.fail("at least one physical core must remain for housekeeping")
    return selected, reserved


def policies(selected, reserved, args, h):
    targets = {}
    for cpu in sorted(reserved):
        policy = (CPU / f"cpu{cpu}" / "cpufreq").resolve()
        if not policy.is_dir():
            h.fail(f"CPU {cpu} has no CPUFreq policy")
        if str(policy) in targets:
            continue
        related = h.parse_cpu_list(read(policy / "related_cpus"))
        if not related <= reserved:
            h.fail(f"{policy} also controls housekeeping CPUs; cannot isolate clocks")
        if "performance" not in read(policy / "scaling_available_governors").split():
            h.fail(f"{policy} lacks the performance governor")
        nominal = CPU / f"cpu{cpu}" / "acpi_cppc" / "nominal_freq"
        if args.frequency_mhz is None and not nominal.exists():
            h.fail("nominal frequency unavailable; supply --frequency-mhz explicitly")
        target = args.frequency_mhz * 1000 if args.frequency_mhz else int(read(nominal)) * 1000
        if not int(read(policy / "cpuinfo_min_freq")) <= target <= int(read(policy / "cpuinfo_max_freq")):
            h.fail(f"frequency {target} kHz is outside {policy}'s supported range")
        desired = {"scaling_governor": "performance", "scaling_min_freq": str(target),
                   "scaling_max_freq": str(target)}
        if (policy / "energy_performance_preference").exists():
            if "performance" not in read(policy / "energy_performance_available_preferences").split():
                h.fail(f"{policy} lacks performance EPP")
            desired["energy_performance_preference"] = "performance"
        if (policy / "boost").exists():
            desired["boost"] = "0"
        targets[str(policy)] = desired
    return targets


def scheduler_name():
    root = pathlib.Path("/sys/kernel/sched_ext")
    names = [read(p) for p in root.glob("*/ops")]
    return ",".join(names)


def scheduler_state():
    path = pathlib.Path("/sys/kernel/sched_ext/state")
    return read(path) if path.exists() else "disabled"


def plan(args, h):
    selected, reserved = selection(args, h)
    desired = policies(selected, reserved, args, h)
    binary = None
    if args.scheduler == "bpfland":
        if not pathlib.Path("/sys/kernel/sched_ext/state").exists():
            h.fail("this kernel lacks sched_ext; use --scheduler native")
        binary = shutil.which("scx_bpfland", path=h.SAFE_ROOT_ENV["PATH"])
        if binary is None:
            h.fail("scx_bpfland is not installed; use --scheduler native")
    primaries = {next(iter(selected & set(g[2])), min(g[2]))
                 for g in h.physical_core_groups() if set(g[2]) & reserved}
    return {"cpus": h.format_cpu_list(selected), "reserved": h.format_cpu_list(reserved),
            "idle_siblings": h.format_cpu_list(reserved - primaries),
            "unused_primary_cpus": h.format_cpu_list(primaries - selected),
            "housekeeping": h.format_cpu_list(h.online_cpus() - reserved),
            "scheduler": args.scheduler, "scheduler_binary": binary,
            "frequency_khz": sorted({int(d["scaling_min_freq"]) for d in desired.values()}),
            "policies": desired, "timeout_seconds": args.timeout,
            "lock_wait_seconds": args.wait_lock,
            "aslr_disabled": not args.keep_aslr, "deep_idle_disabled": not args.keep_deep_idle,
            "session": args.session, "memory_max": args.memory_max}


def user_manager(h, uid, *argv):
    account = pwd.getpwuid(uid)
    return command(h, "/usr/sbin/runuser", "-u", account.pw_name, "--", "/usr/bin/env",
                   f"XDG_RUNTIME_DIR=/run/user/{uid}",
                   f"DBUS_SESSION_BUS_ADDRESS=unix:path=/run/user/{uid}/bus",
                   "/usr/bin/systemctl", "--user", *argv, check=False)


def snapshot(config, h):
    if h.read_state() is not None:
        h.fail("an existing reservation or stale reservation state must be resolved first")
    if h.systemctl_show("rj-benchmark-profile.service", "ActiveState") == "active":
        h.fail("stop the previous rj-benchmark-profile.service before using this profile")
    if scheduler_state() != "disabled":
        h.fail("an unrelated sched_ext scheduler is active; leave it unchanged")
    for unit in MAINTENANCE:
        if h.systemctl_show(unit, "ActiveState") == "active":
            h.fail(f"maintenance is already running: {unit}; wait for it to finish")
    state = {"config": config, "restored": False,
             "previous": h.snapshot_units(h.parse_cpu_list(config["reserved"])),
             "frequency": {}, "idle": {}, "services": []}
    if config["deep_idle_disabled"]:
        for cpu in h.parse_cpu_list(config["reserved"]):
            for idle in (CPU / f"cpu{cpu}" / "cpuidle").glob("state*"):
                if int(read(idle / "latency")) >= 10:
                    state["idle"][str(idle / "disable")] = read(idle / "disable")
    for unit in POWER_UNITS + TIMERS:
        if h.systemctl_show(unit, "ActiveState") == "active":
            state["services"].append({"unit": unit, "user": False})
    if user_manager(h, config["uid"], "is-active", "plasma-powerdevil.service").stdout.strip() == "active":
        state["services"].append({"unit": "plasma-powerdevil.service", "user": True})
    units = {entry["unit"] for entry in state["services"]}
    if "power-profiles-daemon.service" in units:
        binary = shutil.which("powerprofilesctl", path=h.SAFE_ROOT_ENV["PATH"])
        if binary is None:
            h.fail("powerprofilesctl is required to preserve the active power profile")
        active = command(h, binary, "get").stdout.strip()
        if not active:
            h.fail("could not read the active power profile")
        state["power_profile"] = {"command": binary, "active": active}
    frequency = {pathlib.Path(policy) for policy in config["policies"]}
    if units & {"power-profiles-daemon.service", "plasma-powerdevil.service"}:
        # Restarting a power service can also reset housekeeping CPUs.
        frequency.update(policy.resolve() for policy in (CPU / "cpufreq").glob("policy*"))
    for policy in sorted(frequency):
        state["frequency"][str(policy)] = {name: read(policy / name) for name in FREQUENCY_FIELDS
                                          if (policy / name).exists()}
    return state


def service(h, state, entry, action):
    def control(*argv):
        if entry["user"]:
            return user_manager(h, state["config"]["uid"], *argv)
        return command(h, h.SYSTEMCTL, *argv, check=False)
    result = control(action, entry["unit"])
    # Several short benchmark sessions can exhaust systemd's restart budget.
    # Reset only a start-limit hit on a service this profile had paused; retain
    # ordinary failures for diagnosis instead of retrying an unhealthy daemon.
    if result.returncode and action == "start":
        reason = control("show", entry["unit"], "-p", "Result", "--value").stdout.strip()
        if reason == "start-limit-hit":
            reset = control("reset-failed", entry["unit"])
            if reset.returncode == 0:
                result = control("start", entry["unit"])
    if result.returncode:
        raise RuntimeError(f"{action} {entry['unit']}: {result.stderr.strip()}")


def apply(state, h):
    # Stop the desktop consumer before its power-policy provider, so a D-Bus
    # request during shutdown cannot reactivate the provider behind our back.
    for entry in reversed(state["services"]):
        service(h, state, entry, "stop")
    h.restrict_units(state["previous"], h.parse_cpu_list(state["config"]["reserved"]))
    for path in state["idle"]:
        pathlib.Path(path).write_text("1")
    for policy, desired in state["config"]["policies"].items():
        root = pathlib.Path(policy)
        # Lower the minimum first so a new maximum cannot violate the old range.
        (root / "scaling_min_freq").write_text(read(root / "cpuinfo_min_freq"))
        for key in ("boost", "scaling_governor", "energy_performance_preference",
                    "scaling_max_freq", "scaling_min_freq"):
            if key in desired:
                (root / key).write_text(desired[key])


def verify(state, h):
    config = state["config"]
    if h.online_cpus() != h.parse_cpu_list(config["online"]):
        raise RuntimeError("online CPU topology changed")
    for policy, desired in config["policies"].items():
        for key, value in desired.items():
            if read(pathlib.Path(policy) / key) != value:
                raise RuntimeError(f"power policy drift: {policy}/{key}")
    for path in state["idle"]:
        if read(path) != "1":
            raise RuntimeError(f"idle policy drift: {path}")
    reserved = h.parse_cpu_list(config["reserved"])
    h.verify_restrictions(state["previous"], reserved)
    online = h.parse_cpu_list(config["online"])
    for unit, old in state["previous"].items():
        expected = (h.parse_cpu_list(old) if old else online) - reserved
        if h.parse_cpu_list(h.systemctl_show(unit, "AllowedCPUs")) != expected:
            raise RuntimeError(f"CPU policy drift: {unit}")
    if config["scheduler"] == "bpfland":
        if scheduler_state() != "enabled" or "bpfland" not in scheduler_name():
            raise RuntimeError("Bpfland detached or was replaced")
    elif scheduler_state() != "disabled":
        raise RuntimeError("an unexpected sched_ext scheduler attached")


def restore_power(state, h):
    # A desktop service can finish applying its policy after systemctl start
    # returns. Require a quiet interval before declaring restoration complete.
    deadline = time.monotonic() + 5
    stable_since = None
    while True:
        changed, errors = [], []
        profile = state.get("power_profile")
        if profile:
            try:
                if command(h, profile["command"], "get").stdout.strip() != profile["active"]:
                    command(h, profile["command"], "set", profile["active"])
                    changed.append("active power profile")
            except Exception as exc:
                errors.append(f"power profile: {exc}")
        for policy, previous in state["frequency"].items():
            root = pathlib.Path(policy)
            try:
                if all(read(root / key) == value for key, value in previous.items()):
                    continue
                changed.append(policy)
                (root / "scaling_min_freq").write_text(read(root / "cpuinfo_min_freq"))
                for key in FREQUENCY_FIELDS:
                    if key in previous:
                        (root / key).write_text(previous[key])
            except Exception as exc:
                errors.append(f"{policy}: {exc}")
        if errors:
            raise RuntimeError("; ".join(errors))
        now = time.monotonic()
        if changed:
            stable_since = None
        elif stable_since is None:
            stable_since = now
        elif now - stable_since >= 1:
            return
        if now >= deadline:
            raise RuntimeError("power settings did not settle after service restart")
        time.sleep(.1)


def restore(directory, h):
    state_path = directory / "state.json"
    if not state_path.exists():
        return
    state = json.loads(state_path.read_text())
    if state["restored"]:
        return
    if state["config"].get("session"):
        h.broker_module().SOCKET.unlink(missing_ok=True)
    # Ensure no workload remains while the original policy is restored.
    h.stop_unit(state["config"]["scope"])
    errors = []
    for path, value in state["idle"].items():
        try:
            pathlib.Path(path).write_text(value)
        except Exception as exc:
            errors.append(str(exc))
    if not h.restore_units(state["previous"]):
        errors.append("CPU-set restoration failed")
    # Start providers first, then desktop consumers.
    for entry in state["services"]:
        try:
            service(h, state, entry, "start")
        except Exception as exc:
            errors.append(str(exc))
    # Services may overwrite both the profile and raw CPU settings on startup.
    # Restore these last, including manually selected governor/EPP values.
    try:
        restore_power(state, h)
    except Exception as exc:
        errors.append(str(exc))
    state["restored"] = not errors
    state["restore_errors"] = errors
    atomic(state_path, state)
    if not errors:
        current = h.read_state()
        if current and current.get("benchmark_directory") == str(directory):
            h.clear_state()
    print("Benchmark restoration: " + ("complete" if not errors else "; ".join(errors)), flush=True)
    if errors:
        raise RuntimeError("restoration incomplete; saved state retained at " + str(state_path))


def checked_directory(value, h):
    directory = pathlib.Path(value)
    if directory.parent != RUN or not directory.name.startswith("agent-benchmark-"):
        h.fail("invalid benchmark runtime directory")
    info = directory.lstat()
    if not directory.is_dir() or directory.is_symlink() or info.st_uid != 0 or info.st_mode & 0o077:
        h.fail("runtime directory must be root-owned and private")
    return directory


def guard(directory, h):
    h.require_root()
    scheduler = None
    stopped = False
    lock = None
    broker = None
    def stop(_signum, _frame):
        nonlocal stopped
        stopped = True
    for signum in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP):
        signal.signal(signum, stop)
    try:
        config = json.loads((directory / "config.json").read_text())
        # The guard owns the existing host lock from before snapshot until after
        # restoration. Waiting contenders do not inspect or change active state.
        lock = h.LOCK_PATH.open("w")
        deadline = time.monotonic() + config["lock_wait_seconds"]
        while True:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if stopped or time.monotonic() >= deadline:
                    raise RuntimeError("another reserved harness is active; lock wait expired")
                time.sleep(.1)
        os.sched_setaffinity(0, h.parse_cpu_list(config["housekeeping"]))
        state = snapshot(config, h)
        atomic(directory / "state.json", state)
        atomic(h.STATE_PATH, {"unit": config["scope"], "owner_uid": config["uid"],
                             "previous": state["previous"], "benchmark_directory": str(directory),
                             "guard_unit": config["guard"]})
        apply(state, h)
        if config["scheduler"] == "bpfland":
            scheduler = subprocess.Popen([config["scheduler_binary"]], env=h.SAFE_ROOT_ENV)
            deadline = time.monotonic() + 15
            while scheduler_state() != "enabled" and time.monotonic() < deadline:
                if stopped or scheduler.poll() is not None:
                    raise RuntimeError("Bpfland startup interrupted or failed")
                time.sleep(.1)
        verify(state, h)
        if config["session"]:
            broker = h.broker_module().Broker(config, h)
        address = os.environ["NOTIFY_SOCKET"]
        if address.startswith("@"):
            address = "\0" + address[1:]
        with socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM) as sock:
            sock.connect(address)
            sock.sendall(b"READY=1")
        while not stopped:
            if scheduler is not None and scheduler.poll() is not None:
                raise RuntimeError("Bpfland exited")
            verify(state, h)
            if broker is None:
                time.sleep(1)
            else:
                broker.serve()
    finally:
        if broker is not None:
            broker.close()
        # Stop the workload before allowing scheduler fallback or clock changes.
        if "config" in locals():
            h.stop_unit(config["scope"])
        if scheduler is not None and scheduler.poll() is None:
            scheduler.terminate()
            try:
                scheduler.wait(timeout=10)
            except subprocess.TimeoutExpired:
                scheduler.kill()
                scheduler.wait()
        try:
            restore(directory, h)
        finally:
            if lock is not None:
                lock.close()


def run(args, h):
    h.require_root()
    h.ensure_workload_lock()
    try:
        uid, gid = int(os.environ["SUDO_UID"]), int(os.environ["SUDO_GID"])
    except (KeyError, ValueError):
        h.fail("run through sudo from your non-root account")
    if uid == 0 or h.process_uid(args.caller_pid) != uid or not h.is_ancestor(args.caller_pid, os.getpid()):
        h.fail("caller PID must be an ancestor belonging to the sudo user")
    if not args.command or not os.path.isabs(args.command[0]):
        h.fail("the command must have an absolute executable path")
    config = plan(args, h)
    # Refuse conflicting profiles before installing a guard or changing settings.
    config.update(uid=uid, gid=gid, online=h.format_cpu_list(h.online_cpus()))
    config.update(version=1, started_utc=datetime.datetime.now(datetime.timezone.utc).isoformat(),
                  kernel=platform.release(), hostname=socket.gethostname())
    if h.systemctl_show("rj-benchmark-profile.service", "ActiveState") == "active":
        h.fail("stop the previous rj-benchmark-profile.service before using this profile")
    directory = pathlib.Path(tempfile.mkdtemp(prefix="agent-benchmark-", dir=RUN))
    stamp = f"{os.getpid()}-{time.monotonic_ns()}"
    config.update(scope=f"agent-reserved-{stamp}.scope", guard=f"agent-benchmark-{stamp}.service")
    helper = directory / "agent-cpu-reserve"
    support = directory / "agent-benchmark-profile.py"
    broker = directory / "agent-reservation-broker.py"
    shutil.copyfile(h.__file__, helper)
    shutil.copyfile(__file__, support)
    shutil.copyfile(pathlib.Path(h.__file__).with_name(broker.name), broker)
    helper.chmod(0o700)
    support.chmod(0o600)
    broker.chmod(0o600)
    config["helper_sha256"] = hashlib.sha256(helper.read_bytes()).hexdigest()
    config["profile_sha256"] = hashlib.sha256(support.read_bytes()).hexdigest()
    config["broker_sha256"] = hashlib.sha256(broker.read_bytes()).hexdigest()
    config["wrapper_sha256"] = hashlib.sha256(pathlib.Path("/usr/local/bin/agent-reserved-run").read_bytes()).hexdigest()
    if config["scheduler_binary"]:
        config["scheduler_sha256"] = hashlib.sha256(pathlib.Path(config["scheduler_binary"]).read_bytes()).hexdigest()
    atomic(directory / "config.json", config)
    print("Stable benchmark profile: " + json.dumps({k: v for k, v in config.items() if k != "policies"}), flush=True)
    interrupted = 0
    def stop(signum, _frame):
        nonlocal interrupted
        if interrupted:
            return
        interrupted = signum
        h.stop_unit(config["scope"])
        h.stop_unit(config["guard"])
    old_handlers = {s: signal.signal(s, stop) for s in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP)}
    try:
        command(h, h.SYSTEMD_RUN, "--quiet", "--collect", "--service-type=notify",
                f"--unit={config['guard']}", "--property=NotifyAccess=main",
                f"--property=RuntimeMaxSec={args.timeout}s",
                f"--property=TimeoutStartSec={args.wait_lock + 120}s",
                "--property=TimeoutStopSec=120s", "--property=KillMode=mixed",
                "--property=WorkingDirectory=/",
                f"--property=ExecStopPost=/usr/bin/python3 -I {helper} benchmark-restore {directory}",
                "--", "/usr/bin/python3", "-I", str(helper), "benchmark-guard", str(directory))
        if interrupted:
            return 128 + interrupted
        workload = args.command
        if not args.keep_aslr:
            workload = ["/usr/bin/setarch", platform.machine(), "-R", *workload]
        if not args.session:
            workload = ["/usr/local/bin/agent-reserved-run", "--wait-lock", str(args.wait_lock), "--", *workload]
        shared_options = (["--property=Delegate=yes"] if args.session else [])
        child_options = (["--shared-session"] if args.session else [])
        result = subprocess.run([
            h.SYSTEMD_RUN, "--scope", "--quiet", "--collect", f"--unit={config['scope']}",
            "--slice=benchmark.slice", f"--property=AllowedCPUs={config['cpus']}",
            f"--property=MemoryMax={config['memory_max']}",
            "--property=MemorySwapMax=0", "--property=OOMPolicy=kill",
            "--property=KillMode=control-group", f"--property=BindsTo={config['guard']}",
            f"--property=After={config['guard']}", *shared_options, "--", "/usr/bin/python3", "-I", str(helper),
            "child", f"--uid={uid}", f"--gid={gid}", f"--caller-pid={args.caller_pid}",
            f"--working-directory={os.readlink('/proc/' + str(args.caller_pid) + '/cwd')}",
            f"--cpus={config['cpus']}", "--no-new-privileges", *child_options, "--", *workload,
        ], env=h.SAFE_ROOT_ENV, check=False)
        return 128 + interrupted if interrupted else result.returncode
    finally:
        h.stop_unit(config["scope"])
        h.stop_unit(config["guard"])
        for signum, handler in old_handlers.items():
            signal.signal(signum, handler)
        path = directory / "state.json"
        if path.exists() and not json.loads(path.read_text())["restored"]:
            restore(directory, h)
        # Retain root-owned state/config for diagnostics and independent audit.


def main(argv, h):
    if argv[0] in ("benchmark-guard", "benchmark-restore"):
        h.require_root()
        if len(argv) != 2:
            h.fail("internal benchmark action requires one runtime directory")
        directory = checked_directory(argv[1], h)
        if argv[0] == "benchmark-guard":
            guard(directory, h)
        else:
            state_path = directory / "state.json"
            if not state_path.exists() or json.loads(state_path.read_text())["restored"]:
                return 0
            lock = h.acquire_lock()
            try:
                restore(directory, h)
            finally:
                lock.close()
        return 0
    parser = argparse.ArgumentParser(description=__doc__)
    choose = parser.add_mutually_exclusive_group()
    choose.add_argument("--cores", type=int, default=24)
    choose.add_argument("--cpus")
    parser.add_argument("--reserve-cpus")
    parser.add_argument("--scheduler", choices=("bpfland", "native"), default="bpfland")
    parser.add_argument("--frequency-mhz", type=int)
    parser.add_argument("--timeout", type=int, default=93600)
    parser.add_argument("--wait-lock", type=int, default=0)
    parser.add_argument("--keep-aslr", action="store_true")
    parser.add_argument("--keep-deep-idle", action="store_true")
    parser.add_argument("--session", action="store_true")
    parser.add_argument("--memory-max", default="infinity")
    parser.add_argument("--caller-pid", type=int)
    parser.add_argument("command", nargs=argparse.REMAINDER)
    args = parser.parse_args(argv[1:])
    if not 1 <= args.timeout <= 93600:
        h.fail("timeout must be between 1 and 93600 seconds")
    if not 0 <= args.wait_lock <= 86400:
        h.fail("lock wait must be between 0 and 86400 seconds")
    if args.frequency_mhz is not None and args.frequency_mhz <= 0:
        h.fail("frequency must be positive")
    if args.command and args.command[0] == "--":
        args.command = args.command[1:]
    if argv[0] == "benchmark-plan":
        value = plan(args, h)
        print(json.dumps({k: v for k, v in value.items() if k != "policies"}, indent=2))
        return 0
    if args.caller_pid is None:
        h.fail("--caller-pid is required")
    try:
        return run(args, h)
    except subprocess.CalledProcessError as exc:
        print("Error: benchmark setup failed: " + (exc.stderr or str(exc)).strip(), file=h.sys.stderr)
        return 1
    except RuntimeError as exc:
        print("Error: " + str(exc), file=h.sys.stderr)
        return 1
