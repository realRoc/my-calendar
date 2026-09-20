---
name: pr
description: Create or update a lightweight GitHub PR, review an open PR with Claude Opus 5 through Teamorouter, post a deterministic code-review comment, and record it in my-calendar. Use for /pr, create/open/update PR, quick PR handoff, rerun code review, or my-calendar review requests.
---

# PR

Create or update a GitHub PR, delegate review to `claude-opus-5` through Teamorouter, post the validated result, and record the comment in my-calendar. Never merge the PR.

Do not run `/ship`: no version bump, changelog, release promotion, or review army.

## Reliability contract

- Use `scripts/review_with_opus.sh`; never ask the current model to replace Opus.
- Opus returns JSON-Schema-validated findings only. `scripts/render_review.py` renders the publishable Markdown and derives the conclusion locally.
- Ignore Claude's free-form `result`; only `structured_output` may reach the renderer. This prevents analysis drafts and format instructions from leaking into GitHub.
- Retry invalid/API responses up to three fresh, non-persistent Opus calls.
- Use `scripts/review_and_post.sh` for posting. Do not construct or submit the GitHub comment manually.
- The posting helper rechecks the PR SHA, uses canonical markers, reuses an existing same-SHA comment, verifies the posted body, and records it in my-calendar.
- In review-only mode, claim my-calendar only after a valid review exists. A pre-comment failure releases the current-session claim automatically.
- Never delete or rewrite an existing review comment.

## Severity policy

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

## 1. Select mode and preflight

Use handoff mode when creating/updating a PR from the current branch. Use review-only mode when reviewing an existing PR URL without changing its branch or metadata.

Run:

```bash
git rev-parse --show-toplevel
git branch --show-current
git remote get-url origin
git status --short
gh auth status
bash "$SKILL_DIR/scripts/review_with_opus.sh" --check-config
```

Confirm the PR is `OPEN`. In handoff mode, abort on the default/base branch, a non-GitHub origin, failed tests, or uncommitted changes that are not explicitly allowed. In review-only mode, do not switch/push/edit the user's branch or PR metadata.

## 2. Understand and verify

Read repository instructions and the changed diff. Run the smallest existing check that covers the changed surface. Do not invent a release checklist.

In handoff mode, commit only intended files and prepare an intentional title and body containing:

- `解决什么问题`
- `实现方式`
- `验证`

## 3. Create or reserve the PR

Handoff mode:

```bash
bash "$SKILL_DIR/scripts/light_pr.sh" \
  --title "fix(scope): clear outcome" \
  --body-file /path/to/pr-body.md
```

The helper pushes without force, creates or updates only an `OPEN` PR to the default branch, and reserves its current SHA in my-calendar. Never reuse a merged/closed PR.

Review-only mode: do not claim yet. Review first so a malformed/provider failure cannot leave a stale pending marker.

## 4. Generate and inspect the review

```bash
REVIEW_FILE="$(mktemp "${TMPDIR:-/tmp}/pr-review.XXXXXX")"
REVIEW_STATUS="$(bash "$SKILL_DIR/scripts/review_with_opus.sh" \
  --pr-url <pr-url> \
  --output "$REVIEW_FILE")"
printf '%s\n' "$REVIEW_STATUS"
REVIEW_HEAD_SHA="$(awk -F= '$1 == "REVIEW_HEAD_SHA" { print $2 }' <<<"$REVIEW_STATUS" | tail -1)"
```

Require all of:

- `REVIEW_MODEL=claude-opus-5`
- `REVIEW_PROVIDER=teamorouter`
- `REVIEW_TRANSPORT=json-schema`
- `REVIEW_GENERATION=ok`
- one final canonical conclusion line

Inspect each finding against the visible diff. If a finding is materially unsupported, write factual feedback and run exactly one replacement review:

```bash
FEEDBACK_FILE="$(mktemp "${TMPDIR:-/tmp}/pr-review-feedback.XXXXXX")"
CORRECTED_FILE="$(mktemp "${TMPDIR:-/tmp}/pr-review-corrected.XXXXXX")"
bash "$SKILL_DIR/scripts/review_with_opus.sh" \
  --pr-url <pr-url> \
  --output "$CORRECTED_FILE" \
  --validation-feedback-file "$FEEDBACK_FILE"
```

Use the corrected file unchanged when it validates. Never manually edit, delete, reorder, or change Opus findings or verdicts.

## 5. Post, verify, and record

Review-only mode:

```bash
bash "$SKILL_DIR/scripts/review_and_post.sh" \
  --pr-url <pr-url> \
  --repo-root <repo-root> \
  --review-file "$REVIEW_FILE" \
  --review-head-sha "$REVIEW_HEAD_SHA"
```

Handoff mode passes the claim created by `light_pr.sh`:

```bash
bash "$SKILL_DIR/scripts/review_and_post.sh" \
  --pr-url <pr-url> \
  --repo-root <repo-root> \
  --review-file "$REVIEW_FILE" \
  --review-head-sha "$REVIEW_HEAD_SHA" \
  --already-claimed
```

Require:

- `GITHUB_COMMENT=posted` or `GITHUB_COMMENT=reused`
- a canonical `REVIEW_COMMENT_URL`
- `MY_CALENDAR_RECORD=...success`
- `REVIEW_AND_POST=ok`

If GitHub returns an ambiguous failure, the helper queries the canonical SHA marker before retrying, preventing duplicate comments. If posting fails before a comment exists, it safely releases only the matching `current-session` claim.

## 6. Report

Return only the PR URL, review verdict, comment URL, my-calendar result, and focused verification. Do not merge.

## Safety

- Never force-push or use `--no-verify`.
- Never post a review for a stale SHA.
- Never target a non-default base unless explicitly requested.
- Never reuse merged/closed PRs.
- Preserve unrelated local changes.
- If my-calendar is unavailable, keep the verified GitHub comment and report the recording failure; do not post a duplicate.
