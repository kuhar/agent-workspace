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
  --wait-lock 3600 --timeout 7200 \
  -- ./your-benchmark
```

`--reserve-cpus` names one thread per physical core in the larger reserved pool. Its SMT peers are excluded as well. `--cpus` controls the workload's affinity and must fit inside that pool. Both lists are checked against actual topology. The explicit numbers above apply to this workstation; use `--cores` or inspect topology on other hosts.

## A session for several commands

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

Normal exit, a failed command, Ctrl-C, termination, or the active-profile timeout stops the workload and restores the snapshot. `--timeout` defaults to 7,200 seconds and limits the active profile, separately from lock waiting and startup. The guard checks settings every second and aborts if clocks, CPU sets, topology, or scheduler identity drift.

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
