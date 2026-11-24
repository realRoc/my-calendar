#!/usr/bin/env bash
# Wait until the my-calendar PR watcher has published its review for the PR's
# current head SHA, then print where it is and what it concluded.
#
# The watcher is the only publisher: it picks the cross-family reviewer (Codex
# for Claude-written PRs, Claude for Codex-written ones) and posts exactly one
# comment per head SHA. This helper never posts anything.

set -euo pipefail
export PATH="${PATH:-/usr/bin:/bin}:/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin"

PR_URL=""
TIMEOUT_SEC="${PR_REVIEW_WAIT_TIMEOUT_SEC:-3600}"
INTERVAL_SEC="${PR_REVIEW_WAIT_INTERVAL_SEC:-30}"

usage() {
    cat <<'EOF'
Usage:
  wait_for_review.sh --pr-url <github-pr-url> [--timeout <seconds>]

Polls the PR until a watcher review comment for the current head SHA exists
(default timeout 3600s, poll every 30s). Prints:
  REVIEW_HEAD_SHA, REVIEW_COMMENT_URL, REVIEW_REVIEWER (评审模型 line), REVIEW_VERDICT,
  REVIEW_RESULT = pass (✅) | fix (❌/⚠️) | unknown (no recognizable 结论 line)
Exit 0 when found, 3 on timeout (GitHub API errors are retried until then),
4 when the PR is no longer open.
A new commit during the wait restarts the wait for the new head.
EOF
}

while [[ $# -gt 0 ]]; do
    case "$1" in
        --pr-url) PR_URL="${2:-}"; shift 2 ;;
        --timeout) TIMEOUT_SEC="${2:-}"; shift 2 ;;
        -h|--help) usage; exit 0 ;;
        *) echo "ERROR: unknown argument: $1" >&2; usage >&2; exit 2 ;;
    esac
done

if [[ ! "$PR_URL" =~ ^https://github\.com/([^/]+/[^/]+)/pull/([0-9]+)$ ]]; then
    echo "ERROR: --pr-url must be a canonical GitHub PR URL" >&2
    exit 2
fi
repo="${BASH_REMATCH[1]}"
number="${BASH_REMATCH[2]}"
if [[ ! "$TIMEOUT_SEC" =~ ^[1-9][0-9]*$ ]]; then
    echo "ERROR: --timeout must be a positive integer" >&2
    exit 2
fi

# The watcher posts with the user's own gh login; only trust comments by that
# login, since anyone who can comment could paste the markers.
deadline=$(( $(date +%s) + TIMEOUT_SEC ))

# GitHub API hiccups (TLS handshake timeout, EOF) are common on this network.
# Treat a failed call as "no answer this round" and keep polling until the
# deadline, instead of letting `set -e` end the wait with an unexplained exit 1.
gh_retry_or_timeout() {
    echo "WARN: $1 failed; retrying in ${INTERVAL_SEC}s" >&2
    if [[ "$(date +%s)" -ge "$deadline" ]]; then
        echo "ERROR: GitHub API still failing after ${TIMEOUT_SEC}s." >&2
        exit 3
    fi
    sleep "$INTERVAL_SEC"
}

until login="$(gh api user --jq .login)" && [[ -n "$login" ]]; do
    gh_retry_or_timeout "gh api user"
done

while :; do
    if ! pr_json="$(gh pr view "$PR_URL" --json state,headRefOid)"; then
        gh_retry_or_timeout "gh pr view"
        continue
    fi
    state="$(jq -r '.state' <<<"$pr_json")"
    head="$(jq -r '.headRefOid' <<<"$pr_json")"
    if [[ "$state" != "OPEN" ]]; then
        echo "REVIEW_HEAD_SHA=$head"
        echo "ERROR: PR is $state; no review to wait for" >&2
        exit 4
    fi
    marker="<!-- pr-watcher-head-sha: $head -->"
    if ! comments="$(gh api "repos/$repo/issues/$number/comments" --paginate \
        --jq ".[] | select(.user.login == \"$login\") | select(.body | contains(\"$marker\")) | {url: .html_url, body: .body}")"; then
        gh_retry_or_timeout "gh api comments"
        continue
    fi
    found="$(jq -s 'last // empty' <<<"$comments")"
    if [[ -n "$found" ]]; then
        body="$(jq -r '.body' <<<"$found")"
        echo "REVIEW_HEAD_SHA=$head"
        echo "REVIEW_COMMENT_URL=$(jq -r '.url' <<<"$found")"
        echo "REVIEW_REVIEWER=$(grep -m1 '^\*\*评审模型\*\*' <<<"$body" || echo 'unknown (comment predates cross-model routing)')"
        verdict="$(grep -E '^结论[：:]' <<<"$body" | tail -1 || true)"
        case "$verdict" in
            *❌*|*⚠*) result=fix ;;
            *✅*) result=pass ;;
            *) result=unknown ;;
        esac
        echo "REVIEW_VERDICT=$verdict"
        echo "REVIEW_RESULT=$result"
        exit 0
    fi
    if [[ "$(date +%s)" -ge "$deadline" ]]; then
        echo "REVIEW_HEAD_SHA=$head"
        echo "ERROR: no watcher review for ${head:0:8} after ${TIMEOUT_SEC}s." >&2
        echo "       Check ~/Desktop/my-calendar/logs/pr-watcher.log, or retrigger with light_pr.sh --trigger-only '$PR_URL'." >&2
        exit 3
    fi
    sleep "$INTERVAL_SEC"
done
