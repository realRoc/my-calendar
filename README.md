# my-calendar

> A local-only macOS "second brain" calendar with two features:
>
> 1. **Holiday & gift reminders** — Chinese legal + Western common holidays +
>    your custom dates, written into Apple Calendar with the gift history of
>    past years attached so you can decide what to do this year.
> 2. **GitHub PR auto-review** — every `git push` you do triggers a `codex`
>    code review on the PR (cross-org), posts the review as a comment, and
>    drops a calendar event with the comment link + `codex resume <id>` so you
>    can pick up the session.
>
> No cloud, no account, no UI — all data lives in local markdown files. Drive
> it through [Claude Code](https://claude.com/claude-code) conversations or
> the included scripts.

> 一个完全本地的 macOS 日历"外脑"，干两件事：
>
> 1. **节日提醒 & 送礼记录** — 中国法定节日 + 西方常见节日 + 自定义日期写入苹果日历，
>    历年送礼记录 / 对方反馈一并附在事件描述里，方便你今年做决策。
> 2. **GitHub PR 自动 review** — 每次 `git push` 触发 `codex` 给该 PR 做代码评审，
>    自动发到 PR comment 里，并往苹果日历写一条事件：评论链接 + 可 resume 的 codex
>    session id。跨多个 organization 都能跑。
>
> 全本地、无账号、无 UI——数据都是本地 markdown 文件。可以让 Claude Code 帮你录入，
> 也可以手动调脚本。

---

## 特性

### 节日提醒

- **节日识别**：固定公历日期（情人节）、农历（春节/端午/中秋/七夕）、第 N 个星期 X（母亲节/父亲节/感恩节）三种规则
- **去重幂等**：每个事件有确定性 key + state.json 索引，重复运行只更新不新建
- **懒补跑**：错过 lead_day 但节日还没过 → 当天补一发，notes 顶端标 ⚠️
- **缺失追溯**：节日已过但你没记录 → 写入 MISSING.md，下次开 Claude 它会主动问你
- **零云端、零账号**：所有数据都是本地 markdown + frontmatter，离线可用，可放进自己的 git
- **不污染主日历**：写入专门的"节日提醒"日历，可单独显示/隐藏/删除

### PR 自动 review

- **触发零延迟**：全局 git pre-push hook，本地 push 后 2-3 秒内启动后台 review
- **跨 org**：通过 `gh` CLI 一次性扫所有 organization 下你发起的 open PR
- **只看默认分支**：自动剔除 base ≠ default branch 的 PR（feature → feature 的不触发）
- **幂等**：以 `(pr_url, head_sha)` 为键，同一 commit 只评论一次；force-push 重写 SHA 后才会重审
- **可 resume**：日历事件里附 `codex resume <thread_id>` 命令，需要二次交互直接接着上次的会话
- **兜底通道**：launchd 每 30 min 一次轮询，抓本地 hook 漏掉的事件（网页创建/异机 push/bot 提交）
- **隔离日历**：评论提醒写入独立的 **"PR 监控"** 日历，不和节日混在一起

## 架构总览

```
节日侧：
┌──────────────┐  daily 06:00   ┌──────────────────┐  EventKit  ┌─────────────┐
│   launchd    │ ──────────────▶│  daily_check.py  │ ─────────▶ │ Apple       │
└──────────────┘                │  (Python)        │            │ Calendar    │
                                │                  │ ◀───────── │ "节日提醒"   │
                                └────┬─────────────┘            └─────────────┘
                                     │
                                     ▼
                         ┌─────────────────────────────┐
                         │  holidays/  people/         │
                         │  history/   MISSING.md      │  ← all markdown
                         └─────────────────────────────┘

PR 侧：
┌─────────────────┐         ┌──────────────────────┐         ┌──────────────┐
│  git push       │──fire──▶│  global pre-push     │──spawn─▶│ pr_local_    │
│  (任意 repo)    │         │  hook (~/.config/…)  │         │ trigger.sh   │
└─────────────────┘         └──────────────────────┘         └──────┬───────┘
                                                                    │ 找到 PR
┌──────────────┐  30min     ┌──────────────────┐                    ▼
│   launchd    │ ──fallback▶│  pr_watcher.py   │◀────── --force <pr-url>
└──────────────┘            │  (Python)        │
                            └────┬─────────────┘
                                 │ codex exec --json --dangerously-bypass…
                                 ▼
                            ┌──────────┐  posts comment  ┌──────────────┐
                            │  codex   │ ───────────────▶│  GitHub PR   │
                            │  (yolo)  │                 └──────────────┘
                            └────┬─────┘
                                 │ thread_id / 评论 URL / 完整评论原文
                                 ▼
                            ┌──────────────────────────┐  EventKit
                            │  build calendar event    │ ─────────▶ "PR 监控" 日历
                            └──────────────────────────┘
```

详细 schema 与 Claude 工作约定见 [`AGENTS.md`](./AGENTS.md)（`CLAUDE.md` 是它的 symlink）。

---

## 安装（约 5 分钟）

### 0. 前置

- macOS（要用 EventKit）
- Python 3.10+
- Claude Code CLI（[claude.com/claude-code](https://claude.com/claude-code)）
- PR 监控功能额外需要：`gh` CLI（已 `gh auth login` 完成，scopes 至少含 `repo`）、`codex` CLI（[github.com/openai/codex](https://github.com/openai/codex)）

### 1. clone + 装依赖

```bash
git clone https://github.com/realRoc/my-calendar.git
cd my-calendar
./scripts/setup.sh
```

`setup.sh` 会创建 `.venv/` 并装 `pyobjc-framework-EventKit`、`lunardate`、`pyyaml`。

### 2. 首次跑触发日历授权

```bash
.venv/bin/python scripts/calendar_sync.py
```

第一次会弹 macOS 系统对话框问"是否允许 Terminal 访问日历"——选"好"。看到 `calendar OK: '节日提醒'` 即成功。

> 误点了"不允许"：去 **系统设置 → 隐私与安全性 → 日历**，手动勾选 Terminal，然后重跑。

### 3. 注册 launchd

```bash
./scripts/install_launchd.sh
```

会装两个 LaunchAgent：

- `com.<user>.calendar.daily` — 每天 06:00 节日扫描
- `com.<user>.calendar.pr-watcher` — 每 30 分钟一次 PR 兜底轮询

如果你只想要节日功能，可以编辑脚本的 `JOBS` 数组移除 `pr-watcher`。

### 4. 真跑一次，检查日历

```bash
.venv/bin/python scripts/daily_check.py
```

打开 macOS 日历 app，左侧应该多一个 **节日提醒** 日历。

### 5. （可选）启用 PR 监控

```bash
./scripts/install_git_hook.sh                              # 装全局 pre-push hook
.venv/bin/python scripts/pr_watcher.py --seed-only         # 把现有 open PR 写入 state，首轮不评论
```

之后每次本地 `git push`：hook 在 2-3 秒内异步起 codex review、发评论、写日历事件——不阻塞 push。

> 如果某个 repo 已经有 `.git/hooks/pre-push`（CI 校验等），把它改名为 `.git/hooks/pre-push.local`，全局 hook 会先 exec 它再触发 watcher。某个 repo 不想被监控：`git -C <repo> config core.hooksPath .git/hooks` 关掉。

---

## 日常使用

### 通过 Claude Code 录入数据

进入项目目录开会话即可。Claude 会读 `CLAUDE.md` → `AGENTS.md` → 按 skill 文档帮你写文件：

| 你说 | Claude 调用的 skill |
|---|---|
| "加一个节日：奶奶生日是 7 月 12 号" | `add-holiday` |
| "记一下妈妈喜欢剧场，对玫瑰过敏" | `add-person` |
| "今天送了妈妈 580 块剧院票" | `record-history`（新建） |
| "妈妈说那票她很喜欢" | `record-history`（补反馈） |

### 手动调试

```bash
# 看接下来 14 天有什么节日，今天会不会触发
.venv/bin/python scripts/daily_check.py --dry-run --days 14

# 强制把窗口内所有节日都写进日历（忽略 lead_days）
.venv/bin/python scripts/daily_check.py --force --days 30

# 假装今天是别的日子（测试用）
.venv/bin/python scripts/daily_check.py --dry-run --today 2026-09-25
```

### 查历史

```bash
ls history/*/*__mom.md                    # 给妈妈做过的所有事
ls history/*/*__mothers-day__*.md         # 历年母亲节做过的事
grep -l 'feedback: ""' history/*/*.md     # 还没收到反馈的所有送礼
```

### PR 监控调试

```bash
# 看候选 PR + 哪些会触发 codex（不真跑）
.venv/bin/python scripts/pr_watcher.py --dry-run

# 对指定 PR 强制跑一次（绕过 state 检查）
.venv/bin/python scripts/pr_watcher.py --force https://github.com/<owner>/<repo>/pull/<n>

# 实时观察 hook 触发情况
tail -f ~/.config/my-calendar/git-hooks/logs/trigger.log

# launchd 兜底通道日志
tail -f logs/pr-watcher.log
```

更详细的设计取舍、错误恢复、prompt 自定义见 [`AGENTS.md`](./AGENTS.md) 的 **PR 监控（pr_watcher）** 一节。

---

## 已预填的节日

| ID | 中文名 | 类别 | 日期规则 |
|---|---|---|---|
| `new-year` | 元旦 | cn-legal | 公历 1-1 |
| `spring-festival` | 春节 | cn-legal | 农历 1-1 |
| `qingming` | 清明节 | cn-legal | 公历 4-5（节气近似） |
| `labor-day` | 劳动节 | cn-legal | 公历 5-1 |
| `dragon-boat` | 端午节 | cn-legal | 农历 5-5 |
| `mid-autumn` | 中秋节 | cn-legal | 农历 8-15 |
| `national-day` | 国庆节 | cn-legal | 公历 10-1 |
| `valentines-day` | 情人节 | western | 公历 2-14 |
| `mothers-day` | 母亲节 | western | 5月第二个周日 |
| `fathers-day` | 父亲节 | western | 6月第三个周日 |
| `qixi` | 七夕 | western | 农历 7-7 |
| `thanksgiving` | 感恩节 | western | 11月第四个周四 |
| `christmas` | 圣诞节 | western | 公历 12-25 |

custom/ 目录留给你自己加（家人生日、纪念日、私人节日）。

---

## 卸载

```bash
# 节日 launchd
launchctl unload ~/Library/LaunchAgents/com.YOURNAME.calendar.daily.plist
rm ~/Library/LaunchAgents/com.YOURNAME.calendar.daily.plist

# PR 监控 launchd
launchctl unload ~/Library/LaunchAgents/com.YOURNAME.calendar.pr-watcher.plist
rm ~/Library/LaunchAgents/com.YOURNAME.calendar.pr-watcher.plist

# 全局 git hook
git config --global --unset core.hooksPath
rm -rf ~/.config/my-calendar
```

苹果日历里的 **"节日提醒"** 和 **"PR 监控"** 两个日历可以在日历 app 里手动右键删除（连同所有事件）。

---

## License

MIT — 见 [LICENSE](./LICENSE)。

随便用、随便改、随便商用，保留版权声明就行。
