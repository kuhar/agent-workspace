---
name: peanut-review
description: Orchestrate structured multi-agent code review for local changes or GitHub PRs using peanut-review CLI
user_invocable: true
---

# Peanut Review — Orchestrator Skill

You are the orchestrator for a structured multi-agent code review session.
You drive the review lifecycle using the `peanut-review` CLI tool.

## Prerequisites

- Use the checked-out CLI directly: `tools/peanut-review/bin/peanut-review`.
  It sets `PYTHONPATH` for the local checkout, so no setup step is needed.
- For package-local development and tests, run commands from
  `tools/peanut-review` with `uv run ...`.
- Personas live in `skills/peanut-gallery-review/personas/`
- Agent prompt template: `skills/peanut-review/agent-prompt.md`

## Agent Permissions

Cursor-based agents need a `.cursor/cli.json` in the workspace before launch.
Copy the template to the workspace root:

```bash
cp skills/peanut-review/cli.sample.json <WORKSPACE>/.cursor/cli.json
```

Run from the repo root, or adjust the path to `cli.sample.json` accordingly.
The template includes `Shell(peanut-review **)` plus read-only filesystem and
git commands, test runners, and build tools.

Keep `Shell(**)` out of the deny list. A deny entry of `Shell(**)` overrides
all Shell allows, so agents cannot run peanut-review or project inspection
commands. The launch command warns if it detects this configuration.

## Choose Review Mode

Peanut Review supports two different lifecycles. Choose the mode first and do
not mix the lifecycles unless the user explicitly asks for it.

Use **GitHub PR review** when the input is a PR number, PR URL, or external
author changes. The goal is to import existing GitHub context, run agent
reviewers, curate the findings, and push review comments or an
approve/request-changes decision back to GitHub. Do not default to applying
fixes, resolving comments, or running the rebuttal loop for someone else's PR.

Use **local agent-authored review** when reviewing code in the current
workspace that you or another agent authored locally. The orchestrator owns the
patch, so the round 1 → triage/fix/rebuttal → final verdict flow is appropriate.

If the user's intent is unclear, ask one short question: "Is this a local
author-owned review, or a GitHub PR review?"

## GitHub PR Reviews

### Step 1 — Use the project config

The happy path is config-driven. Do not choose the review root, workspace path,
or agent lineup by hand when a `.peanut-review.json` exists. Worktree setup
scripts should copy or generate this file in the worktree parent, next to
`.cursor` and `.claude`:

```json
{
  "reviewRoot": "/home/jakub/reviews",
  "workspaceRoot": ".",
  "repoRelative": "iree",
  "reviewAgentTimeoutSeconds": 900,
  "agents": [
    {"name": "vera", "model": "gpt-5.5-high", "persona": "vera.md", "runner": "cursor"},
    {"name": "irene", "model": "gpt-5.5-high", "persona": "irene.md", "runner": "cursor"},
    {"name": "petra", "model": "composer-2", "persona": "petra.md", "runner": "cursor"},
    {"name": "soren", "model": "composer-2", "persona": "soren.md", "runner": "cursor"}
  ]
}
```

Config semantics:

- `reviewRoot`: persistent peanut-review session root. The web server should
  scan this same root.
- `workspaceRoot`: project worktree root. Relative paths are resolved relative
  to the config file, so `"."` means the directory containing the config.
- `repoRelative`: source repository path under `workspaceRoot`.
- `reviewAgentTimeoutSeconds`: per-agent wall-clock timeout for reviewer runs.
- `agents`: exact reviewer lineup. Use it as-is; do not re-select agents.

Start a PR review from the worktree parent:

```bash
peanut-review start <pr-number-or-url>
```

`start` searches upward for `.peanut-review.json`, resolves a bare PR number
with `gh` from the configured checkout, creates the session at
`<reviewRoot>/<owner>-<repo>-pr-<number>`, initializes GitHub metadata, pulls
existing GitHub PR comments and review summaries into the session, and launches
the configured agents. Imported GitHub review threads carry their resolved
status into the local session.

If the project still needs a build before agents run, initialize without
launching, build, then launch:

```bash
peanut-review start <pr-number-or-url> --no-launch
# build/test project as needed
peanut-review --session <printed-session-path> launch
```

After launch, set `PEANUT_SESSION=<printed-session-path>` or pass
`--session <printed-session-path>` on subsequent commands. Always verify that
agents actually started:

```bash
peanut-review --session <printed-session-path> status
```

