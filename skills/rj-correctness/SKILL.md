---
name: rj-correctness
description: Use for RocJITsu correctness failures, regression tests, simulator coverage, or AMDGPU instruction audits. Match the investigation to the reported failure or requested architecture scope.
---

# RocJITsu correctness

1. **Reproduce on the right revision.** For a new develop regression, record the SHA of fresh `origin/develop`. For a downstream report, start with the reported SDK, compiler, target and wave mode. Save the failing test name, input and shortest reproducer. For CI, run the failing configuration on both the PR and its base. For a flake, give failures per attempt on each.
2. **Find the first wrong decision.** Reduce the workload until competing causes give different results. Trace that difference across the compiler, runtime, decoder or execution boundary. For instruction behavior, use [ISA evidence](references/isa.md). If the PR fails while its base passes under matched conditions, investigate the PR diff. If the reported failure no longer reproduces, check the setup and look for a landed fix.
3. **Bound the fix.** Check sibling paths and targets that share the cause. Group confirmed instances and track unrelated findings separately. A triage request ends with the reproducer, attribution and open questions.
4. **Verify the repair.** Choose a [regression test](references/regressions.md) that fails on the baseline and passes on the candidate. Rerun the original workload. If several branches will run together, validate their final combined tips.

Report the revisions, cause, changed test outcomes and remaining uncertainty. Label simulator-only results and name any hardware actually tested. Distinguish a passing reduced probe from a passing downstream workload.
