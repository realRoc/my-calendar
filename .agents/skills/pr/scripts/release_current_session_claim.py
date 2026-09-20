#!/usr/bin/env python3
"""Release a failed current-session my-calendar PR claim safely."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--pr-url", required=True)
    parser.add_argument("--my-calendar", required=True, type=Path)
    args = parser.parse_args()

    scripts_dir = args.my_calendar.resolve() / "scripts"
    if not scripts_dir.is_dir():
        print(f"ERROR: my-calendar scripts directory is missing: {scripts_dir}", file=sys.stderr)
        return 1
    sys.path.insert(0, str(scripts_dir))

    try:
        import pr_session_review  # type: ignore[import-not-found]
        import pr_watcher  # type: ignore[import-not-found]

        pr = pr_session_review.fetch_pr(args.pr_url)
        lock_fd = pr_watcher.acquire_pr_lock_nb(pr.url)
        if lock_fd is None:
            raise ValueError("PR review lock is held; claim was not released")
        try:
            state = pr_watcher.load_state()
            entry = state.setdefault("prs", {}).setdefault(pr.url, {})
            if entry.get("pending_review_sha") != pr.head_sha:
                print("MY_CALENDAR_CLAIM_RELEASE=noop")
                return 0
            if entry.get("pending_review_source") != "current-session":
                raise ValueError("pending review belongs to a different source")
            if entry.get("last_commented_sha") == pr.head_sha:
                raise ValueError("current SHA is already recorded")

            lookup = pr_watcher.fetch_latest_ai_comment_since(
                pr.repo,
                pr.number,
                entry.get("pending_review_started_at"),
                head_sha=pr.head_sha,
            )
            if lookup.status != "absent":
                raise ValueError(
                    f"AI comment lookup is {lookup.status}; keeping claim to avoid a duplicate"
                )

            pr_watcher._clear_pending_review(entry)
            pr_watcher.save_state(state, touched_prs={pr.url})
        finally:
            pr_watcher.release_lock_fd(lock_fd)
    except (OSError, ValueError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1

    print("MY_CALENDAR_CLAIM_RELEASE=ok")
    print(f"HEAD_SHA={pr.head_sha}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
