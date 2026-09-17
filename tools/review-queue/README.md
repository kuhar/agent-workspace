# review-queue

`review-queue` is a foreground TUI that discovers pull requests with `gh`,
orders their immutable heads by persistent project/author/PR priorities, and
invokes project-owned `review-pr.sh` adapters in a bounded set of managed
wrapper directories.

The scheduler deliberately has no knowledge of the review implementation
behind a launcher. It understands only the versioned queue protocol, process
status, logs, and result JSON.

## Run

```bash
cd /home/jakub/jakub-env/agent-workspace/tools/review-queue
./bin/review-queue init
./bin/review-queue doctor
./bin/review-queue
```

The first command writes the default configuration to
`${XDG_CONFIG_HOME:-~/.config}/review-queue/config.toml`. Persistent state and
logs live below `${XDG_STATE_HOME:-~/.local/state}/review-queue`.

Project priorities and `start_paused` initialize a new state database. After
that, priority and pause changes made through the TUI are persistent and the
database is authoritative. Changing a configured project name to point at a
different repository is rejected; use a new project name or reset the state
database intentionally.

Use `./bin/review-queue poll --json` for a one-shot discovery pass and
`./bin/review-queue state --json` for a non-interactive snapshot.

Quitting stops launchers and records active reviews as interrupted. On restart,
the next successful GitHub poll retries those reviews once, including in manual
mode, if the PR is still eligible and its revision is unchanged. The retry reuses
the workspace and incremental build output; it starts a new launcher attempt.
Explicitly cancelled reviews stay cancelled. A newer revision requires its own
manual enqueue. Interrupted attempts and their logs remain in the state database.

The scheduler polls once at startup, every configured interval (120 seconds by
default), and immediately after a review job finishes. PRs that merge or close
are removed from waiting work on that poll; terminal manually included PRs are
also removed from the persistent watch set. An already-running review is
allowed to finish.

The RocJITsu adapter retains review commits in the repository's shared Git
directory before launching reviewers. Saved peanut-review pages and comment
anchors use that durable storage after queue-owned worktrees are recycled.
The shared repository must remain available; the wrapper limit only bounds
temporary checkouts and build directories.

Failed runs automatically release unpinned workspaces after cleanup preflight
passes, even when the wrapper limit has not been reached. Source changes or a
queued retry keep the workspace. Failed cleanup retries wait 60 seconds and do
not change review ordering. Failure records and logs remain in the state
directory; recent failures also reappear in history after restarting the TUI.
Use `n` to enqueue a failed PR again after its workspace has been recycled.

The active-review panel grows to fit retained workspaces, within the terminal's
available height. Short terminals keep the rows scrollable. Reviews keep their
workspace creation order as their status changes. Both tables update changed
cells in place, preserving selection and scrolling on routine refreshes; the
waiting queue still follows dispatch priority.

Both PR lists show GitHub approvals separately from local review rounds:
`You ✓` means the authenticated GitHub account has approved, and `Others N`
counts other approving accounts. `You –` means no current personal approval;
`Approvals ?` means the queue has not fetched that information yet. The values
refresh on each poll and with `g`, using GitHub's latest opinionated reviews
so comments do not replace approvals and dismissed approvals do not count.

Drag the separator above the bottom activity pane up or down to resize it.
`Alt+Up` and `Alt+Down` grow or shrink it one row at a time. Scroll over the
history pane with the mouse wheel to read the last 50 activity entries.
Resizing keeps space for the queue and adapts when the terminal size changes.

## Dispatch modes

Press `m` to cycle **active → manual → paused**. Space pauses dispatch or resumes
the previous active/manual mode. The selection is saved across restarts.

In manual mode, discovery and polling continue and discovered PRs stay visible
in the waiting pane. Press `r` to enqueue the selected head, or `n` to include
and enqueue a PR. The ▶ marker identifies explicitly enqueued work. Existing
automatic entries stay held; a later push needs a new manual enqueue even for
manually watched PRs. Filters, priorities, concurrency, and storage limits still
apply. Mode changes allow running reviews to finish. Paused mode starts no work,
including manually enqueued reviews.

You can also run `review-queue mode manual` (or `active` / `paused`). For a new
state database, `[queue] start_mode = "manual"` selects the initial mode;
the existing `start_paused` setting is still supported when `start_mode` is
omitted. Saved mode selections take precedence over startup configuration.

## Permanent exclusions

