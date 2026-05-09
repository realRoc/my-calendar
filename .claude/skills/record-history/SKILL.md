---
name: record-history
description: 记录在某个节日为某人做的事，或后续补充对方的反馈。当用户说"记一下今天送了 X"、"今天是 X 节，我给 Y 做了 Z"、"X 收到礼物说 Y/不喜欢"、"补一下上次的反馈"等表达时调用。负责创建或更新 history/YYYY/YYYY-MM-DD__holiday__person.md。
---

# record-history

这是项目里最重要的 skill。**不要直接手写文件**——按流程走，特别注意"新动作"vs"补反馈"的判断。

## 第 0 步：判断意图

读用户原话，判断是哪种：

| 信号词 | 意图 |
|---|---|
| "送了"、"做了"、"买了"、"今天 X 节我..." | **新动作**（create） |
| "反馈"、"很喜欢"、"不喜欢"、"她说"、"他觉得"、"上次那个..." | **补反馈**（update） |
| 同时提到动作 + 反馈（如"上周送了花，今天她说很喜欢"） | **两步**：先 create 当天的，再 update feedback |

含糊时（如"记一下"），先问一句："是新增一笔送礼记录，还是补上次的反馈？"

---

## 分支 A：新动作（create）

### A.1 收集字段

问清（缺什么补什么，不要重复问已经说过的）：

- **节日**：哪个节日？匹配 `holidays/*/*.md` 里已有的 id；找不到就提议先 `add-holiday`
- **对象**：给谁？匹配 `people/*.md`；找不到就提议先 `add-person`
- **日期**：默认今天（用对话上下文里的"今天"），用户明说则用用户的
- **做了什么**：礼物 / 行动 / 文字（自由文本）
- **花费**（可选）：数字，元
- **渠道**（可选）：从哪买/通过什么方式
- **反馈**：默认留空（`feedback: ""`），后续补
- **备注/标签**（可选）

### A.2 写文件

路径 `history/<YYYY>/<YYYY-MM-DD>__<holiday-id>__<person-id>.md`

如果 `history/<YYYY>/` 目录不存在，先 `mkdir -p`。

如果同路径文件已存在 → 询问用户：覆盖、追加，还是改日期？

frontmatter（严格按 AGENTS.md schema）：

```yaml
---
holiday: <holiday-id>
date: <YYYY-MM-DD>
person: <person-id>
action: <做了什么，一行文本>
cost: <数字>           # 可选
channel: <渠道>        # 可选
feedback: ""           # 留空字符串！表示尚未收到反馈
feedback_recorded_at:  # 留空
rating:                # 留空
tags: []
---

<可选的正文：背景、决策、原话引用等>
```

### A.3 反馈

告诉用户：
- 写到了哪
- 提示一句："等收到反馈了再告诉我，我会找到这条记录补上去"

---

## 分支 B：补反馈（update）

### B.1 找到目标记录

如果用户说了哪个节日哪个人 → 直接 grep：
```bash
ls history/*/*__<holiday-id>__<person-id>.md
```

如果没说清 → 找所有 `feedback: ""`（空反馈）的记录：
```bash
grep -l 'feedback: ""' history/*/*.md
```

筛选条件优先级：
1. 用户明说的人/节日
2. 时间最近的空 feedback 记录（通常是最近送的那次）
3. 如果有多条候选，列给用户选

### B.2 更新字段

用 Edit 工具修改 frontmatter：

- `feedback: ""` → `feedback: <用户原话或概述>`
- `feedback_recorded_at:` → `feedback_recorded_at: <今天 YYYY-MM-DD>`
- 询问 `rating`（1-5），用户没说就不填
- 如果用户提了具体细节（"她说花有点小"），把原话也写进正文段

**保留所有原有字段**——只动 feedback 相关的几个。

### B.3 反馈

告诉用户：
- 更新了哪个文件
- 摘要：动作是什么 + 反馈是什么 + 评分（如果有）
- 如果评分低（1-2），轻轻提一句"明年同节日时这条会被高亮在日历事件里作为'避雷'参考"

---

## 跨年聚合（用户问"妈妈过去几年都收到过什么"时）

不需要数据库，直接：
```bash
ls history/*/*__mom.md         # 所有给妈妈的
ls history/*/*__mothers-day__*.md   # 所有母亲节的
```

按文件名排序就是按时间顺序。读 frontmatter 提取 action/feedback/rating 即可。

---

## 边界

- 日期一律用 ISO `YYYY-MM-DD`
- 金额一律纯数字（人民币元），不写"¥"或"元"
- `feedback: ""` 是"已送出未反馈"的语义；如果是"完全没送/没做"那就根本不该有这个 history 条目
- 不要给 history 文件加任何"已删除"标记——错了就用 `git rm` 或直接删
