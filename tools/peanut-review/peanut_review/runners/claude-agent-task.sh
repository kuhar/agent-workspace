#!/usr/bin/env bash
set -euo pipefail

usage() {
    cat <<'EOF'
Usage: claude-task [OPTIONS] --workspace DIR [PROMPT...]

Run a Claude Code agent non-interactively (`claude -p`) to produce a markdown
file. The agent's final text response is captured into output.md.

Claude Code has no working-directory flag, so this script cd's into the
workspace before exec. The prompt is fed on stdin (not as a positional arg)
because `--add-dir` is variadic and would otherwise swallow a trailing prompt.

Options:
  --model MODEL          Claude model id or alias (e.g. opus, sonnet,
                         claude-opus-4-8). Defaults to claude's configured
                         default if omitted.
  --workspace DIR        Workspace directory (required)
  --output-dir DIR       Output directory (default: <workspace>/.claude/tasks)
  --name NAME            Task name for the output subdirectory (default: timestamp)
  --timeout SECS         Timeout in seconds (default: 480)
  --prompt TEXT|FILE     Prompt text, or path to a prompt file (single token + exists)
  --prompt-file FILE     Read prompt from FILE
  --add-dir DIR          Extra directory the agent may access (repeatable).
                         peanut-review needs this for the session dir and /tmp.
  --dry-run              Print the command without executing
  -h, --help             Show this help

The prompt can be provided as: --prompt, --prompt-file, or trailing positional
arguments. If multiple sources are given, the first one wins (in that order).
EOF
    exit "${1:-0}"
}

model=""
workspace=""
output_dir=""
task_name=""
timeout_secs=480
opt_prompt=""
opt_prompt_file=""
dry_run=0
add_dirs=()

while [[ $# -gt 0 ]]; do
    case "$1" in
        --model)       model="$2"; shift 2 ;;
        --workspace)   workspace="$2"; shift 2 ;;
        --output-dir)  output_dir="$2"; shift 2 ;;
        --name)        task_name="$2"; shift 2 ;;
        --timeout)     timeout_secs="$2"; shift 2 ;;
        --prompt)      opt_prompt="$2"; shift 2 ;;
        --prompt-file) opt_prompt_file="$2"; shift 2 ;;
        --add-dir)     add_dirs+=("$2"); shift 2 ;;
        --dry-run)     dry_run=1; shift ;;
        -h|--help)     usage 0 ;;
        --)            shift; break ;;
        -*)            echo "Error: Unknown option: $1" >&2; usage 1 ;;
        *)             break ;;
    esac
done

positional_prompt="$*"

resolve_prompt_arg() {
    local text="$1"
    if [[ "$text" != *" "* && -f "$text" ]]; then
        cat "$text"
    else
        printf '%s' "$text"
    fi
}

if [[ -n "$opt_prompt" ]]; then
    prompt="$(resolve_prompt_arg "$opt_prompt")"
elif [[ -n "$opt_prompt_file" ]]; then
    if [[ ! -f "$opt_prompt_file" ]]; then
        echo "Error: --prompt-file not found: $opt_prompt_file" >&2
        exit 1
    fi
    prompt="$(cat "$opt_prompt_file")"
elif [[ -n "$positional_prompt" ]]; then
    prompt="$positional_prompt"
else
    prompt=""
fi

if [[ -z "$workspace" || -z "$prompt" ]]; then
    echo "Error: --workspace and a prompt are required." >&2
    usage 1
fi

workspace="$(realpath "$workspace")"
if [[ ! -d "$workspace" ]]; then
    echo "Error: workspace does not exist: $workspace" >&2
    exit 1
fi

CLAUDE="${CLAUDE:-$(command -v claude || true)}"
if [[ -z "$CLAUDE" || ! -x "$CLAUDE" ]]; then
    echo "Error: claude not found on PATH (set CLAUDE env var to override)." >&2
    exit 1
fi

if [[ -z "$output_dir" ]]; then
    output_dir="$workspace/.claude/tasks"
fi
if [[ -z "$task_name" ]]; then
    task_name="$(date +%Y%m%d-%H%M%S)"
fi
task_dir="$output_dir/$task_name"
output_file="$task_dir/output.md"
mkdir -p "$task_dir"

meta_file="$task_dir/meta.json"
start_time="$(date -Iseconds)"
pgid="$(ps -o pgid= -p "$$" | tr -d ' ' || true)"

if command -v jq >/dev/null; then
    jq -n \
        --arg model "$model" \
        --arg workspace "$workspace" \
        --arg prompt "$prompt" \
        --arg start "$start_time" \
        --arg timeout "$timeout_secs" \
        --arg pid "$$" \
        --arg pgid "$pgid" \
        --arg supervisor_pid "${PEANUT_SUPERVISOR_PID:-}" \
        '{runner: "claude", model: $model, workspace: $workspace, prompt: $prompt,
          start: $start, timeout: ($timeout | tonumber),
          pid: ($pid | tonumber),
          pgid: (if $pgid == "" then null else ($pgid | tonumber) end),
          supervisor_pid: (if $supervisor_pid == "" then null else ($supervisor_pid | tonumber) end)}' \
        > "$meta_file"
fi

echo "claude-task" >&2
echo "  Runner:    claude (claude -p)" >&2
echo "  Model:     ${model:-<claude default>}" >&2
echo "  Workspace: $workspace" >&2
echo "  Output:    $output_file" >&2
echo "  Timeout:   ${timeout_secs}s" >&2
echo "" >&2

# `--output-format text` prints only the final assistant message to stdout,
# which we capture as output.md (the agent does its real work via executed
# peanut-review CLI calls). `--dangerously-skip-permissions` is required for a
# non-interactive agent: in print mode an ungranted permission aborts the run
# rather than prompting, so there is no human to approve tool calls. The
# prompt goes on stdin so the variadic `--add-dir` cannot consume it.
cmd=("$CLAUDE" -p
     --output-format text
     --dangerously-skip-permissions)

if [[ -n "$model" ]]; then
    cmd+=(--model "$model")
fi

if (( ${#add_dirs[@]} > 0 )); then
    cmd+=(--add-dir "${add_dirs[@]}")
fi

if (( dry_run )); then
    printf '%q ' "${cmd[@]}" >&2
    echo >&2
    exit 0
fi

# Final metadata and timeout cleanup are handled by the Python supervisor.
cd "$workspace"
exec "${cmd[@]}" > "$output_file" <<<"$prompt"
