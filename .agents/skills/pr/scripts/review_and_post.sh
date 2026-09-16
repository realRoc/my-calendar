#!/usr/bin/env bash
# Generate, post, verify, and record a deterministic Opus PR review.

set -euo pipefail
export PATH="${PATH:-/usr/bin:/bin}:/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin"

readonly SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

PR_URL=""
REPO_ROOT=""
ALREADY_CLAIMED=0
REVIEW_FILE=""
REVIEW_HEAD_SHA_ARG=""
EXTERNAL_REVIEW_FILE=0
COMMENT_FILE=""
claim_acquired=0
comment_exists=0
my_calendar_home=""

usage() {
    cat <<'EOF'
Usage:
  review_and_post.sh --pr-url <github-pr-url> --repo-root <checkout> [--already-claimed]
  review_and_post.sh --pr-url <github-pr-url> --repo-root <checkout> \
    --review-file <validated-review> --review-head-sha <sha> [--already-claimed]

Runs the structured Opus reviewer, verifies the PR SHA, idempotently posts the
canonical GitHub comment, verifies it, and records it in my-calendar.
EOF
}

while [[ $# -gt 0 ]]; do
    case "$1" in
        --pr-url)
            PR_URL="${2:-}"
            shift 2
            ;;
        --repo-root)
            REPO_ROOT="${2:-}"
            shift 2
            ;;
        --already-claimed)
            ALREADY_CLAIMED=1
            shift
            ;;
        --review-file)
            REVIEW_FILE="${2:-}"
            EXTERNAL_REVIEW_FILE=1
            shift 2
            ;;
        --review-head-sha)
            REVIEW_HEAD_SHA_ARG="${2:-}"
            shift 2
            ;;
        -h|--help)
            usage
            exit 0
            ;;
        *)
            echo "ERROR: unknown argument: $1" >&2
            usage >&2
            exit 2
            ;;
    esac
done

