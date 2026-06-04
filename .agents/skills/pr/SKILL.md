---
name: pr
description: Lightweight GitHub PR shipping workflow that creates or updates a pull request for the current branch and immediately triggers my-calendar's PR review/calendar pipeline. Use when the user says "/pr", "create a PR", "open a PR", "light ship", "走 my-calendar 检查", "发个 PR 让日历 review", "别跑完整 ship", or otherwise wants a quick PR handoff instead of the full release /ship flow.
---

# PR

Create or update a GitHub PR, then hand it to my-calendar's `pr-created` hook so Codex review runs asynchronously and writes the result into the "PR 监控" calendar.

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

4. Run the helper from this skill directory.

   Resolve the path relative to the `SKILL.md` you loaded:

   ```bash
   bash scripts/light_pr.sh
   ```

   The helper will:
   - detect the repo root, current branch, GitHub repo, and default branch
   - push `HEAD` to `origin/<branch>` without force
   - reuse an existing PR for the branch or create one with `gh pr create --fill`
   - call my-calendar's `pr-created` hook with the PR URL and repo root

5. Report only the essentials.

   Return the PR URL, whether my-calendar was triggered, and any verification command that passed or was skipped.

## Helper Options

Use these only when the user or repo context calls for them:

```bash
bash scripts/light_pr.sh --base <branch>
bash scripts/light_pr.sh --draft
bash scripts/light_pr.sh --title "PR title"
bash scripts/light_pr.sh --body-file /path/to/body.md
bash scripts/light_pr.sh --allow-dirty
bash scripts/light_pr.sh --trigger-only <pr-url>
```

`--trigger-only` skips push and PR creation and just hands an existing GitHub PR URL to my-calendar.

Use `--allow-dirty` only after you have listed the remaining local changes and confirmed they are unrelated user-owned work that must not be included in this PR.

## Safety Rules

- Never force-push.
- Never use `--no-verify`.
- Never push from the base/default branch.
- Never target a non-default base unless the user explicitly asks.
- If tests fail, stop before pushing unless the user explicitly requested a WIP/draft PR.
- If using `--allow-dirty`, explicitly report which local changes were left out.
- If the my-calendar hook is missing, still report the PR URL, then tell the user to run `bash scripts/install_git_hook.sh` from their my-calendar checkout.

## Boundary With Other Skills

Use `/ship` for production/default-branch release promotion and heavyweight release checks. Use `/ship-dev` for a repository-specific direct-to-dev deployment workflow. Use this `pr` skill for the fast PR loop where my-calendar performs the asynchronous review and calendar handoff.
