---
name: health-review
description: 健康周期复盘。当用户说"健康周报"、"减脂进展怎么样"、"这周复盘一下"、"调整一下训练计划"等表达时调用。读 health/goals + 近期 log 算趋势，对比体态照，迭代 health/plans/current.md。
---

# health-review

数据驱动的复盘：先算数，再下判断，最后才动计划。

## 第 1 步：收集数据

```bash
# 活跃目标
grep -l "status: active" health/goals/*.md

# 体重时间序（文件名即时间序）
grep -H "weight_kg:" health/log/*/*.md | tail -30

# 本周期的运动/症状/睡眠
ls health/log/<YYYY>/ | tail -14   # 然后读最近的文件
```

复盘窗口默认**上次 review 至今**（看 `plans/current.md` 的 `updated`），用户明说则用用户的。

## 第 2 步：算趋势

- 体重：周期内首末均值差（单日波动是水分，别拿两个点说事；≥3 个点才谈趋势）
- 对照 goal 的 `metrics` 与 plan 的预期节奏（如每周 -0.4~0.5kg），给出：**超速 / 正常 / 停滞 / 反弹**
- 运动依从性：实际 workouts vs 计划频次
- 症状：右腕/右膝有没有新增或加重记录
- 如果用户本周期有新体态照且上期也有 → 用 Read 并排看两张，给视觉变化描述

补充数据源：`meals` 记录（两顿依从性、常见构成问题）、`meds` 异常（漏服频率）也进复盘。

## 第 3 步：迭代计划（谨慎）

只在有数据支撑时改 `health/plans/current.md`：

- 触发 plan 末尾的升级/降级规则 → 按规则改，`version` +1、`updated` 与 `next_review` 更新
- 症状加重 → 移除相关动作，并建议就医
- 趋势正常 → **不改内容**，只更新 `next_review`（不折腾是特性）
- 大改前把旧版存到 `health/plans/archive/v<N>-<date>.md`（目录不存在先建）

目标本身达成/放弃 → 改 `health/goals/<id>.md` 的 `status`，恭喜或复盘原因。

## 第 3.5 步：重写下周 week.md

每次周报都**整体重写** `health/plans/week.md`（未来 7 天）：

- 逐日给 `meal1` / `meal2` / `workout`（周末带 `workout_time: "15:00"` 游泳）
- 建议写**具体食物 + 手掌法份量**，不写热量数字；参考本周 `meals` 记录里用户实际爱吃、买得到的东西，越贴近现状越容易执行
- 保留每周一顿"自由餐"（守住先蛋白质后其他 + 18 点收窗两条即可）
- 写完跑 `.venv/bin/python scripts/health_check.py` 让明天起的日历事件立即生效

## 第 4 步：输出周报

对话里给一段人话周报：

1. **一句话结论**（在轨 / 停滞 / 需要注意 X）
2. 数字：体重变化、运动次数、睡眠概况
3. 做得好的一件事 + 下周期只改的一件事（不要一次提 5 个建议）
4. 计划是否有变，变了什么

## 边界

- 不是医生：症状持续加重一律建议就医，不做诊断
- 不因单周停滞就大改计划（脂肪丢失被水分掩盖很常见，两周再说）
- 周报只输出在对话里，不额外落盘（log 和 plan 已经是持久层）
