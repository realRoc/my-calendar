#!/usr/bin/env bash
# Lightweight GitHub PR handoff for my-calendar.
#
# This helper intentionally does only the mechanical part:
# push the current branch, create/reuse a PR, then call my-calendar's
# pr-created hook. The agent using the skill remains responsible for
# understanding the diff, running relevant checks, and creating commits.

set -euo pipefail

BASE=""
DRAFT=0
TITLE=""
BODY_FILE=""
TRIGGER_ONLY=0
PR_URL_ARG=""
ALLOW_DIRTY=0

usage() {
    cat <<'EOF'
Usage:
  light_pr.sh [--base <branch>] [--draft] [--title <title>] [--body-file <file>] [--allow-dirty]
  light_pr.sh --trigger-only <github-pr-url>

Creates or reuses a GitHub PR for the current branch and triggers my-calendar's
PR review pipeline via the pr-created hook.
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
need_cmd gh
need_cmd jq

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

if [[ "$TRIGGER_ONLY" -eq 1 ]]; then
    if [[ "$PR_URL_ARG" != https://github.com/*/*/pull/* ]]; then
        echo "ERROR: --trigger-only requires a GitHub PR URL" >&2
        exit 2
    fi
    echo "PR_URL=$PR_URL_ARG"
    trigger_my_calendar "$PR_URL_ARG" "$repo_root"
    exit 0
fi

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
git push -u origin HEAD >/dev/null

existing_pr="$(gh pr view --json url -q '.url' 2>/dev/null || true)"
if [[ -n "$existing_pr" && "$existing_pr" != "null" ]]; then
    pr_url="$existing_pr"
else
    args=(pr create --base "$BASE" --fill)
    if [[ "$DRAFT" -eq 1 ]]; then
        args+=(--draft)
    fi
    if [[ -n "$TITLE" ]]; then
        args+=(--title "$TITLE")
    fi
    if [[ -n "$BODY_FILE" ]]; then
        args+=(--body-file "$BODY_FILE")
    fi
    pr_url="$(gh "${args[@]}")"
fi

if [[ "$pr_url" != https://github.com/*/*/pull/* ]]; then
    echo "ERROR: gh did not return a GitHub PR URL: $pr_url" >&2
    exit 1
fi

echo "PR_URL=$pr_url"
trigger_my_calendar "$pr_url" "$repo_root"
