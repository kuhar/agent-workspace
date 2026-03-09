---
name: weekly-snippet
description: >-
  Generate a weekly activity snippet from GitHub. Use when the user asks for a
  weekly summary, weekly snippet, status update, or "what did I do this week".
---

# Weekly Snippet Generator

Generate a concise weekly activity summary grouped by project and task, using the `my-activity` script.

## Data Collection

1. Run the activity script to get structured JSON:
   ```bash
   /home/jakub/jakub-env/agent-workspace/scripts/my-activity --days 7 --json --detailed
   ```
2. If the user specifies a different time range, adjust `--days` accordingly.

## Output Format

Organize output by **project**, then by **task/theme** within each project. Order tasks within each project by impact (most impactful first).

### Structure

```
### <Project Name (e.g., IREE, LLVM/MLIR)>

* **<Task/theme summary>**
  - <Executive summary if the task has notable quantitative outcomes>
  - <One-line description focusing on motivation/outcome>: <url>
  - ...

### <Next Project>
...

---

**Code reviews:** <count> PRs reviewed across <N> repos (<repo list>)
```

### Rules

- **Group related PRs** under a shared task heading (e.g., multiple PRs fixing the same bug across dialects belong together).
- **Cross-project tasks** should live where they logically belong. For example, cherry-picking an LLVM fix into IREE stays under the LLVM heading.
- **One-line descriptions** should focus on the motivation or outcome, not just restate the PR title. Use past tense (e.g., "Auto-enabled abi3 for CPython 3.12+" not "Auto-enable abi3").
- **Code reviews** go at the bottom as a single aggregate line with total count and repo list. Do not list individual reviews under each project.
- **Issues** should be inlined under the relevant task if one exists, not in a separate section.
- **Direct commits** (fork branches without PRs) should be mentioned only if they represent work not already covered by a listed PR.
- **Skip projects** where nothing interesting happened beyond reviews.
- **Executive summaries** are warranted when a task has notable quantitative results (e.g., size reduction, performance improvement). Pull data from linked issues/PRs if needed. Keep to one line.
- **Open PRs** needing review should be marked with `(needs review)`.

### Example

```
### IREE

* **Reduce Python release size via Stable ABI (abi3) enablement** https://github.com/iree-org/iree/issues/23646
  - Combined with dropping py3.9, release size went from 1.74 GB to 1.16 GB (-33%, -573 MB), also giving us py3.14+ support for free.
  - Auto-enabled abi3 for CPython 3.12+ with GIL: https://github.com/iree-org/iree/pull/23629
  - Fixed release validation installing wrong package versions: https://github.com/iree-org/iree/pull/23634
  - Stopped building redundant cp313 wheels on MacOS and Windows: https://github.com/iree-org/iree/pull/23640

* **Add `iree_codegen.constraints` op for autotuner**
  - Introduced infrastructure to express tuning constraints in codegen: https://github.com/iree-org/iree/pull/23687 (needs review)

### LLVM/MLIR

* **Fixed the notorious double-space bug in ODS-generated op printers**
  - Root cause fix in the ODS printer generator: https://github.com/llvm/llvm-project/pull/184253
  - Cherry-picked fix into IREE and updated downstream tests: https://github.com/iree-org/iree/pull/23690

---

**Code reviews:** 21 PRs reviewed across 5 repos (llvm/llvm-project, iree-org/iree, llvm/mlir-www, nod-ai/amd-shark-ai)
```
