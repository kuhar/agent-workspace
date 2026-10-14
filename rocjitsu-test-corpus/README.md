# Corpus reviews

`review-pr.sh` reviews PRs in `ROCm/rocjitsu-test-corpus`, using
`$ROCJITSUS/corpus` (falls back to `~/rocjitsu/corpus`) as the persistent workspace (`CORPUS_HOME` overrides it).

The workspace contains:

- `rocjitsu-test-corpus/`: the durable clone on `develop`, including Git storage
  for review snapshots after disposable worktrees are removed.
- `.peanut-review.json`: the configured reviewers and shared review-session root.
- `.cursor/cli.json`, `AGENTS.md`, `.envrc`: reviewer permissions and environment guidance.
- `venv/`: the standalone Python and TheRock environment.
- `worktrees/`: disposable wrappers, each with its own checkout and virtualenv.

Copy the existing RocJITsu peanut config into the workspace and set
`repoRelative` to `rocjitsu-test-corpus`. Keep `workspaceRoot` as `.` and the
existing `reviewRoot`. Create the canonical clone and install its requirements
with `uv`, using PyPI plus `https://rocm.nightlies.amd.com/whl-multi-arch/` and
`rocm[libraries,devel]`. The adapter uses this nightly SDK rather than the old
version pinned in the corpus's `requirements-rocm.txt`.

## Standalone review

```sh
"${ROCJITSUS:-$HOME/rocjitsu}/corpus/review-pr.sh" 43
# Prepare, check, and create a session without starting agents:
"${ROCJITSUS:-$HOME/rocjitsu}/corpus/review-pr.sh" --no-launch 43
```

The adapter creates `worktrees/manual-pr-43`, fetches an exact PR snapshot,
installs the PR's Python requirements and TheRock, runs Python unit checks, and
collects gfx1250 corpus tests. The repository has several independent build
projects, so this does not build or execute the complete GPU corpus. Reviewers
can select relevant compiler, hardware, or simulator checks for the patch.

It creates/reuses the peanut session in the configured review root, retains
Git objects, launches the configured reviewers, and waits for curation. Up to
half the reviewers may fail; unfinished reviewers and curator failures still
fail the review. Phase markers include completed reviewer counts.

`PR_BIN` can override the peanut CLI. The default is the sibling
`tools/peanut-review/bin/peanut-review` in this checkout.

## Queue

Add to `~/.config/review-queue/config.toml` and restart the TUI when idle:

```toml
[[projects]]
name = "corpus"
repo = "ROCm/rocjitsu-test-corpus"
query = "draft:false -author:app/dependabot -author:@me"
launcher = "$JAKUB_ENV_DIR/agent-workspace/rocjitsu-test-corpus/review-pr.sh"
wrapper_root = "$ROCJITSUS/corpus/worktrees"
estimated_wrapper_gib = 32
priority = 0
```

The queue supplies protocol-v1 ownership metadata for `prepare`, `run`, and
`cleanup`. Cleanup requires a matching ownership marker, an available wrapper
lock, a clean worktree from the canonical clone, no unknown top-level entries,
and no live reviewers. Failed reviews retain their session and logs outside
the disposable wrapper. Keep the canonical clone and the configured review root.
