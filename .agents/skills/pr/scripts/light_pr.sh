#!/usr/bin/env bash
# Lightweight GitHub PR handoff for my-calendar.
#
# This helper intentionally does only the mechanical part:
# push the current branch, create/update an open PR with a clear title/body,
# then reserve that PR for the current Codex/Claude session's review. The
# agent using the skill remains responsible for understanding the diff, running
# relevant checks, posting the review comment, and recording it into calendar.

set -euo pipefail
export PATH="${PATH:-/usr/bin:/bin}:/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin"

BASE=""
DRAFT=0
TITLE=""
BODY_FILE=""
TRIGGER_ONLY=0
PR_URL_ARG=""
ALLOW_DIRTY=0
AUTO_BODY_FILE=""
TITLE_SOURCE="provided"
BODY_SOURCE="provided"
REVIEW_MODE="current-session"

cleanup() {
    if [[ -n "${AUTO_BODY_FILE:-}" && -f "$AUTO_BODY_FILE" ]]; then
        rm -f "$AUTO_BODY_FILE"
    fi
}
trap cleanup EXIT

usage() {
    cat <<'EOF'
Usage:
  light_pr.sh [--base <branch>] [--draft] [--title <title>] [--body-file <file>] [--allow-dirty] [--trigger-async-review]
  light_pr.sh --trigger-only <github-pr-url>

Creates a new GitHub PR or updates an existing OPEN PR for the current branch.
By default, it reserves the PR for the current agent session's review and does
not launch the background codex watcher. MERGED/CLOSED PRs are never reused as
the current handoff.
EOF
}

while [[ $# -gt 0 ]]; do
    case "$1" in
        --base)
            BASE="${2:-}"
            shift 2
            ;;
        --draft)
            DRAFT=1
            shift
            ;;
        --title)
            TITLE="${2:-}"
            shift 2
            ;;
        --body-file)
            BODY_FILE="${2:-}"
            shift 2
            ;;
        --allow-dirty)
            ALLOW_DIRTY=1
            shift
            ;;
        --trigger-async-review)
            REVIEW_MODE="async"
            shift
            ;;
        --trigger-only)
            TRIGGER_ONLY=1
            PR_URL_ARG="${2:-}"
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

need_cmd() {
    if ! command -v "$1" >/dev/null 2>&1; then
        echo "ERROR: required command not found: $1" >&2
        exit 1
    fi
}

need_cmd git

repo_root="$(git rev-parse --show-toplevel)"
cd "$repo_root"

trigger_my_calendar() {
    local pr_url="$1"
    local origin_cwd="$2"
    local hook="${MY_CALENDAR_PR_CREATED_HOOK:-}"

    if [[ -z "$hook" ]]; then
        hook="$HOME/.config/my-calendar/git-hooks/pr-created"
    fi

    if [[ -x "$hook" ]]; then
        "$hook" "$pr_url" "$origin_cwd"
        echo "MY_CALENDAR_TRIGGER=hook:$hook"
        return 0
    fi

    local checkout="${MY_CALENDAR_HOME:-}"
    if [[ -n "$checkout" && -x "$checkout/scripts/pr_created_trigger.sh" ]]; then
        "$checkout/scripts/pr_created_trigger.sh" "$pr_url" "$origin_cwd"
        echo "MY_CALENDAR_TRIGGER=checkout:$checkout/scripts/pr_created_trigger.sh"
        return 0
    fi

    for candidate in "$HOME/Desktop/my-calendar" "$HOME/my-calendar" "$HOME/src/my-calendar"; do
        if [[ -x "$candidate/scripts/pr_created_trigger.sh" ]]; then
            "$candidate/scripts/pr_created_trigger.sh" "$pr_url" "$origin_cwd"
            echo "MY_CALENDAR_TRIGGER=checkout:$candidate/scripts/pr_created_trigger.sh"
            return 0
        fi
    done

    echo "MY_CALENDAR_TRIGGER=missing"
    echo "WARNING: my-calendar pr-created hook was not found." >&2
    echo "         Run bash scripts/install_git_hook.sh from your my-calendar checkout, then retry:" >&2
    echo "         light_pr.sh --trigger-only '$pr_url'" >&2
    return 0
}

