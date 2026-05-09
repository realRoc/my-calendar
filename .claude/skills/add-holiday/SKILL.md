---
name: add-holiday
description: 新增一个节日定义。当用户说"加一个节日"、"新增 X 节"、"X 是个纪念日，记一下"、"加个 X 的生日"等表达时调用。负责把节日写入 holidays/<category>/<id>.md，遵循 AGENTS.md 中的 holiday frontmatter schema。
---

# add-holiday

用户想新增一个节日定义。**不要直接手写文件**——按下面的流程走，确保 schema 一致、id 不冲突、日期规则正确。

## 步骤

### 1. 确认基本信息
问清以下字段（一次性问完，不要逐个挤牙膏）：

- **中文名**（必须）
- **英文名**（可选，没有就跳过）
- **类别**：`cn-legal` / `western` / `custom`
  - 拿不准就归 `custom`
- **日期类型**：固定公历 / 农历 / 某月第 N 个星期 X
- **日期具体值**（看类型）
- **关联人物**（可选）：默认会想到谁？比如母亲节默认 mom
- **备注**（可选）：送礼参考、忌讳、历史背景等

### 2. 推导 holiday-id

规则：
- 优先用英文名 kebab-case（`Father's Day` → `fathers-day`）
- 没英文名就用拼音（`七夕` → `qixi`）
- 自定义生日：`<person-id>-birthday`（如 `mom-birthday`）
- 自定义纪念日：`<event>-anniversary`（如 `wedding-anniversary`）

### 3. 检查是否已存在

```bash
ls holidays/*/<id>.md 2>/dev/null
```

已存在 → 询问用户是覆盖还是改 id。

### 4. 写文件

路径 `holidays/<category>/<id>.md`，frontmatter 严格按 AGENTS.md 的 holiday schema：

```yaml
---
id: <id>
name_cn: <中文名>
name_en: <英文名>            # 没有则省略此行
category: <category>
date_rule:
  type: fixed | lunar | nth_weekday
  # type=fixed:
  month: <1-12>
  day: <1-31>
  # type=lunar:
  month: <1-12>
  day: <1-30>
  # type=nth_weekday:
  month: <1-12>
  weekday: <0-6>             # 0=周一, 6=周日 (ISO weekday - 1)
  n: <±1..5>                 # 第几个；负数表示倒数（-1=最后一个）
default_people: [<id>, ...]  # 可选
lead_days: [7, 1]            # 可选，默认 [7, 1]
notes: |                     # 可选
  <自由文本>
---
```

正文部分可以为空，或写一段对节日含义的简短说明。

### 5. 反馈

告诉用户：
- 写到了哪个路径
- 下次跑 `daily_check.py` 会被自动扫到
- 如果是今天/最近 7 天内的节日，提示可以立即跑一次 `daily_check.py` 让事件出现在日历

## 常见日期换算速查

| 节日 | 规则 |
|---|---|
| 母亲节 | nth_weekday: 5月第2个周日 → `month:5, weekday:6, n:2` |
| 父亲节 | nth_weekday: 6月第3个周日 → `month:6, weekday:6, n:3` |
| 感恩节(美) | nth_weekday: 11月第4个周四 → `month:11, weekday:3, n:4` |
| 春节 | lunar: 正月初一 → `month:1, day:1` |
| 端午 | lunar: 五月初五 → `month:5, day:5` |
| 中秋 | lunar: 八月十五 → `month:8, day:15` |
| 元宵 | lunar: 正月十五 → `month:1, day:15` |
| 重阳 | lunar: 九月初九 → `month:9, day:9` |

## 边界

- **清明节**不是固定 4月5日，是节气；预填数据用 fixed 近似（4-5 号），需要精确就手填日期
- **复活节**是 computus 算法；目前不做，需要时再加 type:`easter_relative`
- **农历闰月**：`lunardate` 默认非闰月；需要闰月场景再说