if [[ ! "$PR_URL" =~ ^https://github\.com/[^/]+/[^/]+/pull/[0-9]+$ ]]; then
    echo "ERROR: --pr-url must be a canonical GitHub PR URL" >&2
    exit 2
fi
if [[ -z "$REPO_ROOT" || ! -d "$REPO_ROOT/.git" && ! -f "$REPO_ROOT/.git" ]]; then
    echo "ERROR: --repo-root must be a Git checkout or worktree" >&2
    exit 2
fi
if [[ "$EXTERNAL_REVIEW_FILE" -eq 1 && ( ! -s "$REVIEW_FILE" || -z "$REVIEW_HEAD_SHA_ARG" ) ]]; then
    echo "ERROR: --review-file requires a non-empty file and --review-head-sha" >&2
    exit 2
fi
if [[ "$EXTERNAL_REVIEW_FILE" -eq 0 && -n "$REVIEW_HEAD_SHA_ARG" ]]; then
    echo "ERROR: --review-head-sha requires --review-file" >&2
    exit 2
fi
REPO_ROOT="$(cd "$REPO_ROOT" && git rev-parse --show-toplevel)"

need_cmd() {
    if ! command -v "$1" >/dev/null 2>&1; then
        echo "ERROR: required command not found: $1" >&2
        exit 1
    fi
}

need_cmd gh
need_cmd git
need_cmd jq
need_cmd rg

resolve_my_calendar() {
    local candidate=""
    if [[ -n "${MY_CALENDAR_HOME:-}" && -d "$MY_CALENDAR_HOME/scripts" ]]; then
        my_calendar_home="$MY_CALENDAR_HOME"
        return 0
    fi
    for candidate in "$HOME/Desktop/my-calendar" "$HOME/my-calendar" "$HOME/src/my-calendar"; do
        if [[ -d "$candidate/scripts" ]]; then
            my_calendar_home="$candidate"
            return 0
        fi
    done
    return 1
}

release_claim() {
    local python=""
    [[ "$claim_acquired" -eq 1 && "$comment_exists" -eq 0 ]] || return 0
    [[ -n "$my_calendar_home" ]] || return 0
    python="$my_calendar_home/.venv/bin/python"
    [[ -x "$python" ]] || python="python3"
    "$python" "$SCRIPT_DIR/release_current_session_claim.py" \
        --pr-url "$PR_URL" \
        --my-calendar "$my_calendar_home" >&2 || true
}

cleanup() {
    local status=$?
    if [[ "$status" -ne 0 ]]; then
        release_claim
    fi
    if [[ "$EXTERNAL_REVIEW_FILE" -eq 0 && -n "${REVIEW_FILE:-}" ]]; then
        rm -f "$REVIEW_FILE"
    fi
    [[ -n "${COMMENT_FILE:-}" ]] && rm -f "$COMMENT_FILE"
    return "$status"
}
trap cleanup EXIT

if ! resolve_my_calendar; then
    echo "ERROR: my-calendar checkout is required for current-session review state" >&2
    exit 1
fi

cd "$REPO_ROOT"
pr_json="$(gh pr view "$PR_URL" --json url,number,state,headRefOid)"
pr_state="$(jq -r '.state // empty' <<<"$pr_json")"
head_sha="$(jq -r '.headRefOid // empty' <<<"$pr_json")"
pr_number="$(jq -r '.number // empty' <<<"$pr_json")"
owner_repo="$(gh repo view --json nameWithOwner --jq .nameWithOwner)"
if [[ "$pr_state" != "OPEN" || -z "$head_sha" || -z "$pr_number" || -z "$owner_repo" ]]; then
    echo "ERROR: PR is not open or its identity could not be resolved" >&2
    exit 1
fi

find_existing_comment() {
    gh api --paginate "repos/$owner_repo/issues/$pr_number/comments?per_page=100" \
        | jq -sr --arg head "<!-- pr-watcher-head-sha: $head_sha -->" \
            --arg marker '<!-- ai-coauthor: codex; agent: pr_watcher; mode: automated -->' \
            --arg review_marker '<!-- pr-review-model: claude-opus-5; provider: teamorouter -->' '
                [.[][] | select((.body // "") | contains($head) and contains($marker) and contains($review_marker))]
                | sort_by(.created_at) | last | .html_url // empty
            '
}

if [[ "$EXTERNAL_REVIEW_FILE" -eq 1 ]]; then
    review_head_sha="$REVIEW_HEAD_SHA_ARG"
else
    REVIEW_FILE="$(mktemp "${TMPDIR:-/tmp}/pr-review.XXXXXX")"
    review_status="$(bash "$SCRIPT_DIR/review_with_opus.sh" --pr-url "$PR_URL" --output "$REVIEW_FILE")"
    printf '%s\n' "$review_status"
    review_head_sha="$(awk -F= '$1 == "REVIEW_HEAD_SHA" { print $2 }' <<<"$review_status" | tail -1)"
fi
conclusion_count="$(rg -c '^结论：(✅ 可以合并|❌ 暂不可合并)$' "$REVIEW_FILE" || true)"
last_nonempty="$(awk 'NF { line=$0 } END { print line }' "$REVIEW_FILE")"
if [[ "$conclusion_count" != "1" ]] \
    || [[ ! "$last_nonempty" =~ ^结论：(✅\ 可以合并|❌\ 暂不可合并)$ ]] \
    || rg -q 'ai-coauthor:|pr-watcher-head-sha:|pr-review-model:' "$REVIEW_FILE"; then
    echo "ERROR: supplied review file is not canonical publishable output" >&2
    exit 1
fi
if [[ "$review_head_sha" != "$head_sha" ]]; then
    echo "ERROR: reviewer SHA does not match the preflight SHA" >&2
    exit 1
fi

fresh_head_sha="$(gh pr view "$PR_URL" --json headRefOid --jq .headRefOid)"
if [[ "$fresh_head_sha" != "$review_head_sha" ]]; then
    echo "ERROR: PR head moved during review; rerun against the new SHA" >&2
    exit 1
fi

existing_comment_url="$(find_existing_comment)"
if [[ -n "$existing_comment_url" ]]; then
    comment_url="$existing_comment_url"
    comment_exists=1
    comment_action="reused"
else
    if [[ "$ALREADY_CLAIMED" -eq 1 ]]; then
        claim_acquired=1
    else
        claim_output="$(cd "$REPO_ROOT" && bash "$SCRIPT_DIR/light_pr.sh" --claim-only "$PR_URL")"
        printf '%s\n' "$claim_output"
        claim_acquired=1
    fi

    COMMENT_FILE="$(mktemp "${TMPDIR:-/tmp}/pr-comment.XXXXXX")"
    {
        printf '%s\n' '> 🤖 由 Codex 自动生成'
        printf '%s\n' '<!-- ai-coauthor: codex; agent: pr_watcher; mode: automated -->'
        printf '<!-- pr-watcher-head-sha: %s -->\n' "$head_sha"
        printf '%s\n' '<!-- pr-review-model: claude-opus-5; provider: teamorouter -->'
        printf '\n'
        command cat "$REVIEW_FILE"
    } > "$COMMENT_FILE"

    post_output=""
    post_status=0
    post_output="$(gh pr comment "$PR_URL" --body-file "$COMMENT_FILE" 2>&1)" || post_status=$?
    comment_url="$(rg -o 'https://github\.com/[^[:space:]]+#issuecomment-[0-9]+' <<<"$post_output" | tail -1 || true)"
    if [[ "$post_status" -ne 0 || -z "$comment_url" ]]; then
        comment_url="$(find_existing_comment)"
    fi
    if [[ -z "$comment_url" ]]; then
        echo "ERROR: GitHub comment was not created and no idempotent match was found" >&2
        [[ -n "$post_output" ]] && printf '%s\n' "$post_output" >&2
        exit 1
    fi
    comment_exists=1
    comment_action="posted"
fi

comment_id="${comment_url##*issuecomment-}"
comment_body="$(gh api "repos/$owner_repo/issues/comments/$comment_id" --jq .body)"
if [[ "$comment_body" != *"<!-- pr-watcher-head-sha: $head_sha -->"* ]] \
    || [[ "$comment_body" != *'<!-- ai-coauthor: codex; agent: pr_watcher; mode: automated -->'* ]] \
    || [[ "$comment_body" != *'<!-- pr-review-model: claude-opus-5; provider: teamorouter -->'* ]]; then
    echo "ERROR: posted GitHub comment failed canonical marker verification" >&2
    exit 1
fi

record_script="$my_calendar_home/scripts/pr_record_review_trigger.sh"
if [[ ! -x "$record_script" ]]; then
    echo "GITHUB_COMMENT=$comment_action"
    echo "REVIEW_COMMENT_URL=$comment_url"
    echo "MY_CALENDAR_RECORD=missing"
    echo "ERROR: my-calendar record trigger is missing: $record_script" >&2
    exit 1
fi

record_output="$(bash "$record_script" "$PR_URL" "$comment_url" "$REPO_ROOT")"
printf '%s\n' "$record_output"
verdict="$(awk 'NF { line=$0 } END { print line }' "$REVIEW_FILE")"

echo "PR_URL=$PR_URL"
echo "HEAD_SHA=$head_sha"
echo "REVIEW_VERDICT=$verdict"
echo "GITHUB_COMMENT=$comment_action"
echo "REVIEW_COMMENT_URL=$comment_url"
echo "REVIEW_AND_POST=ok"
