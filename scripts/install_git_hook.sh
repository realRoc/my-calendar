#!/usr/bin/env bash
# Install the global pre-push hook used by my-calendar's PR watcher.
#
# Effect:
#   1. Copies templates/git-hooks/pre-push and templates/git-hooks/pr-created
#      (the version-controlled sources) to ~/.config/my-calendar/git-hooks/,
#      marking them executable.
#   2. Sets `git config --global core.hooksPath ~/.config/my-calendar/git-hooks`.
#
# Idempotent: re-running with no changes is a no-op. If the deployed hook
# differs from the template, the script refuses to overwrite without --force
# (and prints the diff so you know what changes).
#
# Safety:
#   - If core.hooksPath is already set to something else, refuses to overwrite.
#     Pass --force to override.
#   - Existing per-repo hooks at .git/hooks/pre-push are bypassed by global hooks
#     (this is git's semantics, not ours). Migrate them to .git/hooks/pre-push.local
#     and our global hook will chain-call them before triggering the watcher.

set -euo pipefail

FORCE=0
if [[ "${1:-}" == "--force" ]]; then
    FORCE=1
fi

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
TEMPLATE_FILE="$REPO_ROOT/templates/git-hooks/pre-push"
PR_CREATED_TEMPLATE_FILE="$REPO_ROOT/templates/git-hooks/pr-created"
TRIGGER_SCRIPT="$REPO_ROOT/scripts/pr_local_trigger.sh"
PR_CREATED_TRIGGER_SCRIPT="$REPO_ROOT/scripts/pr_created_trigger.sh"
HOOK_DIR="$HOME/.config/my-calendar/git-hooks"
HOOK_FILE="$HOOK_DIR/pre-push"
PR_CREATED_HOOK_FILE="$HOOK_DIR/pr-created"

if [[ ! -f "$TEMPLATE_FILE" ]]; then
    echo "✗ template hook not found in repo: $TEMPLATE_FILE" >&2
    echo "  This installer is supposed to deploy the version-controlled template." >&2
    echo "  If the file is missing, the repo is in a broken state." >&2
    exit 1
fi
if [[ ! -f "$PR_CREATED_TEMPLATE_FILE" ]]; then
    echo "✗ template hook not found in repo: $PR_CREATED_TEMPLATE_FILE" >&2
    echo "  This installer is supposed to deploy the version-controlled template." >&2
    echo "  If the file is missing, the repo is in a broken state." >&2
    exit 1
fi

if [[ ! -f "$TRIGGER_SCRIPT" ]]; then
    echo "✗ trigger script not found in repo: $TRIGGER_SCRIPT" >&2
    echo "  The pre-push hook would deploy with a dangling TRIGGER_SCRIPT path." >&2
    exit 1
fi
if [[ ! -f "$PR_CREATED_TRIGGER_SCRIPT" ]]; then
    echo "✗ PR-created trigger script not found in repo: $PR_CREATED_TRIGGER_SCRIPT" >&2
    echo "  The pr-created hook would deploy with a dangling trigger path." >&2
    exit 1
fi

mkdir -p "$HOOK_DIR"

# ── 1. Render the template ───────────────────────────────────────────────────
# Substitute __TRIGGER_SCRIPT__ with the absolute path inside *this* checkout.
# Previously the template hardcoded $HOME/Desktop/my-calendar, which silently
# broke any clone in a different directory (the hook fired but the trigger
# branch always missed and PR watcher never ran).
RENDERED_TMP="$(mktemp -t my-calendar-prepush.XXXXXX)"
PR_CREATED_RENDERED_TMP="$(mktemp -t my-calendar-prcreated.XXXXXX)"
trap 'rm -f "$RENDERED_TMP" "$PR_CREATED_RENDERED_TMP"' EXIT
# Escape replacement for sed: backslashes, ampersands, and the delimiter (|).
escaped_trigger=$(printf '%s' "$TRIGGER_SCRIPT" | sed -e 's/[\\&|]/\\&/g')
sed -e "s|__TRIGGER_SCRIPT__|${escaped_trigger}|g" "$TEMPLATE_FILE" > "$RENDERED_TMP"
escaped_pr_created_trigger=$(printf '%s' "$PR_CREATED_TRIGGER_SCRIPT" | sed -e 's/[\\&|]/\\&/g')
sed -e "s|__PR_CREATED_TRIGGER_SCRIPT__|${escaped_pr_created_trigger}|g" "$PR_CREATED_TEMPLATE_FILE" > "$PR_CREATED_RENDERED_TMP"

# Sanity-check the substitution actually happened.
if grep -q '__TRIGGER_SCRIPT__' "$RENDERED_TMP"; then
    echo "✗ rendered hook still contains __TRIGGER_SCRIPT__ placeholder — sed substitution failed" >&2
    exit 1
fi
if grep -q '__PR_CREATED_TRIGGER_SCRIPT__' "$PR_CREATED_RENDERED_TMP"; then
    echo "✗ rendered PR-created hook still contains __PR_CREATED_TRIGGER_SCRIPT__ placeholder — sed substitution failed" >&2
    exit 1
fi

