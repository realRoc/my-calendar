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
  local dir="$1" expected="$2" url normalized git_err
  # Capture stderr separately: bundled launcher runs in the .app's TCC sandbox.
  # If $dir is under ~/Desktop/~/Documents/~/Downloads and the .app lacks the
  # corresponding NSXxxFolderUsageDescription, git fails with "Operation not
  # permitted" and the user otherwise sees a misleading "not a git worktree".
  if ! git_err=$(git -C "$dir" rev-parse --git-dir 2>&1 >/dev/null); then
    echo "  validate: git -C $dir rev-parse failed: ${git_err:-<no stderr>}" >> "$LOG_FILE"
    if [[ "$git_err" == *"Operation not permitted"* || "$git_err" == *"permission denied"* ]]; then
      show_alert "MyCalFix: 读取 $dir 被 macOS 拒绝（TCC）。去 系统设置 → 隐私与安全性 → 文件与文件夹 → MyCalFix 勾选对应文件夹，然后再点一次链接。"
    fi
    return 1
  fi
  url=$(git -C "$dir" config --get remote.origin.url 2>/dev/null || true)
  # Allow `.` in repo names (e.g. owner/my.repo) — GitHub repo names may
  # contain dots. The lazy `+?` combined with the optional `.git` suffix and
  # `/?$` anchor still strips `.git` cleanly from both SSH and HTTPS URLs.
  normalized=$(python3 -c '
import sys, re
m = re.search(r"[:/]([^/:]+/[^/:]+?)(?:\.git)?/?$", sys.argv[1])
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

# Parse + validate URL via external module so unit tests can import it.
# Validates scheme/action, cross-checks pr URL repo, and constrains comment to
# a GitHub PR comment URL on the same repo + PR (rejects control chars).
#
# Capture stdout + exit status separately. Bare `eval "$(python3 …)"` swallows
# a missing python3 / parser crash silently — the subshell prints nothing,
# `set -u` then trips on unbound `URL_ERROR`/`repo`/… below with a cryptic
# message instead of going through `show_alert`. Surface the failure here.
if ! parser_output=$(python3 "$HERE/parse_fix_url.py" "$URL" 2>>"$LOG_FILE"); then
  msg="MyCalFix: URL 解析器执行失败 (python3 不可用或脚本异常)，查看 $LOG_FILE"
  echo "$msg" >> "$LOG_FILE"
  show_alert "$msg"
  exit 2
fi
eval "$parser_output"

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

# Refuse to fetch/checkout on top of uncommitted work. The generated Terminal
# command runs `git checkout <branch> && git pull --ff-only`, which would
# carry the user's dirty tree onto the target branch before claude even gets
# a chance to abort. Bail here instead.
dirty=$(git -C "$origin_cwd" status --porcelain 2>/dev/null || echo __ERR__)
if [[ "$dirty" == "__ERR__" ]]; then
  msg="MyCalFix: 无法在 $origin_cwd 跑 git status，放弃。"
  echo "$msg" >> "$LOG_FILE"
  show_alert "$msg"
  exit 6
fi
if [[ -n "$dirty" ]]; then
  msg="MyCalFix: $origin_cwd 有未提交的改动，请先 commit/stash 后再点修复入口。"
  echo "  dirty worktree, aborting" >> "$LOG_FILE"
  echo "$dirty" | head -10 >> "$LOG_FILE"
  show_alert "$msg"
  exit 6
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
# Fetch uses an explicit `+refs/heads/<b>:refs/remotes/origin/<b>` refspec so
# `git switch <b>` works in both directions: local branch exists (just check it
# out) and local branch missing (auto-create tracking from origin/<b>). Plain
# `git fetch origin <b>` only writes FETCH_HEAD in some configs, and the
# follow-up `git checkout <b>` then fails when the local branch isn't there.
cmd=$(
  CWD="$origin_cwd" BRANCH="$branch" PROMPT="$rendered_prompt" \
  python3 -c '
import os, shlex
cwd = os.environ["CWD"]
branch = os.environ["BRANCH"]
prompt = os.environ["PROMPT"]
refspec = "+refs/heads/{0}:refs/remotes/origin/{0}".format(branch)
parts = [
    "cd " + shlex.quote(cwd),
    "echo \x27[MyCalFix] fetching origin/\x27" + shlex.quote(branch),
    "git fetch origin " + shlex.quote(refspec),
    "git switch " + shlex.quote(branch),
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
