---
name: pr
description: Lightweight GitHub PR shipping workflow that creates a new PR or updates an existing OPEN PR with a clear title/body, never reuses merged/closed PRs, then performs the PR review in the current agent session and records that review into my-calendar. Use when the user says "/pr", "create a PR", "open a PR", "light ship", "走 my-calendar 检查", "发个 PR 让日历 review", "别跑完整 ship", or otherwise wants a quick PR handoff instead of the full release /ship flow.
---

# PR

Create or update a GitHub PR with a clear title/body, then review it in this same agent session. After posting the GitHub PR comment, ask my-calendar to record that already-posted comment into the "PR 监控" calendar.

This is the light path. Do not run the full `/ship` ceremony: no version bump, no CHANGELOG, no TODOS sweep, no review army, no release promotion.

Why current-session review: Codex Desktop may not have Calendar permission and detached background Codex sessions are harder to surface back into the active thread. The helper reserves the PR SHA so launchd will not race the current review, while Terminal handles the short Calendar write if macOS TCC requires it.

## Workflow

1. Preflight the repo.

   ```bash
   git rev-parse --show-toplevel
   git branch --show-current
   git remote get-url origin
   git status --short
   gh auth status
   ```

   Abort if the current branch is the PR base/default branch, if the remote is not GitHub, or if `gh` is not authenticated.

2. Understand the diff and run a narrow verification.

   Use the repository's existing instructions first (`AGENTS.md`, `README`, package scripts, Makefile, CI config). Prefer the smallest check that reasonably covers the changed surface. Do not invent a release checklist.

   If no obvious check exists, say that and continue only if the change is documentation/config-only or the user explicitly asked for a quick PR.

3. Commit local changes if needed.

   Stage files deliberately. Do not use `git add -A` unless the repository explicitly allows it and the status is clean of generated/local files. Preserve any unrelated user changes.

   Follow the repo's commit style. If the commit is AI-authored and the repo has an AI coauthor convention, include the appropriate trailer.

4. Prepare the PR title and description.

   Always write an intentional PR title and description before invoking the helper. Do not rely on GitHub's default `--fill` text except as a last-resort fallback.

   Title:
   - Use the repository's commit/PR style when obvious.
   - Name the user-visible or operator-visible outcome, not just the edited file.
   - For fix PRs, prefer `fix(scope): concise problem/outcome`.

   Description must include:
   - `解决什么问题`: what broke, who saw it, and why this PR exists.
   - `实现方式`: the concrete technical approach and important tradeoffs.
   - `验证`: the focused checks that passed, or what was intentionally skipped and why.

   Keep it concise, but make it useful to a reviewer opening the PR cold.

5. Run the helper from this skill directory.

   Resolve the path relative to the `SKILL.md` you loaded:

   ```bash
   SKILL_DIR=/path/to/pr
   bash "$SKILL_DIR/scripts/light_pr.sh" --title "fix(scope): clear outcome" --body-file /path/to/pr-body.md
   ```

   The helper will:
   - detect the repo root, current branch, GitHub repo, and default branch
   - push `HEAD` to `origin/<branch>` without force, setting `MY_CALENDAR_PR_SKIP_PRE_PUSH_REVIEW=1` only for that push so the global pre-push hook does not start a detached background review
   - inspect existing PRs for the current branch and target base
   - reuse only an existing `OPEN` PR, then update its title and description
   - create a new PR when the previous PR for that branch is `MERGED` or `CLOSED`
   - mark the current PR SHA as `pending_review_source=current-session` in my-calendar state, which prevents the 10-minute launchd fallback from racing this session

6. Review the PR in the current session.

   Fetch the final PR metadata and diff after the helper returns:

   ```bash
   gh pr view <pr-url> --json url,number,title,baseRefName,headRefName,headRefOid
   gh pr diff <pr-url>
   ```

   Treat this as a code review: prioritize bugs, behavioral regressions, missing tests, security/data risks, and operational hazards. Keep style nits out unless they hide a real defect. If the diff is too large for a reliable review, say so in the comment and use the `❌ 暂不可合并` conclusion.

   Before posting, re-read `headRefOid`; the hidden SHA marker must match the current head.