For an unrelated PR, select its queued entry or retained review and press `i`.
Confirming Ignore removes waiting work and excludes future pushed heads of
that PR. The decision is saved in SQLite and survives restarts. A running
review is allowed to finish. Press `n` and enter the PR to include it again.

For recurring categories, edit the project's `query` in
`~/.config/review-queue/config.toml`. The default RocJITsu query excludes
Dependabot and PRs authored by the authenticated GitHub user:

```toml
query = "draft:false team-review-requested:ROCm/rocjitsu-core-team -author:app/dependabot -author:@me"
include_paths = ["emulation/"]
```

Other GitHub search exclusions, such as `-author:LOGIN` or `-label:LABEL`,
can be appended to the same query. Restart the TUI after editing configuration.
Query exclusions affect automatic discovery; explicitly included PRs remain
watched until ignored with `i`. The TUI's text filter only changes what is
displayed and does not exclude PRs from scheduling.

`include_paths` requires at least one changed file under one of the listed
repository-relative directories. RocJITsu uses `emulation/`, so a PR touching
only other parts of `rocm-systems` is excluded even if the team is requested
for review or the PR is manually included. Manual retry cannot bypass this
rule. Deletions and either side of a rename count as touching a directory.
An excluded PR can become eligible when a later push adds a matching change.

The queue paginates GitHub's file list and verifies the base/head commits
before accepting a result. Results are cached for that commit pair and filter.
An incomplete file list or API failure marks the project stale and holds its
waiting work until a successful poll. After enabling or changing the filter,
existing waiting PRs must be checked against the new rule before dispatch.
Already-running reviews may finish. Omit `include_paths` or use `[]` for
projects that do not need a directory filter.

## Accelerated demo

Run the production TUI against an isolated, deterministic synthetic trace:

```bash
./bin/review-queue --demo
./bin/review-queue --demo --demo-speed 1800
```

Demo mode does not read the real configuration or state database, call `gh`,
invoke a launcher, or create worktrees. It continuously replays PR arrivals,
pushes, a PR returning after author changes, a temporary GitHub outage, staged
review progress, successful reviews, one first-attempt test failure, and
wrapper-cap recycling. Each completed pass adds an `R` marker, so a PR returning
for a third pass is shown as `RR`.

The default clock runs at 300×. Press `1`, `2`, `3`, or `4` for 1×, 60×,
300×, or 1800×; `0` pauses only simulated time and `R` restarts the trace.
Dispatch pause remains independent: press Space to let PRs accumulate, adjust
their priorities, then press Space again to observe the new order. In demo
mode, `g` injects the next scheduled event immediately.

## Launcher protocol

Each project supplies an executable launcher. The queue never imports or
assumes a particular review implementation; it invokes these protocol-v1
commands instead:

```text
review-pr.sh queue protocol
review-pr.sh queue prepare <github-pr-url>
review-pr.sh queue run <github-pr-url>
review-pr.sh queue cleanup --check
review-pr.sh queue cleanup
```

`queue protocol` must print one JSON object with `version`, `repository`, and
`operations`. Version 1 requires `prepare`, `run`, `cleanup-check`, and
`cleanup`. `prepare` creates or refreshes the checkout and build environment at
the requested immutable head. `run` performs the project-owned review workflow.
Both commands run with the managed wrapper as their working directory and must
propagate termination to their children.

Launchers report their current phase using whole-line log markers:

```bash
echo '::group::Build'
echo '::group::Clang ASan/UBSan'
cmake --build --preset clang-23-asan-ubsan
echo '::endgroup::'
echo '::endgroup::'
```

Groups nest: this displays `Build › Clang ASan/UBSan` with elapsed time in the
current phase in selected-review details. The table shows the innermost phase
so long parent names do not hide the active step. The job's scheduling state
remains `preparing`, `running`, or
`cancelling`. Closing a group restores its parent's phase and original start
time. Each operation starts with an empty stack. Empty titles and unmatched end
markers are ignored. Titles support `%25`, `%0A`, and `%0D` escapes; whitespace
is normalized for display. Only these two group markers are interpreted.

Phase updates are parsed as output arrives and saved in SQLite. Group starts
appear in history, including phases that finish between UI refreshes. Press
`l` for the selected review's phase and launcher log. An end marker closes a
scope; the process exit code determines success. Leave the failing group open
on an error, and avoid emitting new groups from failure cleanup, so the failure
report retains the original phase. The RocJITsu launcher follows this rule.

