# my-calendar

> **A local-first workflow for collaborating with AI coding agents (Claude Code / codex), wired onto Apple Calendar as the notification bus.**
> Push code → an AI reviews the PR → the verdict lands in your calendar → one click starts a local AI fix session. No self-hosted cloud, no UI — all state lives in local files. (The AI review/fix workflow does need GitHub, Codex, and Claude accounts, and sends your PR contents to those services; the holiday reminders are the part that's fully local, no account required.)

*Read this in [中文](./README.zh-CN.md).*

The methodology behind these workflows comes from my other open-source project — **[git-hired](https://realroc.github.io/git-hired/)** (AI-native software collaboration: issue-first onboarding and AI review). **my-calendar** is what it looks like to land that "let AI agents take part in everyday software collaboration" idea onto a single Mac, using Apple Calendar as the surface that ties it together.

![demo](./assets/demo.gif)

---

## What it does (the AI workflow)

Three independent-but-composable workflows turn your local machine + Apple Calendar into an AI collaboration loop:

### 1. Automatic PR review on every push (`pr_watcher`)

- **Zero-latency trigger** — a global `git pre-push` hook fires a background worker within ~2–3s of any local `git push`.
- **Post-create trigger** — local tools can call `~/.config/my-calendar/git-hooks/pr-created <pr-url> [origin-cwd]` right after `gh pr create`; gstack `/ship` uses this to avoid missing PRs created after the push polling window.
- **Codex → Terminal bridge** — when an immediate PR trigger is launched from Codex Desktop, it hands the watcher to a Terminal-opened `.command` file so Calendar writes use Terminal's EventKit permission instead of Codex's.
- **Cross-org** — one `gh` GraphQL call sweeps every open PR you authored, across all organizations.
- **Default-branch only** — PRs whose base ≠ the repo's default branch are skipped (feature → feature never triggers).
- **Idempotent** — keyed on `(pr_url, head_sha)`; the same commit is reviewed once. A force-push that rewrites the SHA re-triggers.
- **`codex` does the review** and posts it as a PR comment; the watcher writes a calendar event into a dedicated **"PR 监控"** calendar with the comment link and a `codex resume <thread_id>` command so you can pick the session back up.
- **Cancel + restart on new commit** — if a new commit lands mid-review, the in-flight review is cancelled and restarted against the latest SHA; the watcher re-checks the head around final local writes and rolls back stale artifacts, so old reviews do not remain in calendar/state.
- **Conservative fallback** — a launchd poll every 10 min seeds static historical first-seen PRs, reviews post-install PR activity missed by local hooks, retries stale pending reviews, and catches later missed commits.

### 2. One-click AI fix from the calendar event (`MyCalFix.app` + `mycalfix://`)

- When a review verdict is **⚠️ (fix-then-merge)** or **❌ (not yet)**, the calendar event carries a `mycalfix://fix?...` URL.
- Click it → `MyCalFix.app` pops a per-click **Yolo / Interactive / Cancel** dialog → on confirm it opens Terminal, fetches the branch into an isolated `git worktree`, and starts an interactive `claude` session pre-loaded with a fix prompt.
- The fix happens in a throwaway worktree (your main checkout's WIP is untouched); `claude` pushes the fix back to the same PR branch.
- Hard guardrails in the prompt: abort if the diff is > 1000 lines, only touch what the review named, run the project's self-checks, no `--force` push. Yolo is an **explicit per-session choice**, never a silent default.

![one-click AI fix](./assets/auto-fix.gif)

### 3. AI co-author attribution convention

Every artifact that is "AI-generated, no human in the loop" carries a machine-detectable marker, so a downstream dashboard can cleanly separate AI vs. human activity:

| Artifact | Marker |
|---|---|
| codex PR review comment | comment header: a `> 🤖 由 Codex 自动生成` blockquote + `<!-- ai-coauthor: codex; agent: pr_watcher; mode: automated -->` HTML comment + hidden `<!-- pr-watcher-head-sha: ... -->` commit marker |
| `claude` fix commit (via MyCalFix) | `Co-Authored-By: Claude <noreply@anthropic.com>` trailer in the commit body |
| hand-typed human PR / comment | none |

The blockquote is the human-visible signal; the HTML comment is the stable grep key for scanners. Prompt templates hard-enforce both, and `scripts/test_pr_watcher.py` locks the canonical strings so an edit that mangles them turns the tests red.

---

## Architecture (PR side)

```
┌─────────────────┐         ┌──────────────────────┐         ┌──────────────┐
│  git push       │──fire──▶│  global pre-push     │──spawn─▶│ pr_local_    │
│  (any repo)     │         │  hook (~/.config/…)  │         │ trigger.sh   │
└─────────────────┘         └──────────────────────┘         └──────┬───────┘
                                                                    │ finds PR
┌──────────────┐  10min     ┌──────────────────┐                    ▼
│   launchd    │ ──retry───▶│  pr_watcher.py   │◀────── --force <pr-url>
└──────────────┘            │  (Python)        │
                            └────┬─────────────┘
                                 │ codex exec --json --dangerously-bypass…
                                 ▼
                            ┌──────────┐  posts comment  ┌──────────────┐
                            │  codex   │ ───────────────▶│  GitHub PR   │
                            └────┬─────┘                 └──────────────┘
                                 │ thread_id / comment URL / full body
                                 ▼
                            ┌──────────────────────────┐  EventKit   ┌──────────────┐
                            │  build calendar event    │ ──────────▶ │ "PR 监控"     │
                            │  (mycalfix:// fix URL)    │             │  calendar    │
                            └──────────────────────────┘             └──────────────┘
                                 │ click fix URL
                                 ▼
                            MyCalFix.app → Terminal → git worktree → claude (fix session)
```

Full schema and the Claude working contract live in [`AGENTS.md`](./AGENTS.md) (`CLAUDE.md` is a symlink to it).

---

## Install (~5 min)

### Prerequisites

- macOS (for EventKit)
- Python 3.10+
- [Claude Code CLI](https://claude.com/claude-code)
- For PR review: `gh` CLI (logged in, scopes include `repo`) and the [`codex` CLI](https://github.com/openai/codex)

### Steps

```bash
git clone https://github.com/realRoc/my-calendar.git
cd my-calendar
./scripts/setup.sh                               # creates .venv/ + installs deps

.venv/bin/python scripts/calendar_sync.py        # first run triggers the macOS calendar-access prompt → "Allow"

./scripts/install_launchd.sh                      # daily holiday scan + PR fallback poll

# enable PR review
./scripts/install_git_hook.sh                     # global pre-push hook
.venv/bin/python scripts/pr_watcher.py --seed-only  # record current open PRs, never comment on the first pass

# optional: install the lightweight /pr skill for Claude Code + Codex
bash scripts/install_pr_skill.sh

# enable one-click fix
bash scripts/install_app.sh                       # builds + installs MyCalFix.app, registers mycalfix:// scheme
```

After that, every local `git push` kicks off a background codex review (comment + calendar event) within a couple of seconds, without blocking the push. In Claude Code or Codex, say `/pr` when you want the light PR path: focused checks, push/create PR, then immediate my-calendar review handoff.

> If a repo already has its own `.git/hooks/pre-push` (CI checks etc.), rename it to `.git/hooks/pre-push.local` — the global hook execs it first, then triggers the watcher. To opt a repo out entirely: `git -C <repo> config core.hooksPath .git/hooks`.

---

## Debugging the AI workflow

```bash
# candidate PRs + which would trigger codex (no real run)
.venv/bin/python scripts/pr_watcher.py --dry-run

# force a run on a specific PR (bypasses the state check)
.venv/bin/python scripts/pr_watcher.py --force https://github.com/<owner>/<repo>/pull/<n>

# trigger the installed post-create hook used by local PR tools
~/.config/my-calendar/git-hooks/pr-created https://github.com/<owner>/<repo>/pull/<n> "$PWD"

# watch the push-trigger chain live
tail -f ~/.config/my-calendar/git-hooks/logs/trigger.log

# conservative launchd seed/retry channel
tail -f logs/pr-watcher.log

# how each mycalfix:// URL was parsed + launched
tail -f ~/Library/Logs/MyCalFix/launch_fix.log
```

Concurrency cap, prompt customization, cancel-restart internals, and the security trade-offs of yolo mode are documented in the **PR 监控** and **PR review 修复入口** sections of [`AGENTS.md`](./AGENTS.md).

---

# The original feature: holiday & gift reminders

The project started life as a local holiday reminder, and that half still runs alongside the AI workflow.

It scans the next 7 days of holidays (Chinese legal + Western common + your custom dates) and writes a reminder event into Apple Calendar, with the history of what you did for the same holiday in past years — and how the recipient reacted — attached to the event description, so you can decide what to do this year.

### Holiday-side features

- **Date rules** — fixed Gregorian (Valentine's), lunar (Spring Festival / Dragon Boat / Mid-Autumn / Qixi), and nth-weekday (Mother's / Father's / Thanksgiving).
- **Idempotent** — deterministic event key + `state.json` index; re-runs update, never duplicate.
- **Lazy catch-up** — missed a lead day but the holiday hasn't passed → fire once today with a ⚠️ at the top of the notes.
- **Missing-record tracking** — holiday passed but you logged nothing → written into `MISSING.md`; next time you open Claude it proactively asks you to fill it in.
- **Local-only** — all data is local markdown + frontmatter; offline-capable; commit it to your own git.
- **Doesn't pollute your main calendar** — writes to a dedicated **"节日提醒"** calendar you can show / hide / delete on its own.

### Entering data via Claude Code

Open a session in the project directory. Claude reads `CLAUDE.md` → `AGENTS.md` and writes the files for you per the skill docs:

| You say | Skill |
|---|---|
| "Add a holiday: grandma's birthday is July 12" | `add-holiday` |
| "Note that mom likes theater and is allergic to roses" | `add-person` |
| "Gave mom a ¥580 theater ticket today" | `record-history` (create) |
| "Mom said she loved the ticket" | `record-history` (add feedback) |

### Manual holiday commands

```bash
# what's coming in the next 14 days, will it trigger today
.venv/bin/python scripts/daily_check.py --dry-run --days 14

# force-write every holiday in the window (ignore lead_days)
.venv/bin/python scripts/daily_check.py --force --days 30

# query history
ls history/*/*__mom.md                    # everything done for mom
grep -l 'feedback: ""' history/*/*.md     # gifts with no feedback yet
```

### Pre-filled holidays

| ID | Name | Category | Date rule |
|---|---|---|---|
| `new-year` | 元旦 | cn-legal | Gregorian 1-1 |
| `spring-festival` | 春节 | cn-legal | lunar 1-1 |
| `qingming` | 清明节 | cn-legal | Gregorian ~4-5 |
| `labor-day` | 劳动节 | cn-legal | Gregorian 5-1 |
| `dragon-boat` | 端午节 | cn-legal | lunar 5-5 |
| `mid-autumn` | 中秋节 | cn-legal | lunar 8-15 |
| `national-day` | 国庆节 | cn-legal | Gregorian 10-1 |
| `valentines-day` | 情人节 | western | Gregorian 2-14 |
| `mothers-day` | 母亲节 | western | 2nd Sunday of May |
| `fathers-day` | 父亲节 | western | 3rd Sunday of June |
| `qixi` | 七夕 | western | lunar 7-7 |
| `thanksgiving` | 感恩节 | western | 4th Thursday of Nov |
| `christmas` | 圣诞节 | western | Gregorian 12-25 |

`custom/` is yours to fill (birthdays, anniversaries, private holidays).

---

## Uninstall

```bash
launchctl unload ~/Library/LaunchAgents/com.YOURNAME.calendar.daily.plist
rm ~/Library/LaunchAgents/com.YOURNAME.calendar.daily.plist
launchctl unload ~/Library/LaunchAgents/com.YOURNAME.calendar.pr-watcher.plist
rm ~/Library/LaunchAgents/com.YOURNAME.calendar.pr-watcher.plist
git config --global --unset core.hooksPath
rm -rf ~/.config/my-calendar
rm -rf ~/Applications/MyCalFix.app
```

The **"节日提醒"** and **"PR 监控"** calendars can be deleted by right-clicking them in Calendar.app (removes all their events too).

## License

MIT — see [LICENSE](./LICENSE). Use, modify, and ship it freely; just keep the copyright notice.
