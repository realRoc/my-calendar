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

Usage:
  python pr_watcher.py                  # one polling tick (the launchd entrypoint)
  python pr_watcher.py --dry-run        # show what would happen, no codex, no calendar
  python pr_watcher.py --seed-only      # populate state with current head_shas, never trigger codex
  python pr_watcher.py --force <url>    # force-trigger codex for a specific PR (ignores state)
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, time as dtime, timezone
from pathlib import Path

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
        headRefOid
        repository {
          nameWithOwner
          defaultBranchRef { name }
        }
      }
    }
  }
}
""".strip()


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


def load_state() -> dict:
    if STATE_PATH.exists():
        return json.loads(STATE_PATH.read_text(encoding="utf-8"))
    return {"_meta": {}, "prs": {}}


def save_state(state: dict) -> None:
    state.setdefault("_meta", {})["last_run"] = datetime.now().isoformat(timespec="seconds")
    STATE_PATH.write_text(
        json.dumps(state, indent=2, ensure_ascii=False, sort_keys=True),
        encoding="utf-8",
    )


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


def run_codex(prompt: str, pr_url: str) -> CodexResult:
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    SCRATCH_BASE.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    safe_id = pr_url.replace("https://github.com/", "").replace("/", "_")
    jsonl_path = LOG_DIR / f"{stamp}__{safe_id}.jsonl"
    last_msg_path = LOG_DIR / f"{stamp}__{safe_id}.last.txt"
    scratch = SCRATCH_BASE / f"{stamp}-{uuid.uuid4().hex[:8]}"
    scratch.mkdir(parents=True, exist_ok=True)

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
    with jsonl_path.open("w", encoding="utf-8") as f:
        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            env=env,
            cwd=str(scratch),
        )
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
                proc.kill()
                f.write('{"type":"_killed_by_watcher","reason":"timeout"}\n')
                break
        proc.wait()

    last_msg = ""
    if last_msg_path.exists():
        last_msg = last_msg_path.read_text(encoding="utf-8").strip()

    return CodexResult(
        thread_id=thread_id,
        last_message=last_msg,
        exit_code=proc.returncode,
        jsonl_path=jsonl_path,
        scratch_dir=scratch,
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


def build_event(
    pr: PRSnap,
    result: CodexResult,
    comment_url: str | None,
    comment_body: str | None,
    now: datetime,
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

    # ── metadata 折到最后 ──
    metadata: list[str] = [
        "",
        "─" * 40,
        f"PR 标题：  {pr.title}",
        f"PR 链接：  {pr.url}",
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

    notes = "\n".join(body_section + metadata)

    return ReminderEvent(
        key=f"my-calendar:pr-comment:{pr.url}:{pr.head_sha}",
        title=title,
        notes=notes,
        on_date=now.date(),
        start_at=now,
        duration_min=15,
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
    result = run_codex(prompt, pr.url)
    elapsed = time.time() - t0
    print(f"      exit={result.exit_code} thread_id={result.thread_id} log={result.jsonl_path.name} elapsed={fmt_duration(elapsed)}", flush=True)

    comment_url, comment_body = fetch_latest_comment(pr.repo, pr.number)
    body_preview = (comment_body or "")[:80].replace("\n", " ")
    print(f"      comment_url={comment_url}  body={len(comment_body or '')}B  preview={body_preview!r}", flush=True)

    now = datetime.now()
    event = build_event(pr, result, comment_url, comment_body, now)
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
    }
    meta_path = result.jsonl_path.with_suffix(".meta.json")
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

    # 清理 scratch 目录（codex 已退出，无后续依赖）
    try:
        shutil.rmtree(result.scratch_dir, ignore_errors=True)
    except Exception:
        pass

    return f"codex ran, calendar={cal_action}"


def main() -> int:
    redirect_stdio_to_log()
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--dry-run", action="store_true", help="list candidates, do not invoke codex / write calendar / mutate state")
    parser.add_argument("--seed-only", action="store_true", help="record current head_shas in state without invoking codex")
    parser.add_argument("--force", type=str, default=None, help="force trigger codex for a specific PR URL (bypass state check)")
    parser.add_argument("--ignore-throttle", action="store_true", help="ignore nighttime throttle (useful for manual runs)")
    args = parser.parse_args()

    now = datetime.now()
    state = load_state()

    if not args.ignore_throttle and not args.dry_run and not args.seed_only and not args.force:
        if should_skip_this_tick(state, now):
            print(f"[pr-watcher] {now.isoformat(timespec='seconds')}  夜间节流：距上次运行不足 5min，跳过")
            return 0

    # First-ever run stamps installed_at as the "new PR vs pre-existing PR" cutoff.
    # PRs created after this timestamp are treated as actionable on first sight;
    # PRs created before are seeded silently (the original behaviour).
    if not args.dry_run and "installed_at" not in state.get("_meta", {}):
        state.setdefault("_meta", {})["installed_at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
        print(f"[pr-watcher] first run: stamped installed_at = {state['_meta']['installed_at']}")

    installed_at_dt = _parse_iso_utc(state.get("_meta", {}).get("installed_at", ""))

    print(f"[pr-watcher] {now.isoformat(timespec='seconds')}  daytime={in_daytime(now)}  dry_run={args.dry_run}  seed_only={args.seed_only}")

    if args.force:
        # Bypass GraphQL listing; fetch this single PR's metadata
        forced = subprocess.run(
            ["gh", "pr", "view", args.force,
             "--json", "url,number,title,isDraft,baseRefName,headRefOid,createdAt"],
            capture_output=True, text=True, check=True,
        )
        data = json.loads(forced.stdout)
        # gh pr view doesn't give defaultBranchRef directly; treat base as if it were default for --force.
        # (The pre-push trigger script already verifies base==default before invoking --force.)
        pr = PRSnap(
            url=data["url"],
            number=int(data["number"]),
            title=data.get("title", ""),
            is_draft=bool(data.get("isDraft", False)),
            repo=args.force.replace("https://github.com/", "").rsplit("/pull/", 1)[0],
            base=data.get("baseRefName", ""),
            default_branch=data.get("baseRefName", ""),
            head_sha=data.get("headRefOid", ""),
            created_at=data.get("createdAt", "") or "",
        )
        action = process_pr(pr, state, dry_run=False)
        save_state(state)
        print(f"  forced: {pr.url}  → {action}")
        return 0

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
                continue

            # 装好之后才创建的 PR → 即便 git pre-push hook 没抓到（网页 UI、异机 push、gh pr create…），也直接评论
            print(f"    NEW PR (post-install)  {pr.url}  sha={pr.head_sha[:8]}  created={pr.created_at}")
            action = process_pr(pr, state, dry_run=args.dry_run)
            print(f"      → {action}")
            continue

        if args.seed_only:
            print(f"    seed-only (skip codex)  {pr.url}  sha={pr.head_sha[:8]}")
            if not args.dry_run:
                state["prs"][pr.url]["last_seen_sha"] = pr.head_sha
            continue

        # 已 seed 过：若 head_sha 自上次"评论或 seed"以来未变 → 跳过
        baseline = prev.get("last_commented_sha") or prev.get("last_seen_sha")
        if baseline == pr.head_sha:
            print(f"    unchanged  {pr.url}  sha={pr.head_sha[:8]}")
            continue

        # 有新 commit → 触发 codex
        print(f"    NEW COMMIT  {pr.url}  {baseline[:8] if baseline else 'NEW'} → {pr.head_sha[:8]}")
        action = process_pr(pr, state, dry_run=args.dry_run)
        print(f"      → {action}")

    if not args.dry_run:
        save_state(state)
    print(f"[pr-watcher] done")
    return 0


if __name__ == "__main__":
    sys.exit(main())