If status shows agents as done immediately or there are no comments, inspect
`<session>/log/*.log` before assuming the review is running.

### Step 2 — Build if needed

Ensure the project compiles/builds before agents review it. If the checkout was
prepared by a project `setup-review --build` or `--test` command, this is
already handled. Otherwise use `peanut-review start --no-launch`, fix build
errors, then launch.

### Step 3 — Launch agents if deferred

Skip this step if `peanut-review start` already launched agents. If you used
`--no-launch`, run:

```bash
peanut-review --session <printed-session-path> launch
```

This spawns one agent per configured reviewer, each with their persona and a
rendered prompt containing the session path and diff commands.

### Step 4 — Monitor the agent review

Periodically check for agent questions:
```bash
peanut-review inbox
```

Reply to any questions:
```bash
peanut-review reply --agent <name> --id <qid> "your answer"
```

Wait for all agents to complete the first review pass:
```bash
peanut-review wait-all round-done --timeout 900
```

Do not signal `next-round` just to force a rebuttal pass. For GitHub PRs, a
second pass is for author updates, a substantial new push, or an explicit human
request for another agent review.

### Step 5 — Curate findings

Fetch any GitHub comments that arrived while agents were running, then view all
comments posted so far:
```bash
peanut-review gh-pull
peanut-review comments
```

Global comments can carry GitHub-style review categories. Use `comment` for
ordinary high-level feedback, `approve` for approvals, and `request-changes`
for blocking reviews. Approval/blocking categories are only valid on top-level
global comments.

For GitHub PRs, curate rather than fix by default:

- Remove duplicate/noisy local comments before pushing if needed with
  `peanut-review delete <c_id>`.
- Add replies only when they clarify a finding for the PR author.
- Do not resolve imported GitHub comments unless the GitHub discussion was
  actually resolved or the user asks you to manage it.
- Remember the id of the last comment you reviewed. Use `--since <id>` in later
  passes to see only new activity.

When the review has an overall conclusion, add one top-level global comment
with the GitHub review category:

```bash
peanut-review add-global-comment --category request-changes --body "Blocking issue: ..."
peanut-review add-global-comment --category approve --body "LGTM"
```

Use `--category comment` or omit `--category` for non-verdict high-level
feedback.

### Step 6 — Push to GitHub

Preview before mutating GitHub:

```bash
peanut-review gh-push --dry-run
```

If the plan is correct, push local anchored comments, global comments, and any
global approval/blocking category to GitHub:

```bash
peanut-review gh-push
```

For self-owned PRs, GitHub may reject approve/request-changes events. In that
case use a normal global comment instead of an approval/blocking category.

### Step 7 — Re-review after author updates

When the PR author pushes new commits, update the local checkout using the
project's normal PR-refresh flow, then refresh the peanut-review session:

```bash
peanut-review gh-pull
peanut-review migrate
```

If another agent pass is useful, launch the configured agents again and review
only the new comments:

```bash
peanut-review launch
peanut-review wait-all round-done --timeout 900
peanut-review comments --since <last-comment-id>
```

## Local Agent-Authored Reviews

Use this mode when reviewing local changes that the orchestrator can modify
directly. This is where the triage/rebuttal/final-verdict loop belongs.

### Step 1 — Initialize and launch

Create a local session against the workspace and diff under review. If a
project `.peanut-review.json` exists, reuse its `agents` lineup rather than
choosing reviewers by hand.

```bash
peanut-review --session <session-path> init \
  --workspace <repo-path> \
  --base <base-ref> \
  --topic HEAD \
  --agents '<agents-json-or-file>'
peanut-review --session <session-path> launch
```

Then set `PEANUT_SESSION=<session-path>` or pass `--session <session-path>` on
subsequent commands.

### Step 2 — Monitor Round 1

```bash
peanut-review inbox
peanut-review wait-all round-done --timeout 900
peanut-review comments
```

Reply to any agent questions:

```bash
peanut-review reply --agent <name> --id <qid> "your answer"
```

### Step 3 — Triage and fix

For each finding, evaluate it and either:

- Apply the fix in code, then resolve the comment with
  `peanut-review resolve <c_id>`.
- If the finding is intentionally not fixed, reply with the specific rebuttal:
  `peanut-review add-comment --reply-to <c_id> --body "..."`.

Commit any fixes you applied. Then update the session HEAD so prior comments
anchored to old line numbers get correctly marked stale:

```bash
peanut-review migrate
```

### Step 4 — Run the rebuttal pass

