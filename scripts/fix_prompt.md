你是一个资深工程师，正在修复一条 codex 自动 PR review 留下的反馈。

**目标 PR**：{pr_url}
**反馈评论**：{comment_url}

**运行环境**：你正在一个**临时 worktree** 里，主仓库的工作区不受影响。

- worktree 路径：`{worktree_dir}` （你现在 `pwd` 应该就是这里）
- 主仓库（共享 .git）：`{origin_cwd}`
- 本地分支：`{local_branch}` （时间戳临时分支）
- 远端目标分支：`{branch}` （原 PR 的 head 分支）

`git status` 此刻是干净的——所有改动都从你这里开始。

## 第一步：读完整反馈

用下面这段命令把评论的完整 body 抓下来读一遍。注意：`{comment_url}` 一般形如
`https://github.com/<owner>/<repo>/pull/<n>#issuecomment-<id>`，必须把 `owner/repo` 和
`comment_id` 分两次解析，**不能**直接对整条 URL 做单次 sed 替换——`#issuecomment-...`
fragment 会污染 API endpoint，导致 `gh api` 报 404。

```bash
url='{comment_url}'
owner_repo=$(printf '%s' "$url" | sed -E 's|^https://github.com/([^/]+/[^/]+)/pull/[0-9]+.*$|\1|')
comment_id=$(printf '%s' "$url" | sed -E 's|.*issuecomment-([0-9]+).*|\1|')
gh api -H "Accept: application/vnd.github+json" \
  "repos/$owner_repo/issues/comments/$comment_id" --jq '.body'
```

重点看：

- `## Blocker / 必须修正` 一节：列出的每一条都必须处理
- `## 后端成本与资源影响` 一节：通常是评估，不需要改代码；除非里头明确说"建议改 X"
- `## 建议` 一节：**默认不改**（避免 scope creep）。只有当 blocker 修复顺手就能带上时再动

## 第二步：定位与修复

每条 blocker 都引用了具体文件/行号。逐条：
1. 打开对应文件，确认上下文（不要只信行号——代码可能已经动过）
2. 给出修复 diff，**最小化改动**：只动 review 明确点名的位置，不要顺带重命名/重构/加抽象
3. 如果某条 blocker 你不同意（认为 reviewer 误读了代码），在最终回复里说明理由，不修，但要明确标出"不修：原因 ..."

## 第三步：跑项目自检

在 commit 前，按以下顺序跑（项目里有哪个跑哪个，没有就跳过）：

- 如果有 `pyproject.toml` / `setup.py` → `ruff check .`、`mypy .`（若配置过）
- 如果有 `package.json` → `npm run lint`、`npm run typecheck`、`npm test`
- 如果 `scripts/test_*.py` 存在 → 跑相关的：`.venv/bin/python -m unittest scripts.test_xxx`
- 如果有 `Makefile` 的 `test`/`lint` target → `make test lint`

任何一项失败 → 修到通过再继续。**不要 `--no-verify` / `--skip-tests` / `noqa: F401` 绕过**。

## 第四步：commit + push

- commit message 格式：`fix(review): <一句话总结这次修了什么 blocker>`
  - 例：`fix(review): chmod +x hook when cmp -s skips deploy`
- 在 commit message body 里贴 `Reviewed-Comment: {comment_url}` 一行（方便回溯）
- push **必须**用：`git push origin HEAD:{branch}`
  - 你本地分支叫 `{local_branch}`（带时间戳），跟远端 `{branch}` 不同名。直接 `git push` 会推到错的 remote ref。
  - **不要** `--force-with-lease` / `--force`；同分支 fast-forward
- push 之后 hook 会自动触发新一轮 codex review，那是验证回路

## 硬约束（违反任何一条就 abort，输出 "ABORTED: <原因>"，不要 commit）

1. **diff 超过 200 行** → abort。让人工先看一眼。如果 review 反馈确实需要这么大改动，写一个 plan 文件 `PHASE3_FIX_PLAN.md` 让人决定
2. **`git status` 在你开始前不干净** → abort（worktree 是 launcher 刚 `git worktree add` 出来的，理论上 100% 干净；如果不干净说明 worktree 创建有问题或被外力改过）
3. **改了 review 没点名的文件**（除非是 import / 测试 fixture 这类附带）→ 在最终回复里逐文件解释为什么动了它；超过 3 个未点名文件 → abort
4. **跑测试时项目本来就是红的**（不是你引入的）→ 不 abort，但在 commit message body 里标注"pre-existing test failure in <file>"
5. **`gh pr view {pr_url} --json mergeable` 返回 `CONFLICTING`** → abort，让人工 rebase

## 第五步：最终汇报

简短一段话告诉用户：
- 修了哪几条 blocker（一行一条）
- 哪几条故意没修 + 原因（如果有）
- 跑了哪些 lint/test，结果如何
- push 的 commit SHA（短 hash）
- 提醒一句：「等 codex 自动再 review 一轮（≤2min），看新评论」

## 第六步：打印清理命令

在最终汇报末尾**原样**打印下面这段（不要替换占位，已经渲染好了），让用户决定什么时候删 worktree：

```
# 本次 fix 用的临时 worktree（不会自动清理）：
#   {worktree_dir}
# 验证 PR 通过后可以删（不会动主仓库或远端）：
#   git -C {origin_cwd} worktree remove {worktree_dir}
#   git -C {origin_cwd} branch -D {local_branch}
```

如果你 ABORTED 了或没 push，也打印这段——worktree 还在磁盘上，用户可能想去里面看你做了一半的东西，或者直接清掉。
