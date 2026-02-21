#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
CURSOR_TASK="$SCRIPT_DIR/cursor-agent-task.sh"

usage() {
    cat <<'EOF'
Usage: cursor-multi [OPTIONS] --workspace DIR PROMPT...

Run multiple Cursor agents in parallel with different models, same prompt.

Each agent gets its own output subdirectory under the task directory.
Uses cursor-task internally.

Options:
  --models M1,M2,...  Comma-separated model IDs (default: see below)
  --names N1,N2,...   Comma-separated agent names (default: see below)
  --workspace DIR     Workspace directory (required)
  --output-dir DIR    Output directory (default: <workspace>/.cursor/tasks)
  --task TASK         Task name prefix (default: timestamp)
  --timeout SECS      Per-agent timeout in seconds (default: 480)
  -h, --help          Show this help

Default models: gpt-5.3-codex-fast, sonnet-4.6, gemini-3.1-pro, gemini-3-flash
Default names:  gpt, claude, gemini-pro, gemini-flash

Examples:
  cursor-multi --workspace ~/iree/main \
    "Summarize the VMVX backend architecture"

  cursor-multi --workspace ~/iree/main \
    --models gpt-5.3-codex-fast,sonnet-4.6 \
    --names gpt,claude --task compiler-review \
    "Review the compiler pipeline for potential issues"
EOF
    exit "${1:-0}"
}

DEFAULT_MODELS="gpt-5.3-codex-fast,sonnet-4.6,gemini-3.1-pro,gemini-3-flash"
DEFAULT_NAMES="gpt,claude,gemini-pro,gemini-flash"

models=""
names=""
workspace=""
output_dir=""
task_prefix=""
timeout_secs=480

while [[ $# -gt 0 ]]; do
    case "$1" in
        --models)     models="$2"; shift 2 ;;
        --names)      names="$2"; shift 2 ;;
        --workspace)  workspace="$2"; shift 2 ;;
        --output-dir) output_dir="$2"; shift 2 ;;
        --task)       task_prefix="$2"; shift 2 ;;
        --timeout)    timeout_secs="$2"; shift 2 ;;
        -h|--help)    usage 0 ;;
        --)           shift; break ;;
        -*)           echo "Error: Unknown option: $1" >&2; usage 1 ;;
        *)            break ;;
    esac
done

prompt="$*"

if [[ -z "$workspace" || -z "$prompt" ]]; then
    echo "Error: --workspace and a prompt are required." >&2
    usage 1
fi

ws="$(realpath "$workspace")"

if [[ -z "$output_dir" ]]; then
    output_dir="$ws/.cursor/tasks"
fi

# Apply defaults: default names only pair with default models.
if [[ -z "$models" ]]; then
    models="$DEFAULT_MODELS"
    if [[ -z "$names" ]]; then
        names="$DEFAULT_NAMES"
    fi
fi

# Parse models and names into arrays.
IFS=',' read -ra model_arr <<< "$models"
if [[ -n "$names" ]]; then
    IFS=',' read -ra name_arr <<< "$names"
    if [[ ${#name_arr[@]} -ne ${#model_arr[@]} ]]; then
        echo "Error: --names count (${#name_arr[@]}) must match --models count (${#model_arr[@]})." >&2
        exit 1
    fi
else
    name_arr=("${model_arr[@]}")
fi

if [[ -z "$task_prefix" ]]; then
    task_prefix="$(date +%Y%m%d-%H%M%S)"
fi

echo "cursor-multi"
echo "  Workspace: $workspace"
echo "  Output:    $output_dir/$task_prefix"
echo "  Models:    ${model_arr[*]}"
echo "  Names:     ${name_arr[*]}"
echo "  Timeout:   ${timeout_secs}s"
echo ""

# Launch agents in parallel.
pids=()
log_files=()
for i in "${!model_arr[@]}"; do
    model="${model_arr[$i]}"
    name="${name_arr[$i]}"
    task_name="$task_prefix/$name"
    log_dir="$output_dir/$task_name"
    mkdir -p "$log_dir"
    log_file="$log_dir/agent.log"
    log_files+=("$log_file")

    echo "Starting: $name ($model)"

    "$CURSOR_TASK" \
        --model "$model" \
        --workspace "$workspace" \
        --output-dir "$output_dir" \
        --name "$task_name" \
        --timeout "$timeout_secs" \
        "$prompt" \
        > "$log_file" 2>&1 &
    pids+=($!)
done

echo ""
echo "All ${#pids[@]} agents launched. Waiting..."
echo ""

# Wait for agents in completion order.
# Build a pid-to-index map for lookup.
declare -A pid_to_idx
for i in "${!pids[@]}"; do
    pid_to_idx[${pids[$i]}]=$i
done

failures=0
remaining=${#pids[@]}
while [[ "$remaining" -gt 0 ]]; do
    finished_pid=""
    rc=0
    wait -n -p finished_pid "${pids[@]}" || rc=$?
    i="${pid_to_idx[$finished_pid]}"
    name="${name_arr[$i]}"
    model="${model_arr[$i]}"
    log="${log_files[$i]}"

    # Remove the reaped PID so subsequent wait -n calls don't error.
    for j in "${!pids[@]}"; do
        if [[ "${pids[$j]}" == "$finished_pid" ]]; then
            unset 'pids[$j]'
            break
        fi
    done

    output_file="$output_dir/$task_prefix/$name/output.md"
    if [[ "$rc" -eq 0 ]]; then
        echo "[done] $name ($model)"
    else
        echo "[FAIL] $name ($model) - exit code $rc"
        ((failures++)) || true
    fi
    echo "       Log: $log"
    if [[ -s "$output_file" ]]; then
        echo "       ---"
        head -10 "$output_file" | sed 's/^/       /'
        lines=$(wc -l < "$output_file")
        if [[ "$lines" -gt 10 ]]; then
            echo "       ... ($((lines - 10)) more lines)"
        fi
        echo ""
    fi
    ((remaining--)) || true
done

echo ""
echo "Results:"
for i in "${!name_arr[@]}"; do
    name="${name_arr[$i]}"
    output_file="$output_dir/$task_prefix/$name/output.md"

    if [[ -f "$output_file" ]]; then
        echo "  $name: $output_file"
    else
        echo "  $name: (no output)"
    fi
done

if [[ "$failures" -gt 0 ]]; then
    echo ""
    echo "$failures agent(s) failed. Check logs above."
    exit 1
fi
