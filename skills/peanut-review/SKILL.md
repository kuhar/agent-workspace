---
name: peanut-review
description: Orchestrate structured multi-agent code review for local changes or GitHub PRs using the peanut-review CLI, and curate existing peanut-review sessions. Use when starting or managing review sessions, or when asked to deduplicate, shorten, validate, dismiss, filter, or decide whether agent review comments are worth pushing.
---

# Peanut Review

You are the orchestrator for a structured multi-agent review. Drive the review
lifecycle with `tools/peanut-review/bin/peanut-review`; it sets `PYTHONPATH`
for the local checkout, so no install step is needed.

## Subcommands

Codex skills do not have a separate subcommand registry. Treat the first word
after `/peanut-review` as a routing hint when present:

- `/peanut-review curate <session-or-pr-context>`: clean up an existing review
  session's comments. This is not a new reviewer pass.
- `/peanut-review pr <PR URL>`: run the GitHub PR review lifecycle below.
- `/peanut-review local <base-ref>`: run the author-owned local review
  lifecycle below.
- `/peanut-review status <session-path>`: inspect or recover a session without
  changing comments unless asked.

For `curate`, start from the live session state and produce a push-ready,
author-facing comment set:

- Resolve the live session path and source checkout. If the current directory
  is a wrapper, read `.peanut-review.json` first and keep `reviewRoot`,
  `workspaceRoot`, and `repoRelative` separate.
- Inspect `comments --format json` before editing. Also inspect
  `comments --include-deleted --format json` when duplicate cleanup, prior
  deletions, or mistaken cleanup might matter.
- Bucket local agent comments into keep/rewrite, merge, delete, or undelete.
  Leave imported GitHub comments alone unless the discussion is actually
  resolved or the user asks you to manage it.
- Validate likely survivors against exact files, generated artifacts, or the
  smallest useful repro/test. Spend verification time on comments that might
  survive, not on likely deletes.
- Rewrite kept comments as concise PR feedback. Start with the requested change
  or scoped question, include only compact evidence, and align severity with
  confidence. Avoid internal triage wording such as "confirmed" or "partly
  confirmed".
- Delete duplicate, incorrect, stale, nitpicky, speculative, praise-only,
  overly broad, or low-ROI comments. When merging duplicates, edit the kept
  comment first so it absorbs any useful detail, then delete the redundant
  copy.
- For GitHub-backed sessions, finish with `gh-push --dry-run`. Treat it as
  authoritative for what will surface and whether anchors are pushable. If an
  anchor is out of range, recreate the finding as a global comment preserving
  the original `file:line`, then delete the stale anchored copy.

Do not launch or rerun reviewers, patch source, or push to GitHub during
`curate` unless the user explicitly asks.

## Operator Checklist

Track these items explicitly. If your harness has a todo list, create this list
before running commands and keep it current.

- [ ] Choose one lifecycle: GitHub PR review or local author-owned review.
- [ ] Record the session path, source workspace, any separate build/tool root,
      base/topic refs or PR number, and configured reviewers.
- [ ] Ask for external facts/preferences that are not discoverable: review
      root, web UI root, repo layout, build/test command, session reuse/archive
      choice, persona lineup, runner, and model choices.
- [ ] Confirm the checkout is built/testable and reviewer-visible tools are
      reachable before launching reviewers.
- [ ] Confirm project config and reviewer permissions are valid.
- [ ] Launch reviewers and verify startup with `status`, `inbox`, logs, and
      `wait-all`.
- [ ] Answer reviewer questions promptly.
- [ ] Track the last reviewed comment id for later `--since` queries.
- [ ] Triage every finding: keep, delete, resolve, reply, or push to GitHub.
- [ ] Finish with the right artifact: GitHub review comments/verdict for PRs,
      or a local verdict/archive for author-owned reviews.

Mode-specific checklist:

- [ ] GitHub PR: prefer `start --no-launch`, build/test, then `launch`, unless
      the user says the checkout is already built.
- [ ] GitHub PR: after all reviewers signal `round-done`, run `kill-agents`
      before curating feedback.
- [ ] GitHub PR: curate feedback; do not fix code, resolve imported GitHub
      threads, or force rebuttal loops unless the user asks.
- [ ] Local review: own the patch; apply fixes, `migrate`, run rebuttal passes,
      and record a final verdict.

## Ask Before Guessing

Use project config and discoverable local facts first. If a choice affects where
state is written, which checkout is reviewed, or which paid/local model runner
is used, and the answer is not already in the repo or user request, ask a
concise question instead of inventing it. Do not ask for facts you can cheaply
read from config files or CLI discovery.

If the lifecycle is unclear, ask: "Is this a local author-owned review, or a
GitHub PR review?"

