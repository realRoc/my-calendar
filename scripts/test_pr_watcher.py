"""Regression tests for scripts/pr_watcher.py.

Run with:
    .venv/bin/python scripts/test_pr_watcher.py
or:
    .venv/bin/python -m unittest scripts.test_pr_watcher
"""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import pr_watcher  # noqa: E402


class ForceOriginCwdTests(unittest.TestCase):
    def test_already_reviewed_force_backfills_state_and_meta_origin_cwd(self):
        pr_url = "https://github.com/realRoc/my-calendar/pull/10"
        head_sha = "abc123456789"

        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            origin = tmp / "checkout"
            origin.mkdir()

            jsonl_path = tmp / "20260522-120000__realRoc_my-calendar_pull_10.jsonl"
            jsonl_path.write_text("{}\n", encoding="utf-8")
            meta_path = jsonl_path.with_suffix(".meta.json")
            meta_path.write_text(
                json.dumps({
                    "pr_url": pr_url,
                    "head_sha": head_sha,
                    "origin_cwd": "/old/checkout",
                }),
                encoding="utf-8",
            )

            state = {
                "_meta": {"installed_at": "2026-05-22T00:00:00+00:00"},
                "prs": {
                    pr_url: {
                        "repo": "realRoc/my-calendar",
                        "number": 10,
                        "last_commented_sha": head_sha,
                        "last_jsonl": str(jsonl_path),
                    },
                },
            }
            saved: list[dict] = []

            def fake_run(*_args, **_kwargs):
                return subprocess.CompletedProcess(
                    args=[],
                    returncode=0,
                    stdout=json.dumps({
                        "url": pr_url,
                        "number": 10,
                        "title": "origin cwd plumbing",
                        "isDraft": False,
                        "baseRefName": "main",
                        "headRefOid": head_sha,
                        "createdAt": "2026-05-22T00:00:00Z",
                    }),
                    stderr="",
                )

            argv = [
                "pr_watcher.py",
                "--force",
                pr_url,
                "--origin-cwd",
                str(origin),
            ]
            with mock.patch.object(sys, "argv", argv), \
                    mock.patch.object(pr_watcher, "redirect_stdio_to_log", lambda: None), \
                    mock.patch.object(pr_watcher, "load_state", lambda: state), \
                    mock.patch.object(pr_watcher, "save_state", lambda s: saved.append(s)), \
                    mock.patch.object(pr_watcher, "acquire_lock", lambda blocking: 999), \
                    mock.patch.object(pr_watcher.subprocess, "run", fake_run):
                rc = pr_watcher.main()

            self.assertEqual(rc, 0)
            self.assertEqual(saved, [state])
            expected_cwd = str(origin.resolve())
            self.assertEqual(state["prs"][pr_url]["origin_cwd"], expected_cwd)
            self.assertEqual(
                json.loads(meta_path.read_text(encoding="utf-8"))["origin_cwd"],
                expected_cwd,
            )


