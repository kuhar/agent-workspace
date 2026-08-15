# SSH reviewers

Peanut-review can run selected reviewer agents on an SSH host while keeping
the session, comments, web UI, Curator, and GitHub operations local. This is
intended for reviewers that need a separate checkout, build tree, toolchain,
or documentation that must remain on the remote system.

SSH is an explicit transport option on an agent. It does not change the
runner: `cursor`, `opencode`, and `codex` still select the model wrapper. There
is no general executor abstraction, container support, or remote Curator.

## Architecture and trust boundary

The local session remains authoritative:

```text
remote reviewer CLI -> remote loopback port
                    -> existing SSH reverse forward
                    -> local loopback reviewer gateway
                    -> one local session's comments, notes, status, and signals
```

The reviewer gateway is a dedicated listener, not the human web API. A remote
reviewer gets a short-lived bearer capability bound to exactly one session,
agent, launch, expiry, and operation set. It can:

- read status, visible comments, and notes;
- create anchored or global comments and replies as its bound agent;
- create notes; and
- signal `round-done`.

It cannot edit, delete, resolve, curate, migrate, launch agents, operate the
web server, or call GitHub. The local Curator and normal web/CLI workflows see
remote findings immediately because they use the same local JSONL store.

The capability is passed through process environment and SSH stdin, never in
argv, prompts, logs, or session JSON. Local capability records contain only a
hash and are mode `0600`. The gateway accepts bounded JSON requests and must
bind to loopback. The SSH tunnel supplies transport protection; do not expose
the gateway directly to a network.

Remote reviewers can read local review comments. Their comments and notes can
also contain information learned from remote-only documentation, so that text
crosses back into the local session and may later be shown in the web UI or
pushed to GitHub by a local human. Treat review text as an explicit data export
from the remote trust domain. Source files, builds, and documentation are not
copied automatically.

## Prerequisites

Prepare the remote host before launching:

1. Create an independent checkout at the exact pinned review head.
2. Make the configured workspace, repository, and every build root readable
   and writable by the remote SSH account.
3. Install the same peanut-review CLI protocol and the configured runner.
4. For Cursor, put a valid `.cursor/cli.json` under the remote workspace.
5. Leave staged and tracked files clean. Untracked build outputs are allowed.
6. Establish an OpenSSH ControlMaster with a private, known `ControlPath`.
7. Reverse-forward a remote loopback port to the local reviewer gateway.

Peanut-review checks and reuses the configured master. It never creates a new
SSH connection, fetches or checks out a ref, resets or cleans a worktree, or
runs a build.

## Start the local gateway and persistent connection

Choose a local review root and gateway port:

```bash
REVIEW_ROOT=$HOME/reviews
LOCAL_GATEWAY_PORT=27184
peanut-review gateway-serve \
  --root "$REVIEW_ROOT" \
  --host 127.0.0.1 \
  --port "$LOCAL_GATEWAY_PORT"
```

In another terminal, create the persistent master and reverse forward. Keep
the control socket in a private directory and use the same destination string
in config:

```bash
SSH_HOST=reviewer@docs-host
CONTROL_PATH=$XDG_RUNTIME_DIR/peanut-docs-host.sock
REMOTE_GATEWAY_PORT=27184

ssh -MNf \
  -o ControlMaster=yes \
  -o ControlPersist=yes \
  -o ExitOnForwardFailure=yes \
  -S "$CONTROL_PATH" \
  -R "${REMOTE_GATEWAY_PORT}:127.0.0.1:${LOCAL_GATEWAY_PORT}" \
  "$SSH_HOST"

ssh -S "$CONTROL_PATH" -O check "$SSH_HOST"
```

To add the reverse forward to a master that is already running:

```bash
ssh -S "$CONTROL_PATH" -O forward \
  -R "${REMOTE_GATEWAY_PORT}:127.0.0.1:${LOCAL_GATEWAY_PORT}" \
  "$SSH_HOST"
```

The remote port must be unused and must remain a loopback listener. If the
host's SSH policy disables remote forwarding, ask its administrator to allow
it for this account and destination.

## Project configuration

Define top-level `sshTargets`, then add `sshTarget` only to agents that should
run remotely:

