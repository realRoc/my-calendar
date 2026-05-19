#!/usr/bin/env bash
# Install the global pre-push hook used by my-calendar's PR watcher.
#
# Effect:
#   - Sets `git config --global core.hooksPath ~/.config/my-calendar/git-hooks`
#   - That dir already contains the executable pre-push hook (shipped by this repo).
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

HOOK_DIR="$HOME/.config/my-calendar/git-hooks"
HOOK_FILE="$HOOK_DIR/pre-push"

if [[ ! -x "$HOOK_FILE" ]]; then
    echo "✗ hook not found or not executable: $HOOK_FILE" >&2
    echo "  expected this file to be shipped with the repo. Check that you have" >&2
    echo "  $HOME/.config/my-calendar/git-hooks/pre-push" >&2
    exit 1
fi

current=""
if current=$(git config --global --get core.hooksPath 2>/dev/null); then
    if [[ -n "$current" && "$current" != "$HOOK_DIR" && "$FORCE" -ne 1 ]]; then
        echo "✗ git config --global core.hooksPath is already set to:" >&2
        echo "      $current" >&2
        echo "  Refusing to overwrite. Re-run with --force to replace, or move" >&2
        echo "  your existing global hooks into $HOOK_DIR (they will be chain-called" >&2
        echo "  via .git/hooks/pre-push.local in each repo)." >&2
        exit 2
    fi
fi

echo "→ git config --global core.hooksPath $HOOK_DIR"
git config --global core.hooksPath "$HOOK_DIR"

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
