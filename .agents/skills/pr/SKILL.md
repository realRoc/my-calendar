---
name: pr
description: Lightweight GitHub PR shipping workflow that creates a new PR or updates an existing OPEN PR with a clear title/body, never reuses merged/closed PRs, and immediately triggers my-calendar's PR review/calendar pipeline. Use when the user says "/pr", "create a PR", "open a PR", "light ship", "走 my-calendar 检查", "发个 PR 让日历 review", "别跑完整 ship", or otherwise wants a quick PR handoff instead of the full release /ship flow.
---

# PR

Create or update a GitHub PR with a clear title/body, then immediately hand it to my-calendar's `pr-created` hook so Codex review runs asynchronously and writes the result into the "PR 监控" calendar.

This is the light path. Do not run the full `/ship` ceremony: no version bump, no CHANGELOG, no TODOS sweep, no review army, no release promotion.

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
   - push `HEAD` to `origin/<branch>` without force
   - inspect existing PRs for the current branch and target base
   - reuse only an existing `OPEN` PR, then update its title and description
   - create a new PR when the previous PR for that branch is `MERGED` or `CLOSED`
   - call my-calendar's `pr-created` hook with the PR URL and repo root immediately after the PR is created or updated

6. Report only the essentials.

   Return the PR URL, whether my-calendar was triggered, and any verification command that passed or was skipped.

## Helper Options

Use these only when the user or repo context calls for them:

```bash
bash "$SKILL_DIR/scripts/light_pr.sh" --base <branch>
bash "$SKILL_DIR/scripts/light_pr.sh" --draft
bash "$SKILL_DIR/scripts/light_pr.sh" --title "PR title"
bash "$SKILL_DIR/scripts/light_pr.sh" --body-file /path/to/body.md
bash "$SKILL_DIR/scripts/light_pr.sh" --allow-dirty
bash "$SKILL_DIR/scripts/light_pr.sh" --trigger-only <pr-url>
```

`--trigger-only` skips push and PR creation and just hands an existing GitHub PR URL to my-calendar.

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
- For an open existing PR, update the PR title and description to match the final branch contents before triggering my-calendar.
- If the my-calendar hook is missing, still report the PR URL, then tell the user to run `bash scripts/install_git_hook.sh` from their my-calendar checkout.

## Boundary With Other Skills

Use `/ship` for production/default-branch release promotion and heavyweight release checks. Use `/ship-dev` for a repository-specific direct-to-dev deployment workflow. Use this `pr` skill for the fast PR loop where my-calendar performs the asynchronous review and calendar handoff.
