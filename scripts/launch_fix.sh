#!/bin/bash
# MyCalFix URL handler: parse mycalfix://fix?... → open Terminal → fetch+checkout → claude
#
# Triggered by ~/Applications/MyCalFix.app via `on open location`. Also
# runnable manually:
#   scripts/launch_fix.sh 'mycalfix://fix?repo=foo%2Fbar&branch=feat&...'
#
# Security: every URL field is untrusted. URL fields are passed to osascript
# via stdin argv (read as `item N of argv`), never string-interpolated into
# AppleScript source. origin_cwd is verified to be a git worktree whose
# remote.origin.url matches the URL's repo field.

set -euo pipefail

HERE=$(cd "$(dirname "$0")" && pwd)
# ~/Library/Logs/ rather than repo logs/: bundled launcher has no TCC access
# to the user's repo if it sits under ~/Desktop/~/Documents/~/Downloads.
LOG_DIR="$HOME/Library/Logs/MyCalFix"
LOG_FILE="$LOG_DIR/launch_fix.log"
PROMPT_FILE="$HERE/fix_prompt.md"
mkdir -p "$LOG_DIR"

# osascript helpers: values pass via argv, NOT interpolated into AS source.
show_alert() {
  osascript - "$1" >/dev/null 2>&1 <<'APPLESCRIPT' || true
on run argv
    display alert "MyCalFix" message (item 1 of argv)
end run
APPLESCRIPT
}

prompt_for_folder() {
  osascript - "$1" 2>>"$LOG_FILE" <<'APPLESCRIPT' || true
on run argv
    try
        set picked to choose folder with prompt ("MyCalFix: 选择 " & (item 1 of argv) & " 的本地 checkout 目录")
        return POSIX path of picked
    on error
        return ""
    end try
end run
APPLESCRIPT
}

# 0 if $1 is a git worktree whose origin matches expected "owner/repo".
validate_origin_cwd() {
  local dir="$1" expected="$2" url normalized
  if ! git -C "$dir" rev-parse --git-dir >/dev/null 2>&1; then
    echo "  validate: $dir is not a git worktree" >> "$LOG_FILE"
    return 1
  fi
  url=$(git -C "$dir" config --get remote.origin.url 2>/dev/null || true)
  normalized=$(python3 -c '
import sys, re
m = re.search(r"[:/]([^/:]+/[^/:.]+?)(?:\.git)?/?$", sys.argv[1])
print(m.group(1) if m else "")
' "$url")
  if [[ "$normalized" != "$expected" ]]; then
    echo "  validate: remote mismatch expected=$expected got=$normalized (from $url)" >> "$LOG_FILE"
    return 1
  fi
  return 0
}

{
  echo "─── $(date '+%Y-%m-%d %H:%M:%S') ──────────────────────────"
  echo "argv: $*"
} >> "$LOG_FILE"

URL="${1:-}"
if [[ -z "$URL" ]]; then
  msg='launch_fix.sh: missing URL argument. Usage: launch_fix.sh "mycalfix://fix?..."'
  echo "$msg" | tee -a "$LOG_FILE" >&2
  show_alert "$msg"
  exit 2
fi

# Parse + validate URL in Python. Output is `key=shlex_quoted_value` lines,
# safe to eval. Validates scheme/action and cross-checks pr URL belongs to
# the same repo as the `repo` field (defends against repo=safe/x but
# pr=evil/y/pull/1 trickery).
eval "$(python3 - "$URL" <<'PYEOF'
import re, sys, shlex, urllib.parse
p = urllib.parse.urlparse(sys.argv[1])
def err(m):
    print(f"URL_ERROR={shlex.quote(m)}")
    sys.exit(0)
if (p.scheme or "").lower() != "mycalfix":
    err(f"URL scheme 不是 mycalfix: {p.scheme!r}")
if (p.netloc or "").lower() != "fix":
    err(f"URL action 不是 fix: {p.netloc!r}")
q = urllib.parse.parse_qs(p.query, keep_blank_values=False)
def first(k):
    v = q.get(k, [""])
    return v[0] if v else ""
repo, pr = first("repo"), first("pr")
m = re.match(r"https?://[^/]+/([^/]+/[^/]+)/(?:pull|issues)/\d+", pr) if pr else None
if pr and (not m or m.group(1) != repo):
    err(f"pr URL 与 repo 不一致: pr={pr!r} repo={repo!r}")
for k in ("repo", "branch", "comment", "pr", "origin_cwd"):
    print(f"{k}={shlex.quote(first(k))}")
PYEOF
)"

