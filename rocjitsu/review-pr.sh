#!/usr/bin/env bash
set -euo pipefail

usage() {
  cat <<'EOF'
Usage: review-pr.sh [--no-build] [--no-pytest] [--no-launch] <pr-number|github-pr-url|owner/repo#number>
       review-pr.sh queue <protocol|prepare|run|cleanup> [args...]

Checks the PR into the selected review workspace's rocm-systems checkout,
updates the existing PR checkout if it is already present, creates/reuses the
peanut-review session from that workspace's .peanut-review.json, launches the
configured reviewers, then waits for every reviewer and the curator to finish.
The script owns the checkout for its full run; concurrent reviews using the
same worktree wait for that ownership to be released.

Environment overrides:
  REVIEW_PARENT       default: invoking ~/rocjitsu/review* directory, or
                      $HOME/rocjitsu/review when run from the tracked source
  WORKSPACE           default: $REVIEW_PARENT/rocm-systems
  ROCJITSU_SOURCE     default: $WORKSPACE/emulation/rocjitsu
  BUILD_DIR           default: $REVIEW_PARENT/build
  VENV_DIR            default: $REVIEW_PARENT/venv
  CMAKE_PRESETS       default: default clang-23-asan-ubsan clang-23-tsan
  CMAKE_PRESET        legacy single-preset override
  CMAKE_CONFIGURE_ARGS optional whitespace-separated extra CMake arguments
  CMAKE_BUILD_TARGET  default: all
  PYTEST_WORKDIR      default: $ROCJITSU_SOURCE/lib/python
  PYTEST_ARGS         default: amdisa/tests/ -x
  PYTEST_PYTHON       default: python
  PYTEST_CMD          optional full command override, run from $PYTEST_WORKDIR
  PYTEST_REQUIRED=1   make pytest failures block session launch
  DEFAULT_REPO        default: ROCm/rocm-systems
  PR_BIN              default: $HOME/jakub-env/agent-workspace/tools/peanut-review/bin/peanut-review
  REVIEW_WAIT_TIMEOUT default: reviewAgentTimeoutSeconds from the config (900)
  ALLOW_DIRTY=1       allow switching with tracked local changes
  UPDATE_SUBMODULES=1 update rocm-systems submodules after checkout
EOF
}

NO_LAUNCH=0
NO_BUILD=0
NO_PYTEST=0
PR_SPEC=""
QUEUE_ACTION=""
QUEUE_ARGS=()
if [[ "${1:-}" == "queue" ]]; then
  shift
  QUEUE_ACTION="${1:-}"
  [[ -n "$QUEUE_ACTION" ]] && shift
  QUEUE_ARGS=("$@")
else
  while (($#)); do
    case "$1" in
      --no-build)
        NO_BUILD=1
        shift
        ;;
      --no-pytest)
        NO_PYTEST=1
        shift
        ;;
      --no-launch)
        NO_LAUNCH=1
        shift
        ;;
      -h|--help)
        usage
        exit 0
        ;;
      -*)
        echo "unknown option: $1" >&2
        usage >&2
        exit 2
        ;;
      *)
        if [[ -n "$PR_SPEC" ]]; then
          echo "only one PR may be specified" >&2
          usage >&2
          exit 2
        fi
        PR_SPEC="$1"
        shift
        ;;
    esac
  done

  if [[ -z "$PR_SPEC" ]]; then
    usage >&2
    exit 2
  fi
fi

DEFAULT_REVIEW_PARENT="$HOME/rocjitsu/review"
INVOCATION_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"
if [[ "$INVOCATION_DIR" == "$HOME/rocjitsu/"* && -f "$INVOCATION_DIR/.peanut-review.json" ]]; then
  DEFAULT_REVIEW_PARENT="$INVOCATION_DIR"