## Working Variables

Set these descriptive names in notes or shell snippets:

```bash
PR_BIN=tools/peanut-review/bin/peanut-review
SESSION=<session-path>
WORKSPACE=<source-workspace>
REVIEW_ROOT=<configured-review-root>
LAST_COMMENT_ID=<last-reviewed-comment-id>
```

Examples assume `PR_BIN` and the intended `SESSION` path are set.
Name new sessions `<repo>-<change-title>`, using the PR head branch or local
branch/topic slug for `change-title`; avoid bare PR-number session names.

## Config And Permissions

When `.peanut-review.json` exists, use it as-is. It belongs in the worktree
parent and defines `reviewRoot`, `workspaceRoot`, `repoRelative`,
`reviewAgentTimeoutSeconds`, and the exact `agents` lineup. Point the web UI at
the same `reviewRoot`. If no config exists, ask before choosing persistent
roots, repo layout, reviewers, runners, or models.

Do not blur roots. `reviewRoot` is session storage/web UI state;
`workspaceRoot` + `repoRelative` identify the checkout under review. If build
outputs or project tools live outside the source checkout, make sure the actual
runner workspace and permissions let agents reach them before launch.

When root/layout changed or is ambiguous, dry-run before spending reviewer
runs:

```bash
"$PR_BIN" start <pr> --config <config> --dry-run --no-launch
"$PR_BIN" start <pr> --config <config> --no-launch
SESSION=<printed-session-path>
"$PR_BIN" --session "$SESSION" launch --dry-run
```

Cursor agents need `.cursor/cli.json` in the actual runner workspace shown by
`launch --dry-run`.

```bash
mkdir -p "$WORKSPACE/.cursor"
cp tools/peanut-review/peanut_review/templates/cli.sample.json "$WORKSPACE/.cursor/cli.json"
```

The launch command validates config and Cursor permissions. Keep
`Shell(peanut-review **)` allowed, and keep `Shell(**)` out of the deny list
because it overrides all Shell allows.

When build tools live outside the runner workspace, Cursor permissions must
also allow the paths or commands reviewers are expected to use.

## GitHub PR Review

Use this for PR numbers, PR URLs, or external author changes. Import GitHub
context, run reviewers, curate findings, then push review comments or an
approve/request-changes decision back to GitHub.

1. Start without launching unless the checkout is already built. The command
   imports existing GitHub context and prints the session path.

   ```bash
   "$PR_BIN" start <pr-number-or-url> --no-launch
   SESSION=<printed-session-path>
   ```

2. Build/test the checkout with the project workflow. If reviewers need
   non-obvious tool paths, record them in a session note before launch.

3. Launch reviewers, wait for the first pass, then stop idle reviewer
   processes:

   ```bash
   "$PR_BIN" --session "$SESSION" launch
   "$PR_BIN" --session "$SESSION" wait-all round-done --timeout 900
   "$PR_BIN" --session "$SESSION" kill-agents
   ```

4. Curate findings. Delete duplicate/noisy local comments with `delete <c_id>`.
   Add replies only when they clarify a finding for the PR author. Do not
   resolve imported GitHub comments unless the GitHub discussion was actually
   resolved or the user asks you to manage it.

   ```bash
   "$PR_BIN" --session "$SESSION" gh-pull
   "$PR_BIN" --session "$SESSION" comments
   "$PR_BIN" --session "$SESSION" comments --since "$LAST_COMMENT_ID"
   ```

5. Add one top-level verdict comment when there is an overall conclusion:

   ```bash
   "$PR_BIN" --session "$SESSION" add-global-comment --category request-changes --body "Blocking issue: ..."
   "$PR_BIN" --session "$SESSION" add-global-comment --category approve --body "LGTM"
   ```

   Use `--category comment` or omit `--category` for non-verdict feedback. For
   self-owned PRs, GitHub may reject approve/request-changes events; use a
   normal global comment in that case.

6. Preview, then push:

   ```bash
   "$PR_BIN" --session "$SESSION" gh-push --dry-run
   "$PR_BIN" --session "$SESSION" gh-push
   ```

After author updates, refresh the checkout with the project PR-update flow,
then run `gh-pull` and `migrate`. Launch a fresh reviewer pass only for
substantial updates or a human request.

```bash
"$PR_BIN" --session "$SESSION" gh-pull
"$PR_BIN" --session "$SESSION" migrate
```

## Local Author-Owned Review

Use this when the orchestrator can modify the patch under review.

1. Create and launch the session. If project config exists, reuse its `agents`
   lineup.

   ```bash
   "$PR_BIN" --session "$SESSION" init \
     --workspace "$WORKSPACE" \
     --base <base-ref> \
     --topic HEAD \
     --agents '<agents-json-or-file>'
   "$PR_BIN" --session "$SESSION" launch
   ```

