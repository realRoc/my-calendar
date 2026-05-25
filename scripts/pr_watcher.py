"""GitHub PR watcher → codex review → Apple Calendar.

Polled by launchd every 2 minutes. The script self-throttles overnight (22:00–09:00
local) so the effective cadence is:

  - 09:00–22:00   every 2 minutes
  - other hours   every 5 minutes (skip if previous run was <5min ago)

Single-pass flow:
  1. GraphQL: list all open PRs authored by @me across every org.
  2. Filter: keep only PRs whose base == repo default branch.
  3. Compare each PR's head_sha against scripts/pr_state.json.
       - PR not in state:
           * if PR.createdAt > _meta.installed_at  → trigger codex review (newly opened PR)
           * else (PR existed before install, or seed-only mode) → record head_sha, DO NOT comment
       - PR in state and head_sha unchanged → skip.
       - PR in state and head_sha changed → trigger codex review.

  _meta.installed_at is stamped on the very first run; it's the cutoff that
  distinguishes "PRs that already existed when the tool was installed" (just
  seed them to avoid back-reviewing history) from "PRs created after install"
  (treat as actionable, even if the local git pre-push hook didn't fire — e.g.
  PR was created via GitHub web UI, gh pr create, or pushed from another
  machine).
  4. For each triggered PR:
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

from calendar_sync import ReminderEvent, upsert_events, PR_CALENDAR_NAME  # noqa: E402
from log_setup import redirect_stdio_to_log  # noqa: E402

STATE_PATH = HERE / "pr_state.json"           # PR-level state (head_sha, thread_id, …)
CAL_STATE_PATH = HERE / "pr_calendar_state.json"   # EventKit event_id index (separate from 节日提醒)
PROMPT_PATH = HERE / "pr_prompt.md"
LOG_DIR = HERE / "pr_logs"
SCRATCH_BASE = Path("/tmp/codex-pr-runs")
LOCK_DIR = HERE / "locks"                     # per-PR flock files + cancel markers + state lock
STATE_LOCK_PATH = LOCK_DIR / "state.lock"     # brief flock around state read/merge/write
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
# Two trigger channels (git pre-push hook + launchd 2-min tick) can fire on the
# same PR within seconds. codex runs ~1–2 min, so without serialisation two
# processes can race past the state check, run codex on the same head_sha, and
# post duplicate comments.
#
# Design: per-PR flock + cancel marker (reset/reboot semantics).
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
#   - State save (see save_state) uses a separate brief flock (locks/state.lock)
#     and does a load-merge-write so concurrent writers for different PRs don't
#     clobber each other's updates.


def _pr_safe_id(pr_url: str) -> str:
    return pr_url.replace("https://github.com/", "").replace("/", "_")


def _pr_lock_path(pr_url: str) -> Path:
    return LOCK_DIR / f"{_pr_safe_id(pr_url)}.lock"


def _pr_cancel_path(pr_url: str) -> Path:
    return LOCK_DIR / f"{_pr_safe_id(pr_url)}.cancel"


def clear_stale_cancel_marker(pr_url: str) -> None:
    """Drop any leftover cancel marker for this PR. MUST be called only by
    the holder of the per-PR lock, and ONLY at lock-acquisition time.

    Why the restriction matters: the marker is the contract between a new
    --force and the current leader's watcher. A new --force that fails to
    grab the lock writes the marker via signal_cancel_and_wait_for_lock();
    the current leader's watcher must observe it and kill its codex run.
    If anyone other than "the holder, at acquisition time" clears the marker
    — e.g. clearing it again later, in run_codex(), after a new --force has
    already written its cancel signal — that cancel signal is silently lost,
    the stale review runs to completion, and the new --force ends up running
    a duplicate review on the fresh sha. See regression test
    test_run_codex_does_not_clear_concurrent_cancel_marker.
    """
    try:
        _pr_cancel_path(pr_url).unlink(missing_ok=True)
    except OSError:
        pass


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
) -> int | None:
    """--force entrypoint helper. Write the cancel marker so the current
    leader's codex is killed, then poll for the per-PR lock until the leader
    releases it. Returns the lock fd on success, None if the leader didn't
    release within timeout_sec (defensive — should normally take <2s after
    kill since codex exits, leader's process_pr short-circuits, and the
    flock is released)."""
    LOCK_DIR.mkdir(parents=True, exist_ok=True)
    _pr_cancel_path(pr_url).touch()
    deadline = time.time() + timeout_sec
    while True:
        fd = acquire_pr_lock_nb(pr_url)
        if fd is not None:
            return fd
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
    head_branch: str = ""     # source branch (headRefName); needed by the
                              # "fix this PR" launcher to git checkout locally.
    head_repo: str = ""       # headRepository.nameWithOwner; differs from .repo
                              # on fork PRs. Used to suppress the fix URL when
                              # the head branch isn't on the base repo's origin.


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


def fetch_latest_comment(repo: str, number: int) -> tuple[str | None, str | None]:
    """Return (html_url, body) of the most recent comment by the current user on the PR."""
    try:
        me_proc = subprocess.run(
            ["gh", "api", "user", "--jq", ".login"],
            capture_output=True, text=True, check=True,
        )
        me = me_proc.stdout.strip()
        proc = subprocess.run(
            [
                "gh", "api",
                f"repos/{repo}/issues/{number}/comments",
                "--jq",
                f'[.[] | select(.user.login == "{me}")] | sort_by(.created_at) | .[-1]',
            ],
            capture_output=True, text=True, check=True,
        )
        out = proc.stdout.strip()
        if not out or out == "null":
            return None, None
        data = json.loads(out)
        return data.get("html_url") or None, data.get("body") or None
    except (subprocess.CalledProcessError, json.JSONDecodeError):
        return None, None


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
            [sys.executable, str(HERE / "dashboard.py")],
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
    stop_cancel_watcher = threading.Event()
    cancel_observed = threading.Event()

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

    def _cancel_watcher() -> None:
        """Poll the cancel marker; if it appears while codex is running, kill
        the subprocess group and set cancel_observed. Exits when
        stop_cancel_watcher fires (i.e. the main thread already finished with
        codex)."""
        while True:
            try:
                marker_exists = cancel_marker.exists()
            except OSError:
                marker_exists = False
            if marker_exists:
                # Only treat as a real cancel if proc is still running. If
                # codex already exited naturally, the marker just means a new
                # --force is waiting for our lock — we clear it so the new
                # leader doesn't see its own past signal as an order to cancel
                # ITSELF.
                if proc is not None and proc.poll() is None:
                    cancel_observed.set()
                    try:
                        cancel_marker.unlink(missing_ok=True)
                    except OSError:
                        pass
                    _kill_proc_group(proc)
                else:
                    try:
                        cancel_marker.unlink(missing_ok=True)
                    except OSError:
                        pass
                return
            if stop_cancel_watcher.wait(CANCEL_POLL_SEC):
                return

    watcher_thread = threading.Thread(target=_cancel_watcher, daemon=True)
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
            watcher_thread.start()
            watcher_started = True
            start = time.time()
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
                if time.time() - start > MAX_RUNTIME_PER_TICK_SEC:
                    _kill_proc_group(proc)
                    f.write('{"type":"_killed_by_watcher","reason":"timeout"}\n')
                    break
            proc.wait()
            if cancel_observed.is_set():
                cancelled = True
                f.write('{"type":"_killed_by_watcher","reason":"cancelled_new_commit"}\n')
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
        stop_cancel_watcher.set()
        if watcher_started:
            try:
                watcher_thread.join(timeout=2)
            except RuntimeError:
                pass
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
        f"diff 超 200 行就 abort。"
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
    # URL goes on EKEvent.url (Calendar.app surfaces as a clickable link); the
    # paste-ready command is also dumped into notes as a degradation path for
    # when MyCalFix.app isn't installed. Fork PRs are explicitly skipped — the
    # launcher's `git fetch origin <branch>` would fail on a head branch that
    # lives in someone else's fork.
    fix_url: str | None = None
    fix_section: list[str] = []
    if verdict in ("⚠️", "❌"):
        fork = _is_fork_pr(pr)
        fix_section = [
            "",
            "─" * 40,
            "🛠 修复入口（MyCalFix）",
        ]
        if fork:
            fix_section.append(
                f"链接：（fork PR，head 在 {pr.head_repo or '未知 fork'}，"
                f"暂不支持自动修复入口；请到对应 fork 本地手动 checkout）"
            )
        else:
            fix_url = _build_fix_url(pr=pr, comment_url=comment_url, origin_cwd=origin_cwd)
            paste_cmd = _build_paste_ready_fix_command(
                pr=pr, comment_url=comment_url, origin_cwd=origin_cwd,
            )
            if fix_url:
                fix_section.append(f"链接：{fix_url}")
            else:
                fix_section.append("链接：（缺 head_branch 或 comment_url，未能构造 mycalfix URL）")
            fix_section.append("")
            if not origin_cwd:
                fix_section.append("⚠️  origin_cwd 未知（本次走的 launchd 兜底路径，没有 hook 喂数据）。")
                fix_section.append("    .app 触发时会弹目录选择器；下次本地 push 同 PR 会自动落 origin_cwd。")
                fix_section.append("")
            fix_section.append("paste-ready 命令（无 .app 时降级用）：")
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
        url=fix_url,
    )


# ─── main flow ─────────────────────────────────────────────────────────────────


def process_pr(pr: PRSnap, state: dict, dry_run: bool) -> str:
    """Return action string for logging."""
    prompt_template = PROMPT_PATH.read_text(encoding="utf-8")
    prompt = prompt_template.replace("{pr_link}", pr.url)

    if dry_run:
        return f"would-trigger codex (prompt {len(prompt)}B)"

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

    comment_url, comment_body = fetch_latest_comment(pr.repo, pr.number)
    body_preview = (comment_body or "")[:80].replace("\n", " ")
    print(f"      comment_url={comment_url}  body={len(comment_body or '')}B  preview={body_preview!r}", flush=True)

    # origin_cwd may have been recorded by --force on this run (hook path) or
    # left over from a prior hook run; resolve once and pass it both into the
    # calendar event (so the "fix this PR" URL/paste-ready command can encode
    # it) and the sidecar (so dashboard / future consumers don't have to parse
    # pr_state.json).
    origin_cwd = (state.get("prs", {}).get(pr.url, {}) or {}).get("origin_cwd")

    now = datetime.now()
    event = build_event(pr, result, comment_url, comment_body, now, origin_cwd=origin_cwd)
    actions = upsert_events([event], CAL_STATE_PATH, dry_run=False, calendar_name=PR_CALENDAR_NAME)
    cal_action = actions.get(event.key, "?")

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
    meta_path = result.jsonl_path.with_suffix(".meta.json")
    try:
        meta_path.write_text(
            json.dumps(meta, indent=2, ensure_ascii=False),
            encoding="utf-8",
        )
    except OSError as e:
        print(f"      warn: failed to write sidecar {meta_path}: {e}", flush=True)

    # Refresh again now that the .meta.json sidecar exists: this swaps the
    # "running" row out of the dashboard and shows the finished review.
    _refresh_dashboard(reason="run-end")

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
         "--json", "url,number,title,isDraft,baseRefName,headRefName,headRefOid,createdAt,headRepository,headRepositoryOwner"],
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
    lock_fd = acquire_pr_lock_nb(pr_url)
    cancelled_prior = False
    if lock_fd is None:
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
        lock_fd = signal_cancel_and_wait_for_lock(pr_url)
        if lock_fd is None:
            print(
                f"  forced: {pr_url}  → cancel signal sent but lock not released "
                f"within {CANCEL_WAIT_LOCK_TIMEOUT_SEC:.0f}s; bailing — next tick will retry"
            )
            return 1
        cancelled_prior = True

    # We now own the per-PR lock. Drop any leftover cancel marker BEFORE
    # entering process_pr / run_codex. This covers two cases:
    #   - "no leader" path: leftover marker from a previous crash/abort.
    #   - "cancelled prior" path: the old leader's watcher usually unlinks
    #     the marker after killing codex, but if the old leader short-circuited
    #     before run_codex (e.g. already_reviewed) the marker would persist.
    # From here on, any marker that appears is a fresh cancel signal aimed at
    # this run, and run_codex's watcher must react to it — NOT delete it as
    # stale. See clear_stale_cancel_marker docstring for the race we're
    # avoiding.
    clear_stale_cancel_marker(pr_url)

    try:
        pr = _gh_view_force_pr(pr_url)

        state = load_state()
        if "installed_at" not in state.get("_meta", {}):
            state.setdefault("_meta", {})["installed_at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
            print(f"[pr-watcher] first run: stamped installed_at = {state['_meta']['installed_at']}")

        new_origin_cwd: str | None = None
        if args.origin_cwd:
            cwd_p = Path(args.origin_cwd).expanduser().resolve()
            if cwd_p.is_dir():
                new_origin_cwd = str(cwd_p)
                state.setdefault("prs", {}).setdefault(pr.url, {})["origin_cwd"] = new_origin_cwd
            else:
                print(f"  warn: --origin-cwd {args.origin_cwd!r} is not a directory; ignoring")

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

    # First-ever run stamps installed_at as the "new PR vs pre-existing PR" cutoff.
    # PRs created after this timestamp are treated as actionable on first sight;
    # PRs created before are seeded silently (the original behaviour).
    if not args.dry_run and "installed_at" not in state.get("_meta", {}):
        state.setdefault("_meta", {})["installed_at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
        print(f"[pr-watcher] first run: stamped installed_at = {state['_meta']['installed_at']}")

    installed_at_dt = _parse_iso_utc(state.get("_meta", {}).get("installed_at", ""))

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
            pr_fd = acquire_pr_lock_nb(pr.url)
            if pr_fd is None:
                print(f"    busy (another process holds the lock)  {pr.url}")
                continue
            # We own the per-PR lock now. Drop any leftover cancel marker
            # before doing any work; from here on, any marker that appears
            # is a fresh cancel signal aimed at this run (see
            # clear_stale_cancel_marker docstring).
            clear_stale_cancel_marker(pr.url)
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
            # 首次见到此 PR：根据它是 install 之前就存在的旧 PR 还是之后新建的 PR 来决定
            if prev is None:
                pr_created_dt = _parse_iso_utc(pr.created_at)
                is_post_install = (
                    not args.seed_only
                    and installed_at_dt is not None
                    and pr_created_dt is not None
                    and pr_created_dt > installed_at_dt
                )

                if not is_post_install:
                    # 装好工具之前就存在的旧 PR / 或显式 --seed-only：只 seed
                    reason = "seed-only" if args.seed_only else "pre-install"
                    print(f"    seed ({reason})  {pr.url}  sha={pr.head_sha[:8]}  created={pr.created_at}")
                    if not args.dry_run:
                        state["prs"][pr.url] = {
                            "repo": pr.repo,
                            "number": pr.number,
                            "last_seen_sha": pr.head_sha,
                            "last_commented_sha": None,
                            "seeded_at": now.isoformat(timespec="seconds"),
                        }
                        pr_state_changed = True
                    continue

                # 装好之后才创建的 PR → 即便 git pre-push hook 没抓到（网页 UI、异机 push、gh pr create…），也直接评论
                print(f"    NEW PR (post-install)  {pr.url}  sha={pr.head_sha[:8]}  created={pr.created_at}")
                action = process_pr(pr, state, dry_run=args.dry_run)
                if not args.dry_run:
                    pr_state_changed = True
                print(f"      → {action}")
                continue

            if args.seed_only:
                print(f"    seed-only (skip codex)  {pr.url}  sha={pr.head_sha[:8]}")
                if not args.dry_run:
                    state["prs"][pr.url]["last_seen_sha"] = pr.head_sha
                    pr_state_changed = True
                continue

            # 已 seed 过：若 head_sha 自上次"评论或 seed"以来未变 → 跳过
            baseline = prev.get("last_commented_sha") or prev.get("last_seen_sha")
            if baseline == pr.head_sha:
                print(f"    unchanged  {pr.url}  sha={pr.head_sha[:8]}")
                continue

            # 有新 commit → 触发 codex
            print(f"    NEW COMMIT  {pr.url}  {baseline[:8] if baseline else 'NEW'} → {pr.head_sha[:8]}")
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
