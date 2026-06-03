#!/usr/bin/env bash
# Trigger my-calendar PR review after a local tool creates a GitHub PR.
#
# Args:
#   $1 = PR URL or text containing a GitHub PR URL
#   $2 = optional origin cwd

set -u

PR_INPUT="${1:-}"
ORIGIN_CWD="${2:-}"

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
REVIEW_TRIGGER="$ROOT/scripts/pr_review_trigger.sh"
LOG_DIR="$HOME/.config/my-calendar/git-hooks/logs"
mkdir -p "$LOG_DIR"

log() {
    printf '[%s] %s\n' "$(date '+%Y-%m-%d %H:%M:%S')" "$*"
}

if [[ -z "$PR_INPUT" ]]; then
    log "ERROR: pr_created_trigger called without PR URL"
    exit 1
fi

PR_URL=""
if [[ "$PR_INPUT" =~ (https://github\.com/[^[:space:]]+/[^[:space:]]+/pull/[0-9]+) ]]; then
    PR_URL="${BASH_REMATCH[1]}"
else
    log "skip: no GitHub PR URL found in pr-created input: $PR_INPUT"
    exit 0
fi

if [[ -z "$ORIGIN_CWD" ]]; then
    ORIGIN_CWD="$(git rev-parse --show-toplevel 2>/dev/null || pwd)"
fi

if [[ ! -x "$REVIEW_TRIGGER" ]]; then
    log "ERROR: review trigger not executable: $REVIEW_TRIGGER"
    exit 1
fi

log "pr-created detected: $PR_URL  (origin_cwd=${ORIGIN_CWD:-<none>})"
"$REVIEW_TRIGGER" --source pr-created "$PR_URL" "$ORIGIN_CWD"
