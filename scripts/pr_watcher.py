"""GitHub PR watcher → codex review → Apple Calendar.

Polled by launchd every 10 minutes. The script self-throttles overnight (22:00–09:00
local) so the effective cadence is:

  - 09:00–22:00   every 10 minutes
  - other hours   every 10 minutes (skip if previous run was <5min ago)

Single-pass flow:
  1. GraphQL: list all open PRs authored by @me across every org.
  2. Filter: keep only PRs whose base == repo default branch.
  3. Compare each PR's head_sha against scripts/pr_state.json.
       - PR not in state and created/head-committed/updated before watcher install → seed head_sha.
       - PR not in state and created/head-committed/updated after watcher install → trigger codex review.
       - PR in state and head_sha unchanged → skip.
       - PR in state and head_sha changed → trigger codex review.

  The launchd tick is conservative only for historical PRs that predate this
  watcher installation. PRs created, updated, or carrying a latest head commit
  after install are reviewed on first sight, so missed `pre-push` / `pr-created`
  hooks still get a code review.
  4. For each triggered PR:
       0. Under the per-PR lock, persist a pending_review_sha BEFORE codex
          starts. The GitHub comment is the irreversible idempotency boundary:
          a crash after comment creation but before local state finalization
          must not make another process post a second comment for the same SHA.
       a. Render prompt = pr_prompt.md.replace("{pr_link}", url)
       b. codex exec --json --dangerously-bypass-approvals-and-sandbox \
            -s danger-full-access --skip-git-repo-check  <prompt>
            (run in /tmp/codex-pr-runs/<uuid>)
       c. Capture thread_id from JSONL stream.
       d. Fetch the newly-posted comment URL via gh api ... issues/<n>/comments.
       e. Write a calendar event into the "PR 监控" calendar.
       f. Persist {head_sha, thread_id, comment_url, timestamp} into state.

  Cancel + restart on new commit (issue #26):
       A --force that arrives for a PR already being reviewed writes a cancel
       marker, the leader's watcher thread sees it and kills the in-flight
       codex, process_pr short-circuits (no calendar event, no .meta sidecar,
       no state mutation), the leader releases the lock, and the waiting
       --force acquires it and runs against the freshly fetched head_sha.
       Both the cancel and the restart fire a banner notification. Net effect:
       the user's last calendar entry / PR comment is always against the most
       recent push, with no queued-up stale reviews.

Usage:
  python pr_watcher.py                          # one polling tick (the launchd entrypoint)
  python pr_watcher.py --dry-run                # show what would happen, no codex, no calendar
  python pr_watcher.py --seed-only              # populate state with current head_shas, never trigger codex
  python pr_watcher.py --force <url>            # force-trigger codex for a specific PR (ignores state)
  python pr_watcher.py --force <url> --origin-cwd <path>
                                                # forwarded by the pre-push hook: the local repo
                                                # root the push came from; saved into state so a
                                                # future "fix this PR" launcher knows where to drop in
"""

from __future__ import annotations

import argparse
import fcntl
import json
import os
import re
import shlex
import shutil
import signal
import subprocess
import sys
import threading
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, time as dtime, timezone
from pathlib import Path
from urllib.parse import quote, urlencode

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(HERE))

from calendar_sync import ReminderEvent, remove_event, upsert_events, PR_CALENDAR_NAME  # noqa: E402
from log_setup import redirect_stdio_to_log  # noqa: E402

STATE_PATH = HERE / "pr_state.json"           # PR-level state (head_sha, thread_id, …)
CAL_STATE_PATH = HERE / "pr_calendar_state.json"   # EventKit event_id index (separate from 节日提醒)
PROMPT_PATH = HERE / "pr_prompt.md"
LOG_DIR = HERE / "pr_logs"
SCRATCH_BASE = Path("/tmp/codex-pr-runs")
LOCK_DIR = HERE / "locks"                     # per-PR flock files + cancel markers + state lock
STATE_LOCK_PATH = LOCK_DIR / "state.lock"     # brief flock around state read/merge/write
COMMENT_BODY_CACHE_DIR = Path.home() / ".cache" / "my-calendar" / "fix-comments"  # see cache_comment_body()
CODEX_SLOT_POLL_SEC = 2.0                     # how often to retry when all slots are full
CODEX_SLOT_TIMEOUT_SEC = 30 * 60              # max time to wait for a slot before bailing
CANCEL_POLL_SEC = 0.5                         # how often the leader checks for the cancel marker
CANCEL_WAIT_LOCK_TIMEOUT_SEC = 90.0           # how long a --force waits for the prior leader to release the lock after signalling cancel

# User-configurable: cap codex executions in flight across all PRs. Override
# via ~/.config/my-calendar/config.json, e.g. {"codex_concurrency_cap": 4} to
# match a smaller machine or tighter budget. Invalid values fall back to 10
# with a stderr warning. Read once at module import — restart launchd agents
# (or rerun manually) to pick up a config change.
USER_CONFIG_PATH = Path.home() / ".config" / "my-calendar" / "config.json"
DEFAULT_CODEX_CONCURRENCY_CAP = 10


