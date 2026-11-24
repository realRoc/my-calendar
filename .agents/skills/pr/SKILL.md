---
name: pr
description: Create or update a lightweight GitHub PR and get exactly one cross-model code review from the my-calendar watcher — Claude-written PRs are reviewed by Codex CLI (gpt-6.2-astra), Codex-written PRs by Claude Code (claude-opus-5-5), each on its subscription first and the Teamorouter API only as fallback. Use for /pr, create/open/update PR, quick PR handoff, rerun code review, or my-calendar review requests.
---

# PR

Create or update a GitHub PR, hand the review to the my-calendar PR watcher, wait for its one comment on the current head SHA, and report it. Never merge the PR.

Do not run `/ship`: no version bump, changelog, release promotion, or review army.

## Who reviews (cross-model)

The watcher is the **only** publisher. It decides who wrote the PR and sends it to the other family:

| PR written by | How it is detected | Reviewer |
| --- | --- | --- |
| Claude Code | branch `claude/…`, or `Co-Authored-By: Claude` / `Generated with [Claude Code]` | Codex CLI, model `review_routing.reviewers.codex.model` (default `gpt-6.2-astra`) |
| Codex | branch `codex/…`, or `Co-authored-by: Codex` | Claude Code, model `review_routing.reviewers.claude.model` (default `claude-opus-5-5`) |
| unknown | neither | `review_routing.reviewer_for_author.unknown` (default Claude) |

- Each reviewer runs on its subscription first (ChatGPT login for Codex, claude.ai login for Claude Code). Only if that run fails does it retry on the API (Teamorouter: the token in `~/.claude/settings.json`, or `api_key_env` / `api_key_file`).
- Model ids and auth order live in `~/.config/my-calendar/config.json` under `review_routing`; a model upgrade is a config edit.
- If the cross-family reviewer fails on both subscription and API, the SHA is retried later; it is not handed to the family that wrote it (`same_family_fallback: false`).
- The comment starts with a `**评审模型**` line naming the model, the account it ran on, and the code author.

Do **not** run `review_with_opus.sh` + `review_and_post.sh` yourself for a PR the watcher handles: that is a second reviewer racing the watcher for the same SHA, and one of them is refused.

## Severity policy (Claude reviewer)

- `P0`: concrete reachable defect with severe production impact: exploitable security/permission failure, material data loss/corruption, widespread outage, or critically wrong financial/business results. Any P0 blocks merge.
- `P1`: real low-impact bug that is safe to merge and may be fixed later. P1 never blocks merge.
- Do not report P2/P3, style nits, speculative hardening, rare edge cases, or missing tests unless they directly prove a P0.

The deterministic renderer emits exactly one final line:

```text
结论：✅ 可以合并
```

or, only when a P0 exists:

```text
结论：❌ 暂不可合并
```

## 1. Preflight

```bash
git rev-parse --show-toplevel
git branch --show-current
git remote get-url origin
git status --short
gh auth status
```

Confirm the PR is `OPEN` (review-only mode). In handoff mode, abort on the default/base branch, a non-GitHub origin, failed tests, or uncommitted changes that are not explicitly allowed. In review-only mode, do not switch/push/edit the user's branch or PR metadata.

## 2. Understand and verify

Read repository instructions and the changed diff. Run the smallest existing check that covers the changed surface. Do not invent a release checklist.

In handoff mode, commit only intended files and prepare an intentional title and body containing:

- `解决什么问题`
- `实现方式`
- `验证`

Claude Code sessions keep the `claude/` branch prefix and the `Co-Authored-By: Claude …` trailer; Codex sessions keep `codex/`. That is how the watcher picks the reviewer.

## 3. Create the PR and trigger the watcher

Handoff mode:

```bash
bash "$SKILL_DIR/scripts/light_pr.sh" \
  --title "fix(scope): clear outcome" \
  --body-file /path/to/pr-body.md \
  --trigger-async-review
```

The helper pushes without force (the my-calendar pre-push hook starts the watcher), creates or updates only an `OPEN` PR to the default branch, and fires the pr-created trigger. Never reuse a merged/closed PR. Later commits: a plain `git push` re-triggers the review for the new head.

Review-only mode (existing PR):

```bash
bash "$SKILL_DIR/scripts/light_pr.sh" --trigger-only <pr-url>
```

## 4. Wait for the review

```bash
bash "$SKILL_DIR/scripts/wait_for_review.sh" --pr-url <pr-url>
```

Run it in the background; a review takes roughly 5–30 minutes. It prints `REVIEW_HEAD_SHA`, `REVIEW_COMMENT_URL`, `REVIEW_REVIEWER` and `REVIEW_VERDICT` once the watcher's comment for the current head exists, and follows a new head if one is pushed meanwhile. Exit 3 = timeout: read `~/Desktop/my-calendar/logs/pr-watcher.log`, then retrigger with `--trigger-only`.

## 5. Report

Return only the PR URL, the reviewer line, the verdict, the comment URL, and the focused verification you ran. Do not merge.

## Safety

- Never force-push or use `--no-verify`.
- Never post a review for a stale SHA.
- Never target a non-default base unless explicitly requested.
- Never reuse merged/closed PRs.
- Preserve unrelated local changes.
- Never post a review comment yourself; the watcher publishes exactly one per head SHA.
- If the watcher is not installed or the review keeps failing, report that to the user instead of reviewing with the same model family that wrote the code.
