#!/usr/bin/env python3
"""Move legacy mycalfix:// EventKit URLs into PR event notes.

iOS Calendar renders custom schemes stored in EKEvent.url as a misleading
"call" action. New events avoid that field; this one-shot migration fixes
older PR monitor events by clearing EKEvent.url and preserving the launcher
URL in the event notes.
"""

from __future__ import annotations

import argparse
import shlex
import sys
from pathlib import Path

from EventKit import EKEventStore, EKSpanThisEvent  # type: ignore[import-not-found]

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

from calendar_sync import PR_CALENDAR_NAME, _load_state, _request_access  # noqa: E402

CAL_STATE_PATH = HERE / "pr_calendar_state.json"
SEPARATOR = "-" * 40


def _absolute_url(event) -> str:
    url = event.URL()
    return str(url.absoluteString()) if url else ""


def _notes_with_mycalfix_link(notes: str, url: str) -> str:
    if url in notes:
        return notes
    block = "\n".join(
        [
            SEPARATOR,
            "MyCalFix 链接（Mac 上点击或复制打开）：",
            url,
            "Mac 终端命令：",
            f"open {shlex.quote(url)}",
        ]
    )
    return f"{notes.rstrip()}\n\n{block}" if notes.strip() else block


def migrate(*, dry_run: bool = False) -> tuple[int, int, int]:
    state = _load_state(CAL_STATE_PATH)
    store = EKEventStore.alloc().init()
    _request_access(store)

    scanned = 0
    changed = 0
    missing = 0
    for key, entry in sorted(state.items()):
        event_id = (entry or {}).get("event_id")
        if not event_id:
            continue
        event = store.eventWithIdentifier_(event_id)
        if event is None:
            missing += 1
            continue

        scanned += 1
        url = _absolute_url(event)
        if not url.startswith("mycalfix://fix?"):
            continue

        changed += 1
        print(f"{'would update' if dry_run else 'update'}: {event.title()}  {key}")
        if dry_run:
            continue

        event.setNotes_(_notes_with_mycalfix_link(event.notes() or "", url))
        event.setURL_(None)
        ok, err = store.saveEvent_span_error_(event, EKSpanThisEvent, None)
        if not ok:
            raise RuntimeError(f"failed to save {key}: {err}")

    return scanned, changed, missing


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    scanned, changed, missing = migrate(dry_run=args.dry_run)
    action = "would clear" if args.dry_run else "cleared"
    print(
        f"{action} {changed} legacy mycalfix EventKit URL(s); "
        f"scanned={scanned} missing={missing} calendar={PR_CALENDAR_NAME!r}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
