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
# Where origin_cwd is validated:
#   The launcher (.app process) does NOT pre-validate origin_cwd: it runs inside
#   the .app's TCC sandbox where `git -C ~/Desktop/<repo>` returns EPERM, which
#   would bounce every URL to a picker. Validation moved into the Terminal-side
#   `.command` script, where Terminal.app's own TCC permissions apply: before
#   any fetch/worktree, the script runs `git -C <origin_cwd> remote get-url
#   origin` and compares (normalized) against the URL's `repo=owner/name`. A
#   wrong repo aborts the script and leaves the error visible in the Terminal
#   window — no silent execution against the wrong remote.
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
MYCALFIX_CONFIG="$HERE/mycalfix_config.py"
mkdir -p "$LOG_DIR"

# Read claude CLI flag from ~/.config/my-calendar/config.json. Default is
# empty (interactive: each tool call asks for approval); set
# `mycalfix_interactive_claude: false` to opt into
# `--dangerously-skip-permissions` (yolo) in the disposable worktree.
#
# Fail-CLOSED: if the helper is missing or crashes, fall back to interactive
# (empty flag). A partial install or a broken helper must NOT silently
# upgrade the click to no-approval tool execution — codex flagged the
# previous fail-open behaviour as a blocker on PR #22.
if ! CLAUDE_FLAG=$(python3 "$MYCALFIX_CONFIG" claude-flag 2>>"$LOG_FILE"); then
  CLAUDE_FLAG=""
  echo "  warn: mycalfix_config.py failed; failing CLOSED to interactive (CLAUDE_FLAG empty)" >> "$LOG_FILE"
fi
# Trim trailing newline from the python output.
CLAUDE_FLAG="${CLAUDE_FLAG%$'\n'}"
echo "  CLAUDE_FLAG: ${CLAUDE_FLAG:-<interactive (empty)>}" >> "$LOG_FILE"

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

# Flow (emitted as multi-line bash so the remote-validation gate can use
# control flow). shlex.quote handles backticks/$/quotes/newlines in every
# interpolated value.
#   1. Validate `git -C <origin_cwd> remote get-url origin` (normalized) ==
#      "<repo>". If not, abort with a clear error and keep the window open.
#   2. fetch origin/<branch> into the user's main repo (.git is shared).
#   3. `git worktree add -b <local_branch> <worktree_dir> origin/<branch>`
#      creates a fresh worktree on a brand-new local branch started from
#      origin/<branch>. The user's main checkout in <origin_cwd> is untouched.
#   4. cd into the worktree and launch claude.
# claude is told (via fix_prompt.md) to push back with `git push origin
# HEAD:<branch>` and to print a `git worktree remove` command for cleanup.
# NOTE on quoting: we feed the python source via `python3 - <<'PYEOF'` heredoc,
# NOT via `python3 -c '<source>'`. The python source below contains many literal
# single quotes (e.g. `printf '%s' ...`, `sed -E 's|...'`). With `python3 -c`,
# bash's outer single-quoted string would be closed by the first literal `'`
# inside the python source, leaving the rest as unquoted shell tokens — exactly
# the regression that broke this path in PR #19 (issue #25): `s|(\\.git)?/*$||`
# would parse as glob/$var/path tokens and bash would abort with
# `syntax error near unexpected token ?/*$'`. `bash -n` doesn't catch it
# because it only static-parses; substitution bodies aren't expanded. The
# `<<'PYEOF'` form keeps every byte of the python source literal — single
# quotes inside have no special meaning to the shell. All data still flows in
# via os.environ so the script needs no shell-side interpolation.
cmd=$(
  CWD="$origin_cwd" REPO="$repo" BRANCH="$branch" PROMPT="$rendered_prompt" \
  WORKTREE_DIR="$worktree_dir" WORKTREE_ROOT="$worktree_root" LOCAL_BRANCH="$local_branch" \
  CLAUDE_FLAG="$CLAUDE_FLAG" \
  python3 - <<'PYEOF'
import os, shlex
cwd = shlex.quote(os.environ["CWD"])
repo = shlex.quote(os.environ["REPO"])
branch = shlex.quote(os.environ["BRANCH"])
prompt = shlex.quote(os.environ["PROMPT"])
worktree_dir = shlex.quote(os.environ["WORKTREE_DIR"])
worktree_root = shlex.quote(os.environ["WORKTREE_ROOT"])
local_branch = shlex.quote(os.environ["LOCAL_BRANCH"])
refspec = shlex.quote("+refs/heads/{0}:refs/remotes/origin/{0}".format(os.environ["BRANCH"]))
origin_ref = shlex.quote("origin/" + os.environ["BRANCH"])
# CLAUDE_FLAG is one of two fixed literals: "--dangerously-skip-permissions"
# or "" (interactive mode). Splice unquoted so the empty string yields no
# argument (NOT `claude '' <prompt>`, which would feed claude an empty prompt
# arg). The two possible values have no shell metacharacters.
claude_flag = os.environ["CLAUDE_FLAG"]
claude_invocation = (
    "claude " + claude_flag + " " + prompt if claude_flag else "claude " + prompt
)
print(f"""set -euo pipefail
# Helper: print error + keep the Terminal window open so the user can read it.
# Without this, `set -e` would silently exit and Terminal might close the tab.
mycalfix_abort() {{
  printf '[MyCalFix] error: %s\\n' "$1" >&2
  exec bash -l
}}
printf '[MyCalFix] validating origin remote on %s...\\n' {cwd}
if ! actual_url=$(git -C {cwd} remote get-url origin 2>/dev/null); then
  printf 'Path is not a git repo, or has no "origin" remote configured.\\n' >&2
  printf 'Refusing to fetch/worktree. Fix origin_cwd in the calendar event URL\\n' >&2
  printf 'or pick a different folder.\\n' >&2
  mycalfix_abort "git -C $(printf '%s' {cwd}) remote get-url origin failed"
fi
# Normalize: strip optional trailing .git and/or trailing slashes, then drop
# everything up to and including github.com[:/]. Handles
# git@github.com:OWNER/REPO, https://github.com/OWNER/REPO,
# https://github.com/OWNER/REPO.git/, https://github.com/OWNER/REPO/.
# Non-github remotes fall through unchanged and trip the != check below.
actual_repo=$(printf '%s' "$actual_url" | sed -E 's|(\\.git)?/*$||' | sed -E 's|^.*github\\.com[:/]||')
if [ "$actual_repo" != {repo} ]; then
  printf '[MyCalFix] error: origin_cwd points at a different repo.\\n' >&2
  printf '  origin_cwd: %s\\n' {cwd} >&2
  printf '  actual origin: %s\\n' "$actual_url" >&2
  printf '  expected repo: %s\\n' {repo} >&2
  printf 'Refusing to fetch/worktree (would push to wrong remote).\\n' >&2
  exec bash -l
fi
printf '[MyCalFix] origin matches %s — proceeding\\n' "$actual_repo"
mkdir -p {worktree_root}
printf '[MyCalFix] fetching origin/%s\\n' {branch}
git -C {cwd} fetch origin {refspec} || mycalfix_abort "git fetch origin failed"
printf '[MyCalFix] creating worktree at %s\\n' {worktree_dir}
git -C {cwd} worktree add -b {local_branch} {worktree_dir} {origin_ref} || mycalfix_abort "git worktree add failed"
cd {worktree_dir} || mycalfix_abort "cd to worktree failed"
{claude_invocation}""")
PYEOF
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
