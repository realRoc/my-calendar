# my-calendar

本地节日提醒系统。每天扫描未来 7 天的节日（中国法定 + 西方常见 + 自定义），把"提醒事件"写入苹果日历，事件描述里附上你过去在同一节日做过什么、对方反馈如何，方便你做今年的决策。

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
│   ├── pr_watcher.py               # PR 监控入口：扫 open PR → codex review → 写日历（launchd 每 2 min）
│   ├── pr_prompt.md                # codex review 用的 prompt 模板（含 {pr_link} 占位）
│   ├── pr_state.json               # 每个 PR 的 head_sha / thread_id / comment_url
│   ├── pr_calendar_state.json      # "PR 监控" 日历的事件 ID 索引
│   └── pr_logs/                    # codex JSONL + 最终回复，每次运行一份
├── logs/                           # launchd stdout/stderr
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

**兜底通道 — launchd 30 min 轮询**

- `com.<user>.calendar.pr-watcher`，`StartInterval=1800`
- 抓本地 hook 漏掉的事件：网页创建的 PR、异机 push、bot 自动 commit、push 时网络抖动导致 hook 失败等
- 休眠不唤醒 Mac
- 每次 tick 单次最长 25 分钟，超时杀掉 codex

### 已知不被 hook 抓到的场景

| 触发源 | hook 抓到？ | launchd 兜底？ |
|---|---|---|
| 本机 `git push` | ✅ 立即 | — |
| 本机 `gh pr create`（无新 push） | ⚠️ 只有同时 push 才行 | ✅ ≤30min |
| GitHub 网页 UI 创建/编辑 PR | ❌ | ✅ ≤30min |
| 其他机器 push | ❌ | ✅ ≤30min |
| PR bot 自动 commit | ❌ | ✅ ≤30min |

### 单次 tick 流程

1. `gh api graphql` 一次拉所有 open PR（跨 org，author=@me）
2. 过滤：`baseRefName == repository.defaultBranchRef.name`
3. 与 `scripts/pr_state.json` 比对：
   - 首次见到 → seed 落 head_sha，**不评论**
   - head_sha 未变 → 跳过
   - head_sha 变了 → 触发 codex
4. codex 调用：`codex exec --json --dangerously-bypass-approvals-and-sandbox -s danger-full-access --skip-git-repo-check -C /tmp/codex-pr-runs/<uuid> "<prompt>"`
5. 从 JSONL 第一行抓 `thread_id`（用于 `codex resume`）
6. codex 自己用 `gh pr comment` 发评论；watcher 用 `gh api ...issues/<n>/comments` 兜底取 URL
7. EventKit 写到独立日历 **"PR 监控"**，UID = `my-calendar:pr-comment:<pr_url>:<sha>`

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
