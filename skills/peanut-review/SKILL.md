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
    {"name": "vera", "model": "opus-4.6-thinking", "persona": "vera.md"},
    {"name": "felix", "model": "sonnet-4.6", "persona": "felix.md"},
    {"name": "petra", "model": "sonnet-4.6", "persona": "petra.md"}
  ]' \
  --bead
```

Each agent may specify `"runner": "cursor"` (default) or `"runner": "opencode"`.
Opencode agents route through the `lcode` wrapper; they accept optional
`"lcode_primary"` and `"lcode_subagent"` fields (default: `"qwen"`, `"null"`).
Example mixed-runner lineup:

```json
[
  {"name": "vera",  "model": "opus-4.6-thinking",           "persona": "vera.md"},
  {"name": "felix", "model": "llama-primary/qwen3.6-35b-a3b",
   "persona": "felix.md", "runner": "opencode"},
  {"name": "petra", "model": "llama-primary/qwen3.6-35b-a3b",
   "persona": "petra.md", "runner": "opencode"}
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
peanut-review wait-all round1-done --timeout 900
```

### Step 5 — Review and triage findings

View all Round 1 comments:
```bash
peanut-review comments --round 1
```

For critical/warning findings:
1. Evaluate each comment
2. Apply fixes for valid findings
3. Commit the fixes

### Step 6 — Record triage decisions

```bash
peanut-review triage \
  --applied '[{"comment_id": "c_xxx", "description": "Added null check"}]' \
  --dismissed '[{"comment_id": "c_yyy", "rebuttal": "Already covered by test X"}]' \
  --commit <FIX_COMMIT_SHA>
```

### Step 7 — Migrate HEAD (if fixes were committed)

If you committed fixes during triage, update the session HEAD so comments
are correctly marked stale:
```bash
peanut-review migrate
```

### Step 8 — Signal agents for Round 2

```bash
peanut-review signal-all triage-done
```

### Step 8 — Monitor Round 2

Same as Step 4:
```bash
peanut-review inbox
peanut-review wait-all round2-done --timeout 600
```

### Step 9 — Review Round 2 comments

```bash
peanut-review comments --round 2
```

Apply any additional fixes if needed.

### Step 10 — Record verdict

```bash
peanut-review verdict --approve --update-bead --body "All critical issues addressed"
```

Or if changes still needed:
```bash
peanut-review verdict --request-changes --body "Outstanding critical issue in X"
```

### Step 11 — (Optional) Archive to git notes

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
- Lets humans post new comments by clicking a line number.
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

- **Always include Vera** — she is the most thorough and valuable reviewer
- Pick 1 expert (Vera, Irene, or Merlin) based on the domain
- Pick 2-3 generics (Felix, Petra, Soren) for breadth
- For compiler/MLIR code: include Irene or Merlin
- Expert personas use stronger models (opus-4.6-thinking)
- Generic personas use faster models (sonnet-4.6, gemini-3.1-pro)

## Handling failures

- If an agent times out, `wait-all` will report which agents didn't signal.
  Check `peanut-review status` and agent logs in `<session>/log/`.
- If an agent crashes mid-review, its partial comments are preserved (atomic
  JSONL appends). Proceed with available feedback.
- If the orchestrator crashes, run `peanut-review status` in a new session
  to discover the current state and resume from where you left off.

## Runners: cursor vs opencode

- **cursor** (default): launches `cursor-agent --print` via `cursor-agent-task.sh`.
  Requires cursor-agent to be logged in. Prefers MCP transport when the
  `peanut-review-mcp` script is installed, falls back to CLI.
- **opencode**: launches `opencode run` via `opencode-agent-task.sh`, which
  forwards through the `lcode` wrapper so local `llama-server` instances boot
  automatically. Currently CLI mode only (MCP integration via `opencode.json`
  is not wired up yet). The first opencode agent spawns the llama-servers; the
  rest reuse the already-running processes via lcode's idempotent health check.
  Runs as the `reviewer` agent (defined by `lcode`'s generated `opencode.json`):
  primary model, no `grep`/`glob`/`codesearch` denies, no subagent delegation.

Only one `lcode` primary/subagent pair can be running at a time, so multiple
opencode agents on the same session should share the same `lcode_primary` /
`lcode_subagent` values (persona diversity still provides review breadth).

## Agent communication: MCP vs CLI

Agents can interact with peanut-review in two ways:

### MCP mode (preferred)

`peanut-review launch` automatically configures an MCP server in
`.cursor/mcp.json` and uses the `agent-prompt-mcp.md` template. The MCP server
uses `uv run` for zero-install — no venv or `pip install` needed (requires
`uv` on PATH). Agents call structured MCP tools (`add_comment`, `signal`,
`wait`, etc.) instead of Shell commands.

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
