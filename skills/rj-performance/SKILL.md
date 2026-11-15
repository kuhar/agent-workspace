---
name: rj-performance
description: Use to interpret rocjitsu timings, diagnose simulator slowdowns, benchmark candidates, or decide whether an optimization should land. Interpret supplied results for analysis questions. Run comparisons for measurement requests.
---

# rocjitsu performance

**Identify the decision.** For a results question, explain what the data supports and which measurement would settle the uncertainty. For a measurement request, reproduce the reported case on its original revisions or compare the candidate with its parent or requested baseline. Record both SHAs, the metric and the deciding workload. For simulator timings, name the host CPU and emulated target. A claim about GPU throughput needs measurements on the named hardware. Respect the requested candidate set and stop condition.

1. **Match the runs.** Choose workloads that exercise the changed path. Short kernels expose dispatch overhead. Tensile, Gluon and IREE exercise different instruction mixes and memory behavior. Pin the workload artifact and SDK/runtime revisions. Match compiler, CPU target, build flags, compilation-cache state, input, warmup and thread count. Vary only the component under study. Check the output and confirm the timed region did the intended work.
2. **Control the host.** Build outside the reservation. Run qualified measurements through `agent-reserved-run` and verify CPU affinity inside the run. For comparison with a saved ML baseline, match its host profile (cores, clocks, scheduler and ASLR). See `$JAKUB_ENV_DIR/agent-workspace/scripts/agent-benchmarking.md` for the stable profile. For ML smokes, use the suite's shared exclusive lock. Reuse the driver's lock if it already holds it. Without a reservation, reproduce and profile the losing case. For best-effort timings, interleave both revisions, record host load and label the result unreserved.
3. **Quantify the effect.** Alternate baseline-first and candidate-first pairs. Keep raw samples, units, pair count, median and spread per workload. Repeat close calls or report them as inconclusive. Collect profiles or counters in separate runs and map the difference to code. Report application time separately from cycle or instruction savings.
4. **Explain conflicting results.** Extract small benchmarks from the instruction and input patterns behind **both the gain and the regression**. Change one thing at a time. Rerun the finalist on the original workloads. A repeatable Tensile loss can outweigh a decode microbenchmark win.

For a landing decision, recommend **land**, **hold for evidence**, or **drop** per candidate. Name the deciding workload, effect, uncertainty and any unresolved regression. If the case for landing rests on unreserved timings or an unknown host profile, report the provisional effect, recommend **hold for evidence**, and name the reserved comparison that would settle it. Label partial sweeps.
