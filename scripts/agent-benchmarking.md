# Local benchmarking with agent-reserved-run

`agent-reserved-run --profile stable` wraps any executable in a temporary benchmark environment. It requires normal sudo authorization to change host settings. The workload runs as your original account, and the previous settings are restored afterward. No permanent service or additional sudo permission is installed by this mode.

## One command

```bash
agent-reserved-run --profile stable --cores 24 -- ./your-benchmark --its-options
```

The helper chooses the last 24 physical cores from the machine's topology and runs on one SMT thread per core. It excludes both threads of each selected core from ordinary system, user, and container slices. CPU numbering is discovered rather than assumed.

To reproduce the CPU pool used in the CachyOS stability calibration, while running on 24 threads:

```bash
agent-reserved-run --profile stable \
  --reserve-cpus 16-63 --cpus 16-39 \
  --wait-lock 3600 --timeout 93600 \
  -- ./your-benchmark
```

`--reserve-cpus` names one thread per physical core in the larger reserved pool. Its SMT peers are excluded as well. `--cpus` controls the workload's affinity and must fit inside that pool. Both lists are checked against actual topology. The explicit numbers above apply to this workstation; use `--cores` or inspect topology on other hosts.

## A session for several commands

To start an agent with the stable profile, use:

```bash
agent-sandbox codex --daemon --reserve-cores 48 --benchmark
```

This requests sudo once on the host, starts Bpfland, fixes the reserved cores at
their nominal frequency, and disables ASLR for the session. It uses the same
restoration controller as `agent-reserved-run --profile stable`. The profile has
a 26-hour timeout; exiting the daemon also restores the previous host settings.
`--memory-limit-gb` remains effective. Ordinary `--reserve-cores` launches retain
their CPU-isolation-only behavior.

Inside the sandbox, run measurements through `agent-reserved-run COMMAND ...`.
This takes the shared workload lock and restores nice 0 without sudo, including
when the agent has lowered the priority of its shell commands. It requires the
inherited nice limit to permit normal priority; failure is reported before the
workload starts. Do builds and other CPU-intensive work outside measurement
intervals, and pin benchmark workers to physical cores away from the agent's
coordinator.

```bash
agent-reserved-run --profile stable \
  --reserve-cpus 16-63 --cpus 16-39 --wait-lock 3600 --session \
  -- bash

# In the resulting shell:
agent-reserved-run --wait-lock 3600 ./baseline
agent-reserved-run --wait-lock 3600 ./candidate
exit
```

The session holds the reservation and profile until the shell exits or the profile times out. Any remaining children are stopped during cleanup. Commands inside it inherit the profile; the original `agent-reserved-run COMMAND ...` form requires no further privilege operation and now takes a per-workload lock. Use one session for paired baseline/candidate runs so settings do not cycle between samples. Do builds and downloads before entering it. In shared sessions, run benchmarks through `agent-reserved-run`; direct commands bypass that per-workload lock.

Shell syntax must be passed explicitly to a shell:

```bash
agent-reserved-run --profile stable --cores 24 -- bash -lc './benchmark > result.txt'
```

The wrapper preserves command arguments, working directory, and the caller's environment. It does not set thread-count variables such as `OMP_NUM_THREADS`; the workload controls those. An activated virtual environment or caller-specified runtime variables are carried into the unprivileged workload.

## Multiple agents

For independently launched agents, start the reservation in a host terminal:

```bash
agent-reserved-run --profile stable --cores 24 --session -- bash
```

Leave that shell open. In each agent sandbox, run measurements with:

```bash
agent-reserved-run --wait-lock 3600 -- ./your-benchmark
```

Only starting the host session asks for sudo. The stable-profile guard exposes
an owner-only Unix socket at `/run/agent-cpu-reserve/broker.sock`. The client
takes the shared workload lock, and the guard admits the connecting process to
the reservation using kernel peer credentials and a peer pidfd (Linux 6.5 or
newer). The request contains no command, path, or PID. Commands execute as the
original user inside the existing sandbox, preserving environment, working
directory, terminal, and file descriptors. Joined workloads receive the same
CPU affinity, nice 0, NoNewPrivs, and ASLR policy as the host session.

