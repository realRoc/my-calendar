"""User-config knobs for the MyCalFix URL handler.

Lives in its own module (not pr_watcher.py) so that launch_fix.sh can shell
out to a tiny Python helper without pulling in pr_watcher's pyobjc/EventKit
imports.

Knob: mycalfix_interactive_claude (default True).

When True (default): MyCalFix launches plain `claude <prompt>` so the user
approves each tool call. Safe-by-default — a calendar-link click does not
upgrade to no-approval local tool execution even though the prompt body
contains untrusted PR diff / review comments.

When False: MyCalFix launches `claude --dangerously-skip-permissions <prompt>`
(yolo) in the disposable worktree. Faster, but the user explicitly opts into
"no per-tool approval" knowing that fix_prompt.md constraints and the
worktree are NOT real sandboxes — Claude Code can still read ~/.ssh,
~/.config, and write outside the worktree.

Validation contract (matches pr_watcher._read_codex_cap):
  - Missing file / missing key: silent default (interactive)
  - Wrong type (str, int, float, list, None): warn on stderr, fall back to
    default (interactive). We use `type(x) is bool` (not isinstance) so an
    accidental `"true"` string doesn't silently flip the flag.
  - Malformed JSON: warn on stderr, fall back to default (interactive)

Fail-closed: every error path returns the safer mode (interactive). A
malformed config or unreadable file must not silently downgrade to yolo —
that's exactly the behaviour codex flagged on PR #22 as a blocker.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

USER_CONFIG_PATH = Path.home() / ".config" / "my-calendar" / "config.json"

DEFAULT_INTERACTIVE_CLAUDE = True
CONFIG_KEY = "mycalfix_interactive_claude"


def read_interactive_claude(
    config_path: Path = USER_CONFIG_PATH,
    default: bool = DEFAULT_INTERACTIVE_CLAUDE,
) -> bool:
    """Return whether MyCalFix should run claude in interactive mode.

    True  → emit plain `claude <prompt>` (each tool call asks for approval).
    False → emit `claude --dangerously-skip-permissions <prompt>` (yolo).
    """
    if not config_path.exists():
        return default
    try:
        cfg = json.loads(config_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        print(
            f"[mycalfix] warn: cannot read {config_path}: {exc}; "
            f"using {CONFIG_KEY}={default}",
            file=sys.stderr,
        )
        return default
    if not isinstance(cfg, dict) or CONFIG_KEY not in cfg:
        return default
    raw = cfg[CONFIG_KEY]
    if type(raw) is not bool:  # noqa: E721 — `True` is also an int; strict-check
        print(
            f"[mycalfix] warn: {CONFIG_KEY}={raw!r} in {config_path} "
            f"is not a JSON boolean (got {type(raw).__name__}); "
            f"using default {default}",
            file=sys.stderr,
        )
        return default
    return raw


def claude_flag(
    config_path: Path = USER_CONFIG_PATH,
    default: bool = DEFAULT_INTERACTIVE_CLAUDE,
) -> str:
    """Shell-emit the CLI flag string for claude.

    Returns '' by default (interactive) or '--dangerously-skip-permissions'
    when the user has explicitly opted into yolo mode. Returned string is
    safe to splice into a shell command without quoting — the two possible
    values are both fixed literals with no metacharacters.
    """
    interactive = read_interactive_claude(config_path=config_path, default=default)
    return "" if interactive else "--dangerously-skip-permissions"


if __name__ == "__main__":
    # CLI entry for launch_fix.sh:  python3 mycalfix_config.py claude-flag
    if len(sys.argv) >= 2 and sys.argv[1] == "claude-flag":
        sys.stdout.write(claude_flag())
        sys.stdout.write("\n")
    else:
        print("usage: mycalfix_config.py claude-flag", file=sys.stderr)
        sys.exit(2)
