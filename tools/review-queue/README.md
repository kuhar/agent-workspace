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

The RocJITsu adapter maps protocol `prepare` and `cleanup` to the sibling
`worktree-scripts` commands `queue-setup` and `queue-cleanup`. The outer
`jakub-env` commit pins both nested repositories, so that adapter and lifecycle
contract must be deployed together.

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
