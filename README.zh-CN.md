# my-calendar

> **一套面向 AI 编码 agent（Claude Code / codex）的本地协作工作流，借苹果日历当"通知总线"落地。**
> 推代码 → AI 自动 review PR → 结论落进日历 → 一键起本地 AI 修复会话。全本地、无账号、无 UI——数据都是本地文件。

*Read this in [English](./README.md).*

这套工作流的方法论来自作者的另一个开源项目 —— **[git-hired](https://realroc.github.io/git-hired/)**（AI-native 软件协作：issue-first 上手与 AI review）。**my-calendar** 是把那套"让 AI agent 深度参与日常软件协作"的思路，落到一台本机 + 苹果日历上的实现。

![demo](./assets/demo.gif)

---

## 它干什么（AI workflow）

三条相互独立、又能组合的工作流，把你的本机 + 苹果日历变成一个 AI 协作回路：

### 1. 每次 push 自动 review PR（`pr_watcher`）

- **触发零延迟** — 全局 `git pre-push` hook，本地 push 后约 2–3 秒内异步起后台 worker。
- **跨 org** — 一次 `gh` GraphQL 调用扫所有 organization 下你发起的 open PR。
- **只看默认分支** — base ≠ 仓库默认分支的 PR 自动剔除（feature → feature 不触发）。
- **幂等** — 以 `(pr_url, head_sha)` 为键，同一 commit 只评论一次；force-push 重写 SHA 后才会重审。
- **`codex` 做评审**，自动发到 PR comment；watcher 把一条事件写进独立的 **"PR 监控"** 日历，附评论链接 + `codex resume <thread_id>` 命令，需要二次交互直接接着上次会话。
- **进行中收到新 commit：cancel + restart** — review 进行中又来新 commit，立即取消旧 review、基于最新 SHA 重新开始，不堆队列。
- **兜底通道** — launchd 每 10 分钟轮询一次，抓本地 hook 漏掉的事件（网页创建的 PR、异机 push、bot 提交）。

### 2. 从日历事件一键 AI 修复（`MyCalFix.app` + `mycalfix://`）

- review 结论是 **⚠️（修正后可合并）** 或 **❌（暂不可合并）** 时，日历事件带一个 `mycalfix://fix?...` 链接。
- 点它 → `MyCalFix.app` 弹一个 per-click 的 **Yolo / Interactive / Cancel** 对话框 → 确认后打开 Terminal，把分支 fetch 进一个隔离的 `git worktree`，起一个已灌好 fix prompt 的交互式 `claude` 会话。
- 修复跑在一次性 worktree 里（主 checkout 的 WIP 完全不受打扰）；`claude` 改完 push 回原 PR 分支。
- prompt 里的硬约束：diff > 1000 行 abort、只改 review 点名处、跑项目自检、不 `--force` push。Yolo 是**显式的 per-session 选择**，永远不是静默默认。

### 3. AI 共著标记约定

所有"AI 生成、人类未介入"的产物都带可机器识别的标记，下游 dashboard 可以干净地区分 AI 与人类活跃度：

| 产物 | 标记 |
|---|---|
| codex PR review 评论 | 第一行：`> 🤖 由 Codex 自动生成` blockquote + `<!-- ai-coauthor: codex; agent: pr_watcher; mode: automated -->` HTML 注释 |
| `claude` 修复 commit（经 MyCalFix） | commit body 里的 `Co-Authored-By: Claude <noreply@anthropic.com>` trailer |
| 手敲的人类 PR / 评论 | 无标记 |

blockquote 是给人看的可见信号；HTML 注释是给扫描器的稳定 grep key。prompt 模板硬约束两者，`scripts/test_pr_watcher.py` 锁住 canonical 字符串——拼错或挪位置测试就红。

---

## 架构总览（PR 侧）

```
┌─────────────────┐         ┌──────────────────────┐         ┌──────────────┐
│  git push       │──fire──▶│  global pre-push     │──spawn─▶│ pr_local_    │
│  (任意 repo)    │         │  hook (~/.config/…)  │         │ trigger.sh   │
└─────────────────┘         └──────────────────────┘         └──────┬───────┘
                                                                    │ 找到 PR
┌──────────────┐  10min     ┌──────────────────┐                    ▼
│   launchd    │ ──fallback▶│  pr_watcher.py   │◀────── --force <pr-url>
└──────────────┘            │  (Python)        │
                            └────┬─────────────┘
                                 │ codex exec --json --dangerously-bypass…
                                 ▼
                            ┌──────────┐  发评论          ┌──────────────┐
                            │  codex   │ ───────────────▶│  GitHub PR   │
                            └────┬─────┘                 └──────────────┘
                                 │ thread_id / 评论 URL / 完整评论原文
                                 ▼
                            ┌──────────────────────────┐  EventKit   ┌──────────────┐
                            │  build calendar event    │ ──────────▶ │ "PR 监控" 日历│
                            │  (mycalfix:// 修复 URL)   │             └──────────────┘
                            └──────────────────────────┘
                                 │ 点修复 URL
                                 ▼
                            MyCalFix.app → Terminal → git worktree → claude（修复会话）
```

详细 schema 与 Claude 工作约定见 [`AGENTS.md`](./AGENTS.md)（`CLAUDE.md` 是它的 symlink）。

---

## 安装（约 5 分钟）

### 前置

- macOS（要用 EventKit）
- Python 3.10+
- [Claude Code CLI](https://claude.com/claude-code)
- PR review 额外需要：`gh` CLI（已 `gh auth login`，scopes 含 `repo`）+ [`codex` CLI](https://github.com/openai/codex)

### 步骤

```bash
git clone https://github.com/realRoc/my-calendar.git
cd my-calendar
./scripts/setup.sh                               # 创建 .venv/ 并装依赖

.venv/bin/python scripts/calendar_sync.py        # 首次跑弹出 macOS 日历授权对话框 → 选"好"

./scripts/install_launchd.sh                      # 节日 daily 扫描 + PR 兜底轮询

# 启用 PR review
./scripts/install_git_hook.sh                     # 全局 pre-push hook
.venv/bin/python scripts/pr_watcher.py --seed-only  # 记录现有 open PR，首轮不评论

# 启用一键修复
bash scripts/install_app.sh                       # 编译 + 安装 MyCalFix.app，注册 mycalfix:// scheme
```

之后每次本地 `git push`：hook 在几秒内异步起 codex review、发评论、写日历事件——不阻塞 push。

> 某个 repo 已有自己的 `.git/hooks/pre-push`（CI 校验等）：改名为 `.git/hooks/pre-push.local`，全局 hook 会先 exec 它再触发 watcher。某个 repo 完全不想被监控：`git -C <repo> config core.hooksPath .git/hooks`。

---

## AI workflow 调试

```bash
# 候选 PR + 哪些会触发 codex（不真跑）
.venv/bin/python scripts/pr_watcher.py --dry-run

# 对指定 PR 强制跑一次（绕过 state 检查）
.venv/bin/python scripts/pr_watcher.py --force https://github.com/<owner>/<repo>/pull/<n>

# 实时观察 push 触发链路
tail -f ~/.config/my-calendar/git-hooks/logs/trigger.log

# launchd 兜底通道
tail -f logs/pr-watcher.log

# 每个 mycalfix:// URL 被怎么解析 + 启动
tail -f ~/Library/Logs/MyCalFix/launch_fix.log
```

并发上限、prompt 自定义、cancel-restart 内部机制、yolo 模式的安全取舍见 [`AGENTS.md`](./AGENTS.md) 的 **PR 监控（pr_watcher）** 与 **PR review 修复入口** 两节。

---

# 最初的功能：节日提醒 & 送礼记录

项目最初是个本地节日提醒系统，这半边和 AI workflow 一起继续跑。

它扫描未来 7 天的节日（中国法定 + 西方常见 + 自定义日期），把"提醒事件"写入苹果日历，并把你历年在同一节日做过什么、对方反馈如何，附在事件描述里，方便你今年做决策。

### 节日侧特性

- **日期规则** — 固定公历（情人节）、农历（春节/端午/中秋/七夕）、第 N 个星期 X（母亲节/父亲节/感恩节）。
- **去重幂等** — 确定性事件 key + `state.json` 索引，重复运行只更新不新建。
- **懒补跑** — 错过 lead_day 但节日还没过 → 当天补一发，notes 顶端标 ⚠️。
- **缺失追溯** — 节日已过但你没记录 → 写入 `MISSING.md`，下次开 Claude 它会主动问你。
- **零云端、零账号** — 数据都是本地 markdown + frontmatter，离线可用，可放进自己的 git。
- **不污染主日历** — 写入专门的 **"节日提醒"** 日历，可单独显示/隐藏/删除。

### 通过 Claude Code 录入数据

进入项目目录开会话即可。Claude 会读 `CLAUDE.md` → `AGENTS.md`，按 skill 文档帮你写文件：

| 你说 | 调用的 skill |
|---|---|
| "加一个节日：奶奶生日是 7 月 12 号" | `add-holiday` |
| "记一下妈妈喜欢剧场，对玫瑰过敏" | `add-person` |
| "今天送了妈妈 580 块剧院票" | `record-history`（新建） |
| "妈妈说那票她很喜欢" | `record-history`（补反馈） |

### 手动节日命令

```bash
# 看接下来 14 天有什么节日，今天会不会触发
.venv/bin/python scripts/daily_check.py --dry-run --days 14

# 强制把窗口内所有节日都写进日历（忽略 lead_days）
.venv/bin/python scripts/daily_check.py --force --days 30

# 查历史
ls history/*/*__mom.md                    # 给妈妈做过的所有事
grep -l 'feedback: ""' history/*/*.md     # 还没收到反馈的所有送礼
```

### 已预填的节日

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

`custom/` 目录留给你自己加（家人生日、纪念日、私人节日）。

---

## 卸载

```bash
launchctl unload ~/Library/LaunchAgents/com.YOURNAME.calendar.daily.plist
rm ~/Library/LaunchAgents/com.YOURNAME.calendar.daily.plist
launchctl unload ~/Library/LaunchAgents/com.YOURNAME.calendar.pr-watcher.plist
rm ~/Library/LaunchAgents/com.YOURNAME.calendar.pr-watcher.plist
git config --global --unset core.hooksPath
rm -rf ~/.config/my-calendar
rm -rf ~/Applications/MyCalFix.app
```

苹果日历里的 **"节日提醒"** 和 **"PR 监控"** 两个日历可以在日历 app 里右键删除（连同所有事件）。

## License

MIT — 见 [LICENSE](./LICENSE)。随便用、随便改、随便商用，保留版权声明就行。
