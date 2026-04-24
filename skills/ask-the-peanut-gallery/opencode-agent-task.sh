#!/usr/bin/env bash
set -euo pipefail

usage() {
    cat <<'EOF'
Usage: opencode-task [OPTIONS] --workspace DIR [PROMPT...]

Run an opencode agent non-interactively (via the lcode wrapper) to produce a
markdown file. The agent's stdout is captured into output.md.

lcode boots the local llama-server(s) and generates a per-session opencode.json
with provider definitions; we then forward to `opencode run` for a one-shot run.

Options:
  --model MODEL          Opencode model id, e.g. llama-primary/qwen3.6-35b-a3b
                         (required)
  --workspace DIR        Workspace directory (required)
  --output-dir DIR       Output directory (default: <workspace>/.opencode/tasks)
  --name NAME            Task name for the output subdirectory (default: timestamp)
  --timeout SECS         Timeout in seconds (default: 480)
  --prompt TEXT|FILE     Prompt text, or path to a prompt file
  --prompt-file FILE     Read prompt from FILE
  --lcode-primary NAME   lcode primary model (default: qwen)
  --lcode-subagent NAME  lcode subagent model (default: null)
  --agent NAME           Opencode agent to run as (default: reviewer)
  --dry-run              Print the command without executing
  -h, --help             Show this help

The prompt can be provided as: --prompt, --prompt-file, or trailing positional
arguments. If multiple sources are given, the first one wins (in that order).

Examples:
  opencode-task --workspace ~/iree/main \
      --model llama-primary/qwen3.6-35b-a3b \
      --prompt "Review the recent changes to the compiler pipeline"

  opencode-task --workspace ~/iree/main --lcode-primary gemma --lcode-subagent qwen \
      --model llama-primary/gemma4-31b \
      --prompt-file /tmp/review/prompt.md
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
lcode_primary="qwen"
lcode_subagent="null"
agent_name="reviewer"
dry_run=0

while [[ $# -gt 0 ]]; do
    case "$1" in
        --model)          model="$2"; shift 2 ;;
        --workspace)      workspace="$2"; shift 2 ;;
        --output-dir)     output_dir="$2"; shift 2 ;;
        --name)           task_name="$2"; shift 2 ;;
        --timeout)        timeout_secs="$2"; shift 2 ;;
        --prompt)         opt_prompt="$2"; shift 2 ;;
        --prompt-file)    opt_prompt_file="$2"; shift 2 ;;
        --lcode-primary)  lcode_primary="$2"; shift 2 ;;
        --lcode-subagent) lcode_subagent="$2"; shift 2 ;;
        --agent)          agent_name="$2"; shift 2 ;;
        --dry-run)        dry_run=1; shift ;;
        -h|--help)        usage 0 ;;
        --)               shift; break ;;
        -*)               echo "Error: Unknown option: $1" >&2; usage 1 ;;
        *)                break ;;
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

if [[ -z "$workspace" || -z "$model" || -z "$prompt" ]]; then
    echo "Error: --workspace, --model, and a prompt are required." >&2
    usage 1
fi

workspace="$(realpath "$workspace")"
if [[ ! -d "$workspace" ]]; then
    echo "Error: workspace does not exist: $workspace" >&2
    exit 1
fi

LCODE="${LCODE:-$(command -v lcode || true)}"
if [[ -z "$LCODE" ]]; then
    echo "Error: lcode wrapper not found on PATH (set LCODE env var to override)." >&2
    exit 1
fi

if [[ -z "$output_dir" ]]; then
    output_dir="$workspace/.opencode/tasks"
fi
if [[ -z "$task_name" ]]; then
    task_name="$(date +%Y%m%d-%H%M%S)"
fi
task_dir="$output_dir/$task_name"
output_file="$task_dir/output.md"
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
        --arg primary "$lcode_primary" \
        --arg subagent "$lcode_subagent" \
        --arg agent "$agent_name" \
        '{runner: "opencode", model: $model, workspace: $workspace, prompt: $prompt,
          start: $start, timeout: ($timeout | tonumber),
          lcode: {primary: $primary, subagent: $subagent}, agent: $agent}' \
        > "$meta_file"
fi

echo "opencode-task" >&2
echo "  Runner:    opencode (via lcode $lcode_primary $lcode_subagent)" >&2
echo "  Model:     $model" >&2
echo "  Agent:     $agent_name" >&2
echo "  Workspace: $workspace" >&2
echo "  Output:    $output_file" >&2
echo "  Timeout:   ${timeout_secs}s" >&2
echo "" >&2

lcode_cmd=(
    "$LCODE" "$lcode_primary" "$lcode_subagent"
    run
    --model "$model"
    --agent "$agent_name"
    --dir "$workspace"
    --dangerously-skip-permissions
    --format default
    "$prompt"
)

if (( dry_run )); then
    printf '%q ' "${lcode_cmd[@]}" >&2
    echo >&2
    exit 0
fi

rc=0
timeout "$timeout_secs" "${lcode_cmd[@]}" > "$output_file" || rc=$?

end_time="$(date -Iseconds)"
if command -v jq >/dev/null && [[ -f "$meta_file" ]]; then
    jq --arg end "$end_time" --argjson rc "$rc" '.end = $end | .exit_code = $rc' "$meta_file" > "$meta_file.tmp" \
        && mv "$meta_file.tmp" "$meta_file"
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