claim_current_session_review() {
    local pr_url="$1"
    local origin_cwd="$2"
    local checkout="${MY_CALENDAR_HOME:-}"
    local script=""
    local python=""

    if [[ -n "$checkout" && -f "$checkout/scripts/pr_session_review.py" ]]; then
        script="$checkout/scripts/pr_session_review.py"
    else
        for candidate in "$HOME/Desktop/my-calendar" "$HOME/my-calendar" "$HOME/src/my-calendar"; do
            if [[ -f "$candidate/scripts/pr_session_review.py" ]]; then
                script="$candidate/scripts/pr_session_review.py"
                checkout="$candidate"
                break
            fi
        done
    fi

    if [[ -z "$script" ]]; then
        echo "MY_CALENDAR_SESSION_CLAIM=missing"
        echo "WARNING: my-calendar current-session claim script was not found." >&2
        return 0
    fi

    python="$checkout/.venv/bin/python"
    if [[ ! -x "$python" ]]; then
        python="python3"
    fi

    if "$python" "$script" --claim --pr-url "$pr_url" --origin-cwd "$origin_cwd"; then
        echo "MY_CALENDAR_SESSION_CLAIM=checkout:$script"
    else
        echo "MY_CALENDAR_SESSION_CLAIM=failed"
        echo "WARNING: failed to claim current-session review in my-calendar state; continuing." >&2
        return 0
    fi
}

ensure_pr_text() {
    local range=""
    local latest_subject=""
    local commits=""
    local diffstat=""

    latest_subject="$(git log -1 --pretty=%s)"
    if [[ -z "$TITLE" ]]; then
        TITLE="$latest_subject"
        TITLE_SOURCE="latest-commit"
    fi

    if [[ -n "$BODY_FILE" ]]; then
        if [[ ! -f "$BODY_FILE" ]]; then
            echo "ERROR: --body-file does not exist: $BODY_FILE" >&2
            exit 2
        fi
        return 0
    fi

    BODY_SOURCE="generated"
    AUTO_BODY_FILE="$(mktemp "${TMPDIR:-/tmp}/light-pr-body.XXXXXX.md")"
    BODY_FILE="$AUTO_BODY_FILE"

    if git rev-parse --verify "origin/$BASE" >/dev/null 2>&1; then
        range="origin/$BASE..HEAD"
    else
        range="HEAD~1..HEAD"
    fi

    commits="$(git log --format='- %s' "$range" 2>/dev/null || git log --format='- %s' -1)"
    diffstat="$(git diff --stat "$range" 2>/dev/null || git show --stat --oneline --format='' HEAD)"

    {
        echo "## 解决什么问题"
        echo
        echo "- $TITLE"
        echo "- 变更范围基于当前分支相对 \`$BASE\` 的 diff。"
        echo
        echo "## 实现方式"
        echo
        if [[ -n "$commits" ]]; then
            echo "$commits"
        else
            echo "- 更新当前分支中的相关实现。"
        fi
        echo
        echo "## 验证"
        echo
        echo "- 未由 helper 采集；调用 /pr 的 agent 应在最终回报中列出已通过或跳过的检查。"
        if [[ -n "$diffstat" ]]; then
            echo
            echo "## 文件摘要"
            echo
            echo '```'
            echo "$diffstat"
            echo '```'
        fi
    } > "$BODY_FILE"
}

