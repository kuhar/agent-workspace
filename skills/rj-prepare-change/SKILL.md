---
name: rj-prepare-change
description: Use when asked to split, commit, rebase or publish a RocJITsu change, draft its PR text, or prepare it for a requested local review session. Stop at the requested handoff.
---

# Prepare a RocJITsu change

**Choose the endpoint.** Distinguish a prose draft, a local branch, a push command and a published PR. Preserve the requested draft or ready state. For local inspection, give the checkout, branch, base and head SHAs, validation results and review state. A command-only handoff ends with the copyable command.

**Split by dependency.** Check current develop and overlapping branches or PRs. Group fixes with the same cause. Give independent changes freestanding branches. Stack a child only when it needs its parent. Prefer one signed commit per coherent branch. On another contributor's branch, preserve their commit and add your follow-up separately unless asked to rewrite their history. Amend your own fixups. Validate the final tips together when they will run together.

**Write for the reader.** For a text-only request, draft from the existing commit or scoped diff and return the text. When preparing a commit, match the author's recent landed subjects and bodies. Explain the cause and remedy for a fix, the workload and measured effect for an optimization, or the intended use for an API. Include a short usage example or issue reference when it helps. Write each prose paragraph on one line in commit and PR bodies. Let GitHub wrap it. When opening or refreshing the PR, seed its title and body from the final commit.

**Check the rendered description.** Preview local drafts with a GitHub Flavored Markdown renderer such as `md2html --gfm`. After creating or editing a PR, inspect the rendered body on GitHub. Check that prose wraps naturally and each table value and unit appears under the intended header. Fix formatting errors and recheck.

**Review the requested range.** Use the existing `peanut-review` skill and project config when the user requests Peanut or a local review session. A plain request for feedback calls for inspecting the diff. Pin the base and head and honor the requested harness. For a full two-commit contribution, review from the original commit's parent through the follow-up. Default to two rounds for a requested local review of a finalized branch. Honor explicit counts, skips and early stops. For a findings-only review, keep the head fixed and report verified findings. When fixes are in scope, amend and migrate the session. Explain dismissals. Repeat review when the change warrants it. Report any reviewer that failed to finish.

**When publication is requested, verify the remote result.** When updating a live PR, check new human feedback, merge conflicts and relevant CI. Attribute failures against the base and validate the final head. Before rewriting a published branch, record the remote tip the rewrite is based on. For an authorized push, lease against that exact SHA. On mismatch, inspect the new commits, integrate them and revalidate before updating the lease. After publication, verify the remote head, PR base and requested draft or ready state.