Run `scripts/setup-agent-cpu-reserve.sh` from a host terminal after updating
the helpers, then restart existing sandboxes to pick up the socket mount and
client. Setup installs no sudoers rule. Its tmpfiles configuration creates the
socket directory and shared lock files at boot, so sandboxes launched before
a reservation can also connect. The directory mount survives session restarts;
the socket is closed and removed when the host session ends. Cleanup stops all
admitted workloads before restoring host settings, including after guard failure.

Each admitted workload has a child cgroup that retains the tightest source
`memory.max`, `memory.swap.max`, and `pids.max` ceilings. All admitted workloads
also share the host reservation's `--memory-max` budget and disabled swap. Memory
accounting moves to the reservation: a source agent's memory ceiling continues
to cap each workload, but no longer caps the combined memory of its coordinator
and admitted workload. Use `--memory-max` on the host session for an aggregate
benchmark budget. Existing memory charges remain with their original cgroup;
new workload allocations are accounted in the reservation.

The socket is available only for stable profiles started with `--session`.
CPU-isolation-only `agent-sandbox --reserve-cores` still supports commands
already inside its reservation. Entry from an unrelated process now requires
a shared stable host session instead of an automatic sudo invocation.

All sessions share `/run/lock/agent-cpu-reserve.lock` with the existing `agent-cpu-reserve run` / `agent-sandbox --reserve-cores` path. The controller acquires the lock before snapshotting or changing settings and holds it until restoration completes. Even reservations using disjoint CPUs are serialized because scheduler and service changes affect the host.

Commands also share a root-owned, readable execution lock at `/run/agent-reserved-workload.lock`. This serializes agents inside the same existing reservation, including commands joined from another terminal. Nested helper calls in the same benchmark process tree reuse their ancestor's lock, so they do not deadlock. An explicit `--session` holds only the profile lock; its agents take the workload lock for each benchmark. Without `--session`, the supplied command holds both locks for its lifetime. Locks are advisory: cooperating benchmarks must use the helper.

`agent-sandbox` binds the actual host lock file read-only into each sandbox. It preserves the same inode rather than copying the file, so separate mount namespaces still contend on the same kernel lock.

- Default: a competing reservation fails immediately.
- `--wait-lock 3600`: wait up to one hour, without starting the workload or changing settings while waiting.
- Lock acquisition uses the kernel's file locking, not an existence check or PID file. A dead owner releases the kernel lock; unfinished restoration remains recorded and prevents a new session from silently overwriting the original state.
- Waiting is bounded; it is not a FIFO job queue and does not promise fairness between competing agents.

The older `rj-benchmark-profile.service` does not share this lock. The new mode explicitly refuses to start while that old profile is active. If it is still active, stop it before using this interface:

```bash
sudo systemctl stop rj-benchmark-profile.service
```

## Applied settings

- Runtime systemd CPU sets reserve complete physical cores, with workload affinity on the selected threads and siblings left online but idle from ordinary tasks.
- Performance governor and, where supported, performance EPP.
- Minimum and maximum frequency requests both set to nominal frequency by default: 2.5 GHz on this machine. Per-policy boost is disabled where supported. `--frequency-mhz N` selects another supported target.
- Deep idle states with exit latency of at least 10 microseconds are disabled on reserved CPUs. `--keep-deep-idle` leaves them unchanged for comparisons.
- Bpfland by default; `--scheduler native` keeps the kernel scheduler and refuses an unrelated active sched_ext scheduler. This enables the planned native/Bpfland comparison at the same clock target.
- Power Profiles Daemon, Ananicy, Plasma PowerDevil, and relevant maintenance timers are paused if active. Already-running maintenance jobs cause a refusal instead of being terminated.
- Workloads run in `benchmark.slice`, at normal priority (nice 0), without swap, with no-new-privileges, and with address randomization disabled for those processes. `--keep-aslr` leaves address randomization enabled.

