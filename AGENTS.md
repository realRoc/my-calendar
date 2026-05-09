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
│   ├── calendar_sync.py            # EventKit 写入苹果日历
│   ├── daily_check.py              # 每日入口：解析+查历史+写日历
│   └── state.json                  # 已创建事件 ID 索引（去重用）
├── logs/                           # launchd stdout/stderr
├── com.YOURNAME.calendar.daily.plist  # 由 install_launchd.sh 从 template 生成
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
