#!/bin/bash
# MyCalFix URL handler: parse mycalfix://fix?... → write the command to a
# /tmp/*.command file → `open -a Terminal` opens it in a new window → Terminal
# creates a disposable worktree under ~/.cache/my-calendar/worktrees/ → launches
# claude. Worktree-based to avoid touching the user's main checkout (no dirty-
# tree abort, no branch switch). claude pushes back via `git push origin HEAD:<b>`.
#
# Triggered by ~/Applications/MyCalFix.app via `on open location`. Also
# runnable manually:
#   scripts/launch_fix.sh 'mycalfix://fix?repo=foo%2Fbar&branch=feat&...'
#
# Why a .command file (not `osascript do script`)?
#   `osascript do script` sends an AppleEvent to Terminal.app. macOS treats that
#   as Automation (TCC's "AppleEvents" class) and requires user authorization
#   for the sending bundle ID. Unsigned osacompile-produced apps frequently get
#   silently denied with errAEEventNotPermitted (-1743) and no prompt. A
#   .command file opened via `open -a Terminal` is a normal user-intent action,
#   needs no AppleEvents permission, and works on first run.
#
# Why no `git rev-parse` validation of origin_cwd?
#   The launcher runs inside the .app's TCC sandbox. Reading a repo under
#   ~/Desktop/~/Documents/~/Downloads is gated by TCC, and once the user (or
#   macOS) records a deny, `git -C <dir>` fails with EPERM regardless of the
#   Info.plist usage-description keys. Validation would then bounce every URL
#   to a folder picker even when the URL contained a perfectly valid path. We
#   trust the URL's origin_cwd: it's written by the local pre-push hook into
#   pr_state.json, so the attack surface is effectively zero. A wrong path is
#   surfaced clearly inside Terminal when `git -C <wrong> fetch` errors out.
#
# Security:
#   - parse_fix_url.py validates scheme/action and that the comment URL is on
#     the same repo+PR as the pr field (rejects control chars).
#   - The Terminal command is written to a .command file we own; no string
#     interpolation into AppleScript source.

set -euo pipefail

HERE=$(cd "$(dirname "$0")" && pwd)
# ~/Library/Logs/ rather than repo logs/: bundled launcher has no TCC access
# to the user's repo if it sits under ~/Desktop/~/Documents/~/Downloads.
LOG_DIR="$HOME/Library/Logs/MyCalFix"
LOG_FILE="$LOG_DIR/launch_fix.log"
PROMPT_FILE="$HERE/fix_prompt.md"
mkdir -p "$LOG_DIR"

# osascript helpers for the .app's own UI. These display dialogs owned by
# osascript itself — no AppleEvent sent to other apps, so no Automation TCC
# requirement. (Distinct from `do script Terminal`, which we deliberately don't use.)
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

# origin_cwd resolution: trust the URL if present, otherwise prompt for a folder.
# No git rev-parse here (see file header for rationale). The Terminal-side
# `git fetch` will surface a clear error if the path is wrong.
if [[ -z "$origin_cwd" ]]; then
  echo "  origin_cwd missing from URL; prompting picker" >> "$LOG_FILE"
  chosen=$(prompt_for_folder "$repo")
  chosen="${chosen%$'\n'}"
  if [[ -z "$chosen" ]]; then
    echo "  picker cancelled" >> "$LOG_FILE"
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

