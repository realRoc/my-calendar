"""User-config knobs for the MyCalFix URL handler.

Lives in its own module (not pr_watcher.py) so that launch_fix.sh can shell
out to a tiny Python helper without pulling in pr_watcher's pyobjc/EventKit
imports.

Knob: mycalfix_interactive_claude (default False).

When False (default): MyCalFix launches `claude --dangerously-skip-permissions
<prompt>` in the disposable worktree. The fix session runs without
per-tool-call approval prompts. Safe-by-construction because (a) the worktree
is isolated under ~/.cache/my-calendar/worktrees/, (b) the launcher is
user-triggered via the mycalfix:// URL, (c) fix_prompt.md has hard
constraints (diff cap, scope cap, no --force push), and (d) Terminal-side
origin validation guarantees the wrong remote can't be pushed to.

When True: MyCalFix launches plain `claude <prompt>` so the user can approve
each tool call individually. Slower but lets a careful user supervise a PR
they're unsure about.

Validation contract (matches pr_watcher._read_codex_cap):
  - Missing file / missing key: silent default
  - Wrong type (str, int, float, list, None): warn on stderr, fall back to
    default. We use `type(x) is bool` (not isinstance) so an accidental
    `"true"` string doesn't silently flip the flag.
  - Malformed JSON: warn on stderr, fall back to default
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

USER_CONFIG_PATH = Path.home() / ".config" / "my-calendar" / "config.json"

DEFAULT_INTERACTIVE_CLAUDE = False
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

    Returns either '--dangerously-skip-permissions' (default) or '' (when the
    user opted into interactive mode). Returned string is safe to splice into
    a shell command without quoting — the two possible values are both
    fixed literals with no metacharacters.
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
