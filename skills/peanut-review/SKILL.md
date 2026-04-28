---
name: peanut-review
description: Orchestrate a structured multi-agent code review using peanut-review CLI
user_invocable: true
---

# Peanut Review — Orchestrator Skill

You are the orchestrator for a structured multi-agent code review session.
You drive the review lifecycle using the `peanut-review` CLI tool.

## Prerequisites

- `peanut-review` CLI: either `pip install -e tools/peanut-review` or use
  `tools/peanut-review/bin/peanut-review` directly (zero-install, sets PYTHONPATH)
- Personas live in `skills/peanut-gallery-review/personas/`
- Agent prompt template: `skills/peanut-review/agent-prompt.md`

## Workflow

### Step 1 — Initialize the session

```bash
peanut-review init \
  --workspace <REPO_PATH> \
  --base <BASE_REF> \
  --agents '[
    {"name": "vera", "model": "claude-opus-4-7-thinking-high", "persona": "vera.md"},
    {"name": "felix", "model": "composer-2", "persona": "felix.md"},
    {"name": "petra", "model": "composer-2", "persona": "petra.md"}
  ]' \
  --bead
```

Model ids above are illustrative. Personas declare a `tier:` (expert or
standard); the orchestrator picks concrete ids at session-init time from the
live `cursor-agent --list-models` / `opencode models` output. See
[Agent selection guidelines](#agent-selection-guidelines) for the resolution
flow and tier guidance.

Each agent may specify `"runner"`:
- `"cursor"` (default): cursor-agent CLI; ids from `cursor-agent --list-models`.
- `"opencode"`: opencode CLI; ids look like `provider/model`. Discover
  available models with `opencode models` (e.g. `openai/gpt-5.5`,
  `llama.cpp/qwen3.5-27b`). For local llama.cpp models the user must boot
  llama-server out of band (e.g. `lcode qwen`); the runner does not.
- `"codex"`: codex CLI (`codex exec`); ids are bare names like `gpt-5.5`.

Example mixed-runner lineup:

```json
[
  {"name": "vera",  "model": "claude-opus-4-7-thinking-high", "persona": "vera.md"},
  {"name": "felix", "model": "openai/gpt-5.5",
   "persona": "felix.md", "runner": "opencode"},
  {"name": "cleo",  "model": "gpt-5.5",
   "persona": "vera.md",  "runner": "codex"}
]
```

This prints the session directory path. Set it for subsequent commands:
```bash
export PEANUT_SESSION=<printed-path>
```

### Step 2 — Build the project

Ensure the project compiles/builds before launching agents. Fix any build
errors first — agents should review working code.

### Step 3 — Launch agents

```bash
peanut-review launch
```

This spawns one cursor-agent per reviewer, each with their persona and a
rendered prompt containing the session path and diff commands.

### Step 4 — Monitor Round 1

Periodically check for agent questions:
```bash
peanut-review inbox
```

Reply to any questions:
```bash
peanut-review reply --agent <name> --id <qid> "your answer"
```

Wait for all agents to complete Round 1:
```bash
peanut-review wait-all round-done --timeout 900
```

### Step 5 — Triage findings

View all comments posted so far:
```bash
peanut-review comments
```

Remember the id of the last comment you reviewed — you'll use it as
`--since <id>` in later passes to see only what's new.

For each finding, evaluate it and either:
- Resolve the comment (`peanut-review resolve <c_id>`) to mark it applied,
  ideally after applying the fix in code
- Reply to it (`peanut-review add-comment --reply-to <c_id> --body "..."`)
  to record a rebuttal or note any partial fix

Commit any fixes you applied. Then update the session HEAD so prior
comments anchored to old line numbers get correctly marked stale:
```bash
peanut-review migrate
```

### Step 6 — Wake agents for the next pass

```bash
peanut-review signal-all next-round
```

This unblocks any agents waiting on `next-round`. There is no round
counter — each pass is just another batch of comments, and the orchestrator
tracks "what's new since last time" via `--since <comment-id>`.

### Step 7 — Monitor the next pass

Same as Step 4:
```bash
peanut-review inbox
peanut-review wait-all round-done --timeout 600
```

### Step 8 — Review the new comments

```bash
peanut-review comments --since <last-comment-id>
```

Apply any additional fixes if needed. For human-led reviews you may
repeat Steps 5–7 with another `signal-all next-round` as many times as
useful; there is no built-in limit on passes.

### Step 9 — Record verdict

```bash
peanut-review verdict --approve --update-bead --body "All critical issues addressed"
```

Or if changes still needed:
```bash
peanut-review verdict --request-changes --body "Outstanding critical issue in X"
```

### Step 10 — (Optional) Archive to git notes

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
  with author, severity, round, and stale/resolved badges.
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
uses `uv run` for zero-install — no venv or `pip install` needed (requires
`uv` on PATH). Agents call structured MCP tools (`add_comment`,
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

## CLI permissions for agents

Copy the template to your workspace:
```bash
cp skills/peanut-review/cli.sample.json <WORKSPACE>/.cursor/cli.json
```
(Run from the repo root, or adjust the path to `cli.sample.json` accordingly.)

The template includes `Shell(peanut-review **)` plus read-only filesystem and
git commands, test runners, and build tools.

**WARNING**: Do NOT add `Shell(**)` to the `deny` list. A deny entry of
`Shell(**)` overrides ALL Shell allows — agents won't be able to run any
shell commands including peanut-review. The launch command will warn if it
detects this configuration.