class ForceOriginCwdFreshRunTests(unittest.TestCase):
    """Fresh --force --origin-cwd run: PR has never been reviewed, so process_pr
    actually executes (mocked) and must persist origin_cwd into both pr_state.json
    AND the freshly-written .meta.json sidecar.

    Complements the already-reviewed backfill test above, which only exercises
    the early-return branch."""

    def test_fresh_force_origin_cwd_persists_to_state_and_sidecar(self):
        pr_url = "https://github.com/realRoc/my-calendar/pull/10"
        head_sha = "fffeeeddd111"

        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            origin = tmp / "checkout"
            origin.mkdir()

            jsonl_path = tmp / "20260522-130000__realRoc_my-calendar_pull_10.jsonl"
            jsonl_path.write_text("{}\n", encoding="utf-8")
            meta_path = jsonl_path.with_suffix(".meta.json")

            state = {
                "_meta": {"installed_at": "2026-05-22T00:00:00+00:00"},
                "prs": {},
            }
            saved: list[dict] = []

            def fake_run(*_args, **_kwargs):
                return subprocess.CompletedProcess(
                    args=[],
                    returncode=0,
                    stdout=json.dumps({
                        "url": pr_url,
                        "number": 10,
                        "title": "origin cwd plumbing",
                        "isDraft": False,
                        "baseRefName": "main",
                        "headRefOid": head_sha,
                        "createdAt": "2026-05-22T00:00:00Z",
                    }),
                    stderr="",
                )

            fake_codex_result = pr_watcher.CodexResult(
                thread_id="thread-xyz",
                last_message="ok",
                exit_code=0,
                jsonl_path=jsonl_path,
                scratch_dir=tmp / "scratch",
            )

            argv = [
                "pr_watcher.py",
                "--force",
                pr_url,
                "--origin-cwd",
                str(origin),
            ]
            with mock.patch.object(sys, "argv", argv), \
                    mock.patch.object(pr_watcher, "redirect_stdio_to_log", lambda: None), \
                    mock.patch.object(pr_watcher, "load_state", lambda: state), \
                    mock.patch.object(pr_watcher, "save_state", lambda s: saved.append(s)), \
                    mock.patch.object(pr_watcher, "acquire_lock", lambda blocking: 999), \
                    mock.patch.object(pr_watcher.subprocess, "run", fake_run), \
                    mock.patch.object(pr_watcher, "notify", lambda *a, **kw: None), \
                    mock.patch.object(pr_watcher, "run_codex", lambda prompt, pr: fake_codex_result), \
                    mock.patch.object(pr_watcher, "fetch_latest_comment", lambda repo, n: ("https://example/c/1", "verdict: pass")), \
                    mock.patch.object(pr_watcher, "upsert_events", lambda events, *a, **kw: {events[0].key: "created"}), \
                    mock.patch.object(pr_watcher, "_refresh_dashboard", lambda *, reason: None):
                rc = pr_watcher.main()

            self.assertEqual(rc, 0)
            expected_cwd = str(origin.resolve())

            # State updated by --force path before process_pr runs, then by
            # process_pr itself; final saved state must carry origin_cwd.
            self.assertEqual(state["prs"][pr_url]["origin_cwd"], expected_cwd)
            self.assertEqual(state["prs"][pr_url]["last_commented_sha"], head_sha)

            # Sidecar written by process_pr should mirror origin_cwd.
            self.assertTrue(meta_path.exists(), "process_pr should have written sidecar")
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
            self.assertEqual(meta["origin_cwd"], expected_cwd)
            self.assertEqual(meta["head_sha"], head_sha)


class OriginCwdWithoutForceTests(unittest.TestCase):
    """Manual-debugging guard: passing --origin-cwd without --force used to
    silently drop the value. Verify we now emit a stderr warning so the user
    notices."""

    def test_origin_cwd_without_force_emits_warning(self):
        import io

        argv = [
            "pr_watcher.py",
            "--dry-run",
            "--origin-cwd",
            "/tmp/whatever",
        ]
        captured = io.StringIO()
        # --dry-run path returns quickly without touching gh/codex; we just want
        # to reach the early warning before any real work.
        with mock.patch.object(sys, "argv", argv), \
                mock.patch.object(pr_watcher, "redirect_stdio_to_log", lambda: None), \
                mock.patch.object(pr_watcher, "load_state", lambda: {"_meta": {}, "prs": {}}), \
                mock.patch.object(pr_watcher, "fetch_open_prs", lambda: []), \
                mock.patch.object(sys, "stderr", captured):
            rc = pr_watcher.main()

        self.assertEqual(rc, 0)
        self.assertIn("--origin-cwd", captured.getvalue())
        self.assertIn("ignored without --force", captured.getvalue())


