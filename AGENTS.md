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

### 配置

`~/.config/my-calendar/config.json` 可选，目前支持：

| key | 默认 | 说明 |
|---|---|---|
| `codex_concurrency_cap` | `10` | 全机器同时跑 codex 的最大数量。pr_watcher 用 `locks/codex-slot-{1..N}.lock` 实现 N 个 slot 的信号量；想给小机器/紧预算降并发就把这个数调小（如 `4`）。**调大也不会被拦**，但 codex 同时执行数、CPU/网络和 LLM 调用成本会随之线性上升——自行评估机器和预算能承受。**严格 JSON integer** 校验：`2.5` / `"4"` / `true` / 负数 / 0 都会落回默认 10 + 一行 stderr 警告，不会崩 |
| `mycalfix_interactive_claude` | `true` | MyCalFix 起 claude 修复会话时的模式开关。**默认 true** → 用 `claude <prompt>`，每个工具调用都需要 Approve/Deny。理由：fix prompt 含 PR diff / review 评论（不可信内容），worktree 不是真沙箱（claude 仍可读 `~/.ssh`、`~/.config` 或改写 worktree 外文件），yolo 模式把"点日历链接"升级成无审批本机工具执行——这是 PR #22 codex review 标的 blocker。设 `false` 显式 opt-in `claude --dangerously-skip-permissions <prompt>`（yolo）：会话仍在 disposable worktree 里跑，但不再弹审批，与 pr_watcher 调 codex 时的 yolo 模式对齐。**严格 JSON boolean** 校验且 **fail-closed**：`"true"` / `1` / `0` / `null` / malformed JSON 等任何非 bool 值都会落回默认 true（交互）+ 一行 stderr 警告——错误路径绝不会静默升级到 yolo |

示例 `config.json`：

