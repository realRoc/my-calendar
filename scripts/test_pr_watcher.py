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


if __name__ == "__main__":
    unittest.main()