class BuildFixUrlTests(unittest.TestCase):
    """The mycalfix:// URL must encode the four required fields and optionally
    origin_cwd. Missing head_branch or comment_url → no URL (launcher can't act)."""

    def _pr(self, **overrides) -> pr_watcher.PRSnap:
        defaults = dict(
            url="https://github.com/realRoc/my-calendar/pull/11",
            number=11,
            title="Phase 3",
            is_draft=False,
            repo="realRoc/my-calendar",
            base="main",
            default_branch="main",
            head_sha="abc12345",
            head_branch="phase3-fix-launcher",
        )
        defaults.update(overrides)
        return pr_watcher.PRSnap(**defaults)

    def test_returns_url_with_origin_cwd_encoded(self):
        url = pr_watcher._build_fix_url(
            pr=self._pr(),
            comment_url="https://github.com/realRoc/my-calendar/pull/11#issuecomment-1",
            origin_cwd="/Users/me/Desktop/my calendar",
        )
        self.assertIsNotNone(url)
        self.assertTrue(url.startswith("mycalfix://fix?"))
        # spaces and slashes in origin_cwd are percent-encoded
        self.assertIn("origin_cwd=%2FUsers%2Fme%2FDesktop%2Fmy%20calendar", url)
        self.assertIn("branch=phase3-fix-launcher", url)
        self.assertIn("repo=realRoc%2Fmy-calendar", url)

    def test_returns_url_without_origin_cwd(self):
        url = pr_watcher._build_fix_url(
            pr=self._pr(),
            comment_url="https://example.com/c/1",
            origin_cwd=None,
        )
        self.assertIsNotNone(url)
        self.assertNotIn("origin_cwd=", url)

    def test_no_url_without_head_branch(self):
        url = pr_watcher._build_fix_url(
            pr=self._pr(head_branch=""),
            comment_url="https://example.com/c/1",
            origin_cwd="/x",
        )
        self.assertIsNone(url)

    def test_no_url_without_comment_url(self):
        url = pr_watcher._build_fix_url(
            pr=self._pr(),
            comment_url=None,
            origin_cwd="/x",
        )
        self.assertIsNone(url)


class BuildEventVerdictRoutingTests(unittest.TestCase):
    """build_event must:
      - set event.url to a mycalfix:// URL on ⚠️ / ❌,
      - leave event.url=None on ✅ / 🤖,
      - always include a paste-ready degraded command on ⚠️ / ❌."""

    def _pr(self):
        return pr_watcher.PRSnap(
            url="https://github.com/realRoc/my-calendar/pull/11",
            number=11,
            title="Phase 3",
            is_draft=False,
            repo="realRoc/my-calendar",
            base="main",
            default_branch="main",
            head_sha="deadbeef",
            head_branch="phase3-fix-launcher",
        )

    def _codex_result(self):
        return pr_watcher.CodexResult(
            thread_id="t-1",
            last_message="ok",
            exit_code=0,
            jsonl_path=Path("/tmp/x.jsonl"),
            scratch_dir=Path("/tmp/scratch"),
        )

    def test_verdict_blocker_sets_url_and_paste_cmd(self):
        body = "存在 blocker。\n\n结论：❌ 暂不可合并（存在 blocker）"
        from datetime import datetime
        event = pr_watcher.build_event(
            pr=self._pr(),
            result=self._codex_result(),
            comment_url="https://example.com/c/2",
            comment_body=body,
            now=datetime(2026, 5, 22, 15, 0, 0),
            origin_cwd="/Users/me/repo",
        )
        self.assertIsNotNone(event.url)
        self.assertTrue(event.url.startswith("mycalfix://fix?"))
        self.assertIn("origin_cwd=%2FUsers%2Fme%2Frepo", event.url)
        self.assertIn("🛠 修复入口", event.notes)
        self.assertIn("paste-ready 命令", event.notes)
        # paste cmd should have actual cwd, not placeholder
        self.assertIn("/Users/me/repo", event.notes)

    def test_verdict_pass_clears_url(self):
        body = "未发现 blocker。\n\n结论：✅ 可以合并"
        from datetime import datetime
        event = pr_watcher.build_event(
            pr=self._pr(),
            result=self._codex_result(),
            comment_url="https://example.com/c/3",
            comment_body=body,
            now=datetime(2026, 5, 22, 15, 0, 0),
            origin_cwd="/Users/me/repo",
        )
        self.assertIsNone(event.url)
        self.assertNotIn("🛠 修复入口", event.notes)

    def test_verdict_blocker_no_origin_cwd_uses_placeholder(self):
        body = "结论：⚠️ 修正后可合并"
        from datetime import datetime
        event = pr_watcher.build_event(
            pr=self._pr(),
            result=self._codex_result(),
            comment_url="https://example.com/c/4",
            comment_body=body,
            now=datetime(2026, 5, 22, 15, 0, 0),
            origin_cwd=None,
        )
        # URL still valid (launcher will folder-pick), just no origin_cwd param
        self.assertIsNotNone(event.url)
        self.assertNotIn("origin_cwd=", event.url)
        self.assertIn("<填入本地 repo 路径>", event.notes)
        self.assertIn("origin_cwd 未知", event.notes)