if [[ -n "${URL_ERROR:-}" ]]; then
  echo "$URL_ERROR" >> "$LOG_FILE"
  show_alert "$URL_ERROR"
  exit 2
fi

echo "parsed: repo=$repo branch=$branch pr=$pr origin_cwd=$origin_cwd" >> "$LOG_FILE"

if [[ -z "$repo" || -z "$pr" || -z "$branch" || -z "$comment" ]]; then
  msg="launch_fix.sh: 必填字段缺失 (repo/pr/branch/comment). URL=$URL"
  echo "$msg" >> "$LOG_FILE"
  show_alert "$msg"
  exit 3
fi

# Resolve + validate origin_cwd. URL-supplied path failure → silent picker
# fallback. Picker failure → alert + abort.
need_prompt=false
if [[ -z "$origin_cwd" || ! -d "$origin_cwd" ]]; then
  need_prompt=true
elif ! validate_origin_cwd "$origin_cwd" "$repo"; then
  echo "  origin_cwd from URL failed validation; falling back to picker" >> "$LOG_FILE"
  need_prompt=true
fi

if $need_prompt; then
  chosen=$(prompt_for_folder "$repo")
  chosen="${chosen%$'\n'}"
  if [[ -z "$chosen" || ! -d "$chosen" ]]; then
    echo "  picker cancelled or invalid" >> "$LOG_FILE"
    exit 4
  fi
  if ! validate_origin_cwd "$chosen" "$repo"; then
    msg="MyCalFix: 选择的目录不是 $repo 的本地 checkout（缺 .git 或 remote.origin.url 不匹配），放弃。"
    echo "$msg" >> "$LOG_FILE"
    show_alert "$msg"
    exit 4
  fi
  origin_cwd="$chosen"
fi

if [[ ! -f "$PROMPT_FILE" ]]; then
  msg="launch_fix.sh: prompt file not found: $PROMPT_FILE"
  echo "$msg" >> "$LOG_FILE"
  show_alert "$msg"
  exit 5
fi

rendered_prompt=$(
  COMMENT_URL="$comment" PR_URL="$pr" BRANCH="$branch" \
  python3 -c '
import os, sys
text = sys.stdin.read()
text = text.replace("{comment_url}", os.environ["COMMENT_URL"])
text = text.replace("{pr_url}", os.environ["PR_URL"])
text = text.replace("{branch}", os.environ["BRANCH"])
sys.stdout.write(text)
' < "$PROMPT_FILE"
)

# shlex.quote handles backticks/$/quotes/newlines in rendered_prompt.
cmd=$(
  CWD="$origin_cwd" BRANCH="$branch" PROMPT="$rendered_prompt" \
  python3 -c '
import os, shlex
cwd = os.environ["CWD"]
branch = os.environ["BRANCH"]
prompt = os.environ["PROMPT"]
parts = [
    "cd " + shlex.quote(cwd),
    "echo \x27[MyCalFix] fetching origin/\x27" + shlex.quote(branch),
    "git fetch origin " + shlex.quote(branch),
    "git checkout " + shlex.quote(branch),
    "git pull --ff-only origin " + shlex.quote(branch),
    "claude " + shlex.quote(prompt),
]
print(" && ".join(parts))
'
)

echo "  opening Terminal: $cmd" >> "$LOG_FILE"

# Pass cmd via osascript argv so embedded quotes/backslashes can't break the AS string literal.
osascript - "$cmd" >>"$LOG_FILE" 2>&1 <<'APPLESCRIPT'
on run argv
    tell application "Terminal"
        activate
        do script (item 1 of argv)
    end tell
end run
APPLESCRIPT

echo "  Terminal launched OK" >> "$LOG_FILE"
