# my-calendar

一套围绕 **Claude Code / codex 等 AI agent 的本地软件协作工作流**，借苹果日历当"通知总线"落地。核心是三条 AI workflow：

1. **PR 自动 review（pr_watcher）** — 每次 `git push` 触发 `codex` 给该 PR 做跨 org 代码评审，自动发到 PR comment，并往日历写一条带 `codex resume <id>` 的事件。
2. **一键 AI 修复（MyCalFix.app + `mycalfix://` URL scheme）** — review 给出 ⚠️/❌ 后，从日历事件直接一键起本地 `claude` 修复会话，自动 checkout / 建 worktree / 灌入 fix prompt。
3. **AI 共著标记约定** — 所有"AI 生成、人类未介入"的 PR 评论 / commit 都带可机器识别的共著标记，便于下游 dashboard 区分 AI 与人类活跃度。

这套工作流的方法论来自作者的另一个开源项目 **[git-hired](https://realroc.github.io/git-hired/)**（AI-native 软件协作 / issue-first 上手与 AI review）——my-calendar 是把那套"让 AI agent 深度参与日常软件协作"的思路落到一台本机 + 苹果日历上的实现。

第二条功能线是项目最初的形态：**本地节日提醒系统**。每天扫描未来 7 天的节日（中国法定 + 西方常见 + 自定义），把"提醒事件"写入苹果日历，事件描述里附上你过去在同一节日做过什么、对方反馈如何，方便你做今年的决策。

所有数据都是本地文件——没有数据库、没有云端、没有账号。

---

## 给 Claude 的工作约定

### 0. 会话开始：检查 MISSING.md（**优先级最高**）

每次新会话开始时，**首先**检查项目根目录是否有 `MISSING.md` 且内容非空。如果有：

1. 主动告诉用户："你有 N 条节日记录待补充：[列出文件中的条目]。要不要现在补一下？"
2. 用户回应后，按 `record-history` skill 流程逐条落盘
3. **关键**：用户说"那天什么都没做 / 不想记"也要写一个最简 history 文件——`action` 填 `未庆祝`，`feedback` 留空。这样下次 daily_check 跑时不会再列出
4. 落盘后无需手动改 MISSING.md，下次 daily_check (每天 06:00) 自动重建
5. 同一会话中，如果你刚为某条写过 history 文件，那条就不要再追问，即使 MISSING.md 还没刷新

如果 MISSING.md 不存在或为空，跳过本步骤，正常处理用户请求。

### 1. skill 触发

当用户说出下面这类话时，**主动调用对应 skill**（不要直接手写文件）：

| 用户说 | 调用 skill |
|---|---|
| "新增/加一个节日"、"X 是个节日，记一下" | `add-holiday` |
| "新增一个家人/朋友档案"、"记一下 X 的偏好" | `add-person` |
| "记一下今天送了 X 给 Y"、"X 说那个礼物 Y/不喜欢"、"补一下反馈" | `record-history` |

skill 文件位置：`.claude/skills/<skill-name>/SKILL.md`

---

## 目录结构

```
my-calendar/
├── AGENTS.md                       # 本文件
├── CLAUDE.md                       # symlink → AGENTS.md
├── README.md                       # 用户向：安装/授权/常用操作
├── MISSING.md                      # 待补记录（自动生成；存在即提示 Claude 主动询问）
├── holidays/                       # 节日定义
│   ├── cn-legal/                   # 中国法定节日
│   ├── western/                    # 西方常见节日
│   └── custom/                     # 用户自定义（生日/纪念日等）
├── people/                         # 人物档案
├── history/                        # 过往行为与反馈
│   └── YYYY/                       # 按年分目录
├── scripts/
│   ├── holiday_resolver.py         # 解析阴历/可变日期、列出未来 N 天节日
│   ├── calendar_sync.py            # EventKit 写入苹果日历（支持多日历名）
│   ├── daily_check.py              # 节日入口：解析+查历史+写日历（launchd 每天 06:00）
│   ├── state.json                  # 节日已创建事件 ID 索引
│   ├── pr_watcher.py               # PR 监控入口：扫 open PR → codex review → 写日历（launchd 每 10 min）
│   ├── pr_prompt.md                # codex review 用的 prompt 模板（含 {pr_link} 占位）
│   ├── pr_state.json               # 每个 PR 的 head_sha / thread_id / comment_url / origin_cwd
│   ├── pr_calendar_state.json      # "PR 监控" 日历的事件 ID 索引
│   ├── pr_logs/                    # codex JSONL + 最终回复，每次运行一份
│   ├── launch_fix.sh               # mycalfix:// URL handler：解析参数 → Terminal → claude 修复会话
│   ├── fix_prompt.md               # 修复 prompt 模板（含 {pr_url}/{comment_url}/{branch} 占位）
│   └── install_app.sh              # 编译 + 安装 MyCalFix.app 到 ~/Applications + 注册 URL scheme
├── app/
│   └── MyCalFix/main.applescript   # MyCalFix.app 的 AppleScript 源（自包含；用 path to me 找 bundled launcher）
├── logs/                           # launchd stdout/stderr（launch_fix.log 在 ~/Library/Logs/MyCalFix/）
├── com.YOURNAME.calendar.daily.plist        # 节日任务（install_launchd.sh 渲染）
├── com.YOURNAME.calendar.pr-watcher.plist   # PR 监控任务（同上）
└── .venv/                          # Python 依赖（pyobjc + lunardate）
```

---

## 文件命名规范

### holidays/

`holidays/<category>/<holiday-id>.md`

- `category`：`cn-legal` / `western` / `custom`
- `holiday-id`：kebab-case 英文 ID（如 `mothers-day`、`spring-festival`、`mom-birthday`）
- 示例：`holidays/western/mothers-day.md`

### people/

`people/<person-id>.md`

- `person-id`：kebab-case，简短可读（如 `mom`、`dad`、`zhang-san`、`li-si`）
- 示例：`people/mom.md`

### history/

`history/<YYYY>/<YYYY-MM-DD>__<holiday-id>__<person-id>.md`

- 双下划线分隔三段
- 日期是事件发生当天（不是录入当天）
- 一个文件 = 一个人 + 一个节日 + 一年
- 示例：`history/2026/2026-05-10__mothers-day__mom.md`

---

## YAML frontmatter Schema

### holiday 文件

```yaml
---
id: mothers-day                    # 必须，与文件名一致
name_cn: 母亲节                    # 必须
name_en: Mother's Day              # 可选
category: western                  # cn-legal | western | custom
date_rule:                         # 必须，三种之一：
  type: fixed                      #   固定公历日期
  month: 12
  day: 25
  # ── 或 ──
  type: lunar                      #   农历日期
  month: 8
  day: 15
  # ── 或 ──
  type: nth_weekday                #   某月第 n 个星期 X
  month: 5
  weekday: 0                       #   0=周一 ... 6=周日（ISO）
  n: 2                             #   第几个；负数表示倒数（-1=最后一个）
default_people: [mom]              # 可选，提醒时会优先关联这些人
lead_days: [7, 1]                  # 可选，提前几天创建提醒事件（默认 [7, 1]）
notes: |                           # 可选，自由备注（送礼参考等）
  妈妈喜欢剧场、古典乐；对玫瑰过敏。
---
```

### person 文件

```yaml
---
id: mom                            # 必须，与文件名一致
name: 妈妈                         # 必须，称呼
relation: mother                   # 可选，关系
birthday:                          # 可选
  type: solar                      # solar | lunar
  year: 1969                       # 可选（出生年，用于算年龄）
  month: 6
  day: 15
preferences: |                     # 可选，喜好
  剧场、古典乐、散文集
allergies: |                       # 可选
  玫瑰、花粉
sizes: |                           # 可选，尺码/型号
  衣服 M、鞋 38
notes: |                           # 可选，自由备注
  ...
---
```

### history 文件

```yaml
---
holiday: mothers-day               # 必须，holiday id
date: 2026-05-10                   # 必须，事件当天 ISO 日期
person: mom                        # 必须，person id
action: 远程订上海大剧院《剧名》门票  # 必须，做了什么
cost: 580                          # 可选，花费（数字，元）
channel: 大麦                      # 可选，购买/送达渠道
feedback:                          # 反馈（可后补）—— 留空字符串表示尚未收到
feedback_recorded_at:              # 反馈录入日期（ISO）
rating:                            # 可选，1-5，5=很满意，1=反响差
                                   #   仅在 feedback 录入时一起填
tags: []                           # 可选，自由打标签（如 ["剧院", "远程"]）
---

正文部分自由发挥：背景、决策过程、对方原话引用、未来注意事项等。
```

**重要**：`feedback` 字段在最初创建时**留空字符串**，表示"已送出但未收到反馈"。后续 `record-history` 在补反馈时会专门搜索这种状态的记录。

---

## 节日 ID / 人物 ID 命名速查

### 已预填的 holiday id

| ID | 中文名 | category |
|---|---|---|
| `new-year` | 元旦 | cn-legal |
| `spring-festival` | 春节 | cn-legal |
| `qingming` | 清明节 | cn-legal |
| `labor-day` | 劳动节 | cn-legal |
| `dragon-boat` | 端午节 | cn-legal |
| `mid-autumn` | 中秋节 | cn-legal |
| `national-day` | 国庆节 | cn-legal |
| `valentines-day` | 情人节 | western |
| `mothers-day` | 母亲节 | western |
| `fathers-day` | 父亲节 | western |
| `qixi` | 七夕 | western (中式情人节) |
| `thanksgiving` | 感恩节 | western |
| `christmas` | 圣诞节 | western |

新加节日时，复用上面的 id 即可；新 id 用 kebab-case。

---

## 脚本入口

```bash
# 看未来 7 天哪些节日命中（不写日历）
.venv/bin/python scripts/daily_check.py --dry-run

# 真跑：扫节日 + 写苹果日历
.venv/bin/python scripts/daily_check.py

# 自定义天数
.venv/bin/python scripts/daily_check.py --days 14

# 查某个节日某人的历史（grep 友好的命名）
ls history/*/*__mothers-day__mom.md
```

---

## 提醒事件长什么样

写入名为 **"节日提醒"** 的专用日历（不污染主日历）。每个节日按 `lead_days` 提前创建全天事件，标题：

> `🎁 母亲节倒计时 7 天 · 5月10日`

事件描述包含：
- 节日 id 与日期
- 关联人物档案要点（偏好/过敏/尺码）
- 历史送礼记录与反馈（按年倒序）
- 建议（基于历史与备注的简单文本）

事件 UID 是确定性的（`my-calendar:<holiday>:<year>:lead<N>`），重复运行不会创建副本。

---

## 调试与排错

- 看 launchd 日志：`tail -f logs/daily.log`
- 强制重建某事件：删 `scripts/state.json` 里对应条目，再跑 daily_check
- 临时禁用某节日：在它的 frontmatter 加 `disabled: true`
- 首次运行会弹"日历访问"权限对话框；如果误点拒绝，去**系统设置 → 隐私与安全性 → 日历**手动勾选 Terminal/Python

---

## PR 监控（pr_watcher）

第二条独立功能：自动 review 自己跨 org 提交的 PR。

### 触发模型（双通道）

**主通道 — 全局 git pre-push hook（即时）**

- `git config --global core.hooksPath ~/.config/my-calendar/git-hooks`
- 每次本地 `git push` 到 GitHub remote 时，hook 异步 fork 一个后台进程
- 后台进程 8 次 × 7.5s ≈ 60s 内轮询 `gh pr list --head <branch>` 找 PR
- 找到 PR + 验证 `base == default` → `pr_watcher.py --force <pr-url>`
- hook 本身立刻返回 0，不阻塞 push

**兜底通道 — launchd 10 min 轮询**

- `com.<user>.calendar.pr-watcher`，`StartInterval=600`
- 抓本地 hook 漏掉的事件：网页创建的 PR、异机 push、bot 自动 commit、push 时网络抖动导致 hook 失败等
- 休眠不唤醒 Mac
- 每次 tick 单次最长 25 分钟，超时杀掉 codex

### 已知不被 hook 抓到的场景

| 触发源 | hook 抓到？ | launchd 兜底？ |
|---|---|---|
| 本机 `git push` | ✅ 立即 | — |
| 本机 `gh pr create`（无新 push） | ⚠️ 只有同时 push 才行 | ✅ ≤10min |
| GitHub 网页 UI 创建/编辑 PR | ❌ | ✅ ≤10min |
| 其他机器 push | ❌ | ✅ ≤10min |
| PR bot 自动 commit | ❌ | ✅ ≤10min |

### 单次 tick 流程

1. `gh api graphql` 一次拉所有 open PR（跨 org，author=@me）
2. 过滤：`baseRefName == repository.defaultBranchRef.name`
3. 与 `scripts/pr_state.json` 比对：
   - 首次见到 → seed 落 head_sha，**不评论**
   - head_sha 未变 → 跳过
   - head_sha 变了 → 触发 codex
4. codex 调用：`codex exec --json --dangerously-bypass-approvals-and-sandbox -s danger-full-access --skip-git-repo-check -C /tmp/codex-pr-runs/<uuid> "<prompt>"`，用 `start_new_session=True` 起独立进程组以便 cancel 时能 `killpg` 整片
5. 从 JSONL 第一行抓 `thread_id`（用于 `codex resume`）
6. codex 自己用 `gh pr comment` 发评论；watcher 用 `gh api ...issues/<n>/comments` 兜底取 URL
7. EventKit 写到独立日历 **"PR 监控"**，UID = `my-calendar:pr-comment:<pr_url>:<sha>`

### 进行中收到新 commit：cancel + restart（issue #26）

同一 PR 在 codex review 进行中又收到新 commit（典型场景：本机连续 `git push`），不再排队，而是 **立即把进行中的 review 取消，并基于最新 sha 重新开始**：

1. 新的 `pr_watcher --force <url>` 抢 per-PR flock 失败（旧 leader 还在跑）
2. 立刻写 `locks/<safe_id>.cancel` marker，并发右上角通知 **🛑 PR review 已取消**
3. 旧 leader 的后台 watcher 线程每 500ms 轮询 marker，发现就 `killpg` 掉 codex 整个进程组（codex + 子 gh / git / node helpers）
4. 旧 leader 的 `process_pr` 走 cancel 短路：**不写日历事件、不写 `.meta.json` sidecar、不更新 `pr_state.json[<pr_url>].last_commented_sha`**，只保留 `.jsonl`（末尾追加 `_killed_by_watcher reason=cancelled_new_commit`）方便事后排查
5. 旧 leader 释放 flock；新 --force 拿到锁，发右上角通知 **🔁 PR review 重启** ，对最新的 head_sha 重新跑一次完整 review

**Persist 锁的原子边界（PR #27 high finding 修复）**：codex 退出后、`process_pr` 写日历 / `.meta` / state 之前还有一段窗口。早先的"在 upsert_events 之前 synchronously 再 check 一次 marker"是 check-then-act：marker writer 可以在 check 通过 *之后* 但 `upsert_events` *之前/之中* 出现，旧 review 还是会落盘。
现在做法：每个 PR 多一把 `locks/<safe_id>.persist.lock`，
- `signal_cancel_and_wait_for_lock` 在 `touch()` cancel marker 时持这把锁；
- `process_pr` 在"最终 check marker + `upsert_events` + 写 `.meta` + 推进内存 state"这一段持同一把锁。
两者互斥，于是 marker write 与 leader 的不可逆写入完全 totally ordered：要么 marker 在 leader 进入临界区之前落地（leader 看到 marker，短路，不写日历），要么 marker 在 leader 释放 persist 锁之后才落地（leader 这次 review 完整落盘，下一轮 --force 用新 marker 再触发一次 fresh review）。marker 不再可能"夹在 check 和 write 中间"。

要点：
- 同一 PR 串行（cancel + restart 保持这个语义）；不同 PR 之间继续并行
- N 次快速 push 不会堆 N 个 review；最终只会留下 1 个 review，对应最后一个 sha
- launchd 兜底 tick 路径遇到 in-flight review 时仍然 **skip**，不参与 cancel（设计取舍：兜底每 10 分钟跑一次，没必要打断当前 review；下一轮自然 re-check）
- 通知用同一 `group="pr-watcher:<pr_url>"`，terminal-notifier 会把同组旧 banner 替换成最新一条，多次 cancel/restart 不会刷屏
- **不要**再加"写 cancel marker"的新代码路径而绕过 `signal_cancel_and_wait_for_lock`：persist 锁约定要求 marker writer 必须在 persist 锁里 `touch()`，绕过会让 PR #27 修复失效

### 关键文件

| 路径 | 作用 |
|---|---|
| `scripts/pr_watcher.py` | 入口 |
| `scripts/pr_prompt.md` | codex prompt 模板（含 `{pr_link}` 占位） |
| `scripts/pr_local_trigger.sh` | hook 后台 worker：轮询找 PR → 调 pr_watcher --force |
| `scripts/install_git_hook.sh` | 一次性安装：设 global core.hooksPath |
| `scripts/install_launchd.sh` | 安装两个 LaunchAgent（daily + pr-watcher 兜底） |
| `~/.config/my-calendar/git-hooks/pre-push` | 全局 hook 本体（不在 repo 里，由 install_git_hook.sh 写入） |
| `~/.config/my-calendar/git-hooks/logs/trigger.log` | 每次 push 触发的轨迹日志 |
| `scripts/pr_state.json` | 每个 PR 的 last_commented_sha / thread_id / comment_url |
| `scripts/pr_calendar_state.json` | "PR 监控" 日历的 event_id 索引（与节日 state.json 隔离） |
| `scripts/pr_logs/<ts>__<owner>_<repo>_<n>.jsonl` | codex 每次完整 JSONL 输出 |
| `scripts/pr_logs/<ts>__<owner>_<repo>_<n>.last.txt` | codex 最终消息（写日历正文用） |
| `logs/pr-watcher.log` / `.err` | launchd 标准输出/错误 |

### 手动入口

```bash
# 一次性安装（已装过则幂等）
bash scripts/install_git_hook.sh         # 全局 pre-push hook
bash scripts/install_launchd.sh          # 节日 daily + PR 兜底 launchd

# 看候选 PR（不调 codex、不写日历）
.venv/bin/python scripts/pr_watcher.py --dry-run

# 首轮 seed：把现有 open PR 的 head_sha 全部记下，永远不评论
.venv/bin/python scripts/pr_watcher.py --seed-only

# 真跑：扫 → 触发 codex → 写日历
.venv/bin/python scripts/pr_watcher.py

# 对指定 PR 强制跑一次（绕过 state 检查；base 校验在 trigger 脚本里做了）
.venv/bin/python scripts/pr_watcher.py --force https://github.com/<owner>/<repo>/pull/<n>

# 忽略夜间节流，立即执行
.venv/bin/python scripts/pr_watcher.py --ignore-throttle
```

### per-repo opt-out

某个 repo 不想被 hook 触发：

```bash
git -C <repo-path> config core.hooksPath .git/hooks
```

某个 repo 已有自己的 pre-push（CI 校验等）想保留：把它改名为 `.git/hooks/pre-push.local`，全局 hook 会自动 exec 它，失败也会让 push 失败（保持原语义）。

### 配置

`~/.config/my-calendar/config.json` 可选，目前支持：

| key | 默认 | 说明 |
|---|---|---|
| `codex_concurrency_cap` | `10` | 全机器同时跑 codex 的最大数量。pr_watcher 用 `locks/codex-slot-{1..N}.lock` 实现 N 个 slot 的信号量；想给小机器/紧预算降并发就把这个数调小（如 `4`）。**调大也不会被拦**，但 codex 同时执行数、CPU/网络和 LLM 调用成本会随之线性上升——自行评估机器和预算能承受。**严格 JSON integer** 校验：`2.5` / `"4"` / `true` / 负数 / 0 都会落回默认 10 + 一行 stderr 警告，不会崩 |

> ⚠️ 已移除：`mycalfix_interactive_claude`。MyCalFix 现在用 osascript 对话框 per-click 选 mode，不再读 config 文件。写在 config.json 里也会被忽略。想跳过对话框（脚本化用途 / sticky preference）请 `export MYCALFIX_MODE=yolo|interactive|cancel` 给 launcher 进程。

示例 `config.json`：

```json
{
  "codex_concurrency_cap": 4
}
```

### 调试

- `tail -f logs/pr-watcher.log` 实时看 launchd 兜底
- `tail -f ~/.config/my-calendar/git-hooks/logs/trigger.log` 看每次 push 的触发链路
- 验证 hook 装好：`git config --global --get core.hooksPath` 应该返回 `~/.config/my-calendar/git-hooks`
- 某个 PR 想要 re-review：删 `pr_state.json` 里那条记录，下次 tick 会重新 seed → 下下次有 commit 才会真跑（如果想直接跑，用 `--force`）
- codex 单次卡死：进程会被 25min 超时杀掉，state 不会更新，下轮会重试
- 想 resume 某次 codex session：日历事件描述里有 `codex resume <thread_id>` 命令，直接复制执行
- 模拟 push 测试 hook：`echo "refs/heads/<branch> sha refs/heads/<branch> sha" | bash ~/.config/my-calendar/git-hooks/pre-push origin <remote-url>`

### 设计取舍

- 用 `(pr_url, head_sha)` 作为幂等键。force-push 改了 SHA 才会重新触发评论——这就是"同一 commit 只评论一次"的语义
- 日历事件用独立日历 "PR 监控"，避免和节日提醒混在一起
- 不过滤 draft PR（用户当前选择）。如果以后想跳过 draft，在 `fetch_open_prs` 后加 `if pr.is_draft: continue`
- 不主动跑历史 PR：首轮所有 open PR 进 seed，不评论；只有后续 commit 才触发
- codex 用 `-s danger-full-access` + `--dangerously-bypass-approvals-and-sandbox`，cwd 隔离在 `/tmp/codex-pr-runs/<uuid>` 但 sandbox 实际是 full access。prompt 模板里明确写了"只读 diff、只发评论、不改文件、不 push"——如果 PR 描述/diff 里有 prompt injection 试图让 codex 干别的，理论上 codex 可能被诱导。这是 yolo 模式的固有 trade-off

---

## AI 共著标记约定（issue #18）

本仓库所有"AI 自动生成、人类未介入"的产物在 GitHub 上**必须**带可机器识别的共著标记。GitHub 没有 first-class 的"共著评论"概念（commit 里有 `Co-Authored-By:` trailer，但评论里没有），所以靠**约定的 footer / 头部标记**+ prompt 模板硬约束实现。

### 三个落点

| 产物 | 标记位置 | 规范文本 |
|---|---|---|
| codex 通过 pr_watcher 发的 PR review 评论 | 评论 body **第一行** | <pre>> 🤖 由 Codex 自动生成<br>&lt;!-- ai-coauthor: codex; agent: pr_watcher; mode: automated --&gt;</pre> |
| claude 通过 MyCalFix 起的修复 commit | commit message body | `Co-Authored-By: Claude <noreply@anthropic.com>`（Claude Code 默认自动加，fix_prompt.md 强制保留） |
| 手敲的、纯人类 PR / 评论 | 无标记 | — |

### 为什么 codex 评论用 blockquote + HTML 注释两层

- **blockquote**（可见）让 GitHub 上看到评论的人立刻知道这条不是手敲的——避免外人误以为我亲自写了 30 个文件的 review
- **HTML 注释**（GitHub 渲染时不显示）给未来的扫描器一个**稳定的 grep key**：`<!-- ai-coauthor: ...`。下游 dashboard（issue #17）可以靠这一行做精确 bucket，不用解析中文 emoji 文本
- 两个都丢掉的话，统计就会把 AI 评论错算成人类活跃度

### 为什么 MyCalFix 修复 commit 用 `Co-Authored-By:`

- GitHub 原生识别这个 trailer：commit 页面会显示两个头像
- Claude Code 默认会自动加；fix_prompt.md 第四步显式要求保留，防 hook / 编辑器吃掉
- 与 pr_prompt.md 的 blockquote 标记互为镜像——一个标记 PR 评论，一个标记 PR commits，#17 dashboard 可以从两个数据源交叉验证

### prompt 模板的硬约束

- `scripts/pr_prompt.md` 第 4 步要求 codex 在评论 body **第一行**就 emit 这两行（原样照抄，包括 HTML 注释），下面才允许空一行 + 正文
- `scripts/fix_prompt.md` 第 4 步要求 commit message body 同时包含 `Reviewed-Comment:` 和 `Co-Authored-By: Claude` 两条 trailer，并解释这是与 pr_prompt.md 镜像的同一套约定

### 测试守门

`scripts/test_pr_watcher.py::AICoAuthorMarkerContractTests` 锁住两个 prompt 文件的 canonical 字符串：blockquote、HTML metadata、`Co-Authored-By:` trailer、以及 pr_prompt.md 里"marker 必须先于 section 规则出现"的结构断言。任何编辑 prompt 时把这些字符串拼错或挪到末尾，测试都会红。

### 老评论怎么办

不动。向前生效。历史评论保持原样，#17 dashboard 的人类活跃度统计应该从启用日（PR #23 land 起）开始算才干净。

---

## PR review 修复入口（MyCalFix.app + mycalfix:// URL scheme）

第三条独立功能：codex 写了 review 之后，从日历事件直接一键起本地 `claude` 修复会话，不用手 cd / checkout / 拷链接。

### 触发链路

```
PR 监控日历事件（verdict 是 ⚠️ 或 ❌）
  ├─ EKEvent.url = mycalfix://fix?repo=...&branch=...&comment=...&pr=...&origin_cwd=...
  └─ 描述里同步贴一段 paste-ready bash 命令（无 .app 时降级）
       ↓ 点链接
  MyCalFix.app（on open location）
       ↓ 调
  scripts/launch_fix.sh '<url>'
       ↓
  - python urllib.parse 解析 URL，校验 scheme=mycalfix / action=fix
  - 交叉校验 pr URL ∈ 同一 owner/repo（防止 repo=safe/x + pr=evil/y）
  - launcher（.app 进程）**不**对 origin_cwd 做任何 git 校验：跑在 .app TCC
    沙箱里，`git -C ~/Desktop/<repo>` 大概率 EPERM，校验会把所有路径打去 picker
    - URL 给了 origin_cwd → 透传给 Terminal-side 脚本
    - URL 没给 → osascript 文件夹选择器（picker），用户取消则 abort
  - **真正的 origin 校验**移到 Terminal-side `.command` 脚本里（Terminal.app 有自己
    的 TCC 权限，不受 .app 沙箱限制）：跑 `git -C <origin_cwd> remote get-url origin`，
    归一化（剥 `.git` + 尾部斜杠 + `^.*github\.com[:/]` 前缀）后跟 URL 的 `repo`
    比对，不一致就 `mycalfix_abort`，**在 fetch / worktree-add 之前**就停下来，
    避免修复跑到错误仓库甚至 push 到错误远端
  - `.command` 用 `set -euo pipefail` + 给 `git fetch` / `git worktree add` / `cd`
    显式 `|| mycalfix_abort`，任一步失败立刻停,不会 fall through 到 `claude`
  - 渲染 scripts/fix_prompt.md → 替换 {pr_url}/{comment_url}/{branch}/{worktree_dir}/{origin_cwd}/{local_branch}
  - 把 Terminal 命令写到 `/tmp/mycalfix.XXXXXX.command`，用 `open -a Terminal <file>` 打开
    （**不**用 `osascript do script Terminal`：那走 AppleEvents/Automation TCC，未签名 .app
    经常被静默拒绝 -1743 errAEEventNotPermitted，连授权对话框都不弹；.command 文件是
    LaunchServices document-open，没有 TCC 门槛）
    .command 文件首行 `rm -f $0` 自删；末尾 `exec bash -l` 保持窗口
    mkdir -p ~/.cache/my-calendar/worktrees/
    → git -C <origin_cwd> fetch origin <branch>
    → git -C <origin_cwd> worktree add -b mycalfix/<branch>-<ts>
        ~/.cache/my-calendar/worktrees/<owner>__<repo>__<branch>__<ts>  origin/<branch>
    → cd <worktree_dir> → claude '<rendered prompt>'
  - 用临时 worktree（不在 origin_cwd 上 git switch），主 checkout 的 WIP 完全不受打扰；
    claude 在 worktree 里改完用 `git push origin HEAD:<branch>` 推回原 PR 分支更新原 PR；
    fix_prompt.md 末尾让 claude 打印 `git worktree remove <worktree_dir>` 让用户决定何时清理
       ↓
  claude 交互模式起来，第一条消息已经是 fix prompt，订阅生效（不是 -p 走 API key）
```

### 触发条件（pr_watcher 里）

`build_event` 根据 codex verdict 决定要不要塞 URL：

| verdict | EKEvent.url | 描述里贴 paste-ready 命令？ |
|---|---|---|
| ✅ 可以合并 | None | 否（不需要修复） |
| ⚠️ 修正后可合并 | mycalfix://... | 是 |
| ❌ 暂不可合并 | mycalfix://... | 是 |
| 🤖 fallback | None | 否（解析不出 verdict，可能 codex 自己跑挂了） |

### 关键文件

| 路径 | 作用 |
|---|---|
| `scripts/fix_prompt.md` | 修复 prompt 模板（含 `{pr_url}` / `{comment_url}` / `{branch}` / `{comment_body_path}` 占位）。第一步优先读 `{comment_body_path}` 指向的本地缓存文件（pr_watcher 预取，无网络），缓存缺失再退到 `gh api` 兜底。硬约束包括：diff >1000 行 abort、只改 review 点名的地方、跑项目自检、commit + push 同分支不要 --force |
| `scripts/launch_fix.sh` | URL handler 本体（repo 里的"源"；install 时被复制进 .app）。每次点击弹 osascript 对话框选 **Yolo / Interactive / Cancel**，默认按钮 Interactive。`MYCALFIX_MODE=yolo\|interactive\|cancel` env var 可跳过对话框（测试 / 脚本化场景用）。所有 URL 都会落到 `~/Library/Logs/MyCalFix/launch_fix.log` 方便排错 |
| `app/MyCalFix/main.applescript` | AppleScript 源（自包含；运行时用 `path to me` 找 `Contents/Resources/launch_fix.sh`） |
| `scripts/install_app.sh` | osacompile + plutil 设 `CFBundleURLTypes` + `LSUIElement=true`（无 Dock 图标）+ 把 `launch_fix.sh` / `parse_fix_url.py` / `fix_prompt.md` 复制进 `Contents/Resources/`（少一个就 abort）+ 安装时 smoke-test bundled parser + `xattr -dr com.apple.quarantine` + `lsregister -f` + `tccutil reset All <bundle-id>`（清旧 TCC 决定） |
| `~/Applications/MyCalFix.app` | 部署后的 .app；`com.wuyupeng.mycalfix` bundle id，注册 `mycalfix:` scheme；`Contents/Resources/` 内含 bundled `launch_fix.sh` + `parse_fix_url.py` + `fix_prompt.md` |
| `~/.cache/my-calendar/fix-comments/<comment_id>.md` | pr_watcher 预取的 codex review comment body（option E）。launch_fix.sh 把这个路径作为 `{comment_body_path}` 喂给 fix_prompt.md，claude 用 Read 工具读它而不是 `gh api` 跑网络拉一遍。Approve 对话框因此能显示具体被读的文件名，且 comment 后续被编辑也不会污染本次会话 |
| `~/Library/Logs/MyCalFix/launch_fix.log` | 每次 URL 触发的解析参数 + Terminal 启动状况 |

### 手动入口

```bash
# 第一次安装 / 每次改 launch_fix.sh / fix_prompt.md / main.applescript 后重装
# （三个文件都被复制进 .app bundle，改了源不重装 .app 不会生效）
bash scripts/install_app.sh

# 验证 .app 已注册 mycalfix scheme
/System/Library/Frameworks/CoreServices.framework/Frameworks/LaunchServices.framework/Support/lsregister -dump | grep -A2 mycalfix:

# 手动触发一次（无 .app 也能跑，直接调脚本）
bash scripts/launch_fix.sh 'mycalfix://fix?repo=foo%2Fbar&branch=main&comment=https%3A%2F%2Fgithub.com%2Ffoo%2Fbar%2Fpull%2F1%23issuecomment-1&pr=https%3A%2F%2Fgithub.com%2Ffoo%2Fbar%2Fpull%2F1&origin_cwd=/path/to/repo'

# 模拟 .app 路径（经过 LaunchServices 派发）
open 'mycalfix://fix?repo=foo%2Fbar&branch=main&comment=https%3A%2F%2Fgithub.com%2Ffoo%2Fbar%2Fpull%2F1%23issuecomment-1&pr=https%3A%2F%2Fgithub.com%2Ffoo%2Fbar%2Fpull%2F1&origin_cwd=/path/to/repo'
```

### 没有 origin_cwd 的事件

launchd 兜底路径触发的评论（PR 在网页/异机创建、bot 自动 commit 等）没经过 pre-push hook，`pr_state.json` 里没有 `origin_cwd`。这种事件：

- URL 还是有效的，只是少了 `origin_cwd` 参数
- launcher 会弹一个 osascript 文件夹选择器让用户手动定位
- 描述里贴的 paste-ready 命令会显示 `<填入本地 repo 路径>` 占位 + 一段"⚠️ origin_cwd 未知"提示
- 一旦用户本地 push 同 PR 一次，pre-push hook 会把 `origin_cwd` 回写到 state + 上一次 review 的 `.meta.json` 里

### 调试

- `tail -f ~/Library/Logs/MyCalFix/launch_fix.log` 看每次 URL 被怎么解析的，最末尾会看到 `launching via .command: /tmp/mycalfix.xxxx.command`
- 改了 `launch_fix.sh` / `fix_prompt.md` / `main.applescript` **都**必须 `bash scripts/install_app.sh` 重装——三个文件都被复制进 bundle，repo 里的源只是模板
- 若看到 EPERM "Operation not permitted (126)"：bundled launcher 路径有问题或 .app 没装好；先 `ls ~/Applications/MyCalFix.app/Contents/Resources/` 确认 `launch_fix.sh` + `fix_prompt.md` 都在
- Gatekeeper 第一次警告：installer 已经做了 `xattr -dr com.apple.quarantine`，理论上不会再弹；如果还弹，**右键 → 打开**，之后永久放行
- `claude` 命令找不到：.command 文件里的 `exec bash -l` 起的是 login shell，PATH 用 shell rc 加载的。如果你的 `claude` 装在 `~/bin` 或自定义 npm prefix，确认它在 `.zshrc` / `.bash_profile` 里被加进 PATH
- Terminal 弹了但 git fetch/checkout 失败：检查 `origin_cwd` 是不是对的 repo、分支名是否真的存在、远端是否能访问。**注意**：launcher 不再做 `git rev-parse` 预校验（见上面"on URL → launch_fix"小节），路径错误现在只能从 Terminal 输出里发现
- Terminal **没弹**（点了链接没反应）：看日志最后一行是不是 `Terminal launched OK`。如果是但窗口没出现，可能 Terminal.app 被禁用或非默认 .command 处理器；用 `duti -d com.apple.Terminal command` 或在 Finder 右键 → 显示简介 → 打开方式重置 .command 绑定

### 单元测试覆盖

`scripts/test_pr_watcher.py` 覆盖：

- `_build_fix_url`：有 origin_cwd / 无 origin_cwd / 缺 head_branch / 缺 comment_url 四种情形
- `build_event`：verdict 是 ❌/⚠️ 时 URL 设置 + paste-ready 命令含 origin_cwd；verdict 是 ✅ 时 URL=None 且无修复入口区块；origin_cwd 缺失时 paste 命令带 `<填入本地 repo 路径>` 占位
- `ParseFixUrlTests`：URL 解析器拒绝 wrong scheme / wrong action / non-github pr / repo mismatch / 控制字符等
- `CacheCommentBodyTests`：option E 契约——`cache_comment_body()` 把 codex review body 写到 `~/.cache/my-calendar/fix-comments/<comment_id>.md`，URL 缺 `#issuecomment-<id>` fragment / body 为空 / URL 为空都返回 None
- `FixPromptUsesLocalCommentBodyTests`：fix_prompt.md 必须含 `{comment_body_path}` 占位符，且本地路径段要排在 `gh api` 兜底段之前（确保 claude 优先读本地），同时仍保留 gh api 兜底（老日历事件无缓存时仍能跑）
- `LaunchFixCommandFileRenderTests.test_command_file_contains_renderer_output_yolo_mode`：`MYCALFIX_MODE=yolo` 时渲染出的 .command body 必须含 `claude --dangerously-skip-permissions '...'`
- `LaunchFixCommandFileRenderTests.test_command_file_omits_yolo_flag_in_interactive_mode`：`MYCALFIX_MODE=interactive` 时 .command body **必须不含** `--dangerously-skip-permissions`——这是 option C 的契约
- `LaunchFixCommandFileRenderTests.test_launcher_aborts_when_mycalfix_mode_cancel`：`MYCALFIX_MODE=cancel` 时 launcher 退出非零且不会调到 `open -a Terminal`
- `InstallAppBundleManifestTests`：静态读 `install_app.sh` 校验 `launch_fix.sh` 引用的每个 `$HERE/<helper>`（`parse_fix_url.py` / `fix_prompt.md`）都在 bundle 复制清单里；同时校验 `mycalfix_config.py` 已经从 launcher、installer 与磁盘上**全部**删除（防止哪一侧偷偷加回来）

`launch_fix.sh` 末段的 `.command` 文件生成 + `open -a Terminal` 没有自动测试（依赖 macOS LaunchServices），靠手动 smoke：`bash scripts/launch_fix.sh 'mycalfix://...'` 直接跑（无 .app 也可），看 `~/Library/Logs/MyCalFix/launch_fix.log` 末尾打印的 `.command` 文件路径是否被 Terminal 读到（文件在 Terminal 启动时被 `rm -f $0` 自删）。

### 设计取舍

- **走 .app + URL scheme，不走 Shortcuts**：URL 稳定可版本化、可塞进 repo、不依赖 Shortcuts.app；缺点是要 osacompile 编译，installer 略复杂
- **走 `claude "<prompt>"` 交互模式，不走 `claude -p`**：`-p` 在某些环境会吃 `ANTHROPIC_API_KEY` 而不是订阅；交互式确认走订阅
- **MyCalFix 模式：per-click 对话框 + 预取 comment body（PR #30 codex review pushback 后的最终方案）**。短暂存在过"强制 yolo"版本（PR #30 头一版）被 codex 指出是 blocker：点一次日历链接 = 不可信 PR 内容驱动的无审批本机工具执行 / secret 读取 / 网络外传。这版方案两条同时上：
   - **C：per-click 对话框**。每次点击 mycalfix:// 链接，launch_fix.sh 用 osascript `display dialog` 弹一个 `Cancel / Yolo / Interactive` 三按钮窗，默认按钮 = Interactive（按回车选它）。yolo 不再是"可被遗忘的默认"，是用户主动按下的按钮。`MYCALFIX_MODE` env var 跳过对话框（脚本 / 测试 / 用户自己 export sticky preference 用）：`yolo` / `interactive` / `cancel` 三个值，未知值 / 空值都让对话框接管。osascript 失败 / 用户点 Cancel 都 abort，**不写 .command 文件、不调 `open -a Terminal`**。
   - **E：预取 codex review comment body 到本地缓存**。pr_watcher 在写完日历事件之前调 `cache_comment_body()`，把 codex 的整段 review body 写到 `~/.cache/my-calendar/fix-comments/<comment_id>.md`。launch_fix.sh 把这个路径作为 `{comment_body_path}` 渲染进 fix_prompt.md，claude 用 Read 工具读它而不是 `gh api` 跑网络拉一遍。两个效果：(a) Interactive 模式的 Approve 对话框会显示具体被读的文件名，用户能看出 claude 想读什么，而不是看到一条 prompt 注入可以重定向的 `gh api ...` shell 命令；(b) comment 内容在 review 完成时被冻结到磁盘——上面的 GitHub 评论后来被人编辑加了 prompt-injection payload 也污染不到这次会话。缓存缺失时（老日历事件 / 缓存被清）fix_prompt.md 有 `gh api` 兜底，老 URL 仍能用。
   - **剩余的风险，必须承认**：Yolo 按钮还在，被点了就还是无审批执行。`git worktree` 只隔离 git checkout 不限制 Claude Code 读 `~/.ssh` / `~/.config` 或改写 worktree 外文件，`fix_prompt.md` 的约束不是安全边界。option C 的设计选择：让 yolo 是**显式 per-session 决定**而不是默认值，把"你是否真的想给这次会话无审批"这个问题摆到每次点击的 critical path 上。
   - **辅助工程边界（任何模式都成立）**：(a) worktree 一次性隔离在 `~/.cache/my-calendar/worktrees/`，改坏一删就回去；(b) `.command` 在 fetch/worktree-add 之前已经校验 `git remote get-url origin` 与 URL 的 repo 一致，push 推不到错的 remote；(c) `fix_prompt.md` 硬约束 diff >1000 行 abort、只改 review 点名处、不 --force push。
- **不自动跑修复**：只写日历、塞 URL，由人决定要不要点。修复也是普通的 claude 会话，受用户监督
- **默认 Terminal 不侦测 iTerm2**：减少配置面；用户用 iTerm2 想接管，改 launch_fix.sh 末尾的 `open -a Terminal` 为 `open -a iTerm` 即可（记得改完重装 .app）
- **用 `.command` 文件 + `open -a Terminal`，不用 `osascript do script Terminal`**：osascript do script 走 AppleEvents (Automation TCC)，未签名 osacompile 应用经常被静默拒绝 -1743 errAEEventNotPermitted，连授权对话框都不弹。`.command` 是 LaunchServices 文档打开，没有 TCC 门槛，第一次点链接就能用。.command 文件首行 `rm -f $0` 自删避免堆积
- **`launch_fix.sh` 不做 `git rev-parse` 校验 origin_cwd**：launcher 跑在 .app 的 TCC 沙箱里，`git -C ~/Desktop/<repo>` 会 EPERM（即使 Info.plist 声明了 `NSDesktopFolderUsageDescription`，一旦 TCC 库记下 deny 就不再弹框，重装 .app 也不清）。校验会把每次点击都打去 picker，违背"URL 里给的路径应该被用上"的语义。URL 来源是本地 pre-push hook，攻击面≈0，直接信任。路径错了在 Terminal 里 `git -C <wrong> fetch` 失败，用户能看到
- **install_app.sh 末尾 `tccutil reset All <bundle-id>`**：清掉旧 TCC 决定，避免老版本 .app 留下的 deny 状态污染新版。tccutil 没有记录可清时返回非零，无视即可
- **launcher + parser + prompt 都打包进 .app bundle（`Contents/Resources/`）**：早期版本是 `__LAUNCHER_PATH__` 占位 + 指向 repo 内绝对路径，但若 repo 在 `~/Desktop` / `~/Documents` / `~/Downloads` 这类 macOS TCC 受保护目录下，.app 跑 bundled launcher 会直接 `EPERM (126)`（Info.plist 里也没声明 `NSDesktopFolderUsageDescription`，弹不出权限对话框）。把脚本和 prompt 搬进 bundle 内部后，.app 读自己 bundle 里的资源不受 TCC 管，问题彻底消失。代价是改任意一个源文件都要 `bash scripts/install_app.sh` 重装一次（installer 重新复制）。**bundle 清单与 launch_fix.sh 的 `$HERE/...` 依赖必须保持一致**——任何 helper 漏打包都会让 launch_fix.sh 启动时 abort。`install_app.sh` 装完会跑 bundled parser 的 smoke 测试，`scripts/test_pr_watcher.py::InstallAppBundleManifestTests` 在 CI/单测层面用 regex 静态校验 `install_app.sh` 里每个 helper 都有对应的 `cp <var> "$RESOURCES/<helper>"` 行；同时锁住"`mycalfix_config.py` 已经被删除"这条约定，防止某次回滚悄悄把它装回去（旧版本里它强制 yolo，会绕过 per-click 对话框）
- **log 写 `~/Library/Logs/MyCalFix/`，不写 repo `logs/`**：同一原因——bundled launcher 是 .app 的子进程，写 Desktop 下 `logs/` 也会 EPERM。`~/Library/Logs/` 是 Apple 文档化的应用日志位置，不受 TCC 管
- **`LSUIElement=true`**：MyCalFix 是一次性 URL handler，不该出现在 Dock / 应用切换器里
- **prompt 里硬约束 "diff >1000 行 abort"**：safeguard，防止 claude 被 prompt injection 或误解 review 拐去做大重构
