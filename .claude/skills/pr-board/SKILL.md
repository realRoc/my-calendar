---
name: pr-board
description: 打开本地 PR review 看板。生成一个单文件 HTML，把 pr_logs/ 里所有 codex review 记录按 repo / PR / 时间线三个维度展示，自带日期范围和关键字过滤。无后台进程，每次调用即重新生成。当用户说"打开 review 看板"、"看 PR review 历史"、"PR dashboard"、"/pr-board"、"看看最近 review 了什么 PR"、"列一下最近的 codex 评论" 等表达时调用。
---

# pr-board

本地、按需生成、无服务器的 PR review 看板。复用 `scripts/pr_logs/` 下已有数据，不引入新存储。

## 何时调用

当用户表达"想看 review 历史"时：
- "打开 PR review 看板" / "看 review 看板" / "PR dashboard"
- "/pr-board"
- "最近 review 了哪些 PR" / "列一下最近的 codex 评论"
- "本周给哪些 PR 写了 review"

## 步骤

1. 默认行为：生成并打开看板

   ```bash
   # 从 my-calendar repo 根目录运行。如果当前不在那里，
   # 调用方需要先 cd 过去（路径因机器而异，不要写死）。
   .venv/bin/python scripts/dashboard.py --open
   ```

   这条命令会：
   - 扫描 `scripts/pr_logs/` 下所有 `*.meta.json` / `*.last.txt`
   - 把每条 review 装进 `reviews[]` 数组（含 repo / pr_number / pr_url / timestamp / sha / comment_url / comment_body / thread_id / codex_exit）
   - 写出 `scripts/pr_logs/pr-dashboard.html`（单文件，所有数据 + UI 都在里面）
   - `open` 命令用默认浏览器打开

2. 只生成不打开：去掉 `--open` 即可

3. dry-run（不写文件，仅打印统计）：

   ```bash
   .venv/bin/python scripts/dashboard.py --dry-run
   ```

## 看板用法

- **三个视图 tab**：按 Repo / 按 PR / 时间线
- **日期范围过滤器**：默认"近 30 天"，可切到 7 / 90 / 全部
- **repo 下拉**：只看某个 repo
- **搜索框**：在标题和评论内容里模糊搜
- 点击某条 review → 展开 / 收起评论原文；右上角链接直跳 GitHub 评论或 PR

## 数据来源

| 字段 | 来源 |
|---|---|
| timestamp / repo / pr_number / pr_url | sidecar `.meta.json`（pr_watcher 落盘时写）；老 review 从文件名解析 |
| pr_title / head_sha / thread_id / comment_url / codex_exit | sidecar `.meta.json` 独有；老 review 显示空或 `no-meta` badge |
| comment_body | sidecar 优先；缺则读 `.last.txt` 兜底 |

老 review（sidecar 上线前生成的）会显示 `no-meta` badge，但仍然能看到 timestamp / repo / PR 号和 comment_body 兜底文本。

## 故障排查

- 看板里啥都没有：`ls scripts/pr_logs/` 应该有 `*.last.txt` 或 `*.meta.json` 文件；如果是新装的项目，要先有 review 跑过才会有数据
- 浏览器没自动打开：手动 `open scripts/pr_logs/pr-dashboard.html`
- 想强制刷新：直接重跑 `scripts/dashboard.py --open`，每次都是完整重新生成