```json
{
  "reviewRoot": "$HOME/reviews",
  "workspaceRoot": "$HOME/src/project-review",
  "repoRelative": "source",
  "reviewAgentTimeoutSeconds": 1200,
  "sshTargets": {
    "internal-docs": {
      "host": "reviewer@docs-host",
      "controlPath": "/run/user/1000/peanut-docs-host.sock",
      "gatewayUrl": "http://127.0.0.1:27184",
      "workspaceRoot": "/srv/reviews/project-review",
      "repoRelative": "source",
      "buildRoots": [
        "/srv/reviews/project-review/build-release",
        "/srv/reviews/project-review/build-tests"
      ],
      "peanutReviewBin": "/opt/peanut-review/bin/peanut-review",
      "runtimeRoot": "/srv/reviews/project-review/.peanut-runtime"
    }
  },
  "agents": [
    {
      "name": "Local",
      "model": "gpt-5.5",
      "persona": "local.md",
      "runner": "codex"
    },
    {
      "name": "InternalDocs",
      "model": "gpt-5.5",
      "persona": "internal-docs.md",
      "runner": "codex",
      "sshTarget": "internal-docs"
    },
    {
      "name": "Curator",
      "model": "gpt-5.5",
      "runner": "codex",
      "role": "curator"
    }
  ]
}
```

Target fields:

- `host`: the exact OpenSSH destination used by the master.
- `controlPath`: absolute local path to the existing master socket.
- `gatewayUrl`: remote IPv4-loopback HTTP URL for the reverse forward
  (`127.0.0.1` or `localhost`, with an explicit port).
- `workspaceRoot`: absolute remote runner/build/tool root.
- `repoRelative`: repository path below that workspace, or `.`.
- `buildRoots`: one or more absolute remote build directories.
- `peanutReviewBin`: remote CLI path or command name.
- `runtimeRoot`: private remote staging and process-identity root.

Target names and agent references are validated strictly. Curator agents
cannot have `sshTarget`. Existing configurations without these fields retain
the local launch behavior.

## Launch and observe

Use the normal commands:

```bash
peanut-review start owner/repo#123 --no-launch
SESSION=<printed-session-path>

peanut-review --session "$SESSION" launch --dry-run
peanut-review --session "$SESSION" launch
peanut-review --session "$SESSION" status
peanut-review --session "$SESSION" wait-all round-done --timeout 1200
```

Before model execution, every selected SSH reviewer must pass all checks:

- ControlMaster health;
- reverse gateway reachability and protocol;
- workspace, repository, runtime parent, and build-root permissions;
- remote peanut-review and runner availability;
- Cursor CLI permissions when applicable;
- exact remote `HEAD`, pinned base and topic object availability; and
- no staged or tracked worktree modifications.

All SSH preflights finish before any reviewer in the lineup starts. Status
separately reports the local SSH channel and remote process state. `rerun`,
`wait-all`, `kill-agents`, local Curator launch, and the web UI work with mixed
local/SSH lineups.

On timeout, cancellation, completion, or repeated loss of the reverse gateway,
the remote process group is terminated using its recorded PID, PGID, and Linux
`/proc` start identity. Prompt, persona, and process-identity files are removed,
and the local capability is revoked. PID reuse never authorizes a signal.

If cleanup could not be confirmed because the master was unavailable, status
shows `cleanup=required`. Restore the same persistent connection, then run:

```bash
peanut-review --session "$SESSION" recover-ssh --agent InternalDocs
```

Recovery rechecks identity before signaling, removes staged prompt/persona
files, and is safe to repeat. A new preflight refuses an active, stale, or
unverifiable launch and points to this command.

## Troubleshooting

- `persistent SSH connection is unavailable`: verify `host` and `controlPath`
  match the master, then run `ssh -S "$CONTROL_PATH" -O check "$SSH_HOST"`.
- `reviewer gateway is unavailable`: verify `gateway-serve` is running and the
  reverse forward reaches its local port. Test the remote-loopback port from a
  normal shell on the remote host.
- `remote HEAD ... does not match pinned head`: update the independent remote
  checkout yourself. Peanut-review will not mutate it.
- `remote worktree has staged or tracked modifications`: preserve or revert
  those changes intentionally, then rerun. Untracked build output is ignored.
- `runner executable not found`: fix the remote non-interactive SSH `PATH`, or
  use a `peanutReviewBin` wrapper that sets the required tool environment.
- Cursor permission failures: fix the remote workspace's `.cursor/cli.json`;
  the local file does not apply to remote Cursor.
- `cleanup=required`: restore the master and use `recover-ssh` before rerun.

## Local validation

The gateway and transport suites use real CLI subprocesses, concurrent writes,
authorization failures, pinned Git validation, lifecycle identity checks, and
deterministic fake model binaries:

```bash
.venv/bin/pytest -q tests/test_gateway.py tests/test_ssh_transport.py
```

The localhost workflow starts a temporary real `sshd`, creates a real
ControlMaster and reverse forward, uses two independent clones and separate
build directories, launches a mixed local/SSH lineup plus a local Curator, and
checks cancellation, channel loss, orphan cleanup, capability revocation, and
recovery:

```bash
.venv/bin/pytest -q -n0 tests/test_ssh_localhost.py
```

Run the complete regression suite afterward:

```bash
.venv/bin/pytest -q
```