7. Post the review comment.

   The comment body must start exactly with the AI attribution block and current head SHA marker:

   ```markdown
   > 🤖 由 Codex 自动生成
   <!-- ai-coauthor: codex; agent: pr_watcher; mode: automated -->
   <!-- pr-watcher-head-sha: <head_sha> -->

   ```

   Then write concise findings. End with exactly one conclusion line using one of:

   ```markdown
   结论：✅ 可以合并
   结论：⚠️ 修正后可合并
   结论：❌ 暂不可合并
   ```

   Post with `gh pr comment <pr-url> --body-file <comment-file>`, then capture the returned or latest comment URL.

8. Record the posted comment into my-calendar.

   From the my-calendar checkout, run:

   ```bash
   bash scripts/pr_record_review_trigger.sh <pr-url> <comment-url> <repo-root>
   ```

   If the current app lacks Calendar permission, this trigger opens a short Terminal `.command`, waits for its status file, and prints `MY_CALENDAR_RECORD=terminal-bridge:success` on success. The recorder validates that the comment contains the canonical AI marker and the current `head_sha`, then writes Calendar/state and clears the current-session pending marker.

9. Report only the essentials.

   Return the PR URL, the review verdict, whether my-calendar recording succeeded, and any verification command that passed or was skipped.

## Helper Options

Use these only when the user or repo context calls for them:

```bash
bash "$SKILL_DIR/scripts/light_pr.sh" --base <branch>
bash "$SKILL_DIR/scripts/light_pr.sh" --draft
bash "$SKILL_DIR/scripts/light_pr.sh" --title "PR title"
bash "$SKILL_DIR/scripts/light_pr.sh" --body-file /path/to/body.md
bash "$SKILL_DIR/scripts/light_pr.sh" --allow-dirty
bash "$SKILL_DIR/scripts/light_pr.sh" --trigger-async-review
bash "$SKILL_DIR/scripts/light_pr.sh" --trigger-only <pr-url>
```

`--trigger-async-review` keeps the old behavior: after PR create/update, call my-calendar's `pr-created` hook so detached `pr_watcher.py --force` launches a background Codex review.

`--trigger-only` skips push and PR creation and just hands an existing GitHub PR URL to the old asynchronous my-calendar path.

Use `--allow-dirty` only after you have listed the remaining local changes and confirmed they are unrelated user-owned work that must not be included in this PR.

If you are supplementing an existing PR, first inspect that PR's state. If it is `OPEN`, continue on the same branch, append commits, and let the helper update the PR title/body. If it is `MERGED` or `CLOSED`, use a fresh branch or allow the helper to create a fresh PR for the current branch; do not treat a non-open PR as the active handoff.

## Safety Rules

- Never force-push.
- Never use `--no-verify`.
- Never push from the base/default branch.
- Never target a non-default base unless the user explicitly asks.
- If tests fail, stop before pushing unless the user explicitly requested a WIP/draft PR.
- If using `--allow-dirty`, explicitly report which local changes were left out.
- Never reuse a merged or closed PR as the current handoff. New code after a merged/closed PR needs a new open PR.
- For an open existing PR, update the PR title and description to match the final branch contents before reviewing.
- If the my-calendar hook is missing, still report the PR URL, then tell the user to run `bash scripts/install_git_hook.sh` from their my-calendar checkout.

## Boundary With Other Skills

Use `/ship` for production/default-branch release promotion and heavyweight release checks. Use `/ship-dev` for a repository-specific direct-to-dev deployment workflow. Use this `pr` skill for the fast PR loop where the active agent session performs the review and my-calendar records the result.