The scripts own phase names, including SDK installation and compiler presets.
The reviewer/curator transition is emitted by peanut-review's `wait-all`
command, which knows when the reviewer wait finishes and curation starts.

The RocJITsu adapter maps protocol `prepare` and `cleanup` to the sibling
`worktree-scripts` commands `queue-setup` and `queue-cleanup`. The outer
`jakub-env` commit pins both nested repositories, so that adapter and lifecycle
contract must be deployed together.

RocJITsu reviews build with Clang by default (`default`,
`clang-23-asan-ubsan`, and `clang-23-tsan`). Set `CMAKE_PRESETS` to override
the build list explicitly.

The queue exports these variables for every operation:

- `REVIEW_QUEUE_PROTOCOL=1`
- `REVIEW_QUEUE_WRAPPER`: direct child of the configured wrapper root
- `REVIEW_QUEUE_ROOT`: configured wrapper root
- `REVIEW_QUEUE_OWNER_TOKEN`: random token also stored in
  `.review-queue.json` inside the wrapper
- `REVIEW_QUEUE_TARGET_HEAD`: full Git commit ID (`cleanup` during cleanup)
- `REVIEW_QUEUE_RESULT`: path where `run` must atomically write its result JSON

A successful `run` exits zero and writes:

```json
{
  "protocol": 1,
  "repository": "OWNER/REPO",
  "pr": 123,
  "requested_head": "FULL_COMMIT_ID",
  "reviewed_head": "FULL_COMMIT_ID",
  "status": "succeeded"
}
```

The queue accepts success only when both commit IDs equal the head it scheduled.
On a nonzero exit, either `prepare` or `run` may write a failure result with
`protocol`, `repository`, `pr`, `requested_head`, `exit_code`, `status`, and
`error`. The queue verifies the job identity and exit code before using it.
Use `status: "ineligible"` for a PR that has merged, closed, or become a draft;
use `status: "failed"` for an actual failure. `error` is a readable reason.
Without a matching failure result, the queue extracts diagnostics from the
operation's log and retains the launcher exit code. The TUI shows the reason
in selected-review details and history; press `l` for the full reason and the
last 500 log lines. Ineligible reviews appear as skipped.

`cleanup --check` must be read-only and print a JSON object containing a boolean
`safe`; `cleanup` may remove only the owned wrapper after validating its path,
marker, token, process state, and project-specific clean-worktree rules.

## Layout and themes

The interface is a fixed-height, single-column operations board with no sidebar
or screen-level scroll. Active and retained review slots stay above the
full-width priority queue; selected-item details and the three latest activity
events stay at the bottom. Queue rows have no state column: a yellow `…` beside
the review marker means the push quiet period is still active.

Textual's built-in Monokai, Catppuccin, Tokyo Night, Dracula, Nord, Gruvbox,
Solarized, and Atom One themes are available by name. The queue also registers
a `dark-plus` theme with `dark+` as an alias. Set the startup theme in the
configuration:

```toml
[ui]
theme = "catppuccin-mocha"
```

Use `--theme monokai` for a one-run override or press `t` to cycle Dark+,
Monokai, Catppuccin Mocha, and Tokyo Night.

## Controls

- Up/Down or `j`/`k`: select a PR.
- Left/Right: lower or raise the selected queued PR priority by ten and
  immediately rebalance the queue.
- Tab: switch between the review slots and queue.
- `p`: set project, author, and PR priorities exactly.
- `t`: cycle the preferred themes.
- Space: pause or resume dispatch.
- `m`: cycle active, manual, and paused modes.
- `r`: retry or enqueue the selected head.
- `x`: cancel the selected running review.
- `P`: pin or unpin its retained wrapper.
- `e`: recycle an idle wrapper after confirmation.
- `n`: include an open PR even when it does not match the project's GitHub
  filter. This manual inclusion persists across future pushes and also restores
  a previously ignored PR.
- `i`: permanently ignore the selected PR. Waiting work is removed and future
  pushed heads remain excluded; an already-running review is allowed to finish.
- `/`: filter, `g`: refresh, `?`: help, `q`: quit.

Demo-only controls are `1`–`4` for clock speed, `0` for clock pause, and `R`
to replay from the beginning.

## Development

```bash
uv sync --extra dev
uv run pytest
uv run ruff check .
```
