---
name: pr-board
description: 在终端里显示当前正在运行的 Codex PR review 任务列表，输出 Codex app 可解析的可展开 Markdown 组件，支持点击跳转 PR。当用户说"打开 review 看板"、"看当前 PR review 任务"、"PR dashboard"、"/pr-board"、"看看现在 review 到哪了"、"列一下正在跑的 codex review" 等表达时调用。
---

# pr-board

本地、按需查询、无服务器的 PR review 运行中看板。复用 `scripts/pr_logs/` 下已有 `.running` sidecar，不引入新存储。

## 何时调用

当用户表达"想看当前 review 任务"时：
- "打开 PR review 看板" / "看 review 看板" / "PR dashboard"
- "/pr-board"
- "现在有哪些 PR review 在跑" / "列一下正在跑的 codex review"
- "看看当前 review 到哪了"

## 步骤

1. 默认行为：在终端显示当前运行中的任务

   ```bash
   # 从 my-calendar repo 根目录运行。如果当前不在那里，
   # 调用方需要先 cd 过去（路径因机器而异，不要写死）。
   .venv/bin/python scripts/dashboard.py
   ```

   这条命令会：
   - 扫描 `scripts/pr_logs/` 下所有 `*.running`
   - 过滤已完成或超过 30 分钟的陈旧 sidecar
   - 在当前终端输出 Codex app 可渲染的 Markdown：
     - 标题区显示当前运行中任务数量
     - 每个任务是 `<details>` 可展开块
     - summary 展示 repo / PR / 当前运行时间 / 最近活动
     - PR 链接可直接点击跳转 GitHub
     - 展开后可看标题、开始时间、JSONL 路径、输出大小、SHA

2. 旧 HTML 历史看板仍可手动生成：

   ```bash
   .venv/bin/python scripts/dashboard.py --html
   ```

3. 需要打开旧 HTML 历史看板时：

   ```bash
   .venv/bin/python scripts/dashboard.py --open
   ```

4. dry-run（不写文件，仅打印统计）：

   ```bash
   .venv/bin/python scripts/dashboard.py --dry-run
   ```

## 终端组件用法

- 直接看 summary：repo / PR / 当前运行时间 / 最近活动
- 点击 PR 链接：跳到对应 GitHub PR
- 展开某条任务：看标题、开始时间、JSONL 路径、输出大小、SHA
- 没有运行中任务时，输出"当前没有正在运行的 Codex PR review"

## 数据来源

| 字段 | 来源 |
|---|---|
| repo / pr_number / pr_url / pr_title / head_sha | `.running` sidecar（pr_watcher.run_codex 启动时写） |
| started_at / timestamp | `.running` sidecar；缺失时从文件名兜底 |
| last_active / jsonl_size | sibling `.jsonl` 的 mtime / size |

## 故障排查

- 看板里没有任务：通常表示当前没有 Codex PR review 在跑；`ls scripts/pr_logs/*.running` 可确认
- 任务刚结束但还显示：重跑命令即可；超过 30 分钟的陈旧 `.running` 会被自动隐藏
- 想强制刷新：直接重跑命令，每次都是完整重新生成
- 提示找不到 .venv/bin/python：去 repo 根目录跑 `python3 -m venv .venv && .venv/bin/pip install -r requirements.txt`（项目首次部署时已经做过）
