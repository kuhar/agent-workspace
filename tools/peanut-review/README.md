# peanut-review

Structured code review for humans and agents, with a CLI, JSONL session state,
and a browser UI.

## TL;DR

Run from this checkout:

```bash
PEANUT_REVIEW_DIR=$PWD
PR_BIN=$PEANUT_REVIEW_DIR/bin/peanut-review
"$PR_BIN" --help
```

Use `peanut-review` instead of `$PR_BIN` if it is installed on `PATH`.
Most flows run from the repo being reviewed, so keep `PR_BIN` absolute.

Start the web UI:

```bash
bin/peanut_review_serve.sh
```

Defaults: `root=$HOME/reviews`, `url=http://127.0.0.1:27183/pr`.

## Copy/Paste Prompts

First-time project setup:

```text
Help me set up peanut-review for this repo. Inspect the checkout/worktree
layout, ask before choosing reviewRoot, workspaceRoot, repoRelative, reviewers,
runners, models, or build/test commands, then generate .peanut-review.json and
run dry-run validation. Do not launch reviewers yet.
```

GitHub PR orchestration:

```text
Set up peanut-review for <PR URL> under <review-root>. Use the existing
.peanut-review.json, confirm the exact reviewer lineup and runner/model choices
before launching, and start with --no-launch so I can build/test the checkout.
After reviewers signal round-done, wait-all should run the curator; help me
inspect the curated feedback for the web UI push flow.
```

Local author-owned review:

```text
Run a local peanut-review session for the current branch under <review-root>.
Use base <base-ref>, topic HEAD, and the configured reviewers. Launch reviewers,
triage every finding, apply fixes, migrate comment anchors, and run one rebuttal
round if useful.
```

Refresh an existing GitHub-backed session:

```text
Refresh peanut-review session <SESSION> after the PR changed. Prefer re-running
gh pr co <PR> over manual git operations when updating the local checkout; use
--force only if we intentionally want to discard local divergence. Then run
gh-pull and migrate, and show new/unresolved comments. Do not launch agents
unless the update is substantial or I explicitly ask.
```

After `.peanut-review.json` exists, future PR/session setup is a good task to
delegate to a subagent. Keep the first setup interactive so roots, permissions,
reviewers, and model choices are intentional.

## Project Config

GitHub PR flow usually starts from `.peanut-review.json` in the worktree parent
or repo:

```json
{
  "reviewRoot": "$HOME/reviews",
  "workspaceRoot": "$HOME/src",
  "repoRelative": "my-repo",
  "reviewAgentTimeoutSeconds": 1200,
  "agents": [
    {"name": "Vera", "model": "gpt-5.5", "reasoningEffort": "high", "persona": "vera.md", "runner": "codex"},
    {"name": "Irene", "model": "claude-opus-4-7-thinking-medium", "persona": "irene.md", "runner": "cursor"},
    {"name": "Curator", "model": "gpt-5.5-high", "runner": "cursor", "role": "curator"}
  ]
}
```

Supported runners: `cursor`, `opencode`, `codex`.
GitHub PR sessions require a configured agent with `"role": "curator"` because
the curator model is intentionally owned by project config, not by Python
defaults.

Cursor runners need permissions in the reviewed workspace before `launch`:

```bash
mkdir -p "$WORKSPACE/.cursor"
cp "$PEANUT_REVIEW_DIR/peanut_review/templates/cli.sample.json" \
  "$WORKSPACE/.cursor/cli.json"
```

## Flow: GitHub PR

Use this for changes that came from GitHub.

```bash
PR=https://github.com/owner/repo/pull/123

# Run inside the target checkout. Config discovery walks upward from cwd.
# Pass --config if .peanut-review.json is somewhere else.

# Prefer GitHub CLI checkout/update over manual fetch/refspec plumbing. Re-running
# this fast-forwards an existing local PR branch in place.
gh pr co "$PR"

# If the local PR branch diverged and you intentionally want to discard local
# commits, reset it to the latest PR head:
# gh pr co "$PR" --force

"$PR_BIN" start "$PR" --no-launch
SESSION=<printed-session-path>

# Build/test the checkout with the repo's normal commands.

"$PR_BIN" --session "$SESSION" launch --dry-run
"$PR_BIN" --session "$SESSION" launch
"$PR_BIN" --session "$SESSION" wait-all round-done --timeout 900
```

