#!/usr/bin/env bash
# Debounced launcher for my-calendar PR reviews.
#
# Usage:
#   pr_review_trigger.sh [--source <name>] <pr-url> [origin-cwd]
#
# This is the shared handoff into pr_watcher.py. It verifies that the PR targets
# the repo's default branch, debounces duplicate triggers for the same PR+SHA,
# then starts `pr_watcher.py --force` in the background.

set -u

SOURCE="manual"
if [[ "${1:-}" == "--source" ]]; then
    SOURCE="${2:-manual}"
    shift 2
fi

PR_URL="${1:-}"
ORIGIN_CWD="${2:-}"

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
PYTHON="$ROOT/.venv/bin/python"
WATCHER="$ROOT/scripts/pr_watcher.py"
LOG_DIR="$HOME/.config/my-calendar/git-hooks/logs"
DEBOUNCE_DIR="$HOME/.config/my-calendar/git-hooks/review-triggers"
LOG_FILE="$LOG_DIR/trigger.log"
DEBOUNCE_SECONDS="${MY_CALENDAR_PR_TRIGGER_DEBOUNCE_SECONDS:-180}"
DEBOUNCE_LOCK_STALE_SECONDS="${MY_CALENDAR_PR_TRIGGER_LOCK_STALE_SECONDS:-30}"
mkdir -p "$LOG_DIR" "$DEBOUNCE_DIR"

log() {
    printf '[%s] %s\n' "$(date '+%Y-%m-%d %H:%M:%S')" "$*"
}

if [[ -z "$PR_URL" ]]; then
    log "ERROR: pr_review_trigger called without PR URL (source=$SOURCE)"
    exit 1
fi

if [[ "$PR_URL" != https://github.com/*/*/pull/* ]]; then
    log "skip: unsupported PR URL for my-calendar review trigger: $PR_URL"
    exit 0
fi

if [[ ! -x "$PYTHON" ]]; then
    log "ERROR: python venv not executable: $PYTHON"
    exit 1
fi
if [[ ! -f "$WATCHER" ]]; then
    log "ERROR: pr_watcher missing: $WATCHER"
    exit 1
fi

owner_repo_number="${PR_URL#https://github.com/}"
owner="${owner_repo_number%%/*}"
rest="${owner_repo_number#*/}"
repo="${rest%%/*}"
number="${owner_repo_number##*/}"

meta=$(gh api graphql -f query='
    query($owner: String!, $name: String!, $number: Int!) {
      repository(owner: $owner, name: $name) {
        defaultBranchRef { name }
        pullRequest(number: $number) {
          baseRefName
          headRefOid
          url
        }
      }
    }' \
    -f owner="$owner" \
    -f name="$repo" \
    -F number="$number" 2>/dev/null) || true

base=$(echo "$meta" | jq -r '.data.repository.pullRequest.baseRefName // ""' 2>/dev/null)
default=$(echo "$meta" | jq -r '.data.repository.defaultBranchRef.name // ""' 2>/dev/null)
head_sha=$(echo "$meta" | jq -r '.data.repository.pullRequest.headRefOid // ""' 2>/dev/null)
canonical_url=$(echo "$meta" | jq -r '.data.repository.pullRequest.url // ""' 2>/dev/null)

if [[ -z "$base" || -z "$default" || -z "$head_sha" ]]; then
    log "skip: could not resolve base/default/head for $PR_URL (source=$SOURCE)"
    exit 0
fi
if [[ "$base" != "$default" ]]; then
    log "skip: base=$base ≠ default=$default for $PR_URL (source=$SOURCE)"
    exit 0
fi
if [[ -n "$canonical_url" && "$canonical_url" != "null" ]]; then
    PR_URL="$canonical_url"
fi

stamp_key=$(printf '%s@%s' "$PR_URL" "$head_sha" | shasum -a 256 | awk '{print $1}')
stamp_file="$DEBOUNCE_DIR/$stamp_key.stamp"
stamp_lock_dir="$DEBOUNCE_DIR/$stamp_key.lock"
now=$(date +%s)
if [[ -d "$stamp_lock_dir" ]]; then
    lock_mtime=$(stat -f %m "$stamp_lock_dir" 2>/dev/null || stat -c %Y "$stamp_lock_dir" 2>/dev/null || echo 0)
    lock_age=$((now - lock_mtime))
    if [[ "$lock_age" -ge "$DEBOUNCE_LOCK_STALE_SECONDS" ]]; then
        rmdir "$stamp_lock_dir" 2>/dev/null || true
    fi
fi
lock_acquired=0
for _attempt in 1 2 3 4 5 6 7 8 9 10; do
    if mkdir "$stamp_lock_dir" 2>/dev/null; then
        lock_acquired=1
        break
    fi
    sleep 0.1
done
if [[ "$lock_acquired" -ne 1 ]]; then
    log "skip: debounce lock busy for $PR_URL sha=${head_sha:0:8} source=$SOURCE"
    exit 0
fi
cleanup_lock() {
    rmdir "$stamp_lock_dir" 2>/dev/null || true
}
trap cleanup_lock EXIT

if [[ -f "$stamp_file" ]]; then
    mtime=$(stat -f %m "$stamp_file" 2>/dev/null || stat -c %Y "$stamp_file" 2>/dev/null || echo 0)
    age=$((now - mtime))
    if [[ "$age" -ge 0 && "$age" -lt "$DEBOUNCE_SECONDS" ]]; then
        log "skip duplicate review trigger: $PR_URL sha=${head_sha:0:8} age=${age}s source=$SOURCE"
        exit 0
    fi
fi
printf 'source=%s\npr_url=%s\nhead_sha=%s\ncreated_at=%s\n' \
    "$SOURCE" "$PR_URL" "$head_sha" "$(date '+%Y-%m-%d %H:%M:%S')" > "$stamp_file"

log "triggering pr_watcher --force $PR_URL  (base=$base, sha=${head_sha:0:8}, source=$SOURCE, origin_cwd=${ORIGIN_CWD:-<none>})"
(
    if [[ -n "$ORIGIN_CWD" && -d "$ORIGIN_CWD" ]]; then
        "$PYTHON" "$WATCHER" --force "$PR_URL" --origin-cwd "$ORIGIN_CWD"
    else
        "$PYTHON" "$WATCHER" --force "$PR_URL"
    fi
    rc=$?
    if [[ "$rc" -eq 0 ]]; then
        log "  → pr_watcher done for $PR_URL sha=${head_sha:0:8} source=$SOURCE"
    else
        rm -f "$stamp_file"
        log "  → pr_watcher exited non-zero ($rc) for $PR_URL sha=${head_sha:0:8} source=$SOURCE"
    fi
) >>"$LOG_FILE" 2>&1 </dev/null &

log "  → pr_watcher launched pid=$!"