if [[ "$TRIGGER_ONLY" -eq 1 ]]; then
    if [[ ! "$PR_URL_ARG" =~ ^https://github\.com/[^/]+/[^/]+/pull/[0-9]+$ ]]; then
        echo "ERROR: --trigger-only requires a GitHub PR URL" >&2
        exit 2
    fi
    echo "PR_URL=$PR_URL_ARG"
    trigger_my_calendar "$PR_URL_ARG" "$repo_root"
    exit 0
fi

need_cmd gh
need_cmd jq

branch="$(git branch --show-current)"
if [[ -z "$branch" ]]; then
    echo "ERROR: detached HEAD; create/switch to a branch before opening a PR" >&2
    exit 1
fi

origin_url="$(git remote get-url origin 2>/dev/null || true)"
if [[ "$origin_url" != *github.com* ]]; then
    echo "ERROR: origin remote is not a GitHub remote: ${origin_url:-<missing>}" >&2
    exit 1
fi

repo_json="$(gh repo view --json nameWithOwner,defaultBranchRef)"
owner_repo="$(echo "$repo_json" | jq -r '.nameWithOwner')"
default_branch="$(echo "$repo_json" | jq -r '.defaultBranchRef.name')"
if [[ -z "$owner_repo" || "$owner_repo" == "null" || -z "$default_branch" || "$default_branch" == "null" ]]; then
    echo "ERROR: could not resolve GitHub repo/default branch with gh" >&2
    exit 1
fi

if [[ -z "$BASE" ]]; then
    BASE="$default_branch"
fi

if [[ "$branch" == "$default_branch" ]]; then
    echo "ERROR: refusing to open a PR from the default branch '$default_branch'" >&2
    exit 1
fi

if [[ "$branch" == "$BASE" ]]; then
    echo "ERROR: refusing to open a PR from the base branch '$BASE'" >&2
    exit 1
fi

dirty_status="$(git status --porcelain)"
if [[ -n "$dirty_status" && "$ALLOW_DIRTY" -ne 1 ]]; then
    echo "ERROR: working tree has uncommitted changes; commit them before running light_pr.sh" >&2
    git status --short >&2
    echo "       If the remaining changes are unrelated user work, rerun with --allow-dirty." >&2
    exit 1
fi
if [[ -n "$dirty_status" && "$ALLOW_DIRTY" -eq 1 ]]; then
    echo "WARNING: continuing with local changes not included in HEAD:" >&2
    git status --short >&2
fi

echo "REPO=$owner_repo"
echo "BRANCH=$branch"
echo "BASE=$BASE"

git fetch origin "$BASE" >/dev/null 2>&1 || true
ensure_pr_text
if [[ "$REVIEW_MODE" == "current-session" ]]; then
    MY_CALENDAR_PR_SKIP_PRE_PUSH_REVIEW=1 git push -u origin HEAD >/dev/null
else
    git push -u origin HEAD >/dev/null
fi

pr_list_json="$(gh pr list --head "$branch" --base "$BASE" --state all --json number,url,state,baseRefName,headRefName,updatedAt --limit 20)"
open_pr_json="$(echo "$pr_list_json" | jq -c '[.[] | select(.state == "OPEN")][0] // empty')"
if [[ -n "$open_pr_json" ]]; then
    pr_url="$(echo "$open_pr_json" | jq -r '.url')"
    pr_number="$(echo "$open_pr_json" | jq -r '.number')"
    echo "PR_ACTION=updated"
    echo "EXISTING_PR_STATE=OPEN"
    gh pr edit "$pr_url" --title "$TITLE" --body-file "$BODY_FILE" >/dev/null
else
    non_open_pr_json="$(echo "$pr_list_json" | jq -c '[.[] | select(.state != "OPEN")][0] // empty')"
    if [[ -n "$non_open_pr_json" ]]; then
        echo "EXISTING_PR_STATE=$(echo "$non_open_pr_json" | jq -r '.state')"
        echo "EXISTING_PR_URL=$(echo "$non_open_pr_json" | jq -r '.url')"
        echo "PR_ACTION=created-new-after-non-open"
    else
        echo "PR_ACTION=created"
    fi

    args=(pr create --base "$BASE" --title "$TITLE" --body-file "$BODY_FILE")
    if [[ "$DRAFT" -eq 1 ]]; then
        args+=(--draft)
    fi
    pr_url="$(gh "${args[@]}")"
    pr_number="${pr_url##*/}"
fi

if [[ "$pr_url" != https://github.com/*/*/pull/* ]]; then
    echo "ERROR: gh did not return a GitHub PR URL: $pr_url" >&2
    exit 1
fi

echo "PR_NUMBER=$pr_number"
echo "PR_TITLE_SOURCE=$TITLE_SOURCE"
echo "PR_BODY_SOURCE=$BODY_SOURCE"
echo "PR_URL=$pr_url"
if [[ "$REVIEW_MODE" == "async" ]]; then
    trigger_my_calendar "$pr_url" "$repo_root"
else
    claim_current_session_review "$pr_url" "$repo_root"
fi