Frequency requests are verified, but hardware can still throttle; a sysfs target alone is not proof of realized frequency. Core reservation does not isolate shared memory bandwidth, package power, interrupts, or all kernel threads. The wrapper sets up the environment; it does not replace warmup, paired measurement, confidence intervals, or same-binary calibration in a benchmark harness.

## Preview and scheduler comparison

Planning requires no sudo and changes nothing:

```bash
agent-reserved-run --profile stable --cores 24 --plan
agent-reserved-run --profile stable --cpus 16-39 --reserve-cpus 16-63 --plan
```

The plan reports workload CPUs, reserved CPUs, idle siblings, unused primary CPUs, housekeeping CPUs, frequency targets, scheduler, and limits. `--plan` checks hardware and binary prerequisites, but does not acquire the host lock or guarantee availability at execution time.

```bash
agent-reserved-run --profile stable --scheduler native \
  --reserve-cpus 16-63 --cpus 16-39 -- ./your-benchmark

agent-reserved-run --profile stable --scheduler bpfland \
  --reserve-cpus 16-63 --cpus 16-39 -- ./your-benchmark
```

## Cleanup and recovery

Normal exit, a failed command, Ctrl-C, termination, or the active-profile timeout stops the workload and restores the snapshot. `--timeout` defaults to 93,600 seconds (26 hours) and limits the active profile, separately from lock waiting and startup. The guard checks settings every second and aborts if clocks, CPU sets, topology, or scheduler identity drift.

When a power service is paused, the snapshot includes CPU policies on housekeeping CPUs as well as reserved CPUs. If Power Profiles Daemon is active, `powerprofilesctl` must be available to save its selected profile. Cleanup restarts the services first, then restores the saved profile and exact governor, EPP, clock limits and boost values. It checks for one second of unchanged settings, allowing up to five seconds for delayed service writes to settle. Persistent changes or unreadable settings report incomplete restoration and retain recovery state; cleanup does not silently select a default power mode.

The profile uses a transient systemd guard, an immutable root-owned copy of the helper under `/run`, and an `ExecStopPost` restoration action. Killing the controller cannot skip that restoration action. If the launcher disappears, the guard's timeout still limits the session. Restoration errors retain recovery state and make the invocation fail.

Each invocation prints profile metadata, including the guard unit, CPU lists, target frequency, kernel, and helper/scheduler hashes. Root-owned configuration and original settings are retained under the printed `/run/agent-benchmark-*` directory until reboot. To inspect a specific invocation, use its guard unit with `journalctl`.

For a recorded session, emergency recovery is:

```bash
sudo /usr/local/libexec/agent-cpu-reserve reset
```

This stops a recorded stable-profile guard and retries restoration. For the original reservation mode, reset retains the existing behavior and requires that its active controller release the lock first.

## Installation and checks

From `$JAKUB_ENV_DIR/agent-workspace`, install without adding passwordless permissions:

```bash
bash scripts/setup-agent-cpu-reserve.sh --no-sudoers
python3 -m unittest discover -s tests -p test_benchmark_profile.py -v
```

The default installer still supports the original restricted permission for joining an existing reservation. It does not grant passwordless access to the new benchmark mode. The `--no-sudoers` option leaves any existing sudoers files unchanged.

`tests/manual/smoke_benchmark_profile.py` is an explicit real-host check, not a routine unit test: it changes settings through the installed helper. It currently targets this machine's UID and CPU topology and should run only while no other reservation is active. It checks native/Bpfland execution, environment and argument preservation, unprivileged identity, affinity, nested commands, exit codes, timeout, launcher termination, controller SIGKILL, competing agents, bounded waiting, and exact restoration.

Manual smoke logs and JSON results are written below `$XDG_STATE_HOME/agent-workspace/benchmark-smoke/` (default `~/.local/state/agent-workspace/benchmark-smoke/`). `AGENT_BENCHMARK_SMOKE_DIR` selects an explicit output directory. Generated output is not part of the source checkout.