```json
{
  "codex_concurrency_cap": 4,
  "mycalfix_interactive_claude": false
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
| codex 通过 pr_watcher 发的 PR review 评论 | 评论 body **第一行** | <pre>> 🤖 由 Codex 自动生成（pr_watcher 触发，无人工干预）· 本仓库所有者未介入此条评论的撰写<br>&lt;!-- ai-coauthor: codex; agent: pr_watcher; mode: automated --&gt;</pre> |
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
| `scripts/fix_prompt.md` | 修复 prompt 模板（含 `{pr_url}` / `{comment_url}` / `{branch}` 占位）。硬约束包括：diff >200 行 abort、只改 review 点名的地方、跑项目自检、commit + push 同分支不要 --force |
| `scripts/mycalfix_config.py` | MyCalFix 用户 config 读取器。`claude-flag` 子命令被 `launch_fix.sh` 调用，读 `~/.config/my-calendar/config.json` 决定要不要给 claude 加 `--dangerously-skip-permissions`。严格 JSON bool 校验，**fail-closed**：校验失败 / 文件缺失 / JSON 损坏一律落回 interactive 默认（空 flag），绝不会静默升级到 yolo |
| `scripts/launch_fix.sh` | URL handler 本体（repo 里的"源"；install 时被复制进 .app）。所有 URL 都会落到 `~/Library/Logs/MyCalFix/launch_fix.log` 方便排错 |
| `app/MyCalFix/main.applescript` | AppleScript 源（自包含；运行时用 `path to me` 找 `Contents/Resources/launch_fix.sh`） |
| `scripts/install_app.sh` | osacompile + plutil 设 `CFBundleURLTypes` + `LSUIElement=true`（无 Dock 图标）+ 把 `launch_fix.sh` / `parse_fix_url.py` / `mycalfix_config.py` / `fix_prompt.md` 全复制进 `Contents/Resources/`（少一个就 abort）+ 安装时 smoke-test bundled parser + config helper（HOME 清空跑 `claude-flag` 必须输出空字符串，即 interactive 默认；否则视为 bundle 残缺 fail install）+ `xattr -dr com.apple.quarantine` + `lsregister -f` + `tccutil reset All <bundle-id>`（清旧 TCC 决定） |
| `~/Applications/MyCalFix.app` | 部署后的 .app；`com.wuyupeng.mycalfix` bundle id，注册 `mycalfix:` scheme；`Contents/Resources/` 内含 bundled `launch_fix.sh` + `parse_fix_url.py` + `mycalfix_config.py` + `fix_prompt.md` |
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
- `MyCalFixInteractiveClaudeConfigTests`：默认 interactive（safe-by-default）/ 显式 `false` 切到 yolo / 严格 bool 校验 fail-closed 拒绝 `"true"` `1` `0` `null` 一律回 interactive / malformed JSON 也 fail-closed 回 interactive / `python3 mycalfix_config.py claude-flag` CLI 子命令在干净 HOME 下输出空字符串
- `InstallAppBundleManifestTests`：PR #22 blocker 回归——静态读 `install_app.sh` 校验 `launch_fix.sh` 引用的每个 `$HERE/<helper>`（`parse_fix_url.py` / `mycalfix_config.py` / `fix_prompt.md`）都在 bundle 复制清单里；同时校验 installer 装完会 smoke-test bundled `mycalfix_config.py claude-flag`。漏打包 helper（或反过来给 launch_fix.sh 加新 helper 不更新 manifest）这条测试会挂

`launch_fix.sh` 末段的 `.command` 文件生成 + `open -a Terminal` 没有自动测试（依赖 macOS LaunchServices），靠手动 smoke：`bash scripts/launch_fix.sh 'mycalfix://...'` 直接跑（无 .app 也可），看 `~/Library/Logs/MyCalFix/launch_fix.log` 末尾打印的 `.command` 文件路径是否被 Terminal 读到（文件在 Terminal 启动时被 `rm -f $0` 自删）。

### 设计取舍

- **走 .app + URL scheme，不走 Shortcuts**：URL 稳定可版本化、可塞进 repo、不依赖 Shortcuts.app；缺点是要 osacompile 编译，installer 略复杂
- **走 `claude "<prompt>"` 交互模式，不走 `claude -p`**：`-p` 在某些环境会吃 `ANTHROPIC_API_KEY` 而不是订阅；交互式确认走订阅
- **默认 interactive（safe-by-default），yolo 必须显式 opt-in**：codex PR #22 review 把"默认 `--dangerously-skip-permissions`"标成 blocker——fix prompt 含 PR diff 和 review 评论（不可信内容），`git worktree` 只隔离 git checkout 不限制 Claude Code 读 `~/.ssh` / `~/.config` 或改写 worktree 外文件，`fix_prompt.md` 的约束不是安全边界；这样会把"点一次日历链接"升级成无审批本机工具执行。**默认行为**：launcher 跑 `claude <prompt>`，每个工具调用都弹审批，用户能逐步看到 claude 想干什么。**显式 opt-in 到 yolo**：在 `~/.config/my-calendar/config.json` 写 `mycalfix_interactive_claude: false`——只有完全理解 yolo 模式下 prompt injection 仍是唯一防线、且只在自己 repo 上点 mycalfix 链接的人才该开。**Fail-closed**：launcher 的 helper 调用失败、config 文件不可读、JSON 损坏、值类型不对，每条错误路径都回落到 interactive（空 flag）+ 一行 stderr 警告——绝不会因为"安全阀坏了"而静默升级到 yolo。**辅助安全前提**（即使在 yolo opt-in 下也成立）：(a) worktree 一次性隔离在 `~/.cache/my-calendar/worktrees/`，改坏一删就回去；(b) `.command` 在 fetch/worktree-add 之前已经校验 `git remote get-url origin` 与 URL 的 repo 一致，push 推不到错的 remote；(c) `fix_prompt.md` 硬约束 diff >200 行 abort、只改 review 点名处、不 --force push
- **不自动跑修复**：只写日历、塞 URL，由人决定要不要点。修复也是普通的 claude 会话，受用户监督
- **默认 Terminal 不侦测 iTerm2**：减少配置面；用户用 iTerm2 想接管，改 launch_fix.sh 末尾的 `open -a Terminal` 为 `open -a iTerm` 即可（记得改完重装 .app）
- **用 `.command` 文件 + `open -a Terminal`，不用 `osascript do script Terminal`**：osascript do script 走 AppleEvents (Automation TCC)，未签名 osacompile 应用经常被静默拒绝 -1743 errAEEventNotPermitted，连授权对话框都不弹。`.command` 是 LaunchServices 文档打开，没有 TCC 门槛，第一次点链接就能用。.command 文件首行 `rm -f $0` 自删避免堆积
- **`launch_fix.sh` 不做 `git rev-parse` 校验 origin_cwd**：launcher 跑在 .app 的 TCC 沙箱里，`git -C ~/Desktop/<repo>` 会 EPERM（即使 Info.plist 声明了 `NSDesktopFolderUsageDescription`，一旦 TCC 库记下 deny 就不再弹框，重装 .app 也不清）。校验会把每次点击都打去 picker，违背"URL 里给的路径应该被用上"的语义。URL 来源是本地 pre-push hook，攻击面≈0，直接信任。路径错了在 Terminal 里 `git -C <wrong> fetch` 失败，用户能看到
- **install_app.sh 末尾 `tccutil reset All <bundle-id>`**：清掉旧 TCC 决定，避免老版本 .app 留下的 deny 状态污染新版。tccutil 没有记录可清时返回非零，无视即可
- **launcher + parser + config helper + prompt 都打包进 .app bundle（`Contents/Resources/`）**：早期版本是 `__LAUNCHER_PATH__` 占位 + 指向 repo 内绝对路径，但若 repo 在 `~/Desktop` / `~/Documents` / `~/Downloads` 这类 macOS TCC 受保护目录下，.app 跑 bundled launcher 会直接 `EPERM (126)`（Info.plist 里也没声明 `NSDesktopFolderUsageDescription`，弹不出权限对话框）。把脚本和 prompt 搬进 bundle 内部后，.app 读自己 bundle 里的资源不受 TCC 管，问题彻底消失。代价是改任意一个源文件都要 `bash scripts/install_app.sh` 重装一次（installer 重新复制）。**bundle 清单与 launch_fix.sh 的 `$HERE/...` 依赖必须保持一致**——任何 helper 漏打包都会让 launch_fix.sh 的 fail-safe 落回默认。fail-closed 后的失败模式：`mycalfix_config.py` 漏打包 → `claude-flag` 失败 → launcher 回 interactive（用户即使在 config 里写了 `mycalfix_interactive_claude: false` 也拿不到 yolo，但行为是安全方向，不像之前会静默升级到 yolo 绕过审批）。`install_app.sh` 装完会跑 bundled parser + bundled config helper 的 smoke 测试（HOME 清空跑 `claude-flag` 必须输出空字符串，否则 fail install），`scripts/test_pr_watcher.py::InstallAppBundleManifestTests` 在 CI/单测层面用 regex 静态校验 `install_app.sh` 里每个 helper 都有对应的 `cp <var> "$RESOURCES/<helper>"` 行（PR #22 codex 建议：旧的 `assertIn` 只看路径字符串，会被"删 cp 留 echo"骗过）
- **log 写 `~/Library/Logs/MyCalFix/`，不写 repo `logs/`**：同一原因——bundled launcher 是 .app 的子进程，写 Desktop 下 `logs/` 也会 EPERM。`~/Library/Logs/` 是 Apple 文档化的应用日志位置，不受 TCC 管
- **`LSUIElement=true`**：MyCalFix 是一次性 URL handler，不该出现在 Dock / 应用切换器里
- **prompt 里硬约束 "diff >200 行 abort"**：safeguard，防止 claude 被 prompt injection 或误解 review 拐去做大重构
