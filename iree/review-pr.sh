#!/usr/bin/env bash
set -euo pipefail

usage() {
  cat <<'EOF'
Usage: review-pr.sh [--no-build] [--no-launch] <pr-number|github-pr-url|owner/repo#number>

Checks the PR into ~/iree/review/iree, updates the existing PR checkout if it
is already present, creates/reuses the peanut-review session from the existing
~/iree/review/.peanut-review.json, and launches the configured reviewers. The
script owns the checkout for its full run; concurrent reviews using the same
worktree wait for that ownership to be released.

Environment overrides:
  REVIEW_PARENT   default: $HOME/iree/review
  BUILD_DIR       default: $REVIEW_PARENT/build
  DEFAULT_REPO    default: iree-org/iree
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

REVIEW_PARENT="${REVIEW_PARENT:-$HOME/iree/review}"
WORKSPACE="$REVIEW_PARENT/iree"
BUILD_DIR="${BUILD_DIR:-$REVIEW_PARENT/build}"
CONFIG="$REVIEW_PARENT/.peanut-review.json"
DEFAULT_REPO="${DEFAULT_REPO:-iree-org/iree}"
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
need flock
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
  echo "missing IREE checkout: $WORKSPACE" >&2
  exit 1
fi

WORKTREE_GIT_DIR="$(git -C "$WORKSPACE" rev-parse --absolute-git-dir)"
WORKTREE_LOCK="$WORKTREE_GIT_DIR/peanut-review-worktree.lock"
echo "== Worktree ownership =="
echo "waiting:     $WORKSPACE"
exec {WORKTREE_LOCK_FD}>"$WORKTREE_LOCK"
flock "$WORKTREE_LOCK_FD"
echo "acquired:    $WORKSPACE"
echo

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
    echo "== Final checkout =="
    if ensure_review_checkout "final state"; then
      echo "ready:       $RESOLVED_REPO#$PR_NUMBER"
    else
      status=1
    fi
  fi
  exit "$status"
}
trap finish_review_run EXIT

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
ensure_review_checkout "checkout"
CHECKOUT_READY=1
for required_commit in "$BASE_SHA" "$HEAD_SHA"; do
  if ! git -C "$WORKSPACE" cat-file -e "${required_commit}^{commit}"; then
    echo "missing required review commit: $required_commit" >&2
    exit 1
  fi
done
git -C "$WORKSPACE" submodule update --init
echo "workspace:   $WORKSPACE"
echo

echo "== Build =="
if [[ "$NO_BUILD" == 1 ]]; then
  echo "skipped"
else
  if [[ ! -d "$BUILD_DIR" ]]; then
    echo "missing build directory: $BUILD_DIR" >&2
    exit 1
  fi
  echo "running: ninja -C $BUILD_DIR all iree-test-deps"
  ninja -C "$BUILD_DIR" all iree-test-deps
fi
echo

echo "== Session =="
ensure_review_checkout "session setup"
DRY_RUN="$("$PR_BIN" start "$PR_URL" --config "$CONFIG" \
  --base "$BASE_SHA" --topic "$HEAD_SHA" --dry-run --no-launch)"
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

START_OUTPUT="$("$PR_BIN" start "$PR_URL" --config "$CONFIG" \
  --base "$BASE_SHA" --topic "$HEAD_SHA" --reuse --sync --no-launch)"
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
  ensure_review_checkout "agent launch"
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
Session:   $SESSION
Agents:    $(jq -r '[.agents[].name] | join(", ")' "$SESSION/session.json")

Useful commands:
  ninja -C $BUILD_DIR all iree-test-deps
  $PR_BIN --session $SESSION status
  $PR_BIN --session $SESSION inbox
  $PR_BIN --session $SESSION wait-all round-done --timeout 900
  $PR_BIN --session $SESSION kill-agents
  $PR_BIN --session $SESSION comments --since ${LAST_COMMENT_ID:-<last-comment-id>}
  $PR_BIN --session $SESSION comments --unresolved
  $PR_BIN --session $SESSION gh-pull
  $PR_BIN --session $SESSION sync-pr
  $PR_BIN --session $SESSION gh-push --dry-run

Notes for the next orchestrator:
  - The checkout is refreshed from origin pull/$PR_NUMBER/head, avoiding fork SSH remotes.
  - Session refs are pinned to exact PR commits and changed only by explicit synchronization.
  - Existing sessions are synchronized to the current PR snapshot and rerun; new sessions are launched.
  - Use the "last comment before launch" id above with comments --since to isolate new reviewer feedback.
EOF
