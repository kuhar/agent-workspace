#!/usr/bin/env bash
set -euo pipefail

usage() {
    cat <<'EOF'
Usage: codex-task [OPTIONS] --workspace DIR [PROMPT...]

Run a codex agent non-interactively (`codex exec`) to produce a markdown file.
The agent's final message lands in output.md via --output-last-message; the
full event stream is captured in stream.jsonl for debugging.

Options:
  --model MODEL          Codex model id (e.g. gpt-5.5). Defaults to
                         codex's configured default if omitted.
  --workspace DIR        Workspace directory (required)
  --output-dir DIR       Output directory (default: <workspace>/.codex/tasks)
  --name NAME            Task name for the output subdirectory (default: timestamp)
  --timeout SECS         Timeout in seconds (default: 480)
  --prompt TEXT|FILE     Prompt text, or path to a prompt file (single token + exists)
  --prompt-file FILE     Read prompt from FILE
  --add-dir DIR          Extra writable directory for the agent (repeatable).
                         peanut-review needs this for the session dir.
  --sandbox MODE         Codex sandbox: read-only|workspace-write|danger-full-access
                         (default: workspace-write)
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
sandbox_mode="workspace-write"
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
        --sandbox)     sandbox_mode="$2"; shift 2 ;;
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

if [[ -z "$output_dir" ]]; then
    output_dir="$workspace/.codex/tasks"
fi
if [[ -z "$task_name" ]]; then
    task_name="$(date +%Y%m%d-%H%M%S)"
fi
task_dir="$output_dir/$task_name"
output_file="$task_dir/output.md"
stream_file="$task_dir/stream.jsonl"
mkdir -p "$task_dir"

meta_file="$task_dir/meta.json"
start_time="$(date -Iseconds)"

if command -v jq >/dev/null; then
    jq -n \
        --arg model "$model" \
        --arg workspace "$workspace" \
        --arg prompt "$prompt" \
        --arg start "$start_time" \
        --arg timeout "$timeout_secs" \
        --arg sandbox "$sandbox_mode" \
        '{runner: "codex", model: $model, workspace: $workspace, prompt: $prompt,
          start: $start, timeout: ($timeout | tonumber), sandbox: $sandbox}' \
        > "$meta_file"
fi

echo "codex-task" >&2
echo "  Runner:    codex (codex exec)" >&2
echo "  Model:     ${model:-<codex default>}" >&2
echo "  Workspace: $workspace" >&2
echo "  Sandbox:   $sandbox_mode" >&2
echo "  Output:    $output_file" >&2
echo "  Timeout:   ${timeout_secs}s" >&2
echo "" >&2

# Build the command. --output-last-message captures the agent's final reply
# verbatim (this is what we expose as output.md). --json gives a structured
# event stream we tee into stream.jsonl for debugging.
cmd=(codex exec
     --cd "$workspace"
     --sandbox "$sandbox_mode"
     --skip-git-repo-check
     --json
     --output-last-message "$output_file")

if [[ -n "$model" ]]; then
    cmd+=(--model "$model")
fi

for d in "${add_dirs[@]}"; do
    cmd+=(--add-dir "$d")
done

cmd+=("$prompt")

if (( dry_run )); then
    printf '%q ' "${cmd[@]}" >&2
    echo >&2
    exit 0
fi

# `codex exec` reads stdin when no prompt arg is given OR when stdin is piped
# (it appends as `<stdin>` block). Close stdin explicitly so codex doesn't
# block waiting for input that will never arrive.
rc=0
timeout "$timeout_secs" "${cmd[@]}" > "$stream_file" 2>&1 < /dev/null || rc=$?

end_time="$(date -Iseconds)"
if command -v jq >/dev/null && [[ -f "$meta_file" ]]; then
    jq --arg end "$end_time" --argjson rc "$rc" '.end = $end | .exit_code = $rc' "$meta_file" > "$meta_file.tmp" \
        && mv "$meta_file.tmp" "$meta_file"
fi

# If --output-last-message didn't get written (e.g. agent crashed before
# producing a message), fall back to extracting the last agent_message from
# the JSON event stream so the orchestrator has something to look at.
if [[ ! -s "$output_file" && -s "$stream_file" ]] && command -v jq >/dev/null; then
    jq -r 'select(.type=="item.completed" and .item.type=="agent_message") | .item.text' \
        "$stream_file" 2>/dev/null | tail -n +1 > "$output_file" || true
fi

echo "" >&2
if [[ "$rc" -ne 0 ]]; then
    echo "Agent exited with code $rc. Output: $task_dir" >&2
    exit "$rc"
elif [[ -s "$output_file" ]]; then
    echo "Done. Output: $output_file" >&2
else
    echo "Warning: agent finished but output is empty." >&2
    exit 1
fi
