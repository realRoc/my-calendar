#!/bin/bash
# MyCalFix URL handler: parse mycalfix://fix?... → open Terminal → fetch+checkout → claude
#
# Triggered by app/MyCalFix.app via `on open location`. Also runnable manually:
#   scripts/launch_fix.sh 'mycalfix://fix?repo=foo%2Fbar&branch=feat&comment=...&pr=...&origin_cwd=/path'
#
# Logs every invocation to logs/launch_fix.log for debugging URL scheme dispatch.

set -euo pipefail

HERE=$(cd "$(dirname "$0")" && pwd)
ROOT=$(cd "$HERE/.." && pwd)
LOG_DIR="$ROOT/logs"
LOG_FILE="$LOG_DIR/launch_fix.log"
PROMPT_FILE="$HERE/fix_prompt.md"

mkdir -p "$LOG_DIR"
{
  echo "─── $(date '+%Y-%m-%d %H:%M:%S') ──────────────────────────"
  echo "argv: $*"
} >> "$LOG_FILE"

URL="${1:-}"
if [[ -z "$URL" ]]; then
  msg='launch_fix.sh: missing URL argument. Usage: launch_fix.sh "mycalfix://fix?..."'
  echo "$msg" | tee -a "$LOG_FILE" >&2
  osascript -e "display alert \"MyCalFix\" message \"$msg\"" >/dev/null 2>&1 || true
  exit 2
fi

# ─── parse query string ───
qs="${URL#*\?}"
if [[ "$qs" == "$URL" ]]; then
  qs=""  # no ? in URL
fi

urldecode() {
  # NOTE: do NOT add `--` between -c "..." and "$1"; Python treats everything
  # after -c as positional args, so `--` would land in sys.argv[1] and the real
  # value would be sys.argv[2] (which we don't read). Pass the value directly.
  python3 -c 'import sys, urllib.parse; print(urllib.parse.unquote(sys.argv[1]))' "$1"
}

repo=""; branch=""; comment=""; pr=""; origin_cwd=""
old_ifs="$IFS"
IFS='&'
for kv in $qs; do
  [[ -z "$kv" ]] && continue
  k="${kv%%=*}"
  v="${kv#*=}"
  case "$k" in
    repo)       repo=$(urldecode "$v") ;;
    branch)     branch=$(urldecode "$v") ;;
    comment)    comment=$(urldecode "$v") ;;
    pr)         pr=$(urldecode "$v") ;;
    origin_cwd) origin_cwd=$(urldecode "$v") ;;
  esac
done
IFS="$old_ifs"

{
  echo "parsed:"
  echo "  repo=$repo"
  echo "  branch=$branch"
  echo "  pr=$pr"
  echo "  comment=$comment"
  echo "  origin_cwd=$origin_cwd"
} >> "$LOG_FILE"

if [[ -z "$pr" || -z "$branch" || -z "$comment" ]]; then
  msg="launch_fix.sh: required fields missing (pr / branch / comment). Got: $URL"
  echo "$msg" >> "$LOG_FILE"
  osascript -e "display alert \"MyCalFix\" message \"$msg\"" >/dev/null 2>&1 || true
  exit 3
fi

# ─── fallback: prompt for folder if origin_cwd missing / invalid ───
if [[ -z "$origin_cwd" || ! -d "$origin_cwd" ]]; then
  echo "  origin_cwd missing or invalid — prompting user via osascript" >> "$LOG_FILE"
  chosen=$(osascript <<APPLESCRIPT 2>>"$LOG_FILE" || true
try
    set picked to choose folder with prompt "MyCalFix: 选择 $repo 的本地 checkout 目录"
    return POSIX path of picked
on error
    return ""
end try
APPLESCRIPT
)
  # osascript may emit a trailing newline; trim it
  chosen="${chosen%$'\n'}"
  if [[ -z "$chosen" || ! -d "$chosen" ]]; then
    msg="MyCalFix: 取消或未选择有效目录，放弃。"
    echo "$msg" >> "$LOG_FILE"
    exit 4
  fi
  origin_cwd="$chosen"
fi

# ─── render fix prompt ───
if [[ ! -f "$PROMPT_FILE" ]]; then
  msg="launch_fix.sh: prompt file not found: $PROMPT_FILE"
  echo "$msg" >> "$LOG_FILE"
  osascript -e "display alert \"MyCalFix\" message \"$msg\"" >/dev/null 2>&1 || true
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

# ─── build Terminal command, properly quoted ───
# Use Python's shlex.quote for bulletproof shell escaping (rendered_prompt has
# backticks, $-signs, quotes, newlines).
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

# Escape backslashes and double quotes for embedding inside AppleScript string
# (AppleScript does NOT interpret \n inside "..." — it takes them literally, but
#  do script handles \n; we keep the cmd on one line via &&)
applescript_cmd=$(printf '%s' "$cmd" | python3 -c '
import sys
s = sys.stdin.read()
s = s.replace("\\", "\\\\").replace("\"", "\\\"")
sys.stdout.write(s)
')

echo "  opening Terminal: $cmd" >> "$LOG_FILE"

osascript <<APPLESCRIPT
tell application "Terminal"
    activate
    do script "$applescript_cmd"
end tell
APPLESCRIPT

echo "  Terminal launched OK" >> "$LOG_FILE"
