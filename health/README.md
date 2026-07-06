# health/ — 个人健康模块

个人健康记录与教练模块：体重/饮食/体态照/运动/伤痛/睡眠/用药都落成本地 markdown + 照片文件，
Claude 会话基于这些"记忆"给出建议并按周复盘；`scripts/health_check.py`（launchd 每 30 分钟）
把具体的用药/饮食/运动建议写成带闹钟的定时事件到独立日历 **"健康提醒"**，漏记录会自动催。

**隐私边界**：本目录除本 README 外全部被 `.gitignore` 排除（与 `people/`、`history/` 同一约定）。
体态照片、体重、饮食、用药信息永远不进 git、不上 GitHub。

## 目录

```
health/
├── README.md            # 本文件（唯一入库的文件）
├── profile.md           # 静态档案：身高/生日/伤病/运动偏好/作息约束
├── medications.md       # 用药登记：frontmatter 里 active 条目会生成每日用药提醒事件
├── goals/<goal-id>.md   # 目标（可多个，有生命周期 status: active|paused|done|abandoned）
├── plans/
│   ├── current.md       # 宏观执行计划（health-review 迭代，version 递增）
│   ├── week.md          # 一周逐日具体建议（health_check.py 的数据源；每周日重写）
│   └── archive/         # current.md 的历史版本
├── log/YYYY/YYYY-MM-DD.md          # 每日记录：一天一个文件
└── photos/YYYY/YYYY-MM-DD__<slug>.jpg   # 照片（体态/餐食，导入时压缩到 ≤1600px JPEG）
```

## log 文件 schema

```yaml
---
date: 2026-01-15          # 必须
weight_kg: 70.0           # 可选，晨起空腹体重
fasting: true             # 可选，体重是否空腹测量
waist_cm:                 # 可选
photos: [photos/2026/2026-01-15__fasting-front.jpg]   # 可选，体态照（餐食照放 meals[].photos）
meals:                    # 可选，16+8 两顿制：slot 1 = 上午顿，slot 2 = 傍晚顿
  - slot: 1
    time: "10:30"
    desc: 鸡蛋两个 + 全麦面包 + 牛奶
    photos: [photos/2026/2026-01-15__meal1.jpg]
workouts: []              # 可选，[{type: 游泳, duration_min: 50, notes: ...}]
sleep:                    # 可选，{bedtime: "01:00", wake: "09:00", hours: 8}
symptoms: []              # 可选，[{site: 右膝, severity_1_5: 2, notes: ...}]
meds: []                  # 可选，仅记异常：[{id: <medications.md 里的药 id>, event: 漏服/补服/不良反应, notes: ...}]
mood:                     # 可选
tags: []
---

正文自由：当天想说的任何话。
```

约定：
- 一天一个文件；同一天多条信息（早上体重、两顿饭、晚上运动）由 `record-health` skill upsert 进同一文件
- 照片导入即压缩转 JPEG（`sips --resampleHeightWidthMax 1600`），原图留在用户相册不入库
- 用药正常按时吃**不记** log（避免噪音）；只记漏服/补服/不良反应等异常，长期变更改 `medications.md`
- 趋势查询不需要数据库：`grep -h "weight_kg:" log/*/*.md` 按文件名排序即时间序

## plans/week.md schema（health_check.py 读它）

```yaml
---
updated: 2026-01-12
days:
  "2026-01-16":
    meal1: "第一顿的具体建议文本（写食物，不写热量数字）"
    meal2: "第二顿的具体建议文本"
    workout: "运动建议文本"
    workout_time: "15:00"   # 可选；缺省工作日 09:15、周末 15:00
---
```

- 某天缺失/字段缺失 → `health_check.py` 用内置通用建议兜底，不会开天窗
- **当日联动**：第一顿记录后，Claude 在 record-health 流程里改当天 `meal2` 为针对性建议，
  再跑 `health_check.py`，17:00 的日历事件描述随之更新

## 日历事件（"健康提醒"）

| slot | 时间 | 内容 |
|---|---|---|
| `med-<id>` | 每药 `time`（默认 09:00） | 💊 用药提醒（标题不含药名，详情在描述） |
| `meal1` / `meal2` | 10:00 / 17:00 | 当日具体饮食建议 + 记录提示 |
| `workout` | 工作日 09:15 / 周末 15:00 | 当日运动建议 + 伤处红线 |
| `nag-meal1` / `nag-meal2` | 12:30 / 19:30 后仍未记录时 | 📝 催记录；record-health 落盘后下一 tick 自动删除 |
| `nag-weight` | 体重 ≥3 天未记时 | ⚖️ 催称重 |

UID = `my-calendar:health:<date>:<slot>`；state 在 `scripts/health_state.json`，
内容 hash 缓存在 `scripts/health_plan_cache.json`（无变化的 tick 不碰 EventKit）。

## 相关 skill

- `record-health` — 记录体重/饮食/照片/运动/症状/睡眠/用药异常（upsert 当日 log）；
  记完第一顿会顺手生成第二顿针对性建议并刷新日历
- `health-review` — 周期复盘：读 goal + 近期 log 算趋势，迭代 plans/current.md，重写下周 week.md