2. Run the shared monitoring commands.

3. Triage every finding. Apply real fixes in code and resolve fixed comments;
   reply with a concrete rebuttal for findings that are intentionally not fixed.

   ```bash
   "$PR_BIN" --session "$SESSION" resolve <c_id>
   "$PR_BIN" --session "$SESSION" add-comment --reply-to <c_id> --body "..."
   ```

4. Commit fixes, then update comment anchors:

   ```bash
   "$PR_BIN" --session "$SESSION" migrate
   ```

5. Run a rebuttal pass:

   ```bash
   "$PR_BIN" --session "$SESSION" signal-all next-round
   "$PR_BIN" --session "$SESSION" wait-all round-done --timeout 600
   "$PR_BIN" --session "$SESSION" comments --since "$LAST_COMMENT_ID"
   ```

   Repeat only while useful. There is no round counter; track new work with
   `--since <comment-id>`.

6. Record the final verdict; archive if useful:

   ```bash
   "$PR_BIN" --session "$SESSION" verdict --approve --body "All critical issues addressed"
   "$PR_BIN" --session "$SESSION" verdict --request-changes --body "Outstanding critical issue in X"
   "$PR_BIN" --session "$SESSION" archive
   ```

## Shared Review Mechanics

After any launch, monitor, answer questions, rerun failed reviewers, and stop
processes through the CLI:

```bash
"$PR_BIN" --session "$SESSION" status
"$PR_BIN" --session "$SESSION" inbox
"$PR_BIN" --session "$SESSION" wait-all round-done --timeout 900
"$PR_BIN" --session "$SESSION" comments
"$PR_BIN" --session "$SESSION" reply --agent <name> --id <qid> "answer"
"$PR_BIN" --session "$SESSION" launch --agent Irene
"$PR_BIN" --session "$SESSION" rerun --agent Irene
"$PR_BIN" --session "$SESSION" kill-agents
"$PR_BIN" --session "$SESSION" kill-agents --agent Irene
```

Use `status` for a compact view, but treat signal files, comments, inbox, logs,
and live processes as the real health checks. `process=...` is supervisor-owned
runtime state; `review=done` means the agent posted `round-done`.

After all reviewers signal `round-done`, agents may still be live waiting for a
possible next round. For GitHub PR reviews, run `kill-agents` immediately. For
local reviews, keep them alive only when you are about to send `next-round`.

## Reviewer Selection

Use configured reviewers as-is during a review. When authoring config, ask
before changing persistent roots, runners, personas, or models. If asked to
choose a lineup, include Vera, one domain expert suited to the patch, and two
or three breadth reviewers such as Felix, Petra, or Soren. Map `tier: expert`
personas to the strongest available model and
`tier: standard` to a balanced/fast model. Discover models with
`cursor-agent --list-models` or `opencode models`; common Codex ids are
`gpt-5.5`, `gpt-5.4`, and `gpt-5.3-codex`.

Use display-case agent names in config, e.g. `Vera`, while keeping persona
filenames lowercase, e.g. `vera.md`. The web UI shows the configured agent
name.

## Web UI

The web UI reads the same session storage as the CLI. Its `--root` should match
the configured `reviewRoot`; without `--root`, it uses `$PEANUT_SESSION`'s
parent if set, otherwise `/tmp/peanut-review`.

If the user says the review server is already up, discover its root from the
running `peanut_review serve --root ...` process and use that for session
storage instead of starting a new server or guessing a different root.

```bash
"$PR_BIN" serve --root "$REVIEW_ROOT" --port 27183 --base-url /pr
"$PR_BIN" stop --root "$REVIEW_ROOT"
```

## Runners

- **cursor**: `cursor-agent --print` through Shell/CLI, not MCP; isolated
  runtime home under `<session>/runtime/cursor/`.
- **opencode**: `opencode run`; model ids are `provider/model`, including
  `openai/*`, `opencode/*`, or local `llama.cpp/*`.
- **codex**: `codex exec`; requires `codex login` and gets
  `--add-dir <session>` so it can write session files.

Agents submit findings, replies, questions, notes, and completion signals with
peanut-review CLI commands from the rendered prompt. Notes are for test reports
or non-review activity and are not pushed to GitHub.

## Failure Handling

- If an agent times out, inspect `status` and `<session>/log/`.
- If an agent exits without `round-done`, treat it as failed or incomplete and
  use `rerun --agent <name>` after confirming no live reviewer remains.
- If a session was launched under bad assumptions, prefer archiving it and
  starting fresh over reusing stale signals/comments.
- If the orchestrator crashes, run `status`, then resume from the latest
  comments, questions, and signal state.
