"""Best-effort stdout/stderr redirection for launchd-spawned jobs.

Why this exists: launchd's StandardOutPath opens its target file at spawn
time. If that file's com.apple.macl xattr is corrupted (observed 2026-05-20
after a system restart), launchd silently refuses to spawn at all
(EX_CONFIG=78, zero diagnostics). To sidestep that, plists redirect stdout
to /tmp/*.boot.* and each script reopens its own streams from inside Python
via the MY_CAL_LOG_FILE env var.

Contract: this function MUST NOT raise. Any failure here would re-introduce
exactly the class of bug we're trying to defend against. On failure, fall
back to the inherited stderr (which under launchd is /tmp/*.boot.err) so
the diagnostic is at least recoverable.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path


def redirect_stdio_to_log() -> None:
    log_file = os.environ.get("MY_CAL_LOG_FILE")
    if not log_file:
        return
    try:
        p = Path(log_file)
        p.parent.mkdir(parents=True, exist_ok=True)
        f = open(p, "a", buffering=1)
    except OSError as e:
        print(
            f"[my-calendar] redirect_stdio_to_log: cannot open MY_CAL_LOG_FILE={log_file}: {e}",
            file=sys.stderr,
        )
        return
    sys.stdout = f
    sys.stderr = f