Wake agents for the next pass:

```bash
peanut-review signal-all next-round
```

This unblocks agents waiting on `next-round`. There is no round counter; each
pass is another batch of comments, and the orchestrator tracks "what's new
since last time" via `--since <comment-id>`.

Monitor the next pass:

```bash
peanut-review inbox
peanut-review wait-all round-done --timeout 600
```

Review new comments:

```bash
peanut-review comments --since <last-comment-id>
```

Apply any additional fixes if needed. For human-led reviews you may
repeat this step with another `signal-all next-round` as many times as
useful; there is no built-in limit on passes.

### Step 5 — Record final verdict

```bash
peanut-review verdict --approve --body "All critical issues addressed"
```

Or if changes still needed:
```bash
peanut-review verdict --request-changes --body "Outstanding critical issue in X"
```

### Step 6 — Optional archive to git notes

```bash
peanut-review archive
```

## Human review UI

A browser-based review UI is built into peanut-review and shares the exact
same session storage — no separate tool, no duplicate CLI. Humans post
comments the same way agents do; the web UI is a shell over the existing
`add-comment` / `resolve` paths. One server on one port serves every session
found under its review root.

```bash
# Multi-session: scan /tmp/peanut-review/ (default) — all sessions are listed
peanut-review serve --port 16200
# → http://127.0.0.1:16200/            (session picker)
# → http://127.0.0.1:16200/sessions/<session-id>/

# Or explicitly point at one or more review roots
peanut-review serve --port 16200 --root /tmp/peanut-review --root /path/to/more

# Stop (uses same root inference as serve)
peanut-review stop
peanut-review stop --root /tmp/peanut-review
```

For config-driven project reviews, the server must scan the same `reviewRoot`
as `.peanut-review.json`, for example:

```bash
peanut-review serve --root /home/jakub/reviews --port 27183 --base-url /pr
```

Root inference: if `--root` is omitted, `$PEANUT_SESSION`'s parent is used
(so the existing single-session workflow keeps working); otherwise the
default `/tmp/peanut-review/`. The pidfile lives at `<root>/web.pid`.

The server:
- Renders a session picker at `/` listing every discovered session
  (id, state, base…topic, workspace, comment counts, created_at), sorted
  newest-first. Reloads live every 15s; new sessions created while the
  server is up are auto-discovered on rescan.
- Renders each session's unified diff with pygments syntax highlighting
  under `/sessions/<id>/`.
- Shows existing comments (agent + human) anchored to source-file lines,
  with author, severity, category, round, and stale/resolved badges.
- Lets humans post new comments by clicking a line number, or high-level
  comments via the "High-level feedback" section at the top of each session.
- Auto-detects workspace HEAD shifts (e.g. `git commit --amend`) and runs
  `migrate` — stale comments get dimmed in the UI. No more git-notes
  coupling to commit SHAs.
- Exposes `/api/sessions` (list) and `/sessions/<id>/api/{session,comments,resolve}`
  (per-session JSON).

Standalone human-only review (no agents):

```bash
peanut-review --session /tmp/peanut-review/my-review init \
  --workspace /repo --base main --topic HEAD
peanut-review serve --port 16200
# browse to http://127.0.0.1:16200/ and click into the session
```

## Agent selection guidelines

Use this section only when authoring or updating a project
`.peanut-review.json`. During an actual review launch, use the configured
`agents` list as-is.

### Picking the lineup

- **Always include Vera** — she is the most thorough and valuable reviewer
- Pick 1 expert persona (Vera, Irene, or Merlin) based on the domain
- Pick 2-3 standard personas (Felix, Petra, Soren) for breadth
- For compiler/MLIR code: include Irene or Merlin

### Picking a model per agent (dynamic)

Personas declare a `tier:` in their frontmatter (`expert` or `standard`)
rather than naming specific models. The tier describes the persona's role,
not a specific model class — a local-only review (no cloud models) is still
valid; the orchestrator just maps tier to "best vs. lighter" within whatever
is locally available.

The orchestrator resolves tier → concrete model id at session-init time,
based on what's installed locally. This avoids the persona files going stale
every time a new model lands.

Workflow when building the `--agents` JSON:

1. Read each chosen persona's frontmatter → grab its `tier`.
2. Discover what's available on each runner the user has set up:
   - **cursor**: `cursor-agent --list-models`
   - **opencode**: `opencode models`
   - **codex**: no list command. Common ids: `gpt-5.5`, `gpt-5.4`,
     `gpt-5.3-codex`. Check `~/.codex/config.toml` for the user's pinned
     default if unsure.