# ── 2. Deploy the hooks ───────────────────────────────────────────────────────
deploy_needed=1
if [[ -f "$HOOK_FILE" ]]; then
    if cmp -s "$RENDERED_TMP" "$HOOK_FILE"; then
        # Content matches; still make sure the executable bit is present —
        # git silently ignores non-executable hooks, and a previous chmod
        # could have been undone (umask, rsync, manual edit).
        if [[ ! -x "$HOOK_FILE" ]]; then
            chmod +x "$HOOK_FILE"
            echo "✓ hook content matches template; restored executable bit: $HOOK_FILE"
        else
            echo "✓ hook already up-to-date: $HOOK_FILE"
        fi
        deploy_needed=0
    elif [[ "$FORCE" -eq 1 ]]; then
        echo "→ --force: overwriting hook at $HOOK_FILE"
    else
        echo "✗ deployed hook differs from rendered template:" >&2
        echo "      template:  $TEMPLATE_FILE (rendered with TRIGGER_SCRIPT=$TRIGGER_SCRIPT)" >&2
        echo "      deployed:  $HOOK_FILE" >&2
        echo >&2
        echo "  ── diff (deployed vs rendered template) ───────────────────────" >&2
        diff -u "$HOOK_FILE" "$RENDERED_TMP" >&2 || true
        echo "  ───────────────────────────────────────────────────────────────" >&2
        echo >&2
        echo "  Re-run with --force to overwrite, or update the template to" >&2
        echo "  match if your local edits are intentional." >&2
        exit 2
    fi
fi

if [[ "$deploy_needed" -eq 1 ]]; then
    cp "$RENDERED_TMP" "$HOOK_FILE"
    chmod +x "$HOOK_FILE"
    echo "→ deployed: $HOOK_FILE  (TRIGGER_SCRIPT=$TRIGGER_SCRIPT)"
fi

pr_created_deploy_needed=1
if [[ -f "$PR_CREATED_HOOK_FILE" ]]; then
    if cmp -s "$PR_CREATED_RENDERED_TMP" "$PR_CREATED_HOOK_FILE"; then
        if [[ ! -x "$PR_CREATED_HOOK_FILE" ]]; then
            chmod +x "$PR_CREATED_HOOK_FILE"
            echo "✓ PR-created hook content matches template; restored executable bit: $PR_CREATED_HOOK_FILE"
        else
            echo "✓ PR-created hook already up-to-date: $PR_CREATED_HOOK_FILE"
        fi
        pr_created_deploy_needed=0
    elif [[ "$FORCE" -eq 1 ]]; then
        echo "→ --force: overwriting PR-created hook at $PR_CREATED_HOOK_FILE"
    else
        echo "✗ deployed PR-created hook differs from rendered template:" >&2
        echo "      template:  $PR_CREATED_TEMPLATE_FILE (rendered with PR_CREATED_TRIGGER_SCRIPT=$PR_CREATED_TRIGGER_SCRIPT)" >&2
        echo "      deployed:  $PR_CREATED_HOOK_FILE" >&2
        echo >&2
        echo "  ── diff (deployed vs rendered template) ───────────────────────" >&2
        diff -u "$PR_CREATED_HOOK_FILE" "$PR_CREATED_RENDERED_TMP" >&2 || true
        echo "  ───────────────────────────────────────────────────────────────" >&2
        echo >&2
        echo "  Re-run with --force to overwrite, or update the template to" >&2
        echo "  match if your local edits are intentional." >&2
        exit 2
    fi
fi

if [[ "$pr_created_deploy_needed" -eq 1 ]]; then
    cp "$PR_CREATED_RENDERED_TMP" "$PR_CREATED_HOOK_FILE"
    chmod +x "$PR_CREATED_HOOK_FILE"
    echo "→ deployed: $PR_CREATED_HOOK_FILE  (PR_CREATED_TRIGGER_SCRIPT=$PR_CREATED_TRIGGER_SCRIPT)"
fi

# ── 3. Wire up core.hooksPath ─────────────────────────────────────────────────
current=""
if current=$(git config --global --get core.hooksPath 2>/dev/null); then
    if [[ -n "$current" && "$current" != "$HOOK_DIR" && "$FORCE" -ne 1 ]]; then
        echo "✗ git config --global core.hooksPath is already set to:" >&2
        echo "      $current" >&2
        echo "  Refusing to overwrite. Re-run with --force to replace, or move" >&2
        echo "  your existing global hooks into $HOOK_DIR (they will be chain-called" >&2
        echo "  via .git/hooks/pre-push.local in each repo)." >&2
        exit 3
    fi
fi

if [[ "$current" != "$HOOK_DIR" ]]; then
    echo "→ git config --global core.hooksPath $HOOK_DIR"
    git config --global core.hooksPath "$HOOK_DIR"
fi

echo
echo "✓ installed. Verify:"
echo "    git config --global --get core.hooksPath"
echo "    → $(git config --global --get core.hooksPath)"
echo
echo "  Hook fires on every \`git push\` from this Mac. To opt OUT for a"
echo "  specific repo, set per-repo: git -C <repo> config core.hooksPath .git/hooks"
echo
echo "  Per-repo pre-push hooks: move them to .git/hooks/pre-push.local and they"
echo "  will be chain-called before the watcher trigger fires."
echo
echo "  Trigger logs: $HOME/.config/my-calendar/git-hooks/logs/trigger.log"
echo "  PR-created hook: $HOME/.config/my-calendar/git-hooks/pr-created"
