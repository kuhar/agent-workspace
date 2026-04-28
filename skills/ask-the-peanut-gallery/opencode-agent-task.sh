#!/usr/bin/env bash
set -euo pipefail

usage() {
    cat <<'EOF'
Usage: opencode-task [OPTIONS] --workspace DIR [PROMPT...]

Run an opencode agent non-interactively (`opencode run`) to produce a markdown
file. The agent's stdout is captured into output.md.

opencode is treated as the source of truth for available models and providers.
Use `opencode models` to discover what's installed (cloud providers like
`openai/*`, the free `opencode/*` tier, and any local providers like
`llama.cpp/*` configured in ~/.config/opencode/opencode.json). For local
llama.cpp models, ensure llama-server is running before invoking — boot it
out of band (e.g. `lcode qwen`); peanut-review no longer wraps lcode itself.

Options:
  --model MODEL          Opencode model id (provider/model), e.g.
                         openai/gpt-5.5, llama.cpp/qwen3.5-27b. Required.
  --workspace DIR        Workspace directory (required)
  --output-dir DIR       Output directory (default: <workspace>/.opencode/tasks)
  --name NAME            Task name for the output subdirectory (default: timestamp)
  --timeout SECS         Timeout in seconds (default: 480)
  --prompt TEXT|FILE     Prompt text, or path to a prompt file
  --prompt-file FILE     Read prompt from FILE
  --agent NAME           Opencode agent role to run as (default: opencode's
                         configured default — usually `build`).
  --dry-run              Print the command without executing
  -h, --help             Show this help
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
agent_name=""
dry_run=0

while [[ $# -gt 0 ]]; do
    case "$1" in
        --model)       model="$2"; shift 2 ;;
        --workspace)   workspace="$2"; shift 2 ;;
        --output-dir)  output_dir="$2"; shift 2 ;;
        --name)        task_name="$2"; shift 2 ;;
        --timeout)     timeout_secs="$2"; shift 2 ;;
        --prompt)      opt_prompt="$2"; shift 2 ;;
        --prompt-file) opt_prompt_file="$2"; shift 2 ;;
        --agent)       agent_name="$2"; shift 2 ;;
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

if [[ -z "$workspace" || -z "$model" || -z "$prompt" ]]; then
    echo "Error: --workspace, --model, and a prompt are required." >&2
    usage 1
fi

workspace="$(realpath "$workspace")"
if [[ ! -d "$workspace" ]]; then
    echo "Error: workspace does not exist: $workspace" >&2
    exit 1
fi

OPENCODE="${OPENCODE:-$(command -v opencode || true)}"
if [[ -z "$OPENCODE" ]]; then
    OPENCODE="$HOME/.opencode/bin/opencode"
fi
if [[ ! -x "$OPENCODE" ]]; then
    echo "Error: opencode not found (set OPENCODE env var to override)." >&2
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
        --arg agent "$agent_name" \
        '{runner: "opencode", model: $model, workspace: $workspace, prompt: $prompt,
          start: $start, timeout: ($timeout | tonumber),
          agent: ($agent | select(. != ""))}' \
        > "$meta_file"
fi

echo "opencode-task" >&2
echo "  Runner:    opencode" >&2
echo "  Model:     $model" >&2
[[ -n "$agent_name" ]] && echo "  Agent:     $agent_name" >&2
echo "  Workspace: $workspace" >&2
echo "  Output:    $output_file" >&2
echo "  Timeout:   ${timeout_secs}s" >&2
echo "" >&2

cmd=(
    "$OPENCODE" run
    --model "$model"
    --dir "$workspace"
    --dangerously-skip-permissions
    --format default
)
if [[ -n "$agent_name" ]]; then
    cmd+=(--agent "$agent_name")
fi
cmd+=("$prompt")

if (( dry_run )); then
    printf '%q ' "${cmd[@]}" >&2
    echo >&2
    exit 0
fi

rc=0
timeout "$timeout_secs" "${cmd[@]}" > "$output_file" || rc=$?

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