fi
REVIEW_PARENT="${REVIEW_PARENT:-$DEFAULT_REVIEW_PARENT}"
WORKSPACE="${WORKSPACE:-$REVIEW_PARENT/rocm-systems}"
ROCJITSU_SOURCE="${ROCJITSU_SOURCE:-$WORKSPACE/emulation/rocjitsu}"
BUILD_DIR="${BUILD_DIR:-$REVIEW_PARENT/build}"
VENV_DIR="${VENV_DIR:-$REVIEW_PARENT/venv}"
CONFIG="$REVIEW_PARENT/.peanut-review.json"
DEFAULT_CMAKE_PRESETS="default clang-23-asan-ubsan clang-23-tsan"
CMAKE_PRESET_SPEC="${CMAKE_PRESETS:-${CMAKE_PRESET:-$DEFAULT_CMAKE_PRESETS}}"
CMAKE_PRESET_SPEC="${CMAKE_PRESET_SPEC//,/ }"
read -r -a CMAKE_BUILD_PRESETS <<<"$CMAKE_PRESET_SPEC"
if ((${#CMAKE_BUILD_PRESETS[@]} == 0)); then
  echo "no CMake presets requested" >&2
  exit 2
fi
CMAKE_CONFIGURE_ARGS_SPEC="${CMAKE_CONFIGURE_ARGS:-}"
CMAKE_CONFIGURE_ARGS_ARRAY=()
if [[ -n "$CMAKE_CONFIGURE_ARGS_SPEC" ]]; then
  read -r -a CMAKE_CONFIGURE_ARGS_ARRAY <<<"$CMAKE_CONFIGURE_ARGS_SPEC"
fi
CMAKE_BUILD_TARGET="${CMAKE_BUILD_TARGET:-all}"
PYTEST_WORKDIR="${PYTEST_WORKDIR:-$ROCJITSU_SOURCE/lib/python}"
PYTEST_ARGS_SPEC="${PYTEST_ARGS:-amdisa/tests/ -x}"
PYTEST_PYTHON="${PYTEST_PYTHON:-python}"
DEFAULT_REPO="${DEFAULT_REPO:-ROCm/rocm-systems}"
PR_BIN="${PR_BIN:-$HOME/jakub-env/agent-workspace/tools/peanut-review/bin/peanut-review}"

phase_start() {
  local title="$1"
  title="${title//%/%25}"
  title="${title//$'\r'/%0D}"
  title="${title//$'\n'/%0A}"
  printf '::group::%s\n' "$title"
}

phase_end() {
  echo '::endgroup::'
}

need() {
  if ! command -v "$1" >/dev/null 2>&1; then
    echo "missing required command: $1" >&2
    exit 1
  fi
}

slugify() {
  local value="$1"
  value="$(printf '%s' "$value" | tr '[:upper:]' '[:lower:]')"
  value="$(printf '%s' "$value" | sed -E 's/[^a-z0-9]+/-/g; s/^-+//; s/-+$//; s/-+/-/g')"
  printf '%s' "${value:-pr}"
}

resolve_spec() {
  local spec="$1"
  RESOLVED_REPO="$DEFAULT_REPO"
  RESOLVED_NUMBER=""

  if [[ "$spec" =~ ^[0-9]+$ ]]; then
    RESOLVED_NUMBER="$spec"
    return
  fi

  if [[ "$spec" =~ ^([A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+)#([0-9]+)$ ]]; then
    RESOLVED_REPO="${BASH_REMATCH[1]}"
    RESOLVED_NUMBER="${BASH_REMATCH[2]}"
    return
  fi

  if [[ "$spec" =~ github\.com/([^/]+/[^/]+)/pull/([0-9]+) ]]; then
    RESOLVED_REPO="${BASH_REMATCH[1]}"
    RESOLVED_NUMBER="${BASH_REMATCH[2]}"
    return
  fi

  if [[ "$spec" =~ ^https?:// ]]; then
    local effective
    effective="$(curl -Ls -o /dev/null -w '%{url_effective}' "$spec" || true)"
    if [[ "$effective" =~ github\.com/([^/]+/[^/]+)/pull/([0-9]+) ]]; then
      RESOLVED_REPO="${BASH_REMATCH[1]}"
      RESOLVED_NUMBER="${BASH_REMATCH[2]}"
      return
    fi
  fi

  echo "could not resolve PR spec: $spec" >&2
  echo "use a PR number, GitHub PR URL, or owner/repo#number" >&2
  exit 2
}

queue_validate_wrapper() {
  if [[ "${REVIEW_QUEUE_PROTOCOL:-}" != 1 ]]; then
    echo "REVIEW_QUEUE_PROTOCOL=1 is required" >&2
    return 1
  fi
  for variable in \
    REVIEW_QUEUE_WRAPPER REVIEW_QUEUE_ROOT REVIEW_QUEUE_OWNER_TOKEN REVIEW_QUEUE_TARGET_HEAD; do
    if [[ -z "${!variable:-}" ]]; then
      echo "missing required queue environment: $variable" >&2
      return 1
    fi
  done

  local wrapper root marker marker_token marker_protocol
  wrapper="$(realpath -e -- "$REVIEW_QUEUE_WRAPPER")"
  root="$(realpath -e -- "$REVIEW_QUEUE_ROOT")"
  if [[ -L "$REVIEW_QUEUE_WRAPPER" || "$(dirname -- "$wrapper")" != "$root" ]]; then
    echo "queue wrapper is not a direct managed-root child: $wrapper" >&2
    return 1
  fi
  marker="$wrapper/.review-queue.json"
  if [[ ! -f "$marker" ]]; then
    echo "missing queue ownership marker: $marker" >&2
    return 1
  fi
  marker_token="$(jq -r '.owner_token // empty' "$marker")"
  marker_protocol="$(jq -r '.protocol // empty' "$marker")"
  if [[ "$marker_protocol" != 1 || "$marker_token" != "$REVIEW_QUEUE_OWNER_TOKEN" ]]; then
    echo "queue ownership marker does not match" >&2
    return 1
  fi
  REVIEW_QUEUE_WRAPPER="$wrapper"
  REVIEW_QUEUE_ROOT="$root"
  export REVIEW_QUEUE_WRAPPER REVIEW_QUEUE_ROOT
}

queue_resolve_pr() {
  local spec="$1"
  resolve_spec "$spec"
  QUEUE_PR_JSON="$(gh pr view "$RESOLVED_NUMBER" --repo "$RESOLVED_REPO" \
    --json number,url,headRefName,headRefOid,state,isDraft)"
  QUEUE_PR_NUMBER="$(jq -r '.number' <<<"$QUEUE_PR_JSON")"
  QUEUE_PR_URL="$(jq -r '.url' <<<"$QUEUE_PR_JSON")"
  QUEUE_HEAD_SHA="$(jq -r '.headRefOid' <<<"$QUEUE_PR_JSON")"
  local state draft
  state="$(jq -r '.state' <<<"$QUEUE_PR_JSON")"
  draft="$(jq -r '.isDraft' <<<"$QUEUE_PR_JSON")"
  case "$state:$draft" in
    OPEN:false|OPEN:true|MERGED:false|MERGED:true|CLOSED:false|CLOSED:true) ;;
    *) echo "invalid PR state returned by GitHub: $state (draft: $draft)" >&2; return 1 ;;
  esac
  if [[ "$state" != "OPEN" || "$draft" != false ]]; then
    local reason timestamp
    reason="PR #$QUEUE_PR_NUMBER is ${state,,}"
    [[ "$draft" == true ]] && reason="PR #$QUEUE_PR_NUMBER is a draft"
    echo "$reason; skipping review" >&2
    timestamp="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
    queue_write_result ineligible 1 "$timestamp" "$timestamp" "" "$reason"
    return 1
  fi
  if [[ -n "${REVIEW_QUEUE_TARGET_HEAD:-}" \
        && "$REVIEW_QUEUE_TARGET_HEAD" != "$QUEUE_HEAD_SHA" ]]; then
    echo "PR head changed: requested $REVIEW_QUEUE_TARGET_HEAD, current $QUEUE_HEAD_SHA" >&2
    return 75
  fi
}

queue_prepare() {
  if [[ $# -ne 1 ]]; then
    echo "Usage: review-pr.sh queue prepare <pr>" >&2
    return 2
  fi
  need gh
  need git
  need jq
  need realpath
  phase_start "Check PR"
  queue_validate_wrapper
  queue_resolve_pr "$1"
  phase_end

  local main_workspace worktree_script fetch_ref
  main_workspace="${ROCJITSU_MAIN_WORKSPACE:-$HOME/rocjitsu/develop/rocm-systems}"
  worktree_script="${ROCJITSU_WORKTREE_SCRIPT:-$HOME/jakub-env/worktree-scripts/rocjitsu/rocjitsu-worktree.sh}"
  if [[ ! -x "$worktree_script" ]]; then
    echo "missing RocJITsu worktree script: $worktree_script" >&2
    return 1
  fi
  fetch_ref="refs/remotes/origin/review-queue/$QUEUE_PR_NUMBER"
  phase_start "Fetch PR"
  git -C "$main_workspace" fetch origin \
    "+pull/${QUEUE_PR_NUMBER}/head:${fetch_ref}"
  if [[ "$(git -C "$main_workspace" rev-parse "$fetch_ref")" != "$QUEUE_HEAD_SHA" ]]; then
    echo "fetched PR head does not match the requested queue snapshot" >&2
    return 1
  fi
  phase_end
  phase_start "Prepare workspace"
  "$worktree_script" queue-setup "$REVIEW_QUEUE_WRAPPER" "$QUEUE_HEAD_SHA"
  if [[ "$(git -C "$REVIEW_QUEUE_WRAPPER/rocm-systems" rev-parse HEAD)" != "$QUEUE_HEAD_SHA" ]]; then
    echo "prepared worktree does not match the requested head" >&2
    return 1
  fi
  phase_end
}

queue_write_result() {
  local status="$1"
  local exit_code="$2"
  local started_at="$3"
  local finished_at="$4"
  local reviewed_head="$5"
  local result="${REVIEW_QUEUE_RESULT:-}"
  if [[ -z "$result" ]]; then
    echo "missing required queue environment: REVIEW_QUEUE_RESULT" >&2
    return 1
  fi
  mkdir -p -- "$(dirname -- "$result")"
  local temporary="${result}.tmp.$$"
  jq -n \
    --argjson protocol 1 \
    --arg repository "$RESOLVED_REPO" \
    --argjson pr "$QUEUE_PR_NUMBER" \
    --arg requested_head "$REVIEW_QUEUE_TARGET_HEAD" \
    --arg reviewed_head "$reviewed_head" \
    --arg status "$status" \
    --arg error "${6:-}" \
    --argjson exit_code "$exit_code" \
    --arg started_at "$started_at" \
    --arg finished_at "$finished_at" \
    '{protocol:$protocol,repository:$repository,pr:$pr,
      requested_head:$requested_head,reviewed_head:$reviewed_head,
      status:$status,exit_code:$exit_code,error:$error,
      started_at:$started_at,finished_at:$finished_at}' >"$temporary"
  mv -- "$temporary" "$result"
}

queue_run() {
  if [[ $# -ne 1 ]]; then
    echo "Usage: review-pr.sh queue run <pr>" >&2
    return 2
  fi
  need gh
  need git
  need jq
  need realpath
  phase_start "Check PR"
  queue_validate_wrapper
  queue_resolve_pr "$1"
  phase_end
  if [[ ! -d "$REVIEW_QUEUE_WRAPPER/rocm-systems" ]]; then
    echo "queue wrapper was not prepared: $REVIEW_QUEUE_WRAPPER" >&2
    return 1
  fi

  local script_path started_at finished_at reviewed_head status exit_code
  script_path="$(realpath -- "${BASH_SOURCE[0]}")"
  started_at="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
  set +e
  REVIEW_PARENT="$REVIEW_QUEUE_WRAPPER" \
    WORKSPACE="$REVIEW_QUEUE_WRAPPER/rocm-systems" \
    "$script_path" "$QUEUE_PR_URL"
  exit_code=$?
  set -e
  finished_at="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
  reviewed_head="$(git -C "$REVIEW_QUEUE_WRAPPER/rocm-systems" rev-parse HEAD 2>/dev/null || true)"
  status="failed"
  if [[ "$exit_code" == 0 && "$reviewed_head" == "$REVIEW_QUEUE_TARGET_HEAD" ]]; then
    status="succeeded"
  elif [[ "$exit_code" == 0 ]]; then
    exit_code=1
  fi
  queue_write_result "$status" "$exit_code" "$started_at" "$finished_at" "$reviewed_head"
  return "$exit_code"
}

queue_cleanup() {
  need jq
  need realpath
  need flock
  queue_validate_wrapper
  local worktree_script
  worktree_script="${ROCJITSU_WORKTREE_SCRIPT:-$HOME/jakub-env/worktree-scripts/rocjitsu/rocjitsu-worktree.sh}"
  if [[ ! -x "$worktree_script" ]]; then
    echo "missing RocJITsu worktree script: $worktree_script" >&2
    return 1
  fi
  if [[ "${1:-}" == "--check" && $# -eq 1 ]]; then
    exec "$worktree_script" queue-cleanup --check "$REVIEW_QUEUE_WRAPPER"
  elif [[ $# -eq 0 ]]; then
    exec "$worktree_script" queue-cleanup "$REVIEW_QUEUE_WRAPPER"
  else
    echo "Usage: review-pr.sh queue cleanup [--check]" >&2
    return 2
  fi
}

setup_rocjitsu_env() {
  export CCACHE_BASEDIR="${CCACHE_BASEDIR:-$REVIEW_PARENT}"
  export CCACHE_NOHASHDIR="${CCACHE_NOHASHDIR:-true}"

  if [[ -d "$VENV_DIR" ]]; then
    export VIRTUAL_ENV="$VENV_DIR"
    export PATH="$VENV_DIR/bin:$PATH"
  fi

  if [[ -x "$VENV_DIR/bin/rocm-sdk" ]]; then
    local rocm_root
    rocm_root="$("$VENV_DIR/bin/rocm-sdk" path --root)"
    export ROCM_PATH="${ROCM_PATH:-$rocm_root}"
    export ROCM_HOME="${ROCM_HOME:-$rocm_root}"
    export CMAKE_PREFIX_PATH="$rocm_root/lib/cmake${CMAKE_PREFIX_PATH:+:$CMAKE_PREFIX_PATH}"
    export PATH="$rocm_root/bin:$PATH"
  fi

  if [[ -d "$BUILD_DIR/bin" ]]; then
    export PATH="$BUILD_DIR/bin:$PATH"
  fi
  if [[ -d "$BUILD_DIR/tests" ]]; then
    export PATH="$BUILD_DIR/tests:$PATH"
  fi

  if command -v clang++-23 >/dev/null 2>&1; then
    local rt rt_path rt_dir
    for rt in \
      libclang_rt.asan-x86_64.so \
      libclang_rt.ubsan_standalone-x86_64.so \
      libclang_rt.tsan-x86_64.so; do
      rt_path="$(clang++-23 -print-file-name="$rt")"
      if [[ -f "$rt_path" ]]; then
        rt_dir="$(dirname "$rt_path")"
        case ":${LD_LIBRARY_PATH:-}:" in
          *":$rt_dir:"*) ;;
          *) export LD_LIBRARY_PATH="$rt_dir${LD_LIBRARY_PATH:+:$LD_LIBRARY_PATH}" ;;
        esac
      fi
    done
  fi
}

if [[ -n "$QUEUE_ACTION" ]]; then
  case "$QUEUE_ACTION" in
    protocol)
      if ((${#QUEUE_ARGS[@]} != 0)); then
        echo "Usage: review-pr.sh queue protocol" >&2
        exit 2
      fi
      printf '%s\n' \
        '{"version":1,"repository":"ROCm/rocm-systems","operations":["prepare","run","cleanup-check","cleanup"]}'
      exit 0
      ;;
    prepare)
      queue_prepare "${QUEUE_ARGS[@]}"
      exit $?
      ;;
    run)
      queue_run "${QUEUE_ARGS[@]}"
      exit $?
      ;;
    cleanup)
      queue_cleanup "${QUEUE_ARGS[@]}"
      exit $?
      ;;
    *)
      echo "unknown queue action: $QUEUE_ACTION" >&2
      exit 2
      ;;
  esac
fi

need gh
need git
need jq
need sed
need curl
need flock
if [[ "$NO_BUILD" != 1 ]]; then
  need cmake
fi

if [[ ! -x "$PR_BIN" ]]; then
  echo "peanut-review CLI not executable: $PR_BIN" >&2
  exit 1
fi
if [[ ! -f "$CONFIG" ]]; then
  echo "missing peanut-review config: $CONFIG" >&2
  exit 1
fi
REVIEW_WAIT_TIMEOUT="${REVIEW_WAIT_TIMEOUT:-$(jq -r '.reviewAgentTimeoutSeconds // 900' "$CONFIG")}"
if ! [[ "$REVIEW_WAIT_TIMEOUT" =~ ^[1-9][0-9]*$ ]]; then
  echo "REVIEW_WAIT_TIMEOUT must be a positive integer: $REVIEW_WAIT_TIMEOUT" >&2
  exit 2
fi
if ! git -C "$WORKSPACE" rev-parse --show-toplevel >/dev/null 2>&1; then
  echo "missing rocm-systems checkout: $WORKSPACE" >&2
  exit 1
fi
if [[ ! -d "$ROCJITSU_SOURCE" ]]; then
  echo "missing RocJITsu source directory: $ROCJITSU_SOURCE" >&2
  exit 1
fi

WORKTREE_GIT_DIR="$(git -C "$WORKSPACE" rev-parse --absolute-git-dir)"
WORKTREE_LOCK="$WORKTREE_GIT_DIR/peanut-review-worktree.lock"
phase_start "Wait for worktree lock"
echo "waiting:     $WORKSPACE"
exec {WORKTREE_LOCK_FD}>"$WORKTREE_LOCK"
flock "$WORKTREE_LOCK_FD"
echo "acquired:    $WORKSPACE"
phase_end
echo
phase_start "Inspect PR"

resolve_spec "$PR_SPEC"
PR_JSON="$(gh pr view "$RESOLVED_NUMBER" --repo "$RESOLVED_REPO" \
  --json number,title,url,headRefName,headRefOid,baseRefName,baseRefOid,updatedAt)"

PR_NUMBER="$(jq -r '.number' <<<"$PR_JSON")"
PR_TITLE="$(jq -r '.title' <<<"$PR_JSON")"
PR_URL="$(jq -r '.url' <<<"$PR_JSON")"
HEAD_REF="$(jq -r '.headRefName' <<<"$PR_JSON")"
HEAD_SHA="$(jq -r '.headRefOid' <<<"$PR_JSON")"
BASE_REF="$(jq -r '.baseRefName' <<<"$PR_JSON")"
BASE_SHA="$(jq -r '.baseRefOid' <<<"$PR_JSON")"
UPDATED_AT="$(jq -r '.updatedAt' <<<"$PR_JSON")"
if [[ -n "${REVIEW_QUEUE_TARGET_HEAD:-}" && "$HEAD_SHA" != "$REVIEW_QUEUE_TARGET_HEAD" ]]; then
  echo "PR head changed: requested $REVIEW_QUEUE_TARGET_HEAD, current $HEAD_SHA" >&2
  exit 75
fi
LOCAL_BRANCH="pr-${PR_NUMBER}-$(slugify "$HEAD_REF")"
FETCH_REF="refs/remotes/origin/pr/${PR_NUMBER}"

ensure_review_checkout() {
  local purpose="$1"
  local current_head current_branch
  current_head="$(git -C "$WORKSPACE" rev-parse HEAD)"
  current_branch="$(git -C "$WORKSPACE" symbolic-ref --quiet --short HEAD || true)"
  if [[ "$current_head" != "$HEAD_SHA" || "$current_branch" != "$LOCAL_BRANCH" ]]; then
    echo "$purpose: selecting $RESOLVED_REPO#$PR_NUMBER on $LOCAL_BRANCH"
    git -C "$WORKSPACE" switch -C "$LOCAL_BRANCH" "$HEAD_SHA"
  fi
  current_head="$(git -C "$WORKSPACE" rev-parse HEAD)"
  current_branch="$(git -C "$WORKSPACE" symbolic-ref --quiet --short HEAD || true)"
  if [[ "$current_head" != "$HEAD_SHA" || "$current_branch" != "$LOCAL_BRANCH" ]]; then
    echo "could not prepare the owned worktree for $RESOLVED_REPO#$PR_NUMBER" >&2
    git -C "$WORKSPACE" status --short --branch >&2
    return 1
  fi
  echo "branch:      $current_branch"
  echo "head:        $(git -C "$WORKSPACE" rev-parse --short=12 HEAD)"
}

CHECKOUT_READY=0
finish_review_run() {
  local status=$?
  trap - EXIT
  if [[ "$CHECKOUT_READY" == 1 ]]; then
    echo
    if [[ "$status" == 0 ]]; then
      phase_start "Final checkout"
    else
      echo "Final checkout after failure:"
    fi
    if ensure_review_checkout "final state"; then
      echo "ready:       $RESOLVED_REPO#$PR_NUMBER"
      if [[ "$status" == 0 ]]; then phase_end; fi
    else
      status=1
    fi
  fi
  exit "$status"
}
trap finish_review_run EXIT


echo "repo:        $RESOLVED_REPO"
echo "number:      $PR_NUMBER"
echo "title:       $PR_TITLE"
echo "url:         $PR_URL"
echo "head ref:    $HEAD_REF"
echo "base/head:   ${BASE_SHA:0:12}...${HEAD_SHA:0:12}"
echo "updated at:  $UPDATED_AT"
echo

tracked_status="$(git -C "$WORKSPACE" status --porcelain --untracked-files=no --ignore-submodules=all)"
if [[ -n "$tracked_status" && "${ALLOW_DIRTY:-0}" != "1" ]]; then
  echo "tracked local changes in $WORKSPACE; refusing to switch PRs." >&2
  echo "$tracked_status" >&2
  echo "Set ALLOW_DIRTY=1 to override." >&2
  exit 1
fi

untracked_status="$(git -C "$WORKSPACE" status --porcelain --untracked-files=normal | sed -n 's/^?? //p')"
if [[ -n "$untracked_status" ]]; then
  echo "warning: untracked files/directories in workspace; leaving them alone:" >&2
  while IFS= read -r path; do
    printf '  %s\n' "$path" >&2
  done <<<"$untracked_status"
  echo >&2
fi

phase_end
phase_start "Checkout"
echo "fetching origin $BASE_REF and pull/$PR_NUMBER/head"
git -C "$WORKSPACE" fetch origin "$BASE_REF" "+pull/${PR_NUMBER}/head:${FETCH_REF}"
ensure_review_checkout "checkout"
CHECKOUT_READY=1
for required_commit in "$BASE_SHA" "$HEAD_SHA"; do
  if ! git -C "$WORKSPACE" cat-file -e "${required_commit}^{commit}"; then
    echo "missing required review commit: $required_commit" >&2
    exit 1
  fi
done
if [[ "${UPDATE_SUBMODULES:-0}" == 1 ]]; then
  git -C "$WORKSPACE" submodule update --init
else
  echo "submodules: skipped (set UPDATE_SUBMODULES=1 to update)"
fi
echo "workspace:   $WORKSPACE"
echo

phase_end
phase_start "Build"
if [[ "$NO_BUILD" == 1 ]]; then
  echo "skipped"
else
  setup_rocjitsu_env
  for preset in "${CMAKE_BUILD_PRESETS[@]}"; do
    phase_start "$preset"
    phase_start "Configure"
    echo "preset:      $preset"
    echo "configure:   (cd $ROCJITSU_SOURCE && cmake --preset $preset)"
    (
      cd "$ROCJITSU_SOURCE" || exit
      cmake --preset "$preset" "${CMAKE_CONFIGURE_ARGS_ARRAY[@]}"
    ) || {
      code=$?
      echo "preset: $preset (configure failed)" >&2
      exit "$code"
    }
    phase_end
    phase_start "Compile"
    echo "build:       (cd $ROCJITSU_SOURCE && cmake --build --preset $preset --target $CMAKE_BUILD_TARGET)"
    (
      cd "$ROCJITSU_SOURCE" || exit
      cmake --build --preset "$preset" --target "$CMAKE_BUILD_TARGET"
    ) || {
      code=$?
      echo "preset: $preset (build failed)" >&2
      exit "$code"
    }
    phase_end
    phase_end
  done
fi
echo

phase_end
phase_start "Python tests"
PYTEST_STATUS="skipped"
PYTEST_EXIT_CODE=0
if [[ "$NO_PYTEST" == 1 ]]; then
  echo "skipped"
else
  setup_rocjitsu_env
  PYTEST_STATUS="passed"
  if [[ ! -d "$PYTEST_WORKDIR" ]]; then
    PYTEST_STATUS="failed (missing workdir, non-blocking)"
    PYTEST_EXIT_CODE=1
    echo "missing pytest work directory: $PYTEST_WORKDIR" >&2
  elif [[ -n "${PYTEST_CMD:-}" ]]; then
    echo "command:     (cd $PYTEST_WORKDIR && $PYTEST_CMD)"
    (
      cd "$PYTEST_WORKDIR"
      bash -lc "$PYTEST_CMD"
    ) || PYTEST_EXIT_CODE=$?
  else
    read -r -a PYTEST_ARGS_ARRAY <<<"$PYTEST_ARGS_SPEC"
    if ((${#PYTEST_ARGS_ARRAY[@]} == 0)); then
      echo "no pytest arguments requested" >&2
      exit 2
    fi
    echo "command:     (cd $PYTEST_WORKDIR && $PYTEST_PYTHON -m pytest ${PYTEST_ARGS_ARRAY[*]})"
    (
      cd "$PYTEST_WORKDIR"
      "$PYTEST_PYTHON" -m pytest "${PYTEST_ARGS_ARRAY[@]}"
    ) || PYTEST_EXIT_CODE=$?
  fi

  if ((PYTEST_EXIT_CODE != 0)); then
    PYTEST_STATUS="failed (exit $PYTEST_EXIT_CODE, non-blocking)"
    echo "pytest:      failed with exit $PYTEST_EXIT_CODE; continuing to launch review agents"
    if [[ "${PYTEST_REQUIRED:-0}" == 1 ]]; then
      echo "PYTEST_REQUIRED=1 is set; stopping before session launch" >&2
      exit "$PYTEST_EXIT_CODE"
    fi
  else
    echo "pytest:      passed"
  fi
fi
echo

phase_end
phase_start "Prepare review session"
ensure_review_checkout "session setup"
DRY_RUN="$("$PR_BIN" start "$PR_URL" --config "$CONFIG" \
  --base "$BASE_SHA" --topic "$HEAD_SHA" --dry-run --no-launch)"
SESSION="$(awk '/^Session:/ {print $2; exit}' <<<"$DRY_RUN")"
if [[ -z "$SESSION" ]]; then
  echo "could not resolve session path from peanut-review dry-run" >&2
  echo "$DRY_RUN" >&2
  exit 1
fi
EXPECTED_SESSION_WORKSPACE="$(
  sed -n '/^Workspace:/ { s/^Workspace:[[:space:]]*//; p; q; }' <<<"$DRY_RUN"
)"
if [[ -z "$EXPECTED_SESSION_WORKSPACE" ]]; then
  echo "could not resolve session workspace from peanut-review dry-run" >&2
  echo "$DRY_RUN" >&2
  exit 1
fi
EXPECTED_REPO_RELATIVE="$(jq -r '.repoRelative' "$CONFIG")"
if [[ "$EXPECTED_REPO_RELATIVE" == "." ]]; then
  EXPECTED_REPO_RELATIVE=""
fi

SESSION_RUN_LOCK="${SESSION}.run.lock"
echo "session wait: $SESSION"
exec {SESSION_LOCK_FD}>"$SESSION_RUN_LOCK"
flock "$SESSION_LOCK_FD"
echo "session use:  $SESSION"

SESSION_EXISTED=0
if [[ -f "$SESSION/session.json" ]]; then
  SESSION_EXISTED=1
fi

START_OUTPUT="$("$PR_BIN" start "$PR_URL" --config "$CONFIG" \
  --base "$BASE_SHA" --topic "$HEAD_SHA" --reuse --sync --no-launch)"
printf '%s\n' "$START_OUTPUT"

# Session reads must survive queue cleanup of this execution worktree.
"$PR_BIN" --session "$SESSION" retain-git

if ! jq -e \
  --arg repo "$RESOLVED_REPO" \
  --argjson number "$PR_NUMBER" \
  --arg base "$BASE_SHA" \
  --arg head "$HEAD_SHA" \
  --arg workspace "$EXPECTED_SESSION_WORKSPACE" \
  --arg repo_relative "$EXPECTED_REPO_RELATIVE" \
  '.workspace == $workspace
   and .repo_relative == $repo_relative
   and .base_ref == $base
   and .topic_ref == $head
   and .current_head == $head
   and .diff_commands == ["git diff \($base)...\($head)"]
   and .github.repo == $repo
   and .github.number == $number
   and .github.base_sha == $base
   and .github.head_sha == $head' \
  "$SESSION/session.json" >/dev/null; then
  echo "peanut-review session does not match the pinned PR snapshot" >&2
  "$PR_BIN" --session "$SESSION" status >&2 || true
  exit 1
fi

LAST_COMMENT_ID="$("$PR_BIN" --session "$SESSION" comments --format json | jq -r '.[-1].id // ""')"

echo "session:     $SESSION"
echo "mode:        $([[ "$SESSION_EXISTED" == 1 ]] && echo reuse/rerun || echo new/launch)"
echo "last comment before launch: ${LAST_COMMENT_ID:-<none>}"
echo

phase_end
if [[ "$NO_LAUNCH" == 1 ]]; then
  echo "Launch skipped"
else
  phase_start "Launch reviewers"
  ensure_review_checkout "agent launch"
  mapfile -t AGENTS < <(
    jq -r '.agents[] | select((.role // "reviewer") != "curator") | .name' \
      "$SESSION/session.json"
  )
  if ((${#AGENTS[@]} == 0)); then
    echo "no reviewer agents configured in $SESSION/session.json" >&2
    exit 1
  fi
  MAX_REVIEWER_FAILURES=$((${#AGENTS[@]} / 2))
  if [[ "$SESSION_EXISTED" == 1 ]]; then
    RERUN_ARGS=()
    for agent in "${AGENTS[@]}"; do
      RERUN_ARGS+=(--agent "$agent")
    done
    "$PR_BIN" --session "$SESSION" rerun "${RERUN_ARGS[@]}"
  else
    "$PR_BIN" --session "$SESSION" launch
  fi
  echo

  phase_end
  phase_start "Review"
  echo "timeout:     $REVIEW_WAIT_TIMEOUT seconds per phase (reviewers, then curator)"
  echo "failures:    allow up to $MAX_REVIEWER_FAILURES of ${#AGENTS[@]} reviewers"
  "$PR_BIN" --session "$SESSION" wait-all round-done \
    --timeout "$REVIEW_WAIT_TIMEOUT" --max-reviewer-failures "$MAX_REVIEWER_FAILURES"
  echo

  phase_end
  phase_start "Final review status"
  "$PR_BIN" --session "$SESSION" status
  phase_end
  echo
fi

phase_start "Write review summary"
cat <<EOF
PR:        $RESOLVED_REPO#$PR_NUMBER
URL:       $PR_URL
Title:     $PR_TITLE
Updated:   $UPDATED_AT
Checkout:  $WORKSPACE
Branch:    $LOCAL_BRANCH
Base/head: ${BASE_SHA:0:12}...${HEAD_SHA:0:12}
Config:    $CONFIG
Source:    $ROCJITSU_SOURCE
Build presets: ${CMAKE_BUILD_PRESETS[*]}
Pytest:    $PYTEST_STATUS :: $([[ "$NO_PYTEST" == 1 ]] && echo skipped || echo "$PYTEST_WORKDIR :: ${PYTEST_CMD:-$PYTEST_PYTHON -m pytest $PYTEST_ARGS_SPEC}")
Session:   $SESSION
Agents:    $(jq -r '[.agents[].name] | join(", ")' "$SESSION/session.json")

Useful commands:
  cd $ROCJITSU_SOURCE
  cmake --preset <preset>
  cmake --build --preset <preset> --target $CMAKE_BUILD_TARGET
  cd $PYTEST_WORKDIR && ${PYTEST_CMD:-$PYTEST_PYTHON -m pytest $PYTEST_ARGS_SPEC}
  ctest --test-dir $BUILD_DIR --output-on-failure
  $PR_BIN --session $SESSION status
  $PR_BIN --session $SESSION inbox
  $PR_BIN --session $SESSION wait-all round-done --timeout 900
  $PR_BIN --session $SESSION kill-agents
  $PR_BIN --session $SESSION comments --since ${LAST_COMMENT_ID:-<last-comment-id>}
  $PR_BIN --session $SESSION comments --unresolved
  $PR_BIN --session $SESSION gh-pull
  $PR_BIN --session $SESSION sync-pr
  $PR_BIN --session $SESSION migrate
  $PR_BIN --session $SESSION gh-push --dry-run

Notes for the next orchestrator:
  - The checkout is refreshed from origin pull/$PR_NUMBER/head, avoiding fork SSH remotes.
  - Existing sessions are explicitly synchronized to the current PR base/head and rerun; new sessions are launched.
  - The wrapper verifies both commit objects, checkout HEAD, and persisted session refs before launching.
  - The wrapper waits for every reviewer to finish, then runs Curator if at least half signaled round-done.
  - More than half failing, unfinished reviewers at the wait deadline, or Curator failure/timeout makes the wrapper exit nonzero.
  - RocJITsu builds from $ROCJITSU_SOURCE using the configured CMake preset list.
  - RocJITsu Python tests run from $PYTEST_WORKDIR with the configured pytest command unless --no-pytest is used.
  - Pytest failures are recorded above but do not block reviewer launch unless PYTEST_REQUIRED=1 is set.
  - The default preset is RelWithDebInfo with CMake's default -DNDEBUG, so assertions are disabled there.
  - The local clang-23-asan-ubsan and clang-23-tsan presets are RelWithDebInfo builds that override the per-config flags to omit -DNDEBUG, so assertions stay enabled.
  - Use the "last comment before launch" id above with comments --since to isolate new reviewer feedback.
EOF

phase_end
