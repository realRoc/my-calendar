#!/usr/bin/env python3
"""Claim and record PR reviews performed by the current agent session.

The legacy pr_watcher path launches a detached `codex exec` process, waits for
that process to post a GitHub comment, then writes Calendar/state. This helper
is the smaller path used by the `/pr` skill: the active Codex/Claude session
does the review and posts the comment; this script only reserves the SHA and
records the already-posted comment into my-calendar.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
from datetime import datetime
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import pr_watcher  # noqa: E402
from calendar_sync import PR_CALENDAR_NAME  # noqa: E402


PR_URL_RE = re.compile(r"^https://github\.com/([^/]+)/([^/]+)/pull/([0-9]+)$")
COMMENT_URL_RE = re.compile(r"^https://github\.com/([^/]+)/([^/]+)/pull/([0-9]+)#issuecomment-([0-9]+)$")


def parse_pr_url(url: str) -> tuple[str, str, int]:
    m = PR_URL_RE.match(url)
    if not m:
        raise ValueError(f"not a canonical GitHub PR URL: {url}")
    return m.group(1), m.group(2), int(m.group(3))


def parse_comment_url(url: str) -> tuple[str, str, int, str]:
    m = COMMENT_URL_RE.match(url)
    if not m:
        raise ValueError(f"not a canonical GitHub PR comment URL: {url}")
    return m.group(1), m.group(2), int(m.group(3)), m.group(4)


def fetch_pr(pr_url: str) -> pr_watcher.PRSnap:
    parse_pr_url(pr_url)
    return pr_watcher._gh_view_force_pr(pr_url)


def fetch_comment_body(comment_url: str) -> tuple[str, str]:
    owner, repo, _number, comment_id = parse_comment_url(comment_url)
    proc = subprocess.run(
        ["gh", "api", f"repos/{owner}/{repo}/issues/comments/{comment_id}"],
        capture_output=True,
        text=True,
        check=True,
    )
    data = json.loads(proc.stdout)
    html_url = data.get("html_url") or comment_url
    body = data.get("body") or ""
    return html_url, body


def resolve_origin_cwd(origin_cwd: str | None, state_entry: dict | None = None) -> str | None:
    candidate = origin_cwd or (state_entry or {}).get("origin_cwd")
    return pr_watcher._resolved_origin_cwd(candidate)


def validate_comment_for_pr(pr: pr_watcher.PRSnap, comment_url: str, body: str) -> None:
    pr_owner, pr_repo, pr_number = parse_pr_url(pr.url)
    c_owner, c_repo, c_number, _comment_id = parse_comment_url(comment_url)
    if (c_owner, c_repo, c_number) != (pr_owner, pr_repo, pr_number):
        raise ValueError(f"comment URL does not belong to PR {pr.url}: {comment_url}")

    if pr_watcher.AI_COAUTHOR_METADATA_MARKER not in body:
        raise ValueError("comment body is missing the canonical AI coauthor marker")

    head_marker = pr_watcher.head_sha_metadata_marker(pr.head_sha)
    if head_marker not in body:
        raise ValueError(
            "comment body is missing the current head SHA marker "
            f"{head_marker!r}; refusing to record a stale review"
        )


def _safe_id(pr_url: str) -> str:
    return pr_url.replace("https://github.com/", "").replace("/", "_")


def claim_review(pr_url: str, origin_cwd: str | None) -> dict:
    pr = fetch_pr(pr_url)
    lock_fd = pr_watcher.acquire_pr_lock_nb(pr.url)
    if lock_fd is None:
        raise ValueError(
            f"review already in progress for {pr.url}; refusing to start a "
            "current-session duplicate"
        )

    try:
        state = pr_watcher.load_state()
        entry = state.setdefault("prs", {}).setdefault(pr.url, {})
        now = datetime.now()
        resolved_origin = resolve_origin_cwd(origin_cwd, entry)

        if entry.get("last_commented_sha") == pr.head_sha:
            raise ValueError(
                f"review already recorded for {pr.url} sha={pr.head_sha[:8]}"
            )

        if entry.get("pending_review_sha") == pr.head_sha:
            if not pr_watcher._pending_is_stale(entry, now):
                source = entry.get("pending_review_source") or "unknown"
                raise ValueError(
                    f"review already pending for {pr.url} sha={pr.head_sha[:8]} "
                    f"source={source}"
                )

            guard_action, guard_changed = pr_watcher._same_sha_review_guard(pr, state, now)
            if guard_changed:
                pr_watcher.save_state(state, touched_prs={pr.url})
                entry = state.setdefault("prs", {}).setdefault(pr.url, {})
            if guard_action is not None:
                raise ValueError(f"{guard_action} for {pr.url}")

        entry.update({
            "repo": pr.repo,
            "number": pr.number,
            "last_seen_sha": pr.head_sha,
            "pending_review_sha": pr.head_sha,
            "pending_review_started_at": now.isoformat(timespec="seconds"),
            "pending_review_source": "current-session",
        })
        if resolved_origin:
            entry["origin_cwd"] = resolved_origin

        pr_watcher.save_state(state, touched_prs={pr.url})
    finally:
        pr_watcher.release_lock_fd(lock_fd)

    return {
        "status": "claimed",
        "pr_url": pr.url,
        "head_sha": pr.head_sha,
        "origin_cwd": resolved_origin,
    }


def record_review(
    pr_url: str,
    comment_url: str,
    *,
    origin_cwd: str | None,
    thread_id: str | None,
    comment_body_file: str | None,
    dry_run: bool,
) -> dict:
    pr = fetch_pr(pr_url)
    if comment_body_file:
        canonical_comment_url = comment_url
        comment_body = Path(comment_body_file).read_text(encoding="utf-8")
    else:
        canonical_comment_url, comment_body = fetch_comment_body(comment_url)

    validate_comment_for_pr(pr, canonical_comment_url, comment_body)

    state = pr_watcher.load_state()
    entry = state.setdefault("prs", {}).setdefault(pr.url, {})
    resolved_origin = resolve_origin_cwd(origin_cwd, entry)
    now = datetime.now()
    started_at = now.isoformat(timespec="seconds")

    pr_watcher.LOG_DIR.mkdir(parents=True, exist_ok=True)
    stamp = now.strftime("%Y%m%d-%H%M%S")
    jsonl_path = pr_watcher.LOG_DIR / f"{stamp}__{_safe_id(pr.url)}.current-session.jsonl"
    last_path = jsonl_path.with_suffix(".last.txt")
    payload = {
        "type": "current_session_review",
        "pr_url": pr.url,
        "head_sha": pr.head_sha,
        "comment_url": canonical_comment_url,
        "thread_id": thread_id,
        "created_at": started_at,
    }

    if not dry_run:
        jsonl_path.write_text(json.dumps(payload, ensure_ascii=False) + "\n", encoding="utf-8")
        last_path.write_text(comment_body, encoding="utf-8")

    result = pr_watcher.CodexResult(
        thread_id=thread_id,
        last_message=comment_body,
        exit_code=0,
        jsonl_path=jsonl_path,
        scratch_dir=Path("/tmp"),
        cancelled=False,
    )
    event = pr_watcher.build_event(
        pr,
        result,
        canonical_comment_url,
        comment_body,
        now,
        origin_cwd=resolved_origin,
    )

    if dry_run:
        actions = {event.key: "would-upsert"}
        meta_path = jsonl_path.with_suffix(".meta.json")
    else:
        persist_fd = pr_watcher.acquire_persist_lock(pr.url)
        try:
            fresh_state = pr_watcher.load_state()
            fresh_entry = fresh_state.setdefault("prs", {}).setdefault(pr.url, entry)
            existing_sha = fresh_entry.get("last_commented_sha")
            existing_url = fresh_entry.get("last_comment_url")
            if existing_sha == pr.head_sha and existing_url != canonical_comment_url:
                raise ValueError(
                    f"review already recorded for {pr.url} sha={pr.head_sha[:8]} "
                    f"at {existing_url}"
                )

            actions = pr_watcher.upsert_events(
                [event],
                pr_watcher.CAL_STATE_PATH,
                dry_run=False,
                calendar_name=PR_CALENDAR_NAME,
            )
            pr_watcher.cache_comment_body(canonical_comment_url, comment_body)
            meta_path = jsonl_path.with_suffix(".meta.json")
            meta = {
                "started_at": started_at,
                "repo": pr.repo,
                "pr_number": pr.number,
                "pr_url": pr.url,
                "pr_title": pr.title,
                "head_sha": pr.head_sha,
                "thread_id": thread_id,
                "comment_url": canonical_comment_url,
                "comment_body": comment_body,
                "codex_exit": 0,
                "jsonl_path": str(jsonl_path),
                "origin_cwd": resolved_origin,
                "review_mode": "current-session",
            }
            meta_path.write_text(json.dumps(meta, indent=2, ensure_ascii=False), encoding="utf-8")

            fresh_entry.update({
                "repo": pr.repo,
                "number": pr.number,
                "last_commented_sha": pr.head_sha,
                "last_thread_id": thread_id,
                "last_comment_url": canonical_comment_url,
                "last_run_at": started_at,
                "last_codex_exit": 0,
                "last_jsonl": str(jsonl_path),
                "last_seen_sha": pr.head_sha,
            })
            if resolved_origin:
                fresh_entry["origin_cwd"] = resolved_origin
            pr_watcher._clear_pending_review(fresh_entry)
            pr_watcher.save_state(fresh_state, touched_prs={pr.url})
        finally:
            pr_watcher.release_lock_fd(persist_fd)
        pr_watcher._refresh_dashboard(reason="current-session-record")

    return {
        "status": "recorded",
        "pr_url": pr.url,
        "head_sha": pr.head_sha,
        "comment_url": canonical_comment_url,
        "event_key": event.key,
        "calendar_action": actions.get(event.key, "?"),
        "jsonl_path": str(jsonl_path),
        "meta_path": str(meta_path),
        "origin_cwd": resolved_origin,
    }


def print_result(result: dict) -> None:
    prefix = "PR_SESSION_RECORD" if result.get("status") == "recorded" else "PR_SESSION_CLAIM"
    print(f"{prefix}=ok")
    for key in ("pr_url", "head_sha", "comment_url", "calendar_action", "event_key", "jsonl_path", "origin_cwd"):
        value = result.get(key)
        if value:
            print(f"{key.upper()}={value}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--claim", action="store_true", help="mark the current PR SHA as being reviewed by this session")
    mode.add_argument("--record", action="store_true", help="record an already-posted review comment into Calendar/state")
    parser.add_argument("--pr-url", required=True)
    parser.add_argument("--comment-url")
    parser.add_argument("--origin-cwd")
    parser.add_argument("--thread-id", default=os.environ.get("CODEX_THREAD_ID") or os.environ.get("CODEX_SESSION_ID"))
    parser.add_argument("--comment-body-file")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)

    try:
        if args.claim:
            result = claim_review(args.pr_url, args.origin_cwd)
        else:
            if not args.comment_url:
                parser.error("--record requires --comment-url")
            result = record_review(
                args.pr_url,
                args.comment_url,
                origin_cwd=args.origin_cwd,
                thread_id=args.thread_id,
                comment_body_file=args.comment_body_file,
                dry_run=args.dry_run,
            )
    except (ValueError, subprocess.CalledProcessError, json.JSONDecodeError, OSError) as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 1

    print_result(result)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
