# my-calendar

> A local-only holiday & gift reminder system for macOS. Tracks Chinese legal +
> Western common holidays + your custom dates, writes reminder events into Apple
> Calendar, and learns from your past actions so next year's reminder includes
> "what you did last time and how it landed". Designed to be operated entirely
> through [Claude Code](https://claude.com/claude-code) conversations — no UI,
> no CLI commands to memorize.

> 一个完全本地的 macOS 节日提醒系统：扫描中国法定节日 + 西方常见节日 + 你自定义的日子，
> 把提醒事件写入苹果日历；同时把你每年送了什么礼物、对方反馈如何沉淀下来，
> 明年同一节日的提醒里自动带上历史记录作为参考。
> 所有交互都通过 Claude Code 对话完成——你只要说"加一个节日"、"记一下今天送了 X"，
> 它就会按规范帮你写好文件。

---

## 特性

- **节日识别**：固定公历日期（情人节）、农历（春节/端午/中秋/七夕）、第 N 个星期 X（母亲节/父亲节/感恩节）三种规则
- **去重幂等**：每个事件有确定性 key + state.json 索引，重复运行只更新不新建
- **懒补跑**：错过 lead_day 但节日还没过 → 当天补一发，notes 顶端标 ⚠️
- **缺失追溯**：节日已过但你没记录 → 写入 MISSING.md，下次开 Claude 它会主动问你
- **零云端、零账号**：所有数据都是本地 markdown + frontmatter，离线可用，可放进自己的 git
- **不污染主日历**：写入专门的"节日提醒"日历，可单独显示/隐藏/删除

## 架构总览

```
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
                                     ▲
                                     │ 你说"记一下今天送了..."
                                     │
                                ┌────┴────┐
                                │ Claude  │  reads CLAUDE.md → AGENTS.md → invokes skill
                                │  Code   │  to write the file in the right schema
                                └─────────┘
```

详细 schema 与 Claude 工作约定见 [`AGENTS.md`](./AGENTS.md)（`CLAUDE.md` 是它的 symlink）。

---

## 安装（约 5 分钟）

### 0. 前置

- macOS（要用 EventKit）
- Python 3.10+
- Claude Code CLI（[claude.com/claude-code](https://claude.com/claude-code)）

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

### 3. 注册 launchd（每天 06:00 自动跑）

```bash
./scripts/install_launchd.sh
```

脚本会从 `com.calendar.daily.plist.template` 生成实际 plist（带绝对路径），加载到 launchd。

### 4. 真跑一次，检查日历

```bash
.venv/bin/python scripts/daily_check.py
```

打开 macOS 日历 app，左侧应该多一个 **节日提醒** 日历。

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
launchctl unload ~/Library/LaunchAgents/com.YOURNAME.calendar.daily.plist
rm ~/Library/LaunchAgents/com.YOURNAME.calendar.daily.plist
```

苹果日历里的"节日提醒"日历可以在日历 app 里手动右键删除（这会顺便删掉所有事件）。

---

## License

MIT — 见 [LICENSE](./LICENSE)。

随便用、随便改、随便商用，保留版权声明就行。
