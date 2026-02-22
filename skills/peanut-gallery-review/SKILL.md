---
name: peanut-gallery-review
description: Multi-model code review with two rounds — initial review, triage/fix, then rebuttal review. Orchestrates cursor agents to review your active changes from multiple perspectives. Use when the user says "let the peanut gallery review", "peanut gallery review", "cross-review", "multi-review", "multi-model review", or asks for a review from multiple agents/models.
---

# Peanut Gallery Code Review

Two-round code review using multiple AI models (GPT, Claude/Sonnet, Gemini Pro,
Gemini Flash) as Cursor agents. Round 1 collects reviews, you triage and fix,
then Round 2 validates your rebuttals.

This skill reuses `cursor-agent-multi.sh` from the `ask-the-peanut-gallery`
skill directory. Locate that sibling skill directory to find the script.
For details on script usage, flags, and prerequisites, see
[ask-the-peanut-gallery/SKILL.md](../ask-the-peanut-gallery/SKILL.md).

## Prerequisites

The target workspace must have a `.cursor/cli.json` file that controls what the
Cursor agents are allowed to do. If it is missing, tell the user and show them
the path to `cli.sample.json` (in the `ask-the-peanut-gallery` skill directory)
so they can copy and customize it:

```bash
mkdir -p <workspace>/.cursor
cp /path/to/cli.sample.json <workspace>/.cursor/cli.json
```

The sample allows read-only access and git commands, with all writes denied.
The agents don't need write permissions — their stdout is captured into output
files by the script.

## Steps

### Step 1 — Determine what to review

Figure out what the "active change" is — staged changes, uncommitted work,
or recent commits on a feature branch. Use your judgment; check multiple repos
if it's a multi-repo setup.

**Before proceeding, tell the user exactly what you are reviewing** — show the
diff stat and confirm. Record the exact git commands (repo paths, SHAs, diff
arguments) needed to reproduce the diff — you will pass these to the review
agents in Steps 2 and 4 so they can inspect the changes themselves.

### Step 2 — Round 1: Initial review

Run `cursor-agent-multi.sh` with `--task review-round1`:

```bash
/path/to/cursor-agent-multi.sh \
  --workspace <WORKSPACE> \
  --task review-round1 \
  "<PROMPT>"
```

The prompt MUST include:
- The exact git commands to obtain the diff (repo paths, SHAs, diff arguments
  from Step 1) so the agents can run them themselves
- Clear instruction: "Review this diff. For each issue found, state: the file
  and line(s), the problem, the severity (critical / suggestion / nit), and a
  concrete fix. Also note anything done well."

Do NOT paste the full diff into the prompt — the agents have read-only git
access and can run the commands themselves.

After the script finishes, **read every `output.md`** file it produced.

### Step 3 — Triage and fix

Read all Round 1 reviews and process them:

1. **Group** suggestions by theme (e.g. error handling, naming, performance,
   correctness, style).
2. **For each suggestion**, decide: apply or disregard.
   - **Apply**: Edit the file(s) directly to make the fix. Track what you changed.
   - **Disregard**: Write a clear, specific rebuttal explaining why (e.g.
     "false positive — the null check exists on line 42", "intentional design
     choice because …", "out of scope for this change").
3. **Present a summary to the user** before continuing:

   ## Round 1 Triage

   ### Changes Applied
   - Bullet list of fixes made, with file:line references

   ### Suggestions Disregarded
   - Bullet list with the suggestion and your rebuttal

   Wait for the user to review. If they disagree with any decision, adjust
   before proceeding to Round 2.

4. **Ask the user whether to commit the fixes.** Committing creates a clean
   history that Round 2 agents can diff against: base -> original change ->
   review fixes. This lets them see exactly what was changed in response to
   Round 1 feedback vs the original code. If the user declines, proceed
   anyway — Round 2 will just work off the unstaged changes.

### Step 4 — Round 2: Rebuttal review

Run `cursor-agent-multi.sh` again with `--task review-round2`:

```bash
/path/to/cursor-agent-multi.sh \
  --workspace <WORKSPACE> \
  --task review-round2 \
  "<PROMPT>"
```

The prompt MUST include all of the following (clearly separated with headers):
- **Git commands to obtain the original diff** (same as Round 1)
- **Git commands to obtain the review-fix diff** (if fixes were committed,
  provide the SHA range so agents can diff base->fix separately from the
  original change)
- **All raw Round 1 reviews** (verbatim content from each model's output.md)
- **Changes applied** (list from Step 3)
- **Rebuttals** for disregarded suggestions (from Step 3)
- **Instruction**: "You are reviewing the triage of a code review. Given the
  original reviews, the fixes applied, and the rebuttals for dismissed
  suggestions — do you agree with the dismissals? Are there any remaining
  issues? For each rebuttal you disagree with, explain why the original
  suggestion should be reconsidered."

After the script finishes, **read every `output.md`** file.

If any Round 2 reviewer makes a convincing case for additional changes:
- Apply the change
- Note it in the final summary

### Final output

Present the final summary:

## Peanut Gallery Review: Complete

### Changes Applied (Round 1)
- List of fixes from triage

### Changes Applied (Round 2)
- Any additional fixes, or "None"

### Dismissed Suggestions (upheld)
- Rebuttals that Round 2 reviewers agreed with (or didn't contest)

### Dismissed Suggestions (overturned)
- Any rebuttals that Round 2 convinced you to reconsider

## Important

- This skill DOES modify files in the workspace (that's the point — it applies
  fixes). The Cursor review agents themselves are read-only.
- If some models fail, still proceed with results from the ones that succeeded
  and note which failed.
- If `cursor-agent-multi.sh` fails (e.g., missing cli.json), report the error.
- Keep prompts to the agents concise but complete. If the diff is very large,
  consider summarizing or splitting the review.
- The user can pass `--models` or `--timeout` flags; forward them to both
  `cursor-agent-multi.sh` invocations.
- **Always include this line in every prompt sent to agents:**
  "You are running non-interactively. No human will see your questions or
  reply. Never ask for clarification. Make reasonable assumptions and state
  them. If a tool call fails, try alternative invocations before giving up.
  Provide a complete answer no matter what."