class FixPromptCommentEndpointTests(unittest.TestCase):
    """Regression: the gh api snippet in scripts/fix_prompt.md must produce a
    valid endpoint when {comment_url} carries the #issuecomment-<id> fragment.

    The previous one-shot `sed s#…/pull/<n>#repos/<o>/<r>#` only replaced the
    prefix and left the fragment in the path, yielding
    `repos/<o>/<r>#issuecomment-<id>/issues/comments/<id>` — not a valid
    GitHub API path. The fix splits owner/repo + comment_id into two separate
    extractions; this test pins that contract."""

    FIXTURE_URL = "https://github.com/realRoc/my-calendar/pull/13#issuecomment-4516623335"

    def test_owner_repo_extracted_stripping_fragment(self):
        result = subprocess.run(
            ["sed", "-E", r"s|^https://github.com/([^/]+/[^/]+)/pull/[0-9]+.*$|\1|"],
            input=self.FIXTURE_URL, capture_output=True, text=True, check=True,
        )
        self.assertEqual(result.stdout.strip(), "realRoc/my-calendar")

    def test_comment_id_extracted(self):
        result = subprocess.run(
            ["sed", "-E", r"s|.*issuecomment-([0-9]+).*|\1|"],
            input=self.FIXTURE_URL, capture_output=True, text=True, check=True,
        )
        self.assertEqual(result.stdout.strip(), "4516623335")

    def test_fix_prompt_does_not_regress_to_single_sed(self):
        # Burn-in: the prompt must not regress to the buggy pattern. The old
        # bug was the substitution `…/pull/[0-9]+#repos/…` (no fragment-eating
        # group), which leaves the #issuecomment-<id> tail glued onto the
        # replacement.
        prompt = (HERE / "fix_prompt.md").read_text(encoding="utf-8")
        self.assertNotIn("/pull/[0-9]+#repos/", prompt)


class PasteReadyPlaceholderQuotingTests(unittest.TestCase):
    """Regression: when origin_cwd is unknown, the paste-ready fallback in
    _build_paste_ready_fix_command must single-quote the `<…>` placeholder.
    Otherwise bash parses `cd <填入本地 repo 路径>` as input redirection from
    a non-existent file, instead of a clear `cd: No such file or directory`."""

    def _pr(self) -> pr_watcher.PRSnap:
        return pr_watcher.PRSnap(
            url="https://github.com/realRoc/my-calendar/pull/11",
            number=11,
            title="Phase 3",
            is_draft=False,
            repo="realRoc/my-calendar",
            base="main",
            default_branch="main",
            head_sha="deadbeef",
            head_branch="phase3-fix-launcher",
        )

    def test_placeholder_is_single_quoted_when_origin_cwd_missing(self):
        cmd = pr_watcher._build_paste_ready_fix_command(
            pr=self._pr(),
            comment_url="https://example.com/c/1",
            origin_cwd=None,
        )
        self.assertIn("cd '<填入本地 repo 路径>'", cmd)
        # Sanity: the unquoted form (which bash treats as a redirect) must NOT
        # appear anywhere.
        self.assertNotIn("cd <填入本地 repo 路径>", cmd)

    def test_real_origin_cwd_is_shlex_quoted_not_placeholder(self):
        cmd = pr_watcher._build_paste_ready_fix_command(
            pr=self._pr(),
            comment_url="https://example.com/c/1",
            origin_cwd="/Users/me/Desktop/my calendar",
        )
        self.assertIn("/Users/me/Desktop/my calendar", cmd)
        self.assertNotIn("<填入本地 repo 路径>", cmd)


if __name__ == "__main__":
    unittest.main()
