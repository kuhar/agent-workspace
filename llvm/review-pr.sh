#!/usr/bin/env bash
set -euo pipefail

usage() {
  cat <<'EOF'
Usage: review-pr.sh [--no-build] [--no-launch] <pr-number|github-pr-url|owner/repo#number>

Checks the PR into ~/llvm/review/llvm-project, updates the existing PR checkout
if it is already present, creates/reuses the peanut-review session from the
existing ~/llvm/review/.peanut-review.json, and launches the configured
reviewers.

Environment overrides:
  REVIEW_PARENT   default: $HOME/llvm/review
  WORKSPACE       default: $REVIEW_PARENT/llvm-project
  BUILD_DIR       default: $REVIEW_PARENT/build
  BUILD_TARGETS   default: check-mlir
  DEFAULT_REPO    default: llvm/llvm-project
  FETCH_REMOTE    default: origin
  PR_BIN          default: $HOME/jakub-env/agent-workspace/tools/peanut-review/bin/peanut-review
  ALLOW_DIRTY=1   allow switching with tracked local changes
EOF
}

NO_LAUNCH=0
NO_BUILD=0
PR_SPEC=""
while (($#)); do
  case "$1" in
    --no-build)
      NO_BUILD=1
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

REVIEW_PARENT="${REVIEW_PARENT:-$HOME/llvm/review}"
WORKSPACE="${WORKSPACE:-$REVIEW_PARENT/llvm-project}"
BUILD_DIR="${BUILD_DIR:-$REVIEW_PARENT/build}"
BUILD_TARGETS="${BUILD_TARGETS:-check-mlir}"
CONFIG="$REVIEW_PARENT/.peanut-review.json"
DEFAULT_REPO="${DEFAULT_REPO:-llvm/llvm-project}"
FETCH_REMOTE="${FETCH_REMOTE:-origin}"
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

need gh
need git
need jq
need sed
need curl
if [[ "$NO_BUILD" != 1 ]]; then
  need ninja
fi

if [[ ! -x "$PR_BIN" ]]; then
  echo "peanut-review CLI not executable: $PR_BIN" >&2
  exit 1
fi
if [[ ! -f "$CONFIG" ]]; then
  echo "missing peanut-review config: $CONFIG" >&2
  exit 1
fi
if ! git -C "$WORKSPACE" rev-parse --show-toplevel >/dev/null 2>&1; then
  echo "missing LLVM checkout: $WORKSPACE" >&2
  exit 1
fi
if ! git -C "$WORKSPACE" remote get-url "$FETCH_REMOTE" >/dev/null 2>&1; then
  echo "missing git remote '$FETCH_REMOTE' in $WORKSPACE" >&2
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
FETCH_REF="refs/remotes/${FETCH_REMOTE}/pr/${PR_NUMBER}"

echo "== PR =="
echo "repo:        $RESOLVED_REPO"
echo "number:      $PR_NUMBER"
echo "title:       $PR_TITLE"
echo "url:         $PR_URL"
echo "head ref:    $HEAD_REF"
echo "base/head:   ${BASE_SHA:0:12}...${HEAD_SHA:0:12}"
echo "updated at:  $UPDATED_AT"
echo

tracked_status="$(git -C "$WORKSPACE" status --porcelain --untracked-files=no)"
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
echo "fetching $FETCH_REMOTE $BASE_REF and pull/$PR_NUMBER/head"
git -C "$WORKSPACE" fetch "$FETCH_REMOTE" "$BASE_REF" "pull/${PR_NUMBER}/head:${FETCH_REF}"
git -C "$WORKSPACE" switch -C "$LOCAL_BRANCH" "$FETCH_REF"
echo "workspace:   $WORKSPACE"
echo "branch:      $LOCAL_BRANCH"
echo "head:        $(git -C "$WORKSPACE" rev-parse --short=12 HEAD)"
echo

echo "== Build =="
if [[ "$NO_BUILD" == 1 ]]; then
  echo "skipped"
else
  if [[ ! -d "$BUILD_DIR" ]]; then
    echo "missing build directory: $BUILD_DIR" >&2
    exit 1
  fi
  read -r -a BUILD_TARGET_ARRAY <<<"$BUILD_TARGETS"
  echo "running: ninja -C $BUILD_DIR ${BUILD_TARGET_ARRAY[*]}"
  ninja -C "$BUILD_DIR" "${BUILD_TARGET_ARRAY[@]}"
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

START_OUTPUT="$("$PR_BIN" start "$PR_URL" --config "$CONFIG" --reuse --no-launch)"
printf '%s\n' "$START_OUTPUT"

"$PR_BIN" --session "$SESSION" migrate --new-head "$HEAD_SHA"

DIFF_STAT="$(git -C "$WORKSPACE" diff --stat "${BASE_SHA}...${HEAD_SHA}")"
tmp_session="$(mktemp)"
jq \
  --arg repo "$RESOLVED_REPO" \
  --argjson number "$PR_NUMBER" \
  --arg url "$PR_URL" \
  --arg title "$PR_TITLE" \
  --arg head_ref "$HEAD_REF" \
  --arg base "$BASE_SHA" \
  --arg head "$HEAD_SHA" \
  --arg stat "$DIFF_STAT" \
  '.base_ref=$base
   | .topic_ref=$head
   | .current_head=$head
   | .diff_commands=["git diff \($base)...\($head)"]
   | .diff_stat=$stat
   | .github.repo=$repo
   | .github.number=$number
   | .github.url=$url
   | .github.title=$title
   | .github.head_ref_name=$head_ref
   | .github.base_sha=$base
   | .github.head_sha=$head' \
  "$SESSION/session.json" >"$tmp_session"
mv "$tmp_session" "$SESSION/session.json"

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
Build dir: $BUILD_DIR
Build:     ninja -C $BUILD_DIR $BUILD_TARGETS
Session:   $SESSION
Agents:    $(jq -r '[.agents[].name] | join(", ")' "$SESSION/session.json")

Useful commands:
  ninja -C $BUILD_DIR $BUILD_TARGETS
  $PR_BIN --session $SESSION status
  $PR_BIN --session $SESSION inbox
  $PR_BIN --session $SESSION wait-all round-done --timeout 900
  $PR_BIN --session $SESSION kill-agents
  $PR_BIN --session $SESSION comments --since ${LAST_COMMENT_ID:-<last-comment-id>}
  $PR_BIN --session $SESSION comments --unresolved
  $PR_BIN --session $SESSION gh-pull
  $PR_BIN --session $SESSION migrate
  $PR_BIN --session $SESSION gh-push --dry-run

Notes for the next orchestrator:
  - The checkout is refreshed from $FETCH_REMOTE pull/$PR_NUMBER/head, avoiding fork SSH remotes.
  - Existing sessions are migrated to the current PR head and rerun; new sessions are launched.
  - Use the "last comment before launch" id above with comments --since to isolate new reviewer feedback.
EOF