For GitHub-backed sessions, `wait-all round-done` waits for reviewers, then
launches the dedicated `Curator` agent and waits for it to finish. Inspect the
curated result in the web UI, then use the UI's GitHub push modal when ready:

```bash
"$PR_BIN" --session "$SESSION" gh-pull
"$PR_BIN" --session "$SESSION" comments --unresolved
"$PR_BIN" --session "$SESSION" edit c_1234abcd --body-file /tmp/comment.md
"$PR_BIN" --session "$SESSION" delete c_9999ffff
```

The web UI also exposes manual **curate** and **rerun all** controls in the
Agents section when you need to rerun comment curation or start a fresh
reviewer pass.

## Flow: Local Branch

Use this when you own the patch and want reviewers to find issues before you
ship it.

```bash
REVIEW_ROOT=$HOME/reviews
SESSION=$REVIEW_ROOT/my-repo-my-branch
WORKSPACE=$PWD
AGENTS='[
  {"name":"Vera","model":"gpt-5.5","persona":"vera.md","runner":"codex"},
  {"name":"Irene","model":"claude-opus-4-7-thinking-medium","persona":"irene.md","runner":"cursor"},
  {"name":"Curator","model":"gpt-5.5-high","runner":"cursor","role":"curator"}
]'

"$PR_BIN" --session "$SESSION" init \
  --workspace "$WORKSPACE" \
  --base origin/main \
  --topic HEAD \
  --agents "$AGENTS"

"$PR_BIN" --session "$SESSION" launch --dry-run
"$PR_BIN" --session "$SESSION" launch
"$PR_BIN" --session "$SESSION" wait-all round-done --timeout 900
"$PR_BIN" --session "$SESSION" comments --unresolved
```

Local sessions do not run the curator automatically. If you want comment
cleanup before fixing the patch, use the web UI's **curate** button or:

```bash
"$PR_BIN" --session "$SESSION" curate
```

After fixing code:

```bash
"$PR_BIN" --session "$SESSION" comments --unresolved
COMMENT_ID=c_1234abcd

"$PR_BIN" --session "$SESSION" resolve "$COMMENT_ID"
"$PR_BIN" --session "$SESSION" migrate
"$PR_BIN" --session "$SESSION" rerun --agent Vera --agent Irene
"$PR_BIN" --session "$SESSION" wait-all round-done --timeout 900
"$PR_BIN" --session "$SESSION" verdict --approve --body "All critical issues addressed"
```

## Flow: PR Updated

Use this after the author pushes a new revision.

```bash
# First update the checkout. Re-running this fast-forwards the existing local
# PR branch in place. Use --force only to intentionally discard local divergence.
gh pr co "$PR"

"$PR_BIN" --session "$SESSION" gh-pull
"$PR_BIN" --session "$SESSION" migrate
"$PR_BIN" --session "$SESSION" comments --since "$LAST_COMMENT_ID"
```

Rerun agents only for substantial changes. Use `rerun`, not `launch`, so stale
round signals are cleared before the selected reviewers start:

```bash
"$PR_BIN" --session "$SESSION" rerun --agent Vera --agent Irene
"$PR_BIN" --session "$SESSION" wait-all round-done --timeout 900
```

## Common Commands

```bash
"$PR_BIN" --session "$SESSION" status
"$PR_BIN" --session "$SESSION" inbox
"$PR_BIN" --session "$SESSION" reply --agent Vera --id q_1234abcd "Use ninja check-foo."
"$PR_BIN" --session "$SESSION" note --message "Ran targeted tests; passed."
"$PR_BIN" --session "$SESSION" add-comment --file path/to/file.py --line 42 --severity warning --body-file /tmp/body.md
"$PR_BIN" --session "$SESSION" comments --unresolved   # use this to find c_... ids
"$PR_BIN" --session "$SESSION" curate                 # launch the comment curator
"$PR_BIN" --session "$SESSION" add-global-comment --severity suggestion --body "A few comments."
"$PR_BIN" --session "$SESSION" add-global-comment --category request-changes --body-file /tmp/blocking.md
```