# Compute worktree path + temporary local branch name now (in this process)
# so we can render them into the prompt before passing it to claude. claude
# needs the literal path/branch to print the cleanup command at the end and
# to push correctly. Worktree dir lives under ~/.cache (not Desktop/Documents/
# Downloads — those are TCC-protected and would block git inside Terminal in
# certain installs).
ts=$(date +%Y%m%d-%H%M%S)
safe_repo=${repo//\//__}
worktree_root="$HOME/.cache/my-calendar/worktrees"
worktree_dir="$worktree_root/${safe_repo}__${branch}__${ts}"
# Unique local branch name avoids `git worktree add` collision when the user
# already has $branch checked out in origin_cwd. claude pushes via
# `git push origin HEAD:<branch>` so the remote branch name is preserved.
local_branch="mycalfix/${branch}-${ts}"

rendered_prompt=$(
  COMMENT_URL="$comment" PR_URL="$pr" BRANCH="$branch" \
  WORKTREE_DIR="$worktree_dir" ORIGIN_CWD="$origin_cwd" LOCAL_BRANCH="$local_branch" \
  python3 -c '
import os, sys
text = sys.stdin.read()
for key in ("COMMENT_URL", "PR_URL", "BRANCH", "WORKTREE_DIR", "ORIGIN_CWD", "LOCAL_BRANCH"):
    text = text.replace("{" + key.lower() + "}", os.environ[key])
sys.stdout.write(text)
' < "$PROMPT_FILE"
)

# shlex.quote handles backticks/$/quotes/newlines in rendered_prompt.
# Flow:
#   1. fetch origin/<branch> into the user's main repo (.git is shared)
#   2. `git worktree add -b <local_branch> <worktree_dir> origin/<branch>`
#      creates a fresh worktree at <worktree_dir> on a brand new local
#      branch started from origin/<branch>. The user's main checkout in
#      <origin_cwd> is untouched.
#   3. cd into the worktree and launch claude.
# claude is told (via fix_prompt.md) to push back with `git push origin
# HEAD:<branch>` and to print a `git worktree remove` command for cleanup.
cmd=$(
  CWD="$origin_cwd" BRANCH="$branch" PROMPT="$rendered_prompt" \
  WORKTREE_DIR="$worktree_dir" WORKTREE_ROOT="$worktree_root" LOCAL_BRANCH="$local_branch" \
  python3 -c '
import os, shlex
cwd = os.environ["CWD"]
branch = os.environ["BRANCH"]
prompt = os.environ["PROMPT"]
worktree_dir = os.environ["WORKTREE_DIR"]
worktree_root = os.environ["WORKTREE_ROOT"]
local_branch = os.environ["LOCAL_BRANCH"]
refspec = "+refs/heads/{0}:refs/remotes/origin/{0}".format(branch)
parts = [
    "mkdir -p " + shlex.quote(worktree_root),
    "echo \x27[MyCalFix] fetching origin/\x27" + shlex.quote(branch),
    "git -C " + shlex.quote(cwd) + " fetch origin " + shlex.quote(refspec),
    "echo \x27[MyCalFix] creating worktree at \x27" + shlex.quote(worktree_dir),
    "git -C " + shlex.quote(cwd) + " worktree add -b " + shlex.quote(local_branch)
        + " " + shlex.quote(worktree_dir) + " " + shlex.quote("origin/" + branch),
    "cd " + shlex.quote(worktree_dir),
    "claude " + shlex.quote(prompt),
]
print(" && ".join(parts))
'
)

# Write the command to a .command file and let `open -a Terminal` execute it
# in a new window. Bypasses AppleEvents (Automation) TCC entirely.
# /tmp lives outside TCC-protected folders, so writing+reading is unrestricted.
# Terminal.app retains its own permissions for ~/Desktop/etc., which it'll
# request the first time the embedded `git -C <path>` actually touches such a path.
tmpscript=$(mktemp -t mycalfix.XXXXXX)
mv "$tmpscript" "${tmpscript}.command"
tmpscript="${tmpscript}.command"

{
  printf '#!/bin/bash\n'
  # Self-delete after Terminal has read us into memory. bash slurps the entire
  # script before executing, so removing $0 mid-script is safe.
  printf 'rm -f -- %s\n' "$(printf '%q' "$tmpscript")"
  printf '%s\n' "$cmd"
  # Keep the window around after claude exits so the user can read the cleanup
  # command and any output. Login shell so PATH is fully populated.
  printf 'exec bash -l\n'
} > "$tmpscript"
chmod +x "$tmpscript"

echo "  launching via .command: $tmpscript" >> "$LOG_FILE"
echo "  command body: $cmd" >> "$LOG_FILE"

# `open -a Terminal <file>` opens the file in Terminal.app. No AppleEvent
# needed; macOS launches Terminal via LaunchServices and Terminal's own
# document-open path reads the file. Foregrounds Terminal automatically.
if ! open -a Terminal "$tmpscript" 2>>"$LOG_FILE"; then
  msg="MyCalFix: 无法用 Terminal 打开命令文件 $tmpscript"
  echo "$msg" >> "$LOG_FILE"
  show_alert "$msg"
  exit 6
fi

echo "  Terminal launched OK" >> "$LOG_FILE"
