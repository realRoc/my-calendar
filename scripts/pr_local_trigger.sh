#!/bin/bash
# Background worker spawned by the global pre-push hook.
#
# Args:
#   $1 = remote URL (e.g. git@github.com:owner/repo.git)
#   $2 = path to a temp file containing the pre-push stdin payload
#        (lines of "<local_ref> <local_sha> <remote_ref> <remote_sha>")
#   $3 = (optional) origin cwd — the local repo root the push came from.
#        Forwarded to pr_watcher --force --origin-cwd so the launcher in the
#        calendar event knows where to open a "fix this PR" session.
#
# For each pushed branch:
#   - parse owner/repo from the remote URL
#   - poll gh pr list --head <branch> --base <default-branch> 8 times × 7.5s
#   - on first hit, call pr_watcher.py --force <pr_url>
# Removes the stdin temp file when done.

set -u

REMOTE_URL="${1:-}"
STDIN_FILE="${2:-}"
ORIGIN_CWD="${3:-}"
ROOT="$HOME/Desktop/my-calendar"
PYTHON="$ROOT/.venv/bin/python"
WATCHER="$ROOT/scripts/pr_watcher.py"
LOG_DIR="$HOME/.config/my-calendar/git-hooks/logs"
mkdir -p "$LOG_DIR"

cleanup() {
    [[ -n "$STDIN_FILE" && -f "$STDIN_FILE" ]] && rm -f "$STDIN_FILE"
}
trap cleanup EXIT

log() {
    printf '[%s] %s\n' "$(date '+%Y-%m-%d %H:%M:%S')" "$*"
}

if [[ -z "$STDIN_FILE" || ! -f "$STDIN_FILE" ]]; then
    log "ERROR: stdin temp file missing: $STDIN_FILE"
    exit 1
fi

# ── Parse owner/repo from remote URL ──
# Accept both:  git@github.com:owner/repo(.git)?   and   https://github.com/owner/repo(.git)?
OWNER_REPO=""
if [[ "$REMOTE_URL" =~ github\.com[:/](.+/.+)$ ]]; then
    OWNER_REPO="${BASH_REMATCH[1]}"
    OWNER_REPO="${OWNER_REPO%.git}"
fi
if [[ -z "$OWNER_REPO" ]]; then
    log "could not parse owner/repo from URL: $REMOTE_URL"
    exit 0
fi
log "push detected: $OWNER_REPO  (remote=$REMOTE_URL)"

# ── Collect distinct pushed branches from stdin payload ──
# Same branch may appear twice if pushed via multiple refs in one invocation
# (rare but possible, e.g. pushing the same branch under two names). We want
# to poll/trigger watcher only once per distinct branch.
#
# NOTE: macOS ships bash 3.2 — no associative arrays. A linear in-array scan
# is fine here: N is tiny (refs per push, basically always 1-2). Tried
# `declare -A` once; it silently breaks the whole hook on macOS.
BRANCHES=()
while IFS= read -r line; do
    [[ -z "$line" ]] && continue
    # fields: local_ref local_sha remote_ref remote_sha
    read -ra parts <<< "$line"
    local_sha="${parts[1]:-}"
    remote_ref="${parts[2]:-}"
    # git pre-push protocol:
    #   - branch deletion → local_sha is all-zeros (the local ref no longer exists)
    #   - new branch push → remote_sha is all-zeros (the remote ref doesn't exist yet)
    # We want to skip deletions and keep new-branch pushes, so test local_sha.
    [[ "$local_sha" == "0000000000000000000000000000000000000000" ]] && continue
    [[ "$remote_ref" != refs/heads/* ]] && continue
    branch="${remote_ref#refs/heads/}"
    is_dup=0
    if [[ "${#BRANCHES[@]}" -gt 0 ]]; then
        for existing in "${BRANCHES[@]}"; do
            if [[ "$existing" == "$branch" ]]; then is_dup=1; break; fi
        done
    fi
    [[ "$is_dup" == 1 ]] && continue
    BRANCHES+=("$branch")
done < "$STDIN_FILE"

if [[ "${#BRANCHES[@]}" -eq 0 ]]; then
    log "no branch pushes detected (deletions / tags only) — nothing to do"
    exit 0
fi

# ── For each branch, poll for an open PR up to ~60s ──
for branch in "${BRANCHES[@]}"; do
    log "polling for PR with head=$branch in $OWNER_REPO"
    pr_url=""
    for attempt in 1 2 3 4 5 6 7 8; do
        sleep 7
        # GitHub may need a moment for the push to be visible. Ask for a PR whose
        # head branch == this branch, regardless of base. pr_watcher.py will do
        # the final base==default check.
        candidate=$(gh pr list \
                        --repo "$OWNER_REPO" \
                        --head "$branch" \
                        --state open \
                        --json url \
                        --jq '.[0].url' 2>/dev/null || true)
        if [[ -n "$candidate" && "$candidate" != "null" ]]; then
            pr_url="$candidate"
            log "  attempt $attempt: found $pr_url"
            break
        fi
        log "  attempt $attempt: no PR yet"
    done

    if [[ -z "$pr_url" ]]; then
        log "  gave up after 60s — no PR found for $branch (push without PR is fine)"
        continue
    fi

    # ── Verify base == default branch before triggering codex ──
    # Extract pr number from URL like https://github.com/owner/repo/pull/123
    pr_number="${pr_url##*/}"
    meta=$(gh api graphql -f query='
        query($owner: String!, $name: String!, $number: Int!) {
          repository(owner: $owner, name: $name) {
            defaultBranchRef { name }
            pullRequest(number: $number) { baseRefName }
          }
        }' \
        -f owner="${OWNER_REPO%%/*}" \
        -f name="${OWNER_REPO##*/}" \
        -F number="$pr_number" 2>/dev/null) || true
    base=$(echo "$meta" | jq -r '.data.repository.pullRequest.baseRefName // ""' 2>/dev/null)
    default=$(echo "$meta" | jq -r '.data.repository.defaultBranchRef.name // ""' 2>/dev/null)
    if [[ -z "$base" || -z "$default" ]]; then
        log "  could not resolve base/default for $pr_url — skipping"
        continue
    fi
    if [[ "$base" != "$default" ]]; then
        log "  skip: base=$base ≠ default=$default for $pr_url"
        continue
    fi

    # ── Trigger codex review via pr_watcher --force ──
    # Pass --origin-cwd when the hook captured it, so the calendar event's
    # "open a fix session" launcher knows which repo to drop into. Launchd
    # tick path doesn't have this info and skips the flag entirely.
    # Two explicit code paths instead of an "extra_args[@]" array — macOS
    # bash 3.2 + `set -u` blows up on expanding an empty array.
    log "  triggering pr_watcher --force $pr_url  (base=$base, origin_cwd=${ORIGIN_CWD:-<none>})"
    if [[ -n "$ORIGIN_CWD" && -d "$ORIGIN_CWD" ]]; then
        "$PYTHON" "$WATCHER" --force "$pr_url" --origin-cwd "$ORIGIN_CWD" \
            >>"$LOG_DIR/trigger.log" 2>&1 \
            && log "  → done" \
            || log "  → pr_watcher exited non-zero"
    else
        "$PYTHON" "$WATCHER" --force "$pr_url" \
            >>"$LOG_DIR/trigger.log" 2>&1 \
            && log "  → done" \
            || log "  → pr_watcher exited non-zero"
    fi
done

log "trigger run complete"
