#!/usr/bin/env bash
# Record a current-session PR review into my-calendar.
#
# Usage:
#   pr_record_review_trigger.sh [--timeout <seconds>] <pr-url> <comment-url> [origin-cwd]
#
# When launched from Codex Desktop, Calendar writes are bridged through
# Terminal.app so EventKit attributes the write to Terminal's Calendar
# permission. Unlike the old background review trigger, this operation is
# short, so the caller waits for a small status file and can report success.

set -u
export PATH="${PATH:-/usr/bin:/bin}:/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin"

TIMEOUT=45
if [[ "${1:-}" == "--timeout" ]]; then
    TIMEOUT="${2:-45}"
    shift 2
fi

PR_URL="${1:-}"
COMMENT_URL="${2:-}"
ORIGIN_CWD="${3:-}"

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
PYTHON="$ROOT/.venv/bin/python"
RECORDER="$ROOT/scripts/pr_session_review.py"
LOG_DIR="$HOME/.config/my-calendar/git-hooks/logs"
STATUS_DIR="$HOME/.config/my-calendar/git-hooks/record-status"
LOG_FILE="$LOG_DIR/trigger.log"
mkdir -p "$LOG_DIR" "$STATUS_DIR"

log() {
    printf '[%s] %s\n' "$(date '+%Y-%m-%d %H:%M:%S')" "$*"
}

shell_quote() {
    printf '%q' "$1"
}

should_bridge_to_terminal() {
    case "${MY_CALENDAR_PR_RECORD_TERMINAL_BRIDGE:-auto}" in
        0|false|FALSE|off|OFF|no|NO)
            return 1
            ;;
        1|true|TRUE|on|ON|yes|YES|force|FORCE)
            return 0
            ;;
    esac

    [[ "${__CFBundleIdentifier:-}" == "com.openai.codex" ]]
}

validate_args() {
    if [[ -z "$PR_URL" || -z "$COMMENT_URL" ]]; then
        log "ERROR: pr_record_review_trigger requires PR URL and comment URL"
        exit 2
    fi
    if [[ ! "$PR_URL" =~ ^https://github\.com/[^/]+/[^/]+/pull/[0-9]+$ ]]; then
        log "ERROR: unsupported PR URL: $PR_URL"
        exit 2
    fi
    if [[ ! "$COMMENT_URL" =~ ^https://github\.com/[^/]+/[^/]+/pull/[0-9]+#issuecomment-[0-9]+$ ]]; then
        log "ERROR: unsupported PR comment URL: $COMMENT_URL"
        exit 2
    fi
    if [[ ! -x "$PYTHON" ]]; then
        log "ERROR: python venv not executable: $PYTHON"
        exit 1
    fi
    if [[ ! -f "$RECORDER" ]]; then
        log "ERROR: recorder missing: $RECORDER"
        exit 1
    fi
}

run_recorder() {
    if [[ -n "$ORIGIN_CWD" ]]; then
        "$PYTHON" "$RECORDER" --record --pr-url "$PR_URL" --comment-url "$COMMENT_URL" --origin-cwd "$ORIGIN_CWD"
    else
        "$PYTHON" "$RECORDER" --record --pr-url "$PR_URL" --comment-url "$COMMENT_URL"
    fi
}

wait_for_status() {
    local status_file="$1"
    local out_file="$2"
    local err_file="$3"
    local deadline
    local rc
    deadline=$(( $(date +%s) + TIMEOUT ))

    while [[ "$(date +%s)" -le "$deadline" ]]; do
        if [[ -f "$status_file" ]]; then
            rc="$(awk -F= '$1 == "rc" {print $2}' "$status_file" 2>/dev/null || true)"
            [[ -n "$rc" ]] || rc=1
            [[ -s "$out_file" ]] && cat "$out_file"
            [[ -s "$err_file" ]] && cat "$err_file" >&2
            if [[ "$rc" -eq 0 ]]; then
                echo "MY_CALENDAR_RECORD=terminal-bridge:success"
                return 0
            fi
            echo "MY_CALENDAR_RECORD=terminal-bridge:failed"
            return "$rc"
        fi
        sleep 1
    done

    echo "MY_CALENDAR_RECORD=terminal-bridge:timeout"
    echo "STATUS_FILE=$status_file"
    return 1
}

launch_terminal_bridge() {
    local command_file
    local command_tmp
    local status_key
    local status_file
    local out_file
    local err_file
    local terminal_app="${MY_CALENDAR_PR_TERMINAL_APP:-Terminal}"

    if ! command -v open >/dev/null 2>&1; then
        log "ERROR: cannot bridge record to Terminal because 'open' was not found"
        return 1
    fi

    status_key="$(printf '%s\n%s\n' "$PR_URL" "$COMMENT_URL" | shasum -a 256 | awk '{print $1}')"
    status_file="$STATUS_DIR/$status_key.status"
    out_file="$STATUS_DIR/$status_key.out"
    err_file="$STATUS_DIR/$status_key.err"
    rm -f "$status_file" "$out_file" "$err_file"

    command_tmp="$(mktemp "${TMPDIR:-/tmp}/my-calendar-pr-record.XXXXXX")" || {
        log "ERROR: failed to create Terminal bridge command file for $PR_URL"
        return 1
    }
    command_file="${command_tmp}.command"
    mv "$command_tmp" "$command_file"

    {
        printf '#!/usr/bin/env bash\n'
        printf 'set -u\n'
        printf 'rm -f "$0"\n'
        printf 'mkdir -p %s\n' "$(shell_quote "$STATUS_DIR")"
        printf 'cd %s\n' "$(shell_quote "$ROOT")"
        printf 'export MY_CALENDAR_PR_RECORD_TERMINAL_BRIDGE=0\n'
        printf 'set +e\n'
        printf '%s %s --record --pr-url %s --comment-url %s' \
            "$(shell_quote "$PYTHON")" \
            "$(shell_quote "$RECORDER")" \
            "$(shell_quote "$PR_URL")" \
            "$(shell_quote "$COMMENT_URL")"
        if [[ -n "$ORIGIN_CWD" ]]; then
            printf ' --origin-cwd %s' "$(shell_quote "$ORIGIN_CWD")"
        fi
        printf ' >%s 2>%s\n' "$(shell_quote "$out_file")" "$(shell_quote "$err_file")"
        printf 'rc=$?\n'
        printf 'printf '"'"'rc=%%s\\n'"'"' "$rc" > %s\n' "$(shell_quote "$status_file")"
        printf 'exit "$rc"\n'
    } > "$command_file"
    chmod 700 "$command_file"

    if open -g -a "$terminal_app" "$command_file" >/dev/null 2>&1; then
        log "terminal bridge launched via $terminal_app for current-session record $PR_URL"
        wait_for_status "$status_file" "$out_file" "$err_file"
        return $?
    fi
    if open -a "$terminal_app" "$command_file" >/dev/null 2>&1; then
        log "terminal bridge launched via $terminal_app for current-session record $PR_URL"
        wait_for_status "$status_file" "$out_file" "$err_file"
        return $?
    fi

    rm -f "$command_file"
    log "ERROR: failed to open Terminal bridge for current-session record $PR_URL"
    return 1
}

validate_args

if should_bridge_to_terminal; then
    launch_terminal_bridge
    exit $?
fi

run_recorder
rc=$?
if [[ "$rc" -eq 0 ]]; then
    echo "MY_CALENDAR_RECORD=direct:success"
else
    echo "MY_CALENDAR_RECORD=direct:failed"
fi
exit "$rc"
