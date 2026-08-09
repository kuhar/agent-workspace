#!/usr/bin/env bash
set -euo pipefail

usage() {
  cat <<'EOF'
Usage: review-pr.sh [--no-build] [--no-pytest] [--no-launch] <pr-number|github-pr-url|owner/repo#number>

Checks the PR into the selected review workspace's rocm-systems checkout,
updates the existing PR checkout if it is already present, creates/reuses the
peanut-review session from that workspace's .peanut-review.json, launches the
configured reviewers, then waits for every reviewer and the curator to finish.

Environment overrides:
  REVIEW_PARENT       default: $HOME/rocjitsu/review
  WORKSPACE           default: $REVIEW_PARENT/rocm-systems
  ROCJITSU_SOURCE     default: $WORKSPACE/emulation/rocjitsu
  BUILD_DIR           default: $REVIEW_PARENT/build
  VENV_DIR            default: $REVIEW_PARENT/venv
  CMAKE_PRESETS       default: default gcc-13 clang-23-asan-ubsan clang-23-tsan
  CMAKE_PRESET        legacy single-preset override
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

REVIEW_PARENT="${REVIEW_PARENT:-$HOME/rocjitsu/review}"
WORKSPACE="${WORKSPACE:-$REVIEW_PARENT/rocm-systems}"
ROCJITSU_SOURCE="${ROCJITSU_SOURCE:-$WORKSPACE/emulation/rocjitsu}"
BUILD_DIR="${BUILD_DIR:-$REVIEW_PARENT/build}"
VENV_DIR="${VENV_DIR:-$REVIEW_PARENT/venv}"
CONFIG="$REVIEW_PARENT/.peanut-review.json"
DEFAULT_CMAKE_PRESETS="default gcc-13 clang-23-asan-ubsan clang-23-tsan"
CMAKE_PRESET_SPEC="${CMAKE_PRESETS:-${CMAKE_PRESET:-$DEFAULT_CMAKE_PRESETS}}"
CMAKE_PRESET_SPEC="${CMAKE_PRESET_SPEC//,/ }"
read -r -a CMAKE_BUILD_PRESETS <<<"$CMAKE_PRESET_SPEC"
if ((${#CMAKE_BUILD_PRESETS[@]} == 0)); then
  echo "no CMake presets requested" >&2
  exit 2
fi
CMAKE_BUILD_TARGET="${CMAKE_BUILD_TARGET:-all}"
PYTEST_WORKDIR="${PYTEST_WORKDIR:-$ROCJITSU_SOURCE/lib/python}"
PYTEST_ARGS_SPEC="${PYTEST_ARGS:-amdisa/tests/ -x}"
PYTEST_PYTHON="${PYTEST_PYTHON:-python}"
DEFAULT_REPO="${DEFAULT_REPO:-ROCm/rocm-systems}"
PR_BIN="${PR_BIN:-$HOME/jakub-env/agent-workspace/tools/peanut-review/bin/peanut-review}"

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

need gh
need git
need jq
need sed
need curl
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
LOCAL_BRANCH="pr-${PR_NUMBER}-$(slugify "$HEAD_REF")"
FETCH_REF="refs/remotes/origin/pr/${PR_NUMBER}"

echo "== PR =="
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

echo "== Checkout =="
echo "fetching origin $BASE_REF and pull/$PR_NUMBER/head"
git -C "$WORKSPACE" fetch origin "$BASE_REF" "+pull/${PR_NUMBER}/head:${FETCH_REF}"
git -C "$WORKSPACE" switch -C "$LOCAL_BRANCH" "$FETCH_REF"
CHECKED_OUT_HEAD="$(git -C "$WORKSPACE" rev-parse HEAD)"
if [[ "$CHECKED_OUT_HEAD" != "$HEAD_SHA" ]]; then
  echo "checked-out HEAD does not match GitHub PR head:" >&2
  echo "  checkout: $CHECKED_OUT_HEAD" >&2
  echo "  GitHub:   $HEAD_SHA" >&2
  exit 1
fi
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
echo "branch:      $LOCAL_BRANCH"
echo "head:        $(git -C "$WORKSPACE" rev-parse --short=12 HEAD)"
echo

echo "== Build =="
if [[ "$NO_BUILD" == 1 ]]; then
  echo "skipped"
else
  setup_rocjitsu_env
  for preset in "${CMAKE_BUILD_PRESETS[@]}"; do
    echo "preset:      $preset"
    echo "configure:   (cd $ROCJITSU_SOURCE && cmake --preset $preset)"
    (
      cd "$ROCJITSU_SOURCE"
      cmake --preset "$preset"
    )
    echo "build:       (cd $ROCJITSU_SOURCE && cmake --build --preset $preset --target $CMAKE_BUILD_TARGET)"
    (
      cd "$ROCJITSU_SOURCE"
      cmake --build --preset "$preset" --target "$CMAKE_BUILD_TARGET"
    )
  done
fi
echo

echo "== Pytest =="
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

echo "== Session =="
DRY_RUN="$("$PR_BIN" start "$PR_URL" --config "$CONFIG" --dry-run --no-launch)"
SESSION="$(awk '/^Session:/ {print $2; exit}' <<<"$DRY_RUN")"
if [[ -z "$SESSION" ]]; then
  echo "could not resolve session path from peanut-review dry-run" >&2
  echo "$DRY_RUN" >&2
  exit 1
fi

SESSION_EXISTED=0
if [[ -f "$SESSION/session.json" ]]; then
  SESSION_EXISTED=1
fi

START_OUTPUT="$("$PR_BIN" start "$PR_URL" --config "$CONFIG" --reuse --sync --no-launch)"
printf '%s\n' "$START_OUTPUT"

if ! jq -e \
  --arg repo "$RESOLVED_REPO" \
  --argjson number "$PR_NUMBER" \
  --arg base "$BASE_SHA" \
  --arg head "$HEAD_SHA" \
  '.base_ref == $base
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

if [[ "$NO_LAUNCH" == 1 ]]; then
  echo "== Launch skipped =="
else
  echo "== Launch =="
  if [[ "$SESSION_EXISTED" == 1 ]]; then
    mapfile -t AGENTS < <(
      jq -r '.agents[] | select((.role // "reviewer") != "curator") | .name' \
        "$SESSION/session.json"
    )
    if ((${#AGENTS[@]} == 0)); then
      echo "no reviewer agents configured in $SESSION/session.json" >&2
      exit 1
    fi
    RERUN_ARGS=()
    for agent in "${AGENTS[@]}"; do
      RERUN_ARGS+=(--agent "$agent")
    done
    "$PR_BIN" --session "$SESSION" rerun "${RERUN_ARGS[@]}"
  else
    "$PR_BIN" --session "$SESSION" launch
  fi
  echo

  echo "== Wait for review completion =="
  echo "timeout:     $REVIEW_WAIT_TIMEOUT seconds per phase (reviewers, then curator)"
  "$PR_BIN" --session "$SESSION" wait-all round-done \
    --timeout "$REVIEW_WAIT_TIMEOUT"
  echo

  echo "== Final review status =="
  "$PR_BIN" --session "$SESSION" status
  echo
fi

echo "== Orchestrator context =="
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
  - The wrapper blocks until all reviewers finish, then wait-all launches and waits for Curator.
  - A reviewer/Curator failure or timeout makes the wrapper exit nonzero so callers can serialize review jobs safely.
  - RocJITsu builds from $ROCJITSU_SOURCE using the configured CMake preset list.
  - RocJITsu Python tests run from $PYTEST_WORKDIR with the configured pytest command unless --no-pytest is used.
  - Pytest failures are recorded above but do not block reviewer launch unless PYTEST_REQUIRED=1 is set.
  - The default preset is RelWithDebInfo with CMake's default -DNDEBUG, so assertions are disabled there.
  - The local gcc-13, clang-23-asan-ubsan, and clang-23-tsan presets are RelWithDebInfo builds that override the per-config flags to omit -DNDEBUG, so assertions stay enabled.
  - Use the "last comment before launch" id above with comments --since to isolate new reviewer feedback.
EOF
