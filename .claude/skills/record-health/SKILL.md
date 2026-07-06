---
name: record-health
description: 记录个人健康数据到 health/ 模块。当用户说"记一下今天体重 X"、"第一顿/第二顿吃了 X"（常配餐食照片）、"今天游了泳/骑了车"、"膝盖/手腕今天不舒服"、"昨晚几点睡的"、"漏吃药了"等表达时调用。负责 upsert health/log/YYYY/YYYY-MM-DD.md、导入照片；记录第一顿后生成第二顿针对性建议并刷新"健康提醒"日历。
---

# record-health

把用户随口说的健康信息落到当日 log 文件。**不要直接手写文件结构**——按下面流程走。

## 第 0 步：判断日期与类型

- 日期默认今天；用户说"昨天""周六"则换算成 ISO 日期
- 一句话里可能带多种信息（"今早 78.5，昨晚游了 1 小时"= 两个日期两个文件），拆开处理
- **档案级变化**（新伤病、确诊结果、搬家、作息改变）→ 改 `health/profile.md`
- **用药长期变更**（加药/停药/换剂量/换时间）→ 改 `health/medications.md` frontmatter；
  漏服/补服/不良反应这类一次性事件 → 记当日 log 的 `meds` 字段

## 第 1 步：upsert 当日 log

路径 `health/log/<YYYY>/<YYYY-MM-DD>.md`（目录不存在先 `mkdir -p`）。

- 文件不存在 → 按 `health/README.md` 的 schema 新建，只填用户给的字段，其余留空/留 `[]`
- 文件已存在 → 用 Edit **只改涉及的字段**，正文追加不覆盖

字段速查：

| 用户说 | 落到 |
|---|---|
| "今天 78.5"、"空腹 78.5kg" | `weight_kg` + `fasting: true`（明说空腹或晨起才填 true） |
| "腰围 84" | `waist_cm` |
| "第一顿吃了 X"（10–11 点那顿） | `meals` 追加 `{slot: 1, time, desc, photos}` |
| "晚饭/第二顿吃了 X"（17–18 点那顿） | `meals` 追加 `{slot: 2, ...}` |
| "游了 50 分钟"、"骑车通勤" | `workouts` 追加 `{type, duration_min, notes}` |
| "膝盖有点疼，3 分吧" | `symptoms` 追加 `{site, severity_1_5, notes}` |
| "昨晚 1 点睡 9 点起" | `sleep: {bedtime, wake, hours}` |
| "今天忘吃药了/补吃了" | `meds` 追加 `{id, event, notes}` |
| 发照片 | 走第 2 步；体态照进顶层 `photos`，餐食照进对应 `meals[].photos` |

## 第 2 步：照片导入（如果有）

```bash
mkdir -p health/photos/<YYYY>
sips -s format jpeg -s formatOptions 82 --resampleHeightWidthMax 1600 \
  "<原图路径>" --out "health/photos/<YYYY>/<YYYY-MM-DD>__<slug>.jpg"
```

- `slug` 用内容描述：体态 `fasting-front`/`side`/`back`，餐食 `meal1`/`meal2`（同日多张加序号）
- 原图不复制进 repo；log 正文里记一句原图文件名以便回溯

## 第 3 步：记录的是第一顿 → 生成第二顿建议（**核心联动**）

用户记完 slot 1 的饭之后：

1. 看照片 + 描述，评估这顿的构成：蛋白质够不够一掌半？蔬菜有没有？主食/糖是不是超了？
2. 据此生成**当天第二顿的针对性建议**——写具体食物和份量（手掌法），**不写热量数字**。
   缺什么补什么：早上蛋白质少 → 晚上蛋白质双份；早上碳水超了 → 晚上主食减到半拳；
   早上没蔬菜 → 晚上蔬菜双手捧起步。语气像朋友，给 1–2 个可执行选项（外卖可点到的）
3. 用 Edit 更新 `health/plans/week.md` 当天的 `meal2` 字段为这条建议
4. 刷新日历事件：

```bash
.venv/bin/python scripts/health_check.py
```

5. 对话里也直接把建议告诉用户（日历 17:00 会再提醒一次）

## 第 4 步：反馈 + 顺手洞察

- 告诉用户写到了哪、记了什么
- **体重**：顺手 grep 最近几次 `weight_kg` 报一句趋势（"比上周 -0.6kg，节奏正常"）
- **symptom 涉及右腕/右膝**：对照 `plans/current.md` 降级规则，必要时当场调整今天的 workout 建议
- 上面第 3 步跑过 `health_check.py` 的话，当天的催记录 nag 事件会被自动清掉，不用管
- 距上次 `health-review` 超过 10 天 → 提一句"要不要跑个周报？"

## 边界

- 体重一律 kg 保留一位小数；时间 ISO；不要发明 schema 外的新字段——真需要先改 `health/README.md` 再用
- 饮食建议**永远写食物不写 kcal**（用户明确说过对数字没概念）；份量用手掌法
- 健康数据永不入 git（`.gitignore` 已排除 `health/*`）；不要把数值或照片写进任何会被 commit 的文件
- 用户只是聊健康话题而没有要记录的意图时，不要强行落盘
