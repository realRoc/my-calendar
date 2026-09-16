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

# Idempotency keys on "a review this account already published for this SHA".
# The markers alone cannot carry that meaning: they are plain text in a comment
# body, so anyone able to comment on the PR can paste a head-SHA marker and have
# their own text adopted as this session's review and written into my-calendar.
# The author is what actually distinguishes our comment, so resolve it and match
# on it rather than trusting the markers by themselves.
current_login="$(gh api user --jq .login)"
if [[ -z "$current_login" ]]; then
    echo "ERROR: could not resolve the authenticated GitHub login" >&2
    exit 1
fi

current_head_sha() {
    gh pr view "$PR_URL" --json headRefOid --jq .headRefOid
}

# The preflight check above runs before the claim, and claiming costs a network
# round trip, so the head can move in between. Re-checking closes that window:
# a review published against a superseded SHA reads as the current verdict to
# every later reader, including anything scanning the SHA marker.
assert_head_unchanged() {
    local where="$1"
    local now_sha
    now_sha="$(current_head_sha)"
    if [[ "$now_sha" != "$review_head_sha" ]]; then
        echo "ERROR: PR head moved $where (${review_head_sha:0:8} → ${now_sha:0:8}); rerun against the new SHA" >&2
        return 1
    fi
    return 0
}

# Match only comments authored by the authenticated account, keyed on the
# canonical orchestrator marker plus the head SHA.
#
# The model marker is deliberately NOT part of the key. It records which
# reviewer produced a comment, not whether the comment is ours, and requiring it
# makes a canonical comment from the Codex watcher invisible — which is exactly
# how a second review for the same SHA got posted and then rejected at the record
# step. Including it here would reintroduce that duplicate.
find_existing_comment() {
    gh api --paginate "repos/$owner_repo/issues/$pr_number/comments?per_page=100" \
        | jq -sr --arg head "<!-- pr-watcher-head-sha: $head_sha -->" \
            --arg marker '<!-- ai-coauthor: codex; agent: pr_watcher; mode: automated -->' \
            --arg login "$current_login" '
                [.[][] | select(
                    (.user.login // "") == $login
                    and ((.body // "") | contains($head) and contains($marker))
                )]
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

    # Claiming is a network round trip; the head may have moved since preflight.
    assert_head_unchanged "during claim" || exit 1

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

    # Publishing is another round trip, so re-check once more. A comment that
    # landed for a superseded SHA still carries this SHA's marker, so it looks
    # authoritative to every later reader; retract it instead of leaving a
    # stale verdict standing. Only a comment created by this run is retracted —
    # an idempotently reused one predates the claim and is not ours to delete.
    if ! assert_head_unchanged "during publish"; then
        if gh api -X DELETE "repos/$owner_repo/issues/comments/${comment_url##*issuecomment-}" >/dev/null 2>&1; then
            echo "retracted stale review comment: $comment_url" >&2
        else
            echo "ERROR: could not retract stale review comment: $comment_url" >&2
        fi
        comment_exists=0
        exit 1
    fi
fi

comment_id="${comment_url##*issuecomment-}"
comment_meta="$(gh api "repos/$owner_repo/issues/comments/$comment_id" --jq '{author: .user.login, body: .body}')"
comment_body="$(jq -r '.body // ""' <<<"$comment_meta")"
comment_author="$(jq -r '.author // ""' <<<"$comment_meta")"
# Verify identity, provenance, and shape before treating the comment as ours.
# The author check is the load-bearing one: markers are forgeable text, a login
# is not. Only the head-SHA and coauthor markers are required — the model marker
# records which reviewer ran and is absent from comments the Codex watcher
# published for the same SHA, which are still valid canonical reviews.
if [[ "$comment_author" != "$current_login" ]]; then
    echo "ERROR: comment $comment_url is authored by '$comment_author', not '$current_login'" >&2
    exit 1
fi
if [[ "$comment_body" != *"<!-- pr-watcher-head-sha: $head_sha -->"* ]] \
    || [[ "$comment_body" != *'<!-- ai-coauthor: codex; agent: pr_watcher; mode: automated -->'* ]]; then
    echo "ERROR: posted GitHub comment failed canonical marker verification" >&2
    exit 1
fi
# An adopted body must still read as a review, so a comment that merely carries
# the markers cannot be recorded as this session's verdict.
published_conclusions="$(printf '%s\n' "$comment_body" | rg -c '^结论：(✅ 可以合并|❌ 暂不可合并)$' || true)"
published_last="$(printf '%s\n' "$comment_body" | awk 'NF { line=$0 } END { print line }')"
if [[ "$published_conclusions" != "1" ]] || [[ ! "$published_last" =~ ^结论：(✅\ 可以合并|❌\ 暂不可合并)$ ]]; then
    echo "ERROR: comment $comment_url carries the markers but no canonical conclusion" >&2
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
# Report the verdict that is actually published. On the reuse path the comment
# predates this run, so reading our own freshly generated file would announce a
# verdict that does not match what a reader (or the recorded entry) sees.
verdict="$published_last"

echo "PR_URL=$PR_URL"
echo "HEAD_SHA=$head_sha"
echo "REVIEW_VERDICT=$verdict"
echo "GITHUB_COMMENT=$comment_action"
echo "REVIEW_COMMENT_URL=$comment_url"
echo "REVIEW_AND_POST=ok"