def _read_codex_cap(config_path: Path = USER_CONFIG_PATH, default: int = DEFAULT_CODEX_CONCURRENCY_CAP) -> int:
    """Load codex_concurrency_cap from the user config file.

    Silently returns `default` when the file is missing or doesn't set the
    key — clean installs shouldn't be noisy. When the file IS present but
    unparseable / wrong type / non-positive, emits a one-line stderr warning
    and falls back to `default` so a malformed config can't crash the
    daemon.

    "Integer" means strictly a JSON integer (Python `int` that is NOT a
    `bool`). `2.5`, `"4"`, and `true` are all rejected — silently coercing
    them would let a typo'd config change codex concurrency / cost without
    the user noticing."""
    if not config_path.exists():
        return default
    try:
        cfg = json.loads(config_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as e:
        print(f"[pr-watcher] warn: cannot read {config_path}: {e}; using cap={default}", file=sys.stderr)
        return default
    if not isinstance(cfg, dict) or "codex_concurrency_cap" not in cfg:
        return default
    raw = cfg["codex_concurrency_cap"]
    # Strict: `type(raw) is int` excludes bool (subclass) and any non-int JSON
    # scalar. int(raw) would silently truncate 2.5 → 2 and coerce "4" → 4.
    if type(raw) is not int:
        print(
            f"[pr-watcher] warn: codex_concurrency_cap={raw!r} in {config_path} "
            f"is not a JSON integer (got {type(raw).__name__}); using cap={default}",
            file=sys.stderr,
        )
        return default
    if raw < 1:
        print(
            f"[pr-watcher] warn: codex_concurrency_cap={raw} in {config_path} "
            f"is not positive; using cap={default}",
            file=sys.stderr,
        )
        return default
    return raw


CODEX_CONCURRENCY_CAP = _read_codex_cap()    # max codex executions in flight across all PRs

# terminal-notifier: absolute paths so this works under launchd's stripped PATH.
NOTIFIER_CANDIDATES = ("/opt/homebrew/bin/terminal-notifier", "/usr/local/bin/terminal-notifier")

# launchd-friendly: 工作时间窗口（本地时间）
DAYTIME_START = dtime(9, 0)
DAYTIME_END = dtime(22, 0)
NIGHT_MIN_INTERVAL_SEC = 5 * 60      # 夜间最少间隔 5 分钟
MAX_RUNTIME_PER_TICK_SEC = 25 * 60   # 单次 tick 最长跑 25 分钟，防止两轮叠在一起
SEARCH_QUERY = "is:pr author:@me state:open archived:false"
PENDING_REVIEW_RECOVERY_GRACE_SEC = 10 * 60  # Give GitHub comment listing one fallback tick to settle.
PENDING_REVIEW_STALE_SEC = MAX_RUNTIME_PER_TICK_SEC + PENDING_REVIEW_RECOVERY_GRACE_SEC
AI_COAUTHOR_METADATA_MARKER = "<!-- ai-coauthor: codex; agent: pr_watcher; mode: automated -->"
HEAD_SHA_METADATA_PREFIX = "<!-- pr-watcher-head-sha:"

GRAPHQL_QUERY = """
query($q: String!) {
  search(query: $q, type: ISSUE, first: 100) {
    issueCount
    nodes {
      ... on PullRequest {
        url
        number
        title
        isDraft
        createdAt
        updatedAt
        commits(last: 1) {
          nodes {
            commit {
              committedDate
            }
          }
        }
        baseRefName
        headRefName
        headRefOid
        repository {
          nameWithOwner
          defaultBranchRef { name }
        }
        headRepository {
          nameWithOwner
        }
      }
    }
  }
}
""".strip()


# ─── lock ──────────────────────────────────────────────────────────────────────
#
# Three entrypoints can touch the same PR: two immediate channels (git pre-push
# and pr-created) plus the conservative launchd fallback tick. codex runs
# ~1–2 min, so without serialisation processes can race past the state check,
# run codex on the same head_sha, and post duplicate comments.
#
# Design: per-PR flock + cancel marker + persist lock (reset/reboot semantics).
#   - Per-PR lock (locks/<safe_id>.lock) gates codex execution for that one PR.
#     Different PRs run fully in parallel; within a PR, only one codex at a time.
#   - Cancel marker (locks/<safe_id>.cancel) implements "new commit aborts the
#     in-flight review and immediately restarts" (issue #26). A --force that
#     finds the lock held writes the marker and waits for the leader to release.
#     The leader runs a watcher thread that polls the marker every
#     CANCEL_POLL_SEC and kills its codex subprocess when the marker appears.
#     Once codex is dead, the leader's process_pr short-circuits (no calendar
#     event, no .meta sidecar, no state mutation) and releases the lock. The
#     waiting --force then acquires the lock and starts a fresh review against
#     the latest head_sha. Net effect: the stale review is dropped, the user's
#     last comment is always against the latest sha.
#   - Persist lock (locks/<safe_id>.persist.lock) is the atomic commit boundary
#     PR #27's two follow-up findings asked for. The synchronous late-marker
#     re-check that used to live alone right before upsert_events was a check-
#     then-act race; the stat() + mtime-gated unlink() in
#     clear_stale_cancel_marker was the same shape of race on the cleanup side
#     (stale stat + concurrent touch landing between stat and unlink → leader
#     deletes the FRESH marker, watcher never sees it). The persist lock fixes
#     both by serialising every party that touches the marker file:
#         · signal_cancel_and_wait_for_lock (the only production marker writer)
#           acquires persist_lock around its touch();
#         · clear_stale_cancel_marker acquires persist_lock around its stat() +
#           conditional unlink();
#         · the leader's process_pr acquires persist_lock around the final
#           marker check + upsert_events + .meta sidecar + in-memory state
#           mutation block.
#     Either the marker write fully precedes the leader's critical section
#     (leader sees the marker, short-circuits, drops the stale review), or it
#     fully follows it (leader's persist completes, the new --force then takes
#     over and runs a fresh review against the latest sha). The marker write
#     can no longer be interleaved with either the leader's irreversible
#     writes or the stale-cleanup's stat/unlink window.
#   - State save (see save_state) uses a separate brief flock (locks/state.lock)
#     and does a load-merge-write so concurrent writers for different PRs don't
#     clobber each other's updates.


def _pr_safe_id(pr_url: str) -> str:
    return pr_url.replace("https://github.com/", "").replace("/", "_")


def _pr_lock_path(pr_url: str) -> Path:
    return LOCK_DIR / f"{_pr_safe_id(pr_url)}.lock"


def _pr_cancel_path(pr_url: str) -> Path:
    return LOCK_DIR / f"{_pr_safe_id(pr_url)}.cancel"


def _pr_persist_lock_path(pr_url: str) -> Path:
    return LOCK_DIR / f"{_pr_safe_id(pr_url)}.persist.lock"


def acquire_persist_lock(pr_url: str) -> int:
    """Acquire the per-PR persist lock, blocking until granted. Returns the fd
    the caller MUST release with release_lock_fd when done.

    Held by:
      - signal_cancel_and_wait_for_lock around the touch() of the cancel
        marker (very brief — usually well under 1ms).
      - clear_stale_cancel_marker around stat() + conditional unlink() of the
        marker file (very brief; the second PR #27 follow-up makes the stale-
        cleanup atomic relative to a concurrent marker writer).
      - process_pr around its final marker check + upsert_events + .meta
        sidecar write + in-memory state mutation (bounded by EventKit speed,
        typically <1s).

    All three of these touch the "was a cancel requested?" state — by
    creating, deleting, or reading the cancel marker file. Holding the same
    flock around every side makes the question totally ordered, which closes
    both PR #27 follow-up races:
      · marker appearing between the late re-check and upsert_events
        (process_pr's irreversible writes side); and
      · marker being touched between clear_stale_cancel_marker's stat() and
        its mtime-gated unlink() (the stale-cleanup side).

    Lock acquisition order in the leader path: per-PR lock → codex slot →
    persist lock. Acquisition order in the --force / marker-writer path:
    persist lock (briefly, released before the per-PR-lock poll loop) → per-PR
    lock. The two paths never hold both terminal locks concurrently, so this
    additional flock cannot deadlock with the existing ones."""
    LOCK_DIR.mkdir(parents=True, exist_ok=True)
    fd = os.open(str(_pr_persist_lock_path(pr_url)), os.O_CREAT | os.O_WRONLY, 0o644)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
    except Exception:
        os.close(fd)
        raise
    return fd


def clear_stale_cancel_marker(pr_url: str, before_ns: int) -> None:
    """Drop any cancel marker whose mtime predates `before_ns`. MUST be called
    by the holder of the per-PR lock, and ONLY at lock-acquisition time.

    `before_ns` is the wall-clock nanosecond timestamp captured IMMEDIATELY
    before the successful flock attempt. The mtime gate exists to fix the
    race PR #27 review (issue #26 follow-up #2) called out:

        t0: leader L captures pre_lock_ns
        t1: L's acquire_pr_lock_nb() returns (kernel grants flock atomically)
        t2: new --force F fails flock (because L holds it), writes marker
            (marker.mtime_ns > t0 — F's touch happened AFTER L's pre_lock_ns)
        t3: L calls clear_stale_cancel_marker(pr_url, before_ns=t0)

    Without the mtime gate, t3 would unconditionally unlink F's freshly-
    written marker, silently dropping F's cancel signal. The mtime check
    preserves any marker with mtime > before_ns — those can only have been
    written AFTER our flock attempt (since the only writer is
    signal_cancel_and_wait_for_lock, which fires only after observing the
    flock as held — and the flock is held by US after our acquire returned).

    Stale markers (left behind by a crashed prior leader, or written for a
    previous generation that already finished) have mtime ≤ before_ns and
    are safely removed.

    Second PR #27 follow-up (codex blocker): the mtime gate alone is still a
    check-then-act window between stat() and unlink(). If a stale marker is
    present at lock-acquisition time, a brand-new --force F can fire
    signal_cancel_and_wait_for_lock between our stat (which reads the OLD
    mtime) and our conditional unlink (which uses that stale stat), so we
    end up deleting F's freshly-touched marker even though its real on-disk
    mtime is now > before_ns. The watcher then never sees a marker and the
    obsolete codex run lands.
    Fix: do stat + conditional unlink under the same per-PR persist_lock
    that signal_cancel_and_wait_for_lock holds around its touch(). The
    marker-write side is forced to wait until our stat+unlink finishes (it
    will then re-touch a fresh marker on a clean slate), so the stale-stat-
    racing-with-fresh-touch interleaving is impossible.
    """
    marker = _pr_cancel_path(pr_url)
    persist_fd = acquire_persist_lock(pr_url)
    try:
        try:
            st = marker.stat()
        except FileNotFoundError:
            return
        except OSError:
            return
        if st.st_mtime_ns > before_ns:
            # Fresh signal from a --force that wrote the marker AFTER our flock
            # attempt. Leave it for run_codex's watcher to react to.
            return
        try:
            marker.unlink(missing_ok=True)
        except OSError:
            pass
    finally:
        release_lock_fd(persist_fd)


def acquire_pr_lock_nb(pr_url: str) -> int | None:
    """Try once to acquire the per-PR flock. Return the fd if acquired
    (caller must keep it open until done, then os.close), or None if it's
    currently held by another process. Kernel releases the flock on fd close
    or on process exit."""
    LOCK_DIR.mkdir(parents=True, exist_ok=True)
    fd = os.open(str(_pr_lock_path(pr_url)), os.O_CREAT | os.O_WRONLY, 0o644)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        return fd
    except BlockingIOError:
        os.close(fd)
        return None


def signal_cancel_and_wait_for_lock(
    pr_url: str,
    *,
    timeout_sec: float = CANCEL_WAIT_LOCK_TIMEOUT_SEC,
    poll_sec: float = 1.0,
) -> tuple[int, int] | None:
    """--force entrypoint helper. Write the cancel marker so the current
    leader's codex is killed, then poll for the per-PR lock until the leader
    releases it.

    Returns (fd, pre_lock_ns) on success — pre_lock_ns is the wall-clock ns
    captured immediately BEFORE the acquire_pr_lock_nb call that succeeded,
    to be passed to clear_stale_cancel_marker so any marker that appeared
    after our acquire is preserved (it's a fresh signal targeting US, not
    leftover from a prior generation).

    Returns None if the leader didn't release within timeout_sec (defensive —
    should normally take <2s after kill since codex exits, leader's process_pr
    short-circuits, and the flock is released).

    PR #27 high finding: the touch() runs under the per-PR persist lock. If
    the leader is currently inside its persist critical section (final marker
    check + upsert_events + meta sidecar + state mutation), our acquire of
    persist_lock blocks until that section finishes — making the marker write
    totally ordered relative to the leader's commit. We release persist_lock
    BEFORE the per-PR-lock poll loop so the leader (which acquires persist
    lock NESTED INSIDE the per-PR lock) cannot deadlock with us.
    """
    LOCK_DIR.mkdir(parents=True, exist_ok=True)
    persist_fd = acquire_persist_lock(pr_url)
    try:
        _pr_cancel_path(pr_url).touch()
    finally:
        release_lock_fd(persist_fd)
    deadline = time.time() + timeout_sec
    while True:
        pre_lock_ns = time.time_ns()
        fd = acquire_pr_lock_nb(pr_url)
        if fd is not None:
            return fd, pre_lock_ns
        if time.time() >= deadline:
            return None
        time.sleep(poll_sec)


def release_lock_fd(fd: int) -> None:
    """Release a flock fd defensively — never raises."""
    try:
        fcntl.flock(fd, fcntl.LOCK_UN)
    except Exception:
        pass
    try:
        os.close(fd)
    except Exception:
        pass


def acquire_codex_slot(
    *,
    timeout_sec: float = CODEX_SLOT_TIMEOUT_SEC,
    cancel_marker: Path | None = None,
) -> tuple[int, int] | None:
    """Acquire one of CODEX_CONCURRENCY_CAP global codex slots.

    Caps how many codex executions can run at once across ALL PRs. Without
    this cap, per-PR parallelism plus rapid pushes could spawn unbounded
    concurrent codex processes (CPU/network/$LLM cost).

    Returns (fd, slot_number) on success (caller must release_lock_fd(fd) when
    done), or None on timeout. Polls every CODEX_SLOT_POLL_SEC; waiting is
    quiet after the first "all slots busy" notice.

    If `cancel_marker` is given (the per-PR `.cancel` Path), the wait loop
    polls for the marker each cycle and returns None as soon as it appears.
    Required for issue #26's cancel + restart contract: without it, a new
    --force on the same PR would wait the full 90s for the per-PR lock
    while the leader sits in slot-wait under UNRELATED-PR saturation, then
    silently give up. Callers distinguish timeout vs cancel by inspecting
    `cancel_marker` after a None return (see run_codex)."""
    LOCK_DIR.mkdir(parents=True, exist_ok=True)
    deadline = time.time() + timeout_sec
    announced_wait = False
    while True:
        for n in range(1, CODEX_CONCURRENCY_CAP + 1):
            fd = os.open(str(LOCK_DIR / f"codex-slot-{n}.lock"), os.O_CREAT | os.O_WRONLY, 0o644)
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                return fd, n
            except BlockingIOError:
                os.close(fd)
        # Cancellable wait: bail immediately on marker so per-PR lock is
        # released and the new --force can restart against the latest sha.
        # Marker consumption is the caller's job (same as the watcher thread).
        if cancel_marker is not None:
            try:
                if cancel_marker.exists():
                    return None
            except OSError:
                pass
        if not announced_wait:
            print(
                f"      all {CODEX_CONCURRENCY_CAP} codex slots busy, waiting up to "
                f"{int(timeout_sec)}s...",
                flush=True,
            )
            announced_wait = True
        if time.time() >= deadline:
            return None
        time.sleep(CODEX_SLOT_POLL_SEC)


# ─── state ─────────────────────────────────────────────────────────────────────


@dataclass
class PRSnap:
    url: str
    number: int
    title: str
    is_draft: bool
    repo: str                 # "owner/name"
    base: str
    default_branch: str
    head_sha: str
    created_at: str = ""      # ISO-8601 UTC (e.g. "2026-05-20T03:56:40Z")
    updated_at: str = ""      # ISO-8601 UTC; PR-level updatedAt from GitHub.
    head_committed_at: str = ""  # ISO-8601 UTC; latest PR head commit time.
    head_branch: str = ""     # source branch (headRefName); needed by the
                              # "fix this PR" launcher to git checkout locally.
    head_repo: str = ""       # headRepository.nameWithOwner; differs from .repo
                              # on fork PRs. Used to suppress the fix URL when
                              # the head branch isn't on the base repo's origin.


@dataclass(frozen=True)
class AICommentLookup:
    status: str  # found | absent | failed
    comment_url: str | None = None
    comment_body: str | None = None


def head_sha_metadata_marker(head_sha: str) -> str:
    return f"{HEAD_SHA_METADATA_PREFIX} {head_sha} -->"


def load_state() -> dict:
    if STATE_PATH.exists():
        return json.loads(STATE_PATH.read_text(encoding="utf-8"))
    return {"_meta": {}, "prs": {}}


def save_state(state: dict, *, touched_prs: set[str] | None = None) -> None:
    """Persist `state` to disk under a brief flock.

    When `touched_prs` is provided, do an atomic load-merge-write: only the
    listed PR entries are pushed from in-memory `state` into the on-disk
    state, preserving concurrent updates from other processes to OTHER PRs.
    `_meta` keys present in-memory override on-disk (so `installed_at` etc.
    can be stamped), and `_meta.last_run` is always refreshed.

    When `touched_prs` is None, the in-memory state overwrites disk wholesale.
    This is the legacy behavior — only safe when no other process is
    concurrently writing state (e.g. single-shot --dry-run inspection or
    tests). Production callsites should pass touched_prs."""
    LOCK_DIR.mkdir(parents=True, exist_ok=True)
    lock_fd = os.open(str(STATE_LOCK_PATH), os.O_CREAT | os.O_WRONLY, 0o644)
    try:
        fcntl.flock(lock_fd, fcntl.LOCK_EX)  # blocking; held only for file I/O
        now_iso = datetime.now().isoformat(timespec="seconds")
        if touched_prs is None:
            state.setdefault("_meta", {})["last_run"] = now_iso
            payload = state
        else:
            on_disk = load_state() if STATE_PATH.exists() else {"_meta": {}, "prs": {}}
            on_disk.setdefault("prs", {})
            on_disk.setdefault("_meta", {})
            in_mem_prs = state.get("prs") or {}
            for url in touched_prs:
                if url in in_mem_prs:
                    on_disk["prs"][url] = in_mem_prs[url]
            for k, v in (state.get("_meta") or {}).items():
                if k == "last_run":
                    continue  # always overwritten below
                on_disk["_meta"][k] = v
            on_disk["_meta"]["last_run"] = now_iso
            payload = on_disk
        # Atomic write: load_state() doesn't take state.lock, so it can fire
        # at any moment relative to this save. Write to a sibling tmp file
        # then os.replace — POSIX-atomic. A reader either sees the prior
        # complete state or the new complete state, never a half-flushed
        # buffer that would raise JSONDecodeError mid-tick.
        tmp_path = STATE_PATH.with_suffix(STATE_PATH.suffix + ".tmp")
        tmp_path.write_text(
            json.dumps(payload, indent=2, ensure_ascii=False, sort_keys=True),
            encoding="utf-8",
        )
        os.replace(tmp_path, STATE_PATH)
    finally:
        release_lock_fd(lock_fd)


def sync_meta_origin_cwd(last_jsonl: str | None, origin_cwd: str) -> tuple[Path | None, bool]:
    """Mirror origin_cwd into the prior run's .meta.json sidecar.

    Returns (meta_path, changed). A missing last_jsonl or sidecar is a no-op.
    """
    if not last_jsonl:
        return None, False

    meta_path = Path(last_jsonl).with_suffix(".meta.json")
    if not meta_path.exists():
        return None, False

    meta = json.loads(meta_path.read_text(encoding="utf-8"))
    if meta.get("origin_cwd") == origin_cwd:
        return meta_path, False

    meta["origin_cwd"] = origin_cwd
    meta_path.write_text(
        json.dumps(meta, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )
    return meta_path, True


# ─── throttling ────────────────────────────────────────────────────────────────


def in_daytime(now: datetime) -> bool:
    t = now.time()
    return DAYTIME_START <= t < DAYTIME_END


def should_skip_this_tick(state: dict, now: datetime) -> bool:
    """Night-time throttle: skip if last run was <5min ago."""
    if in_daytime(now):
        return False
    last = state.get("_meta", {}).get("last_run")
    if not last:
        return False
    try:
        delta = (now - datetime.fromisoformat(last)).total_seconds()
    except Exception:
        return False
    return delta < NIGHT_MIN_INTERVAL_SEC


# ─── mac notifications ─────────────────────────────────────────────────────────


def _find_notifier() -> str | None:
    for p in NOTIFIER_CANDIDATES:
        if Path(p).exists():
            return p
    return shutil.which("terminal-notifier")


def notify(title: str, message: str, *, subtitle: str = "", open_url: str | None = None, group: str | None = None) -> None:
    """Fire-and-forget Mac notification. No-op if terminal-notifier is missing."""
    notifier = _find_notifier()
    if not notifier:
        return
    cmd = [notifier, "-title", title, "-message", message]
    if subtitle:
        cmd += ["-subtitle", subtitle]
    if open_url:
        cmd += ["-open", open_url]
    if group:
        cmd += ["-group", group]
    try:
        subprocess.run(cmd, check=False, capture_output=True, timeout=5)
    except Exception:
        pass


def fmt_duration(sec: float) -> str:
    sec = int(sec)
    if sec < 60:
        return f"{sec}s"
    m, s = divmod(sec, 60)
    return f"{m}m{s:02d}s"


# ─── gh / GraphQL ──────────────────────────────────────────────────────────────


def fetch_open_prs() -> list[PRSnap]:
    proc = subprocess.run(
        ["gh", "api", "graphql", "-f", f"query={GRAPHQL_QUERY}", "-f", f"q={SEARCH_QUERY}"],
        capture_output=True, text=True, check=True,
    )
    payload = json.loads(proc.stdout)
    nodes = payload.get("data", {}).get("search", {}).get("nodes", []) or []
    out: list[PRSnap] = []
    for n in nodes:
        if not n:
            continue
        repo = n.get("repository", {}) or {}
        default_ref = (repo.get("defaultBranchRef") or {}).get("name") or ""
        head_repo = (n.get("headRepository") or {}).get("nameWithOwner") or ""
        commit_nodes = ((n.get("commits") or {}).get("nodes") or [])
        latest_commit = (commit_nodes[-1] if commit_nodes else {}) or {}
        head_committed_at = ((latest_commit.get("commit") or {}).get("committedDate") or "")
        out.append(PRSnap(
            url=n["url"],
            number=int(n["number"]),
            title=n.get("title", ""),
            is_draft=bool(n.get("isDraft", False)),
            repo=repo.get("nameWithOwner", ""),
            base=n.get("baseRefName", ""),
            default_branch=default_ref,
            head_sha=n.get("headRefOid", ""),
            created_at=n.get("createdAt", "") or "",
            updated_at=n.get("updatedAt", "") or "",
            head_committed_at=head_committed_at,
            head_branch=n.get("headRefName", "") or "",
            head_repo=head_repo,
        ))
    return out


def _parse_iso_utc(s: str) -> datetime | None:
    """Parse an ISO-8601 string (with or without trailing Z) into a UTC-aware datetime."""
    if not s:
        return None
    try:
        dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.astimezone()
    return dt.astimezone(timezone.utc)


_ISSUECOMMENT_RE = re.compile(r"#issuecomment-(\d+)")


def cache_comment_body(comment_url: str | None, comment_body: str | None) -> Path | None:
    """Pre-fetch the codex review comment body to a local file so the fix
    launcher hands claude a path instead of having claude run `gh api`.

    Why this exists (issue raised on PR #30):
    fix_prompt.md step 1 used to have claude run `gh api .../comments/<id>
    --jq .body` at the start of every fix session. Under prompt injection
    (the body of a different comment, the PR diff, etc.), claude could be
    coaxed into fetching a DIFFERENT URL than intended — e.g. an attacker-
    controlled comment on a different repo carrying further instructions.
    By caching the body here at review-completion time (pr_watcher trusts
    the codex pipeline that produced it), the fix prompt can tell claude to
    read a specific local file. In interactive mode the Approve dialog now
    shows the exact file being read, not an opaque shell command. The
    dynamic-fetch attack surface is gone.

    Cache key is the numeric issue-comment id parsed from the html_url
    (`...#issuecomment-<id>`). Same id is used by launch_fix.sh to look the
    file back up. Returns the written Path on success, None otherwise (no
    URL / no body / unparseable URL / write failure). The launcher falls
    back to gh api when None — old calendar events still work.
    """
    if not comment_url or not comment_body:
        return None
    m = _ISSUECOMMENT_RE.search(comment_url)
    if not m:
        return None
    cid = m.group(1)
    try:
        COMMENT_BODY_CACHE_DIR.mkdir(parents=True, exist_ok=True)
    except OSError as e:
        print(f"      warn: cannot create comment-body cache dir: {e}", flush=True)
        return None
    path = COMMENT_BODY_CACHE_DIR / f"{cid}.md"
    try:
        path.write_text(comment_body, encoding="utf-8")
    except OSError as e:
        print(f"      warn: failed to cache comment body to {path}: {e}", flush=True)
        return None
    return path


def fetch_latest_ai_comment_since(
    repo: str,
    number: int,
    since_iso: str | None,
    *,
    head_sha: str | None = None,
) -> AICommentLookup:
    """Return latest pr_watcher-authored AI comment at or after since_iso.

    Used to recover a pending review after the local process died between the
    irreversible GitHub comment side effect and local state finalization.
    When head_sha is provided, the comment must carry this run's hidden SHA
    marker too; time alone is not enough because GitHub issue-comment
    created_at values have only second-level precision.
    """
    since_dt = _parse_iso_utc(since_iso or "")
    try:
        me_proc = subprocess.run(
            ["gh", "api", "user", "--jq", ".login"],
            capture_output=True, text=True, check=True,
        )
        me = me_proc.stdout.strip()
        proc = subprocess.run(
            [
                "gh", "api",
                f"repos/{repo}/issues/{number}/comments?per_page=100",
                "--paginate",
                "--slurp",
            ],
            capture_output=True, text=True, check=True,
        )
        raw_comments = json.loads(proc.stdout)
    except (subprocess.CalledProcessError, json.JSONDecodeError):
        return AICommentLookup("failed")

    if not isinstance(raw_comments, list):
        return AICommentLookup("failed")

    comments: list[dict] = []
    for page_or_comment in raw_comments:
        page = page_or_comment if isinstance(page_or_comment, list) else [page_or_comment]
        for c in page:
            if isinstance(c, dict):
                comments.append(c)

    candidates: list[tuple[datetime, dict]] = []
    fallback_dt = datetime.min.replace(tzinfo=timezone.utc)
    for c in comments:
        if not isinstance(c, dict):
            continue
        user = c.get("user") or {}
        if (user.get("login") or "") != me:
            continue
        body = c.get("body") or ""
        if AI_COAUTHOR_METADATA_MARKER not in body:
            continue
        if head_sha and head_sha_metadata_marker(head_sha) not in body:
            continue
        created_dt = _parse_iso_utc(c.get("created_at") or "")
        if since_dt is not None and created_dt is not None and created_dt < since_dt:
            continue
        candidates.append((created_dt or fallback_dt, c))

    if not candidates:
        return AICommentLookup("absent")
    _, latest = sorted(candidates, key=lambda item: item[0])[-1]
    comment_url = latest.get("html_url") or None
    if not comment_url:
        return AICommentLookup("failed")
    return AICommentLookup("found", comment_url=comment_url, comment_body=latest.get("body") or None)


def _clear_pending_review(entry: dict) -> None:
    for key in (
        "pending_review_sha",
        "pending_review_started_at",
        "pending_review_source",
    ):
        entry.pop(key, None)


def _mark_review_pending(state: dict, pr: PRSnap, *, source: str, now: datetime) -> None:
    entry = state.setdefault("prs", {}).setdefault(pr.url, {})
    entry.update({
        "repo": pr.repo,
        "number": pr.number,
        "pending_review_sha": pr.head_sha,
        "pending_review_started_at": now.isoformat(timespec="seconds"),
        "pending_review_source": source,
    })


def _remember_calendar_delete(state: dict, pr_url: str, event_key: str) -> None:
    entry = state.setdefault("prs", {}).setdefault(pr_url, {})
    keys = entry.get("pending_calendar_delete_keys")
    if not isinstance(keys, list):
        keys = []
    if event_key not in keys:
        keys.append(event_key)
    entry["pending_calendar_delete_keys"] = keys


def _retry_pending_calendar_deletes(state: dict, pr_url: str) -> bool:
    entry = state.setdefault("prs", {}).setdefault(pr_url, {})
    keys = entry.get("pending_calendar_delete_keys")
    if not isinstance(keys, list) or not keys:
        return False

    changed = False
    remaining: list[str] = []
    for key in keys:
        try:
            removed = remove_event(key, CAL_STATE_PATH)
        except Exception as e:
            print(f"      warn: failed to retry calendar rollback {key}: {e}", flush=True)
            removed = False
        if removed:
            changed = True
        else:
            remaining.append(key)

    if remaining:
        if remaining != keys:
            entry["pending_calendar_delete_keys"] = remaining
            changed = True
    else:
        entry.pop("pending_calendar_delete_keys", None)
        changed = True
    return changed


def _pending_age_seconds(entry: dict, now: datetime) -> float | None:
    started = _parse_iso_utc(entry.get("pending_review_started_at") or "")
    if started is None:
        return None
    now_utc = now.astimezone(timezone.utc) if now.tzinfo else now.astimezone().astimezone(timezone.utc)
    return max(0.0, (now_utc - started).total_seconds())


def _pending_is_stale(entry: dict, now: datetime) -> bool:
    age = _pending_age_seconds(entry, now)
    return age is None or age >= PENDING_REVIEW_STALE_SEC


def _complete_recovered_pending(
    state: dict,
    pr: PRSnap,
    *,
    comment_url: str,
    comment_body: str | None,
    now: datetime,
) -> None:
    entry = state.setdefault("prs", {}).setdefault(pr.url, {})
    pending_started_at = entry.get("pending_review_started_at")
    entry.update({
        "repo": pr.repo,
        "number": pr.number,
        "last_commented_sha": pr.head_sha,
        "last_seen_sha": pr.head_sha,
        "last_comment_url": comment_url,
        "last_run_at": pending_started_at or now.isoformat(timespec="seconds"),
        "last_codex_exit": None,
        "recovered_from_pending_at": now.isoformat(timespec="seconds"),
    })
    _clear_pending_review(entry)
    cache_comment_body(comment_url, comment_body)


def _resolved_origin_cwd(origin_cwd: str | None) -> str | None:
    if not origin_cwd:
        return None
    cwd_p = Path(origin_cwd).expanduser().resolve()
    return str(cwd_p) if cwd_p.is_dir() else None


def _same_sha_review_guard(pr: PRSnap, state: dict, now: datetime) -> tuple[str | None, bool]:
    """Return (action, changed) for same-SHA completed/pending idempotency.

    action is a log string when the caller should skip starting codex. When
    action is None, the caller may proceed. changed tells the caller whether
    state was mutated and must be saved before proceeding/returning.
    """
    entry = state.setdefault("prs", {}).setdefault(pr.url, {})
    if entry.get("last_commented_sha") == pr.head_sha:
        return f"already reviewed sha={pr.head_sha[:8]}", False

    if entry.get("pending_review_sha") != pr.head_sha:
        return None, False

    if not _pending_is_stale(entry, now):
        age = _pending_age_seconds(entry, now)
        age_s = "unknown" if age is None else f"{int(age)}s"
        return f"review already pending sha={pr.head_sha[:8]} age={age_s}", False

    lookup = fetch_latest_ai_comment_since(
        pr.repo,
        pr.number,
        entry.get("pending_review_started_at"),
        head_sha=pr.head_sha,
    )
    if lookup.status == "found" and lookup.comment_url:
        _complete_recovered_pending(
            state,
            pr,
            comment_url=lookup.comment_url,
            comment_body=lookup.comment_body,
            now=now,
        )
        return f"recovered existing AI comment for sha={pr.head_sha[:8]}", True
    if lookup.status == "failed":
        return f"pending recovery lookup failed for sha={pr.head_sha[:8]}, keeping pending", False

    _clear_pending_review(entry)
    return None, True


def _first_seen_pr_should_review(pr: PRSnap, state: dict) -> bool:
    """Return True when a first-seen PR is new enough for fallback review.

    The launchd fallback must not blast old open PRs the first time this
    watcher is installed or state is rebuilt. But after installation, every PR
    commit should eventually get a review even if the local immediate hooks
    missed the PR creation/update. GitHub does not expose a PR-head push time
    in this query, so use PR createdAt, latest head commit time, and PR updatedAt
    versus state._meta installed_at as the line. updatedAt can review a touched
    historical PR once, but it avoids missing an old commit that was pushed after
    install.
    """
    installed_at = _parse_iso_utc((state.get("_meta") or {}).get("installed_at") or "")
    created_at = _parse_iso_utc(pr.created_at or "")
    head_committed_at = _parse_iso_utc(pr.head_committed_at or "")
    updated_at = _parse_iso_utc(pr.updated_at or "")
    candidate_times = [dt for dt in (created_at, head_committed_at, updated_at) if dt is not None]
    if installed_at is None or not candidate_times:
        return False
    return max(candidate_times) >= installed_at


# ─── codex ─────────────────────────────────────────────────────────────────────


@dataclass
class CodexResult:
    thread_id: str | None
    last_message: str
    exit_code: int
    jsonl_path: Path
    scratch_dir: Path
    cancelled: bool = False     # True iff a cancel marker (issue #26 reset)
                                # killed the codex subprocess mid-run; callers
                                # must skip calendar event + state mutation.


def _refresh_dashboard(*, reason: str) -> None:
    """Best-effort regenerate the static HTML dashboard so an open browser tab
    picks up the new state on its next 5s auto-reload. Never break the caller."""
    try:
        subprocess.run(
            [sys.executable, str(HERE / "dashboard.py"), "--html"],
            timeout=15,
            check=False,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    except Exception as e:
        print(f"      warn: dashboard refresh ({reason}) failed: {e}", flush=True)


def _sweep_stale_running_sidecars(now_ts: float) -> None:
    """Remove .running sidecars older than MAX_RUNTIME_PER_TICK_SEC + buffer.
    A previous pr_watcher that died (SIGKILL, panic, OOM) would otherwise pin
    a phantom "running" row in the dashboard forever."""
    cutoff = now_ts - (MAX_RUNTIME_PER_TICK_SEC + 5 * 60)
    if not LOG_DIR.exists():
        return
    for p in LOG_DIR.iterdir():
        if p.name.endswith(".running"):
            try:
                if p.stat().st_mtime < cutoff:
                    p.unlink(missing_ok=True)
            except OSError:
                pass


def run_codex(prompt: str, pr: PRSnap) -> CodexResult:
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    SCRATCH_BASE.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    safe_id = pr.url.replace("https://github.com/", "").replace("/", "_")
    jsonl_path = LOG_DIR / f"{stamp}__{safe_id}.jsonl"
    last_msg_path = LOG_DIR / f"{stamp}__{safe_id}.last.txt"
    running_path = LOG_DIR / f"{stamp}__{safe_id}.running"
    scratch = SCRATCH_BASE / f"{stamp}-{uuid.uuid4().hex[:8]}"
    scratch.mkdir(parents=True, exist_ok=True)

    # NOTE: do NOT clear the cancel marker here. Stale-marker cleanup is the
    # responsibility of the caller, immediately after they acquire the per-PR
    # lock (see clear_stale_cancel_marker). Clearing the marker on run_codex
    # entry would race against a new --force that wrote the marker between
    # lock acquisition and our arrival here — we'd silently drop its cancel
    # signal and run the stale review to completion.
    cancel_marker = _pr_cancel_path(pr.url)

    # Block (poll) for a global codex slot BEFORE writing the running sidecar.
    # If acquire times out and raises RuntimeError, we must not leave behind
    # an orphan sidecar (would pin a phantom "running" row in the dashboard
    # since the cleanup `finally` below never gets entered). The per-PR lock
    # is already held by the caller, so same-PR is serialised; this cap
    # protects the box from unbounded codex fan-out across UNRELATED PRs
    # (CPU/network/$LLM cost).
    #
    # cancel_marker makes slot-wait itself cancellable: a fresh --force on
    # this PR can write the marker to interrupt our wait. Without this,
    # saturated slots from UNRELATED PRs would pin the per-PR lock for up
    # to 30min and break issue #26's cancel + restart contract.
    slot = acquire_codex_slot(cancel_marker=cancel_marker)
    if slot is None:
        # None from acquire means timeout OR cancel-during-wait. The marker
        # is the disambiguator: if it's still there, treat as cancel and
        # consume it (same contract the watcher honours mid-run).
        cancelled_in_slot_wait = False
        try:
            cancelled_in_slot_wait = cancel_marker.exists()
        except OSError:
            pass
        if cancelled_in_slot_wait:
            try:
                cancel_marker.unlink(missing_ok=True)
            except OSError:
                pass
            try:
                jsonl_path.write_text(
                    '{"type":"_killed_by_watcher","reason":"cancelled_in_slot_wait"}\n',
                    encoding="utf-8",
                )
            except OSError:
                pass
            # Short-circuit: no codex spawned, no .running sidecar written.
            # process_pr's cancelled branch rmtree's the (empty) scratch dir.
            return CodexResult(
                thread_id=None,
                last_message="",
                exit_code=-1,
                jsonl_path=jsonl_path,
                scratch_dir=scratch,
                cancelled=True,
            )
        raise RuntimeError(
            f"codex concurrency cap ({CODEX_CONCURRENCY_CAP}) saturated for "
            f">{CODEX_SLOT_TIMEOUT_SEC}s; aborting this run"
        )
    slot_fd, slot_n = slot
    print(f"      codex slot {slot_n}/{CODEX_CONCURRENCY_CAP} acquired", flush=True)

    # If a same-PR --force wrote the cancel marker while we were acquiring a
    # global slot, honour it before spawning Codex. Otherwise a free slot can
    # briefly start a stale Codex run just for the watcher to kill it.
    try:
        marker_exists = cancel_marker.exists()
    except OSError:
        marker_exists = False
    if marker_exists:
        try:
            cancel_marker.unlink(missing_ok=True)
        except OSError:
            pass
        try:
            jsonl_path.write_text(
                '{"type":"_killed_by_watcher","reason":"cancelled_before_codex_start"}\n',
                encoding="utf-8",
            )
        except OSError:
            pass
        release_lock_fd(slot_fd)
        return CodexResult(
            thread_id=None,
            last_message="",
            exit_code=-1,
            jsonl_path=jsonl_path,
            scratch_dir=scratch,
            cancelled=True,
        )

    # Drop the .running sidecar BEFORE codex starts so the dashboard's
    # collect_running() can find it during the 1–2min codex run. Cleaned up
    # in the finally below. Reviewer dashboard refreshes every 5s, so a single
    # synchronous dashboard.py call here is enough to make this PR show up
    # in the "运行中" section as soon as the next browser tick fires.
    _sweep_stale_running_sidecars(time.time())
    started_at = datetime.now().isoformat(timespec="seconds")
    running_meta = {
        "started_at": started_at,
        "repo": pr.repo,
        "pr_number": pr.number,
        "pr_url": pr.url,
        "pr_title": pr.title,
        "head_sha": pr.head_sha,
        "jsonl_path": str(jsonl_path),
    }
    try:
        running_path.write_text(
            json.dumps(running_meta, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
    except OSError as e:
        print(f"      warn: failed to write running sidecar {running_path}: {e}", flush=True)
        running_path = None  # type: ignore[assignment]

    if running_path is not None:
        _refresh_dashboard(reason="run-start")

    cmd = [
        "codex", "exec",
        "--json",
        "--dangerously-bypass-approvals-and-sandbox",
        "-s", "danger-full-access",
        "--skip-git-repo-check",
        "-C", str(scratch),
        "-o", str(last_msg_path),
        prompt,
    ]

    env = os.environ.copy()
    # Make sure brew bins are reachable when run from launchd
    env["PATH"] = "/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin"

    thread_id: str | None = None
    cancelled = False
    proc: subprocess.Popen | None = None
    stop_run_watcher = threading.Event()
    cancel_observed = threading.Event()
    timeout_observed = threading.Event()
    cancel_reason_written = False

    def _kill_proc_group(p: subprocess.Popen) -> None:
        """SIGKILL the codex process group. codex tends to spawn helpers
        (gh, git, node workers), and a plain proc.kill() only targets the
        parent — orphaned children keep the stdout pipe open, our main
        thread's `for line in proc.stdout` blocks until they exit, and the
        cancel can stall for minutes. Killing the whole group (created via
        start_new_session=True in Popen) terminates the helpers too, which
        closes the pipe and lets the read loop drain to EOF."""
        try:
            os.killpg(os.getpgid(p.pid), signal.SIGKILL)
        except ProcessLookupError:
            pass
        except Exception:
            # Fall back to plain kill; better than no signal at all.
            try:
                p.kill()
            except Exception:
                pass

    def _consume_cancel_marker() -> bool:
        try:
            marker_exists = cancel_marker.exists()
        except OSError:
            marker_exists = False
        if not marker_exists:
            return False
        cancel_observed.set()
        try:
            cancel_marker.unlink(missing_ok=True)
        except OSError:
            pass
        return True

    def _append_cancel_reason() -> None:
        nonlocal cancel_reason_written
        if cancel_reason_written:
            return
        try:
            with jsonl_path.open("a", encoding="utf-8") as f:
                f.write('{"type":"_killed_by_watcher","reason":"cancelled_new_commit"}\n')
            cancel_reason_written = True
        except OSError:
            pass

    def _run_watcher(start_monotonic: float) -> None:
        """Poll cancellation and wall-clock timeout while Codex is running.

        Timeout must live outside the stdout read loop. A stuck Codex can emit
        `turn.started` and then stay silent forever; if the only watchdog check
        happens after reading a new line, launchd remains pinned indefinitely.

        Any cancel marker observed during the codex run window is a fresh cancel
        signal from a new --force (with the mtime-based stale cleanup at lock
        acquisition, no stale marker can reach this point — see
        clear_stale_cancel_marker docstring).

        Behaviour:
          - marker + proc alive  → kill codex group, set cancel_observed
          - marker + proc dead   → still set cancel_observed (PR #27 review
            blocker 2b: a marker that lands AFTER codex exited naturally
            but BEFORE process_pr writes calendar/state must still short-
            circuit the calendar/state writes; otherwise the stale review
            gets committed and the waiting --force then writes a duplicate
            for the fresh sha)

          - timeout + proc alive → kill codex group, set timeout_observed

        For cancel markers, we unlink the marker so the next leader's stale-cleanup
        has less to do (and so a marker doesn't survive into a future leader's
        generation as a phantom signal). Exits when stop_run_watcher fires
        (the main thread already finished post-codex housekeeping).
        """
        while True:
            if _consume_cancel_marker():
                if proc is not None and proc.poll() is None:
                    _kill_proc_group(proc)
                return
            if (
                proc is not None
                and proc.poll() is None
                and time.monotonic() - start_monotonic > MAX_RUNTIME_PER_TICK_SEC
            ):
                timeout_observed.set()
                _kill_proc_group(proc)
                return
            if stop_run_watcher.wait(CANCEL_POLL_SEC):
                return

    watcher_thread: threading.Thread | None = None
    watcher_started = False

    try:
        with jsonl_path.open("w", encoding="utf-8") as f:
            proc = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                env=env,
                cwd=str(scratch),
                # New session so the cancel watcher can kill the whole group
                # (codex + any helpers it spawned). Without this, killing the
                # parent leaves orphaned helpers holding the stdout pipe open.
                start_new_session=True,
            )
            start = time.monotonic()
            watcher_thread = threading.Thread(target=_run_watcher, args=(start,), daemon=True)
            watcher_thread.start()
            watcher_started = True
            assert proc.stdout is not None
            for line in proc.stdout:
                f.write(line)
                f.flush()
                s = line.strip()
                if thread_id is None and s.startswith("{") and '"thread.started"' in s:
                    try:
                        obj = json.loads(s)
                        thread_id = obj.get("thread_id")
                    except Exception:
                        pass
            proc.wait()
            # Belt-and-suspenders for PR #27 review blocker 2b: a marker
            # landing in the narrow window between the watcher's last poll
            # and our about-to-fire stop signal would otherwise be missed
            # and we'd commit stale calendar/state. Do one synchronous
            # marker check here so a late marker still flips cancelled=True.
            if not cancel_observed.is_set():
                _consume_cancel_marker()
            if cancel_observed.is_set():
                cancelled = True
                f.write('{"type":"_killed_by_watcher","reason":"cancelled_new_commit"}\n')
                cancel_reason_written = True
            elif timeout_observed.is_set():
                f.write('{"type":"_killed_by_watcher","reason":"timeout"}\n')
    finally:
        # Stop the watcher thread BEFORE releasing the slot so a brand-new
        # codex (acquired by another --force right after) can't be confused
        # by our still-running watcher reacting to a fresh marker for itself.
        #
        # Only join if the thread was actually started — Popen() above can
        # raise (codex not in PATH, permission denied, fd exhaustion, etc.)
        # before watcher_thread.start() runs. join()'ing an unstarted thread
        # raises RuntimeError, which would mask the original error AND skip
        # the slot release + sidecar cleanup below. The double-guard
        # (watcher_started flag + try/except RuntimeError) keeps cleanup
        # running even if the threading API changes underfoot.
        stop_run_watcher.set()
        if watcher_started and watcher_thread is not None:
            try:
                watcher_thread.join(timeout=2)
            except RuntimeError:
                pass
        # Final cancellation decision must happen after the watcher has stopped.
        # Otherwise the watcher can observe and unlink a marker after the main
        # thread's post-wait check, leaving process_pr with neither
        # result.cancelled nor a marker to consume.
        _consume_cancel_marker()
        if cancel_observed.is_set():
            cancelled = True
            _append_cancel_reason()
        # Release the global codex slot so a waiter can acquire it without
        # waiting for the .running sidecar cleanup.
        release_lock_fd(slot_fd)
        # Whether codex exited cleanly, errored, or got killed by the watchdog,
        # the .running sidecar must go away — otherwise the dashboard's running
        # section would still show this PR after the run is over.
        if running_path is not None:
            try:
                running_path.unlink(missing_ok=True)
            except OSError:
                pass

    last_msg = ""
    if last_msg_path.exists():
        last_msg = last_msg_path.read_text(encoding="utf-8").strip()

    return CodexResult(
        thread_id=thread_id,
        last_message=last_msg,
        exit_code=proc.returncode if proc is not None else -1,
        jsonl_path=jsonl_path,
        scratch_dir=scratch,
        cancelled=cancelled,
    )


# ─── calendar event ────────────────────────────────────────────────────────────


def parse_verdict(comment_body: str | None) -> str:
    """Return the verdict emoji (✅ / ⚠️ / ❌) from a codex review comment.

    Looks for the conclusion line `结论：…` that `pr_prompt.md` requires codex
    to output. Falls back to 🤖 when neither the comment body nor the verdict
    line is available.
    """
    if not comment_body:
        return "🤖"
    for line in reversed(comment_body.splitlines()):
        s = line.strip()
        if not s.startswith("结论"):
            continue
        if "❌" in s:
            return "❌"
        if "⚠" in s:
            return "⚠️"
        if "✅" in s:
            return "✅"
    # Last resort: scan whole body for any of the three.
    if "❌" in comment_body:
        return "❌"
    if "⚠" in comment_body:
        return "⚠️"
    if "✅" in comment_body:
        return "✅"
    return "🤖"


def _is_fork_pr(pr: PRSnap) -> bool:
    """True iff the PR's head branch lives on a different repo than its base.

    `head_repo == ""` (head fork deleted, or missing from GraphQL) is treated
    as the safe pessimistic case — same as a fork — because the launcher would
    blindly try `git fetch origin <branch>` on the base repo and fail.
    """
    return bool(pr.repo) and pr.head_repo != pr.repo


def _build_fix_url(
    *,
    pr: PRSnap,
    comment_url: str | None,
    origin_cwd: str | None,
) -> str | None:
    """Compose a mycalfix://fix?... URL the MyCalFix.app handler can open.

    Returns None when we lack the bare minimum (comment_url + head_branch), or
    for fork PRs whose head branch isn't on the base repo's origin (the
    launcher's `git fetch origin <branch>` would fail). origin_cwd is optional
    — the .app falls back to a folder picker when missing.
    """
    if not comment_url or not pr.head_branch:
        return None
    if _is_fork_pr(pr):
        return None
    params = {
        "repo": pr.repo,
        "branch": pr.head_branch,
        "comment": comment_url,
        "pr": pr.url,
    }
    if origin_cwd:
        params["origin_cwd"] = origin_cwd
    return "mycalfix://fix?" + urlencode(params, quote_via=quote)


def _build_paste_ready_fix_command(
    *,
    pr: PRSnap,
    comment_url: str | None,
    origin_cwd: str | None,
) -> str:
    """One-liner the user can copy-paste if MyCalFix.app isn't installed.

    Mirrors what launch_fix.sh does, but expanded inline so it works even on a
    machine without the launcher. <REPO_ROOT> is a placeholder when origin_cwd
    is unknown so the user notices they need to fill it in.
    """
    # Single-quote the placeholder so a copy-paste lands `cd: No such file or
    # directory: <…>` (clear error) instead of bash parsing `<…>` as a redirect.
    cwd = shlex.quote(origin_cwd) if origin_cwd else "'<填入本地 repo 路径>'"
    raw_branch = pr.head_branch or "<branch>"
    branch = shlex.quote(raw_branch)
    # Explicit refspec ensures `refs/remotes/origin/<branch>` is updated even
    # when the local config wouldn't otherwise create it, so `git switch` can
    # auto-track from the remote when the branch doesn't exist locally yet
    # (the launchd-fallback / cross-machine PR case).
    refspec = shlex.quote(f"+refs/heads/{raw_branch}:refs/remotes/origin/{raw_branch}")
    comment_ref = comment_url or pr.url
    prompt = (
        f"针对 {comment_ref} 这条 codex review 反馈做修复。"
        f"只改 review 明确点名的位置；跑项目自检；commit + push 同分支（不要 --force）。"
        f"diff 超 1000 行就 abort。"
    )
    return (
        f"cd {cwd} && "
        f"git fetch origin {refspec} && "
        f"git switch {branch} && git pull --ff-only origin {branch} && "
        f"claude {shlex.quote(prompt)}"
    )


def build_event(
    pr: PRSnap,
    result: CodexResult,
    comment_url: str | None,
    comment_body: str | None,
    now: datetime,
    *,
    origin_cwd: str | None = None,
) -> ReminderEvent:
    sha_short = pr.head_sha[:8]
    verdict = parse_verdict(comment_body)
    title = f"{verdict} #{pr.number} · {pr.repo}"

    # ── body 第一：完整评论内容 ──
    body_section: list[str] = []
    if comment_body:
        body_section.append(comment_body.strip())
    elif result.last_message:
        body_section.append("（comment body 未抓到，以下是 codex 最终回复）")
        body_section.append("")
        body_section.append(result.last_message)
    else:
        body_section.append("（未抓到评论内容）")

    # ── fix 入口（仅 ⚠️ / ❌；✅ 不需要修复） ──
    # Do not put mycalfix:// into EKEvent.url. iOS Calendar renders custom
    # schemes in that field as a misleading "call" action. Keep the launcher
    # URL in notes instead; macOS users can click/copy it, and iOS no longer
    # shows a fake phone action. Fork PRs never get a launcher URL — the
    # launcher's `git fetch origin <branch>` would fail on a head branch that
    # lives in someone else's fork.
    fix_url: str | None = None
    fix_section: list[str] = []
    if verdict in ("⚠️", "❌"):
        fork = _is_fork_pr(pr)
        fix_section = [
            "",
            "─" * 40,
        ]
        if fork:
            fix_section.append(
                f"fork PR（head 在 {pr.head_repo or '未知 fork'}），"
                f"暂不支持自动修复入口；请到对应 fork 本地手动 checkout。"
            )
        else:
            fix_url = _build_fix_url(pr=pr, comment_url=comment_url, origin_cwd=origin_cwd)
            paste_cmd = _build_paste_ready_fix_command(
                pr=pr, comment_url=comment_url, origin_cwd=origin_cwd,
            )
            if not origin_cwd:
                fix_section.append("⚠️  origin_cwd 未知（本次走的 launchd 兜底路径，没有 hook 喂数据）。")
                fix_section.append("    MyCalFix 触发时会弹目录选择器；下次本地 push 同 PR 会自动落 origin_cwd。")
                fix_section.append("")
            if fix_url:
                fix_section.append("MyCalFix 链接（Mac 上点击或复制打开）：")
                fix_section.append(fix_url)
                fix_section.append("Mac 终端命令：")
                fix_section.append(f"open {shlex.quote(fix_url)}")
                fix_section.append("")
            fix_section.append("paste-ready 命令（无 MyCalFix.app 时降级用）：")
            fix_section.append(paste_cmd)

    # ── metadata 折到最后 ──
    metadata: list[str] = [
        "",
        "─" * 40,
        f"PR 标题：  {pr.title}",
        f"PR 链接：  {pr.url}",
        f"分支：     {pr.head_branch or '（未知）'}",
        f"commit：   {sha_short}",
    ]
    if comment_url:
        metadata.append(f"评论：     {comment_url}")
    else:
        metadata.append("评论：     （未抓到 URL，请到 PR 页面查看）")
    if result.thread_id:
        metadata.append(f"session：  {result.thread_id}")
        metadata.append(f"resume：   codex resume {result.thread_id}")
    else:
        metadata.append("session：  （未抓到 thread_id）")
    metadata.append(f"JSONL：    {result.jsonl_path}")

    notes = "\n".join(body_section + fix_section + metadata)

    return ReminderEvent(
        key=f"my-calendar:pr-comment:{pr.url}:{pr.head_sha}",
        title=title,
        notes=notes,
        on_date=now.date(),
        start_at=now,
        duration_min=15,
        url=None,
    )


# ─── main flow ─────────────────────────────────────────────────────────────────


def process_pr(pr: PRSnap, state: dict, dry_run: bool, *, restart_depth: int = 0) -> str:
    """Return action string for logging."""
    prompt_template = PROMPT_PATH.read_text(encoding="utf-8")
    prompt = (
        prompt_template
        .replace("{pr_link}", pr.url)
        .replace("{head_sha}", pr.head_sha)
    )

    if dry_run:
        return f"would-trigger codex (prompt {len(prompt)}B)"

    if _retry_pending_calendar_deletes(state, pr.url):
        save_state(state, touched_prs={pr.url})

    print(f"    → codex exec on {pr.url}", flush=True)
    started_at = datetime.now().isoformat(timespec="seconds")

    notify_group = f"pr-watcher:{pr.url}"
    notify(
        title="🔍 PR review 开始",
        subtitle=pr.repo,
        message=f"#{pr.number} {pr.title}",
        open_url=pr.url,
        group=notify_group,
    )

    t0 = time.time()
    result = run_codex(prompt, pr)
    elapsed = time.time() - t0
    print(f"      exit={result.exit_code} thread_id={result.thread_id} log={result.jsonl_path.name} elapsed={fmt_duration(elapsed)} cancelled={result.cancelled}", flush=True)

    if result.cancelled:
        # Issue #26 reset/reboot semantics: a new commit arrived while this
        # review was in flight, the watcher killed codex, and a fresh --force
        # is waiting for our lock to start over against the latest sha. Drop
        # this run entirely — no calendar event, no .meta sidecar, no state
        # mutation. The jsonl is kept (marked _killed_by_watcher) for forensic
        # debugging. Scratch dir is cleaned up; the caller's save_state will
        # be a no-op for this PR because state["prs"][pr.url] wasn't mutated.
        notify(
            title="🛑 PR review 已取消",
            subtitle=pr.repo,
            message=f"#{pr.number} 检测到新 commit，已取消进行中的 review",
            open_url=pr.url,
            group=notify_group,
        )
        try:
            shutil.rmtree(result.scratch_dir, ignore_errors=True)
        except Exception:
            pass
        _refresh_dashboard(reason="cancelled")
        return f"cancelled (new commit during review, elapsed={fmt_duration(elapsed)})"

    def restart_from_moved_head(current_pr: PRSnap, *, marker_type: str, dashboard_reason: str, source: str) -> str:
        print(
            f"      head moved during review: {pr.head_sha[:8]} → {current_pr.head_sha[:8]}; "
            "dropping stale result",
            flush=True,
        )
        try:
            with result.jsonl_path.open("a", encoding="utf-8") as f:
                f.write(json.dumps({
                    "type": marker_type,
                    "old_head_sha": pr.head_sha,
                    "new_head_sha": current_pr.head_sha,
                }, ensure_ascii=False) + "\n")
        except OSError:
            pass
        try:
            shutil.rmtree(result.scratch_dir, ignore_errors=True)
        except Exception:
            pass
        if restart_depth >= 3:
            _clear_pending_review(state.setdefault("prs", {}).setdefault(pr.url, {}))
            save_state(state, touched_prs={pr.url})
            _refresh_dashboard(reason=dashboard_reason)
            return (
                f"stale result dropped; head kept moving "
                f"{pr.head_sha[:8]}→{current_pr.head_sha[:8]}"
            )
        _mark_review_pending(state, current_pr, source=source, now=datetime.now())
        save_state(state, touched_prs={pr.url})
        notify(
            title="🔁 PR review 重启",
            subtitle=current_pr.repo,
            message=f"#{current_pr.number} 检测到新 commit，基于最新版本重新 review",
            open_url=current_pr.url,
            group=notify_group,
        )
        _refresh_dashboard(reason=dashboard_reason)
        return process_pr(current_pr, state, dry_run=False, restart_depth=restart_depth + 1)

    try:
        current_pr = _gh_view_force_pr(pr.url)
    except (subprocess.CalledProcessError, json.JSONDecodeError, KeyError, TypeError) as e:
        print(
            f"      warn: cannot revalidate current PR head after codex exit: {e}; "
            f"keeping pending sha={pr.head_sha[:8]}",
            flush=True,
        )
        try:
            with result.jsonl_path.open("a", encoding="utf-8") as f:
                f.write(json.dumps({
                    "type": "_head_revalidation_failed",
                    "head_sha": pr.head_sha,
                    "error": str(e),
                }, ensure_ascii=False) + "\n")
        except OSError:
            pass
        try:
            shutil.rmtree(result.scratch_dir, ignore_errors=True)
        except Exception:
            pass
        _refresh_dashboard(reason="head-revalidation-failed")
        return f"head revalidation failed (not finalized, elapsed={fmt_duration(elapsed)})"

    if current_pr.head_sha and current_pr.head_sha != pr.head_sha:
        return restart_from_moved_head(
            current_pr,
            marker_type="_head_moved_before_persist",
            dashboard_reason="head-moved",
            source="post-review-head-moved",
        )

    comment_lookup = fetch_latest_ai_comment_since(pr.repo, pr.number, started_at, head_sha=pr.head_sha)
    if comment_lookup.status == "absent":
        # A detached run can start from stale local state while another path has
        # already posted the same-SHA review. The prompt below tells codex not
        # to post a duplicate in that situation, so the completion boundary may
        # be an older AI-marked comment rather than one created after started_at.
        existing_lookup = fetch_latest_ai_comment_since(pr.repo, pr.number, None, head_sha=pr.head_sha)
        if existing_lookup.status == "found" and existing_lookup.comment_url:
            comment_lookup = existing_lookup
            print(
                f"      recovered existing same-sha AI comment before this run "
                f"{existing_lookup.comment_url}",
                flush=True,
            )
    if comment_lookup.status != "found" or not comment_lookup.comment_url:
        print(
            f"      warn: no AI-marked review comment found after this run "
            f"(status={comment_lookup.status}); keeping pending sha={pr.head_sha[:8]}",
            flush=True,
        )
        try:
            with result.jsonl_path.open("a", encoding="utf-8") as f:
                f.write(json.dumps({
                    "type": "_review_comment_missing",
                    "lookup_status": comment_lookup.status,
                    "head_sha": pr.head_sha,
                }, ensure_ascii=False) + "\n")
        except OSError:
            pass
        try:
            shutil.rmtree(result.scratch_dir, ignore_errors=True)
        except Exception:
            pass
        _refresh_dashboard(reason="comment-missing")
        return f"review comment missing (status={comment_lookup.status}, elapsed={fmt_duration(elapsed)})"

    comment_url = comment_lookup.comment_url
    comment_body = comment_lookup.comment_body
    body_preview = (comment_body or "")[:80].replace("\n", " ")
    print(f"      comment_url={comment_url}  body={len(comment_body or '')}B  preview={body_preview!r}", flush=True)

    # Cache the body to disk so launch_fix.sh can hand claude a local path
    # instead of having claude do `gh api` (prompt-injection mitigation E).
    # Best-effort: a failure here is non-fatal — fix_prompt.md keeps a
    # gh-api fallback for old events / cache misses.
    cache_comment_body(comment_url, comment_body)

    # origin_cwd may have been recorded by --force on this run (hook path) or
    # left over from a prior hook run; resolve once and pass it both into the
    # calendar event (so the "fix this PR" URL/paste-ready command can encode
    # it) and the sidecar (so dashboard / future consumers don't have to parse
    # pr_state.json).
    origin_cwd = (state.get("prs", {}).get(pr.url, {}) or {}).get("origin_cwd")

    now = datetime.now()
    event = build_event(pr, result, comment_url, comment_body, now, origin_cwd=origin_cwd)

    # PR #27 high finding: the cancel watcher inside run_codex stops in its
    # finally block, but the per-PR lock is still held here. A new --force
    # arriving between run_codex's return and the writes below would write a
    # cancel marker that nobody is watching. The synchronous re-check below
    # alone was still a check-then-act race: a --force could write the marker
    # AFTER the check returned False but BEFORE upsert_events / meta sidecar /
    # state mutation finished, and the stale review for the obsolete sha
    # would land in calendar/sidecar/state. The waiting --force would then
    # write again for the fresh sha (two events, state remembering the
    # obsolete sha), violating issue #26's "新 commit 到达后旧 review 不落盘"
    # contract.
    #
    # Fix: hold the per-PR persist lock across the marker re-check, head
    # revalidation, local writes, and post-write revalidation/rollback. The
    # marker writer uses the same lock, so marker writes are totally ordered
    # with this section; remote head movement is caught by the pre/post write
    # gh checks, and post-write movement rolls back local artifacts or records
    # a retryable calendar cleanup key. See acquire_persist_lock for the full
    # contract and acquisition-order argument.
    cancel_marker = _pr_cancel_path(pr.url)
    cancelled_late = False
    already_recorded_late = False
    head_moved_late: PRSnap | None = None
    head_revalidation_late_error: str | None = None
    meta_path = result.jsonl_path.with_suffix(".meta.json")
    event_written = False
    state_entry_before_persist = dict((state.get("prs", {}).get(pr.url) or {}))
    state_had_entry_before_persist = pr.url in state.get("prs", {})
    persist_fd = acquire_persist_lock(pr.url)
    try:
        if cancel_marker.exists():
            try:
                cancel_marker.unlink(missing_ok=True)
            except OSError:
                pass
            try:
                with result.jsonl_path.open("a", encoding="utf-8") as f:
                    f.write('{"type":"_killed_by_watcher","reason":"cancelled_post_codex_pre_persist"}\n')
            except OSError:
                pass
            cancelled_late = True
        else:
            try:
                current_pr = _gh_view_force_pr(pr.url)
            except (subprocess.CalledProcessError, json.JSONDecodeError, KeyError, TypeError) as e:
                head_revalidation_late_error = str(e)
                print(
                    f"      warn: cannot revalidate current PR head before persist: {e}; "
                    f"keeping pending sha={pr.head_sha[:8]}",
                    flush=True,
                )
                try:
                    with result.jsonl_path.open("a", encoding="utf-8") as f:
                        f.write(json.dumps({
                            "type": "_head_revalidation_failed_before_persist",
                            "head_sha": pr.head_sha,
                            "error": str(e),
                        }, ensure_ascii=False) + "\n")
                except OSError:
                    pass
            else:
                if current_pr.head_sha and current_pr.head_sha != pr.head_sha:
                    head_moved_late = current_pr
                else:
                    fresh_state = load_state()
                    fresh_entry = fresh_state.get("prs", {}).get(pr.url) or {}
                    if fresh_entry.get("last_commented_sha") == pr.head_sha:
                        already_recorded_late = True
                        state.setdefault("prs", {})[pr.url] = fresh_entry
                        print(
                            f"      already recorded by another session "
                            f"sha={pr.head_sha[:8]}; dropping duplicate local persist",
                            flush=True,
                        )
                        try:
                            with result.jsonl_path.open("a", encoding="utf-8") as f:
                                f.write(json.dumps({
                                    "type": "_same_sha_already_recorded",
                                    "head_sha": pr.head_sha,
                                    "existing_comment_url": fresh_entry.get("last_comment_url"),
                                }, ensure_ascii=False) + "\n")
                        except OSError:
                            pass
                    else:
                        if fresh_entry:
                            state.setdefault("prs", {})[pr.url] = fresh_entry

                        actions = upsert_events([event], CAL_STATE_PATH, dry_run=False, calendar_name=PR_CALENDAR_NAME)
                        cal_action = actions.get(event.key, "?")
                        event_written = cal_action in {"created", "updated", "unchanged"}

                        # ── sidecar: one .meta.json per review run, canonical history record for dashboard.py ──
                        meta = {
                            "started_at": started_at,
                            "repo": pr.repo,
                            "pr_number": pr.number,
                            "pr_url": pr.url,
                            "pr_title": pr.title,
                            "head_sha": pr.head_sha,
                            "thread_id": result.thread_id,
                            "comment_url": comment_url,
                            "comment_body": comment_body or "",
                            "codex_exit": result.exit_code,
                            "jsonl_path": str(result.jsonl_path),
                            "origin_cwd": origin_cwd,
                        }
                        try:
                            meta_path.write_text(
                                json.dumps(meta, indent=2, ensure_ascii=False),
                                encoding="utf-8",
                            )
                        except OSError as e:
                            print(f"      warn: failed to write sidecar {meta_path}: {e}", flush=True)

                        entry = state["prs"].setdefault(pr.url, {})
                        entry.update({
                            "repo": pr.repo,
                            "number": pr.number,
                            "last_commented_sha": pr.head_sha,
                            "last_thread_id": result.thread_id,
                            "last_comment_url": comment_url,
                            "last_run_at": started_at,
                            "last_codex_exit": result.exit_code,
                            "last_jsonl": str(result.jsonl_path),
                        })

                        # 同时记录"我们见过这个 sha"，用于 first-run 兼容
                        entry["last_seen_sha"] = pr.head_sha
                        _clear_pending_review(entry)

                        try:
                            final_pr = _gh_view_force_pr(pr.url)
                        except (subprocess.CalledProcessError, json.JSONDecodeError, KeyError, TypeError) as e:
                            head_revalidation_late_error = str(e)
                            print(
                                f"      warn: cannot revalidate current PR head after local persist: {e}; "
                                f"rolling back sha={pr.head_sha[:8]}",
                                flush=True,
                            )
                            try:
                                with result.jsonl_path.open("a", encoding="utf-8") as f:
                                    f.write(json.dumps({
                                        "type": "_head_revalidation_failed_after_local_persist",
                                        "head_sha": pr.head_sha,
                                        "error": str(e),
                                    }, ensure_ascii=False) + "\n")
                            except OSError:
                                pass
                        else:
                            if final_pr.head_sha and final_pr.head_sha != pr.head_sha:
                                head_moved_late = final_pr

                    if head_revalidation_late_error or head_moved_late is not None:
                        calendar_delete_failed = False
                        if event_written:
                            try:
                                removed = remove_event(event.key, CAL_STATE_PATH)
                            except Exception as e:
                                print(f"      warn: failed to roll back calendar event {event.key}: {e}", flush=True)
                                removed = False
                            if not removed:
                                calendar_delete_failed = True
                        try:
                            meta_path.unlink(missing_ok=True)
                        except OSError:
                            pass
                        if state_had_entry_before_persist:
                            state.setdefault("prs", {})[pr.url] = state_entry_before_persist
                        else:
                            state.setdefault("prs", {}).pop(pr.url, None)
                        if calendar_delete_failed:
                            _remember_calendar_delete(state, pr.url, event.key)
    finally:
        release_lock_fd(persist_fd)

    # ── post-persist housekeeping (intentionally outside persist_lock to keep
    # the critical section short; nothing here mutates state a marker writer
    # cares about) ──────────────────────────────────────────────────────────
    if cancelled_late:
        notify(
            title="🛑 PR review 已取消",
            subtitle=pr.repo,
            message=f"#{pr.number} 检测到新 commit，已取消进行中的 review",
            open_url=pr.url,
            group=notify_group,
        )
        try:
            shutil.rmtree(result.scratch_dir, ignore_errors=True)
        except Exception:
            pass
        _refresh_dashboard(reason="cancelled")
        return f"cancelled (new commit between codex exit and persist, elapsed={fmt_duration(elapsed)})"

    if already_recorded_late:
        try:
            shutil.rmtree(result.scratch_dir, ignore_errors=True)
        except Exception:
            pass
        _refresh_dashboard(reason="same-sha-already-recorded")
        return f"already reviewed by another session (elapsed={fmt_duration(elapsed)})"

    if head_revalidation_late_error:
        try:
            shutil.rmtree(result.scratch_dir, ignore_errors=True)
        except Exception:
            pass
        _refresh_dashboard(reason="head-revalidation-failed")
        return f"head revalidation failed (not finalized, elapsed={fmt_duration(elapsed)})"

    if head_moved_late is not None:
        return restart_from_moved_head(
            head_moved_late,
            marker_type="_head_moved_inside_persist",
            dashboard_reason="head-moved",
            source="persist-head-moved",
        )

    # Refresh now that the .meta.json sidecar exists: this swaps the
    # "running" row out of the dashboard and shows the finished review.
    _refresh_dashboard(reason="run-end")

    # 清理 scratch 目录（codex 已退出，无后续依赖）
    try:
        shutil.rmtree(result.scratch_dir, ignore_errors=True)
    except Exception:
        pass

    return f"codex ran, calendar={cal_action}"


def _gh_view_force_pr(pr_url: str) -> PRSnap:
    """Fetch a single PR's metadata via gh pr view and build a PRSnap.
    `default_branch` is filled with `base` because gh pr view doesn't return
    defaultBranchRef directly; the pre-push hook already verified base==default
    before invoking --force, so this is safe."""
    forced = subprocess.run(
        ["gh", "pr", "view", pr_url,
         "--json", "url,number,title,isDraft,baseRefName,headRefName,headRefOid,createdAt,updatedAt,headRepository,headRepositoryOwner"],
        capture_output=True, text=True, check=True,
    )
    data = json.loads(forced.stdout)
    base_repo = pr_url.replace("https://github.com/", "").rsplit("/pull/", 1)[0]
    head_owner = (data.get("headRepositoryOwner") or {}).get("login") or ""
    head_name = (data.get("headRepository") or {}).get("name") or ""
    head_repo = f"{head_owner}/{head_name}" if head_owner and head_name else ""
    return PRSnap(
        url=data["url"],
        number=int(data["number"]),
        title=data.get("title", ""),
        is_draft=bool(data.get("isDraft", False)),
        repo=base_repo,
        base=data.get("baseRefName", ""),
        default_branch=data.get("baseRefName", ""),
        head_sha=data.get("headRefOid", ""),
        created_at=data.get("createdAt", "") or "",
        updated_at=data.get("updatedAt", "") or "",
        head_branch=data.get("headRefName", "") or "",
        head_repo=head_repo,
    )


def _run_force(args, now: datetime) -> int:
    """--force entrypoint: per-PR lock + cancel-and-restart (issue #26).

    Concurrency model:
      - Try to grab the per-PR lock.
      - If held by another leader (an in-flight review for an older sha),
        write the cancel marker. The leader's watcher thread sees it, kills
        codex, and process_pr short-circuits (no calendar event, no state
        mutation). We then wait for the leader to release the lock and
        proceed against the fresh head_sha. Notifications fire on both the
        cancel and the restart.
      - If we got the lock immediately, just run normally — no prior leader
        to cancel.
    """
    pr_url = args.force
    # Capture pre_lock_ns BEFORE the acquire so any marker written by a
    # concurrent --force AFTER our flock attempt has mtime > pre_lock_ns
    # and is preserved by clear_stale_cancel_marker (see its docstring for
    # the race this guards against).
    pre_lock_ns = time.time_ns()
    lock_fd = acquire_pr_lock_nb(pr_url)
    cancelled_prior = False
    if lock_fd is None:
        try:
            in_flight_pr = _gh_view_force_pr(pr_url)
            in_flight_state = load_state()
            in_flight_prev = in_flight_state.get("prs", {}).get(in_flight_pr.url) or {}
            if in_flight_prev.get("last_commented_sha") == in_flight_pr.head_sha:
                new_origin_cwd = _resolved_origin_cwd(args.origin_cwd)
                if new_origin_cwd:
                    in_flight_state.setdefault("prs", {}).setdefault(in_flight_pr.url, {})["origin_cwd"] = new_origin_cwd
                    save_state(in_flight_state, touched_prs={in_flight_pr.url})
                print(
                    f"  forced: {in_flight_pr.url}  → already reviewed "
                    f"sha={in_flight_pr.head_sha[:8]}, skipping"
                )
                return 0
            if in_flight_prev.get("pending_review_sha") == in_flight_pr.head_sha:
                new_origin_cwd = _resolved_origin_cwd(args.origin_cwd)
                if new_origin_cwd:
                    in_flight_state.setdefault("prs", {}).setdefault(in_flight_pr.url, {})["origin_cwd"] = new_origin_cwd
                    save_state(in_flight_state, touched_prs={in_flight_pr.url})
                print(
                    f"  forced: {in_flight_pr.url}  → review already pending "
                    f"sha={in_flight_pr.head_sha[:8]}, skipping duplicate trigger"
                )
                return 0
        except (subprocess.CalledProcessError, json.JSONDecodeError) as e:
            print(f"  forced: {pr_url}  → could not inspect in-flight sha before cancel: {e}")
        # Another leader is mid-review on an older sha. Signal cancel and
        # wait. Notify NOW so the user sees the cancellation even before the
        # restart kicks off; the restart notification goes out below once
        # we've fetched the new sha.
        print(f"  forced: {pr_url}  → another review in flight; sending cancel signal")
        notify(
            title="🛑 PR review 已取消",
            subtitle=pr_url.replace("https://github.com/", ""),
            message="检测到新 commit，正在取消进行中的 review",
            open_url=pr_url,
            group=f"pr-watcher:{pr_url}",
        )
        result = signal_cancel_and_wait_for_lock(pr_url)
        if result is None:
            print(
                f"  forced: {pr_url}  → cancel signal sent but lock not released "
                f"within {CANCEL_WAIT_LOCK_TIMEOUT_SEC:.0f}s; bailing — next tick will retry"
            )
            return 1
        # The helper captures its own pre_lock_ns just before the acquire
        # that won — use THAT one (not our top-of-function timestamp) so the
        # mtime gate is anchored to the moment we actually got the lock,
        # not seconds earlier when we first tried.
        lock_fd, pre_lock_ns = result
        cancelled_prior = True

    # We now own the per-PR lock. Drop any leftover cancel marker whose
    # mtime predates pre_lock_ns (those are stale — left behind by a
    # crashed prior leader or written for a previous generation that already
    # finished). Markers with mtime > pre_lock_ns are fresh cancel signals
    # from a NEW --force that fired its touch() after our flock won; we
    # MUST preserve them so run_codex's watcher reacts. See
    # clear_stale_cancel_marker docstring for the race this prevents.
    clear_stale_cancel_marker(pr_url, before_ns=pre_lock_ns)

    try:
        pr = _gh_view_force_pr(pr_url)

        state = load_state()
        if "installed_at" not in state.get("_meta", {}):
            state.setdefault("_meta", {})["installed_at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
            print(f"[pr-watcher] first run: stamped installed_at = {state['_meta']['installed_at']}")

        new_origin_cwd: str | None = None
        if args.origin_cwd:
            new_origin_cwd = _resolved_origin_cwd(args.origin_cwd)
            if new_origin_cwd:
                state.setdefault("prs", {}).setdefault(pr.url, {})["origin_cwd"] = new_origin_cwd
            else:
                print(f"  warn: --origin-cwd {args.origin_cwd!r} is not a directory; ignoring")

        cleanup_changed = _retry_pending_calendar_deletes(state, pr.url)
        prev = state.get("prs", {}).get(pr.url) or {}
        if prev.get("last_commented_sha") == pr.head_sha:
            print(f"  forced: {pr.url}  → already reviewed sha={pr.head_sha[:8]}, skipping")
            # Mirror freshly-stamped origin_cwd into prior run's .meta.json
            # sidecar so dashboard / "fix this PR" consumers don't have to
            # fall back to pr_state.json for that field.
            if new_origin_cwd:
                try:
                    meta_path, changed = sync_meta_origin_cwd(prev.get("last_jsonl"), new_origin_cwd)
                    if changed and meta_path is not None:
                        print(f"      backfilled origin_cwd into {meta_path.name}")
                except (OSError, json.JSONDecodeError) as e:
                    print(f"      warn: failed to backfill origin_cwd in prior .meta.json: {e}")
            _clear_pending_review(prev)
            save_state(state, touched_prs={pr.url})
            return 0

        force_now = datetime.now()
        guard_action, guard_changed = _same_sha_review_guard(pr, state, force_now)
        if guard_action is not None:
            print(f"  forced: {pr.url}  → {guard_action}, skipping")
            if guard_changed or cleanup_changed or new_origin_cwd:
                save_state(state, touched_prs={pr.url})
            return 0

        if cancelled_prior:
            # Pair-notification for the cancel above — the restart against
            # the freshly fetched sha. Same notification group so terminal-
            # notifier collapses prior cancel/start banners into one.
            notify(
                title="🔁 PR review 重启",
                subtitle=pr.repo,
                message=f"#{pr.number} 基于新 commit {pr.head_sha[:7]} 重启 review",
                open_url=pr.url,
                group=f"pr-watcher:{pr.url}",
            )
            print(f"  forced: {pr_url}  → restarting review against fresh sha={pr.head_sha[:8]}")

        _mark_review_pending(state, pr, source="force", now=datetime.now())
        save_state(state, touched_prs={pr.url})
        action = process_pr(pr, state, dry_run=False)
        save_state(state, touched_prs={pr.url})
        print(f"  forced: {pr.url}  → {action}")
    finally:
        release_lock_fd(lock_fd)
    return 0


def main() -> int:
    redirect_stdio_to_log()
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--dry-run", action="store_true", help="list candidates, do not invoke codex / write calendar / mutate state")
    parser.add_argument("--seed-only", action="store_true", help="record current head_shas in state without invoking codex")
    parser.add_argument("--force", type=str, default=None, help="force trigger codex for a specific PR URL (bypass state check)")
    parser.add_argument(
        "--origin-cwd", type=str, default=None,
        help="local repo root the pushed PR came from; persisted in pr_state.json[<pr_url>].origin_cwd"
             " so the calendar event's 'fix this PR' launcher knows where to open a session."
             " Typically passed by the pre-push hook; ignored on the launchd tick path.",
    )
    parser.add_argument("--ignore-throttle", action="store_true", help="ignore nighttime throttle (useful for manual runs)")
    args = parser.parse_args()

    if args.origin_cwd and not args.force:
        # Easy mistake when debugging manually: passing --origin-cwd to a tick run
        # silently drops the value. Be loud about it so users notice instead of
        # assuming state was updated.
        print(
            f"[pr-watcher] WARNING: --origin-cwd {args.origin_cwd!r} ignored without --force "
            "(origin_cwd is only persisted on the --force code path)",
            file=sys.stderr,
        )

    now = datetime.now()
    state = load_state()

    if not args.ignore_throttle and not args.dry_run and not args.seed_only and not args.force:
        if should_skip_this_tick(state, now):
            print(f"[pr-watcher] {now.isoformat(timespec='seconds')}  夜间节流：距上次运行不足 5min，跳过")
            return 0

    # Per-PR locks gate codex execution: different PRs run fully in parallel.
    # --force uses signal_cancel_and_wait_for_lock so a new commit during an
    # in-flight review kills the stale codex and immediately restarts against
    # the latest sha (issue #26 reset/reboot semantics). The tick path takes
    # per-PR locks inside its candidates loop and skips PRs currently being
    # reviewed — the next tick (or the next push hook) will pick up changes
    # the in-flight reviewer didn't cover. --dry-run doesn't mutate and takes
    # no lock at all.

    print(f"[pr-watcher] {now.isoformat(timespec='seconds')}  daytime={in_daytime(now)}  dry_run={args.dry_run}  seed_only={args.seed_only}")

    if args.force:
        return _run_force(args, now)

    # First-ever run stamps installed_at. Historical first-seen PRs still seed
    # silently, but PRs created after this timestamp (or whose latest head
    # commit is after this timestamp) are reviewed on first sight by the
    # launchd fallback if the immediate hooks missed them.
    if not args.dry_run and "installed_at" not in state.get("_meta", {}):
        state.setdefault("_meta", {})["installed_at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
        print(f"[pr-watcher] first run: stamped installed_at = {state['_meta']['installed_at']}")

    prs = fetch_open_prs()
    print(f"  open PRs (any base): {len(prs)}")

    candidates: list[PRSnap] = []
    for pr in prs:
        if not pr.default_branch:
            print(f"    skip (no default branch info)  {pr.url}")
            continue
        if pr.base != pr.default_branch:
            print(f"    skip (base={pr.base} ≠ default={pr.default_branch})  {pr.url}")
            continue
        candidates.append(pr)
    print(f"  candidates (base==default): {len(candidates)}")

    for pr in candidates:
        # Per-PR lock: skip PRs currently being reviewed by another process
        # (typically a --force from the pre-push hook). The next tick will
        # re-check this PR; head_sha-based dedup ensures we don't post a
        # duplicate review for a sha the leader already covered.
        pr_fd: int | None = None
        if not args.dry_run:
            # Capture BEFORE the acquire so any marker written by a
            # concurrent --force after our flock attempt is preserved (its
            # mtime will be > pre_lock_ns). See clear_stale_cancel_marker.
            pre_lock_ns = time.time_ns()
            pr_fd = acquire_pr_lock_nb(pr.url)
            if pr_fd is None:
                print(f"    busy (another process holds the lock)  {pr.url}")
                continue
            # We own the per-PR lock now. Drop any cancel marker whose mtime
            # predates pre_lock_ns; preserve any fresher one as a real cancel
            # signal for this run.
            clear_stale_cancel_marker(pr.url, before_ns=pre_lock_ns)
            # Refresh in-memory state for this PR from disk: a concurrent
            # --force on this PR may have just updated last_commented_sha,
            # and our top-of-main load_state() snapshot would be stale.
            fresh = load_state()
            if pr.url in fresh.get("prs", {}):
                state.setdefault("prs", {})[pr.url] = fresh["prs"][pr.url]
            if "installed_at" in fresh.get("_meta", {}):
                state.setdefault("_meta", {})["installed_at"] = fresh["_meta"]["installed_at"]

        # Did this iteration mutate state["prs"][pr.url]? If so, save under the
        # per-PR lock BEFORE releasing it (see finally block below). Without
        # this, a --force that wakes up between release and the post-loop
        # save_state would read stale state and re-run codex on the same sha.
        pr_state_changed = False
        try:
            prev = state["prs"].get(pr.url)
            # Conservative only for historical PRs: the fallback should not
            # blast old open PRs on first install / rebuild, but post-install
            # PRs still need review if the local pre-push / pr-created hooks
            # missed the first trigger.
            if prev is None:
                should_review_first_seen = (not args.seed_only) and _first_seen_pr_should_review(pr, state)
                if not should_review_first_seen:
                    reason = "seed-only" if args.seed_only else "historical first-seen fallback"
                    print(
                        f"    seed ({reason})  {pr.url}  sha={pr.head_sha[:8]}  "
                        f"created={pr.created_at} head_commit={pr.head_committed_at} updated={pr.updated_at}"
                    )
                    if not args.dry_run:
                        state["prs"][pr.url] = {
                            "repo": pr.repo,
                            "number": pr.number,
                            "last_seen_sha": pr.head_sha,
                            "last_commented_sha": None,
                            "seeded_at": now.isoformat(timespec="seconds"),
                            "seed_reason": reason,
                        }
                        pr_state_changed = True
                    continue

                print(
                    f"    FIRST-SEEN REVIEW  {pr.url}  NEW → {pr.head_sha[:8]}  "
                    f"created={pr.created_at} head_commit={pr.head_committed_at} updated={pr.updated_at}"
                )
                if not args.dry_run:
                    _mark_review_pending(state, pr, source="poll-first-seen", now=now)
                    pr_state_changed = True
                    save_state(state, touched_prs={pr.url})
                action = process_pr(pr, state, dry_run=args.dry_run)
                print(f"      → {action}")
                continue

            if args.seed_only:
                print(f"    seed-only (skip codex)  {pr.url}  sha={pr.head_sha[:8]}")
                if not args.dry_run:
                    state["prs"][pr.url]["last_seen_sha"] = pr.head_sha
                    pr_state_changed = True
                continue

            if not args.dry_run and _retry_pending_calendar_deletes(state, pr.url):
                pr_state_changed = True

            retry_after_pending_clear = False
            if args.dry_run:
                if prev.get("last_commented_sha") == pr.head_sha:
                    print(f"    already reviewed sha={pr.head_sha[:8]}  {pr.url}")
                    continue
                if prev.get("pending_review_sha") == pr.head_sha:
                    age = _pending_age_seconds(prev, now)
                    age_s = "unknown" if age is None else f"{int(age)}s"
                    print(f"    review pending (dry-run, no recovery) sha={pr.head_sha[:8]} age={age_s}  {pr.url}")
                    continue
            else:
                had_matching_pending = (prev.get("pending_review_sha") == pr.head_sha)
                guard_action, guard_changed = _same_sha_review_guard(pr, state, now)
                retry_after_pending_clear = (
                    had_matching_pending
                    and guard_changed
                    and prev.get("pending_review_sha") is None
                    and prev.get("last_commented_sha") != pr.head_sha
                )
                if guard_changed:
                    pr_state_changed = True
                if guard_action is not None:
                    print(f"    {guard_action}  {pr.url}")
                    continue

            # 已 seed 过：若 head_sha 自上次"评论或 seed"以来未变 → 跳过
            baseline = prev.get("last_commented_sha") or prev.get("last_seen_sha")
            if baseline == pr.head_sha and not retry_after_pending_clear:
                print(f"    unchanged  {pr.url}  sha={pr.head_sha[:8]}")
                continue

            # 有新 commit → 触发 codex
            if retry_after_pending_clear:
                print(f"    STALE PENDING RETRY  {pr.url}  sha={pr.head_sha[:8]}")
            else:
                print(f"    NEW COMMIT  {pr.url}  {baseline[:8] if baseline else 'NEW'} → {pr.head_sha[:8]}")
            if not args.dry_run:
                _mark_review_pending(state, pr, source="poll", now=now)
                pr_state_changed = True
                save_state(state, touched_prs={pr.url})
            action = process_pr(pr, state, dry_run=args.dry_run)
            if not args.dry_run:
                pr_state_changed = True
            print(f"      → {action}")
        finally:
            if pr_fd is not None:
                # Persist BEFORE releasing the lock so a concurrent --force
                # for this PR cannot read the pre-codex sha.
                if pr_state_changed:
                    save_state(state, touched_prs={pr.url})
                release_lock_fd(pr_fd)

    if not args.dry_run:
        # Flush _meta updates that happen outside the per-PR scope (e.g. a
        # freshly stamped installed_at when the tick fires on a brand-new
        # install). Per-PR entries were already saved inside the loop above.
        save_state(state, touched_prs=set())
    print(f"[pr-watcher] done")
    return 0


if __name__ == "__main__":
    sys.exit(main())
