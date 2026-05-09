---
name: add-person
description: 新增或更新一个人物档案。当用户说"加一个家人"、"新增朋友档案"、"记一下 X 的偏好/尺码/过敏"、"X 喜欢/讨厌 Y"等表达时调用。负责写 people/<id>.md，遵循 AGENTS.md 中的 person frontmatter schema。
---

# add-person

用户想新增人物，或往已有档案补充信息。**不要直接手写**——按流程走。

## 步骤

### 1. 判断是新增还是更新

```bash
ls people/
```

- 如果用户提到的人已经有档案 → 走"更新分支"
- 否则走"新增分支"

### 2. 新增分支

问清字段（一次性问完）：

- **称呼**（必须）：你平时怎么叫 ta（如"妈妈"、"老张"）
- **关系**（可选）：mother / father / friend / colleague / spouse / sibling / ...
- **生日**（可选）：阳历还是阴历 + 月日
- **偏好**：喜欢的东西/口味/活动
- **过敏/忌讳**：食物、花、香味等
- **尺码/型号**：衣服、鞋、戒指等
- **其他备注**

#### 推导 person-id

- 家庭关系直接用英文：`mom` / `dad` / `wife` / `husband` / `bro` / `sis`
- 朋友/同事用拼音：`zhang-san` / `li-si`
- 多个同名时加后缀：`zhang-san-college`、`zhang-san-work`

#### 写文件

路径 `people/<id>.md`，frontmatter 严格按 schema（缺的字段直接省略，不要留空 `null`）：

```yaml
---
id: <id>
name: <称呼>
relation: <关系>            # 可选
birthday:                   # 可选
  type: solar | lunar
  month: <1-12>
  day: <1-31>
preferences: |
  <一段或多段文本>
allergies: |
  <...>
sizes: |
  <...>
notes: |
  <...>
---
```

### 3. 更新分支

读现有 `people/<id>.md`，把新信息**合并**进对应字段：

- 偏好/过敏/尺码这类长文本：在原文末尾追加新行（带日期前缀更好）
- 生日这类结构化字段：如果用户提供新值且与旧值不同，**确认覆盖**而非静默替换
- 全程不要丢失原有信息

### 4. 如果还涉及生日 → 顺便提议建一个 holiday

如果用户提供了生日：

> 要不要顺便建一个 `<id>-birthday` 的自定义节日，这样每年提前会被提醒？

得到肯定 → 调用 `add-holiday` skill（或直接按其规则写 `holidays/custom/<id>-birthday.md`），日期类型按 birthday.type，`default_people: [<id>]`。

### 5. 反馈

告诉用户：
- 写到/更新了哪个路径
- 摘要列出现在档案里有哪些信息（让用户校对）