3. Pick a concrete id per agent following the tier guidance below. The
   launcher scripts (`cursor-agent-task.sh`, `opencode-agent-task.sh`,
   `codex-agent-task.sh`) all forward `--model` verbatim to the underlying
   CLI, so whatever id the upstream tool accepts will work.

### Tier guidance

- **expert**: pick the strongest reasoning model available within whatever
  the user has set up. With cloud access, prefer thinking/high-reasoning
  variants (e.g. cursor's `claude-opus-*-thinking-high` or `gpt-5.5-high`,
  opencode's `openai/gpt-5.5`, codex's `gpt-5.5`). For a local-only setup,
  pick the largest local model available (e.g. `llama.cpp/qwen3.5-27b`) —
  the persona's role (deep technical analysis) is still doable, just at
  whatever ceiling the local hardware supports.
- **standard**: pick a balanced/fast model. Cheap-and-cheerful is fine here —
  these reviewers cover breadth (style, scope, naming, future-proofing) and
  benefit from being able to scan a lot of code quickly. Examples: cursor's
  `composer-2` or `claude-4.6-sonnet-medium`, opencode's `openai/gpt-5.4-mini`
  or a smaller local `llama.cpp/*` model, codex's `gpt-5.4`.

If the user has explicitly opted into a local model (e.g. their `opencode
models` listing shows a `llama.cpp/*` entry), prefer it — they wouldn't
have it set up if they didn't want it exercised. For an all-local lineup,
use the same local model across all agents if only one is available; the
diversity of personas alone still produces useful review breadth.

### Model id formats per runner

- **cursor**: bare cursor ids from `cursor-agent --list-models`, e.g.
  `claude-opus-4-7-thinking-high`, `composer-2`, `gpt-5.5-high`.
- **opencode**: `provider/model`, e.g. `openai/gpt-5.5`,
  `llama.cpp/qwen3.5-27b`, `opencode/big-pickle`.
- **codex**: bare names, e.g. `gpt-5.5`, `gpt-5.4`.

## Handling failures

- If an agent times out, `wait-all` will report which agents didn't signal.
  Check `peanut-review status` and agent logs in `<session>/log/`.
- If an agent crashes mid-review, its partial comments are preserved (atomic
  JSONL appends). Proceed with available feedback.
- If the orchestrator crashes, run `peanut-review status` in a new session
  to discover the current state and resume from where you left off.

## Runners: cursor, opencode, codex

- **cursor** (default): launches `cursor-agent --print` via `cursor-agent-task.sh`.
  Requires cursor-agent to be logged in. Prefers MCP transport when the
  `peanut-review-mcp` script is installed, falls back to CLI.
- **opencode**: launches `opencode run` directly via `opencode-agent-task.sh`.
  `opencode models` is the source of truth for what's available — cloud
  providers like `openai/*`, the free `opencode/*` tier, or local
  `llama.cpp/*` providers configured in `~/.config/opencode/opencode.json`.
  For local llama.cpp models, ensure llama-server is running before invoking
  (boot it out of band, e.g. `lcode qwen` — peanut-review does not wrap lcode).
  Currently CLI mode only; MCP integration via `opencode.json` is not wired up.
- **codex**: launches `codex exec` via `codex-agent-task.sh`. Requires
  `codex login` (ChatGPT OAuth or API key). The launcher passes
  `--add-dir <session_dir>` so the agent can write peanut-review session files
  outside the workspace sandbox.

## Agent communication: MCP vs CLI

Agents can interact with peanut-review in two ways:

### MCP mode (preferred)

`peanut-review launch` automatically configures an MCP server in
`.cursor/mcp.json` and uses the `agent-prompt-mcp.md` template. The MCP server
uses `uv run` against the checked-out package (requires `uv` on PATH). Agents
call structured MCP tools (`add_comment`,
`add_global_comment`, `signal`, `wait`, etc.) instead of Shell commands.

Benefits:
- Agents call typed functions — no risk of printing commands instead of executing
- No `Shell(peanut-review **)` permission needed
- Better error messages (returned as tool results, not stderr)
- Works reliably with Gemini and other models that struggle with Shell tool use

### CLI mode (fallback)

If the `peanut-review-mcp` script is not found, agents use Shell commands via
the `agent-prompt.md` template. This requires `Shell(peanut-review **)` in
`.cursor/cli.json`.
