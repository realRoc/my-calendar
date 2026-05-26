"""User-config knobs for the MyCalFix URL handler.

Lives in its own module (not pr_watcher.py) so that launch_fix.sh can shell
out to a tiny Python helper without pulling in pr_watcher's pyobjc/EventKit
imports.

Current policy: MyCalFix unconditionally launches claude with
`--dangerously-skip-permissions` (yolo). The previous
`mycalfix_interactive_claude` knob has been removed at the user's request —
the interactive approval flow is no longer offered. Any pre-existing setting
for that key in `~/.config/my-calendar/config.json` is silently ignored.

Safety context (unchanged from prior design):
- claude runs inside a disposable git worktree under
  `~/.cache/my-calendar/worktrees/`; the user's main checkout is untouched.
- `launch_fix.sh` validates `git remote get-url origin` matches the URL's
  repo before any fetch/worktree, so push cannot land on the wrong remote.
- `fix_prompt.md` constrains scope (diff cap, only review-named files, no
  --force push). It is not a sandbox; it is a guardrail.
"""

from __future__ import annotations

import sys

# Single fixed policy: yolo. No config file is read.
CLAUDE_FLAG_VALUE = "--dangerously-skip-permissions"


def claude_flag() -> str:
    """Return the CLI flag string to splice into the claude invocation.

    Always `--dangerously-skip-permissions`. The returned string has no shell
    metacharacters, so it is safe to splice unquoted into the shell command
    launch_fix.sh emits."""
    return CLAUDE_FLAG_VALUE


if __name__ == "__main__":
    # CLI entry for launch_fix.sh:  python3 mycalfix_config.py claude-flag
    if len(sys.argv) >= 2 and sys.argv[1] == "claude-flag":
        sys.stdout.write(claude_flag())
        sys.stdout.write("\n")
    else:
        print("usage: mycalfix_config.py claude-flag", file=sys.stderr)
        sys.exit(2)
