#!/usr/bin/env bash
# Debounced launcher for my-calendar PR reviews.
#
# Usage:
#   pr_review_trigger.sh [--source <name>] <pr-url> [origin-cwd]
#
# This is the shared handoff into pr_watcher.py. It verifies that the PR targets
# the repo's default branch, debounces duplicate triggers for the same PR+SHA,
# then starts `pr_watcher.py --force` in the background.
#
# Calendar writes are owned by macOS TCC's "responsible app", not just by the
# binary doing the write. An app that never declared NSCalendars*UsageDescription
# in its Info.plist — Claude Desktop is one — is denied silently, with no prompt
# and no entry in System Settings to toggle, so "codex-only" bridging left every
# Claude-launched write failing. auto therefore bridges by default and only
# skips the hop when the enclosing app is known to hold the grant. The work is
# handed to a Terminal-opened .command file before touching debounce/state; the
# Terminal child then runs this same script with the bridge disabled.

set -u
export PATH="${PATH:-/usr/bin:/bin}:/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin"

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

shell_quote() {
    printf '%q' "$1"
}

# Apps allowed to write Calendar without going through the bridge. Terminal is
# the bridge target, so re-bridging from there would loop.
#
# Codex Desktop is deliberately NOT here. It does declare NSCalendars* keys, so
# it *can* prompt, but declaring is not being granted — and the pre-existing
# observation on this machine was that Codex often had no Calendar access, i.e.
# exactly the silent no-prompt denial this bridge exists to route around. The
# cost of bridging a fully-authorized app is one extra Terminal window; the cost
# of exempting an unauthorized one is an invisible write failure. Only move an
# app in here after confirming a real grant in System Settings → Privacy &
# Security → Calendar.
#
# Duplicated on purpose in pr_record_review_trigger.sh (no shared lib, so each
# stays runnable when copied into a skill bundle on its own). Change both.
CALENDAR_CAPABLE_BUNDLE_IDS=(
    "com.apple.Terminal"
)

should_bridge_to_terminal() {
    case "${MY_CALENDAR_PR_TERMINAL_BRIDGE:-auto}" in
        0|false|FALSE|off|OFF|no|NO)
            return 1
            ;;
        1|true|TRUE|on|ON|yes|YES|force|FORCE)
            return 0
            ;;
    esac

    # auto: bridge unless we are already running inside an app that can write
    # Calendar itself. No enclosing app at all (detached pre-push child, launchd
    # job) means nobody to ask, so bridge.
    local bundle_id candidate
    bundle_id="${__CFBundleIdentifier:-}"
    [[ -z "$bundle_id" ]] && return 0
    for candidate in "${CALENDAR_CAPABLE_BUNDLE_IDS[@]}"; do
        [[ "$bundle_id" == "$candidate" ]] && return 1
    done
    return 0
}

launch_terminal_bridge() {
    local command_file
    local command_tmp
    local bridge_source="${SOURCE}:terminal-bridge"
    local terminal_app="${MY_CALENDAR_PR_TERMINAL_APP:-Terminal}"

    if ! command -v open >/dev/null 2>&1; then
        log "ERROR: cannot bridge $PR_URL to Terminal because 'open' was not found"
        return 1
    fi

    command_tmp="$(mktemp "${TMPDIR:-/tmp}/my-calendar-pr-review.XXXXXX")" || {
        log "ERROR: failed to create Terminal bridge command file for $PR_URL"
        return 1
    }
    command_file="${command_tmp}.command"
    mv "$command_tmp" "$command_file"

    {
        printf '#!/usr/bin/env bash\n'
        printf 'set -euo pipefail\n'
        printf 'rm -f "$0"\n'
        printf 'LAUNCH_TTY="$(tty 2>/dev/null || echo not-a-tty)"\n'
        printf 'mkdir -p "$HOME/.config/my-calendar/git-hooks/logs"\n'
        printf 'exec >> "$HOME/.config/my-calendar/git-hooks/logs/trigger.log" 2>&1\n'
        printf 'printf '"'"'[%%s] terminal bridge executing for %%s (source=%%s)\\n'"'"' "$(date '"'"'+%%Y-%%m-%%d %%H:%%M:%%S'"'"')" %s %s\n' \
            "$(shell_quote "$PR_URL")" "$(shell_quote "$SOURCE")"
        cat <<'BRIDGE_SCRIPT'
# Close this window once the bridge has handed off. The review itself keeps
# running in a detached child (pr_review_trigger.sh backgrounds pr_watcher.py),
# so the window would otherwise sit open for the full multi-minute codex run
# with nothing left to show. Mirrors pr_record_review_trigger.sh.
auto_close_terminal_bridge_if_success() {
    local rc="$1"
    local tty_name="$2"

    [[ "$rc" -eq 0 ]] || return 0
    case "${MY_CALENDAR_PR_TERMINAL_AUTO_CLOSE:-1}" in
        0|false|FALSE|off|OFF|no|NO)
            return 0
            ;;
    esac

    [[ -n "$tty_name" && "$tty_name" != "not-a-tty" ]] || return 0

    /usr/bin/nohup /usr/bin/osascript - "$tty_name" >/dev/null 2>&1 <<'APPLESCRIPT' &
on run argv
    delay 0.3
    set targetTty to item 1 of argv
    tell application "Terminal"
        repeat with w in windows
            repeat with t in tabs of w
                if (tty of t as text) is targetTty then
                    if (count of tabs of w) is 1 then
                        close w saving no
                    end if
                    return
                end if
            end repeat
        end repeat
    end tell
end run
APPLESCRIPT
}
BRIDGE_SCRIPT
        printf 'cd %s\n' "$(shell_quote "$ROOT")"
        printf 'export MY_CALENDAR_PR_TERMINAL_BRIDGE=0\n'
        # Not `exec`: the bridge must regain control to close its own window.
        # set +e keeps a non-zero trigger status reportable instead of killing
        # the script before the auto-close call.
        printf 'set +e\n'
        printf 'bash %s --source %s %s' \
            "$(shell_quote "$ROOT/scripts/pr_review_trigger.sh")" \
            "$(shell_quote "$bridge_source")" \
            "$(shell_quote "$PR_URL")"
        if [[ -n "$ORIGIN_CWD" ]]; then
            printf ' %s' "$(shell_quote "$ORIGIN_CWD")"
        fi
        printf '\n'
        printf 'rc=$?\n'
        printf 'auto_close_terminal_bridge_if_success "$rc" "$LAUNCH_TTY"\n'
        printf 'exit "$rc"\n'
    } > "$command_file"
    chmod 700 "$command_file"

    if open -g -a "$terminal_app" "$command_file" >/dev/null 2>&1; then
        log "terminal bridge launched via $terminal_app for $PR_URL (source=$SOURCE)"
        return 0
    fi
    if open -a "$terminal_app" "$command_file" >/dev/null 2>&1; then
        log "terminal bridge launched via $terminal_app for $PR_URL (source=$SOURCE)"
        return 0
    fi

    rm -f "$command_file"
    log "ERROR: failed to open Terminal bridge for $PR_URL"
    return 1
}

if [[ -z "$PR_URL" ]]; then
    log "ERROR: pr_review_trigger called without PR URL (source=$SOURCE)"
    exit 1
fi

if [[ "$PR_URL" != https://github.com/*/*/pull/* ]]; then
    log "skip: unsupported PR URL for my-calendar review trigger: $PR_URL"
    exit 0
fi

if should_bridge_to_terminal; then
    launch_terminal_bridge
    exit $?
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
