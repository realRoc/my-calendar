"""Regression tests for current-session PR review recording.

Run with:
    .venv/bin/python -m unittest scripts.test_pr_session_review
"""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import pr_session_review  # noqa: E402
import pr_watcher  # noqa: E402


class CurrentSessionReviewTests(unittest.TestCase):
    def _pr(self, **overrides) -> pr_watcher.PRSnap:
        defaults = {
            "url": "https://github.com/realRoc/my-calendar/pull/42",
            "number": 42,
            "title": "Current-session review",
            "is_draft": False,
            "repo": "realRoc/my-calendar",
            "base": "main",
            "default_branch": "main",
            "head_sha": "abc123def456",
            "head_branch": "feature/current-session",
            "head_repo": "realRoc/my-calendar",
        }
        defaults.update(overrides)
        return pr_watcher.PRSnap(**defaults)

    def _patch_paths(self, tmp: Path):
        lock_dir = tmp / "locks"
        return mock.patch.multiple(
            pr_watcher,
            STATE_PATH=tmp / "pr_state.json",
            CAL_STATE_PATH=tmp / "pr_calendar_state.json",
            LOG_DIR=tmp / "pr_logs",
            LOCK_DIR=lock_dir,
            STATE_LOCK_PATH=lock_dir / "state.lock",
        )

    def test_claim_review_writes_current_session_pending(self):
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            pr = self._pr()

            with self._patch_paths(tmp), \
                    mock.patch.object(pr_session_review, "fetch_pr", return_value=pr):
                result = pr_session_review.claim_review(pr.url, origin_cwd=None)

            self.assertEqual(result["status"], "claimed")
            self.assertEqual(result["head_sha"], pr.head_sha)
            state = json.loads((tmp / "pr_state.json").read_text(encoding="utf-8"))
            entry = state["prs"][pr.url]
            self.assertEqual(entry["pending_review_sha"], pr.head_sha)
            self.assertEqual(entry["pending_review_source"], "current-session")
            self.assertEqual(entry["last_seen_sha"], pr.head_sha)

    def test_claim_review_rejects_when_pr_lock_is_held(self):
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            pr = self._pr()

            with self._patch_paths(tmp), \
                    mock.patch.object(pr_session_review, "fetch_pr", return_value=pr):
                held_fd = pr_watcher.acquire_pr_lock_nb(pr.url)
                self.assertIsNotNone(held_fd)
                try:
                    with self.assertRaisesRegex(ValueError, "already in progress"):
                        pr_session_review.claim_review(pr.url, origin_cwd=None)
                finally:
                    pr_watcher.release_lock_fd(held_fd)

            self.assertFalse((tmp / "pr_state.json").exists())

    def test_claim_review_rejects_already_recorded_same_sha(self):
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            pr = self._pr()

            with self._patch_paths(tmp), \
                    mock.patch.object(pr_session_review, "fetch_pr", return_value=pr):
                pr_watcher.STATE_PATH.write_text(json.dumps({
                    "_meta": {},
                    "prs": {
                        pr.url: {
                            "last_commented_sha": pr.head_sha,
                            "last_comment_url": "https://example/comment",
                        }
                    },
                }), encoding="utf-8")

                with self.assertRaisesRegex(ValueError, "already recorded"):
                    pr_session_review.claim_review(pr.url, origin_cwd=None)

    def test_claim_review_rejects_non_stale_pending_same_sha(self):
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            pr = self._pr()

            with self._patch_paths(tmp), \
                    mock.patch.object(pr_session_review, "fetch_pr", return_value=pr):
                pr_watcher.STATE_PATH.write_text(json.dumps({
                    "_meta": {},
                    "prs": {
                        pr.url: {
                            "pending_review_sha": pr.head_sha,
                            "pending_review_started_at": "2999-01-01T00:00:00",
                            "pending_review_source": "poll",
                        }
                    },
                }), encoding="utf-8")

                with self.assertRaisesRegex(ValueError, "already pending"):
                    pr_session_review.claim_review(pr.url, origin_cwd=None)

    def test_record_review_writes_event_sidecar_and_clears_pending(self):
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            pr = self._pr()
            comment_url = "https://github.com/realRoc/my-calendar/pull/42#issuecomment-123"
            body = "\n".join([
                "> 🤖 由 Codex 自动生成",
                pr_watcher.AI_COAUTHOR_METADATA_MARKER,
                pr_watcher.head_sha_metadata_marker(pr.head_sha),
                "",
                "没有发现阻塞问题。",
                "",
                "结论：✅ 可以合并",
            ])
            events = []

            def fake_upsert(new_events, *args, **kwargs):
                events.extend(new_events)
                return {new_events[0].key: "created"}

            with self._patch_paths(tmp), \
                    mock.patch.object(pr_session_review, "fetch_pr", return_value=pr), \
                    mock.patch.object(pr_session_review, "fetch_comment_body", return_value=(comment_url, body)), \
                    mock.patch.object(pr_watcher, "upsert_events", fake_upsert), \
                    mock.patch.object(pr_watcher, "cache_comment_body", lambda *a, **kw: None), \
                    mock.patch.object(pr_watcher, "_refresh_dashboard", lambda *, reason: None):
                pr_watcher.STATE_PATH.write_text(json.dumps({
                    "_meta": {},
                    "prs": {
                        pr.url: {
                            "pending_review_sha": pr.head_sha,
                            "pending_review_source": "current-session",
                        }
                    },
                }), encoding="utf-8")

                result = pr_session_review.record_review(
                    pr.url,
                    comment_url,
                    origin_cwd=None,
                    thread_id="thread-1",
                    comment_body_file=None,
                    dry_run=False,
                )

            self.assertEqual(result["calendar_action"], "created")
            self.assertEqual(result["event_key"], f"my-calendar:pr-comment:{pr.url}:{pr.head_sha}")
            self.assertEqual(len(events), 1)
            self.assertIn("结论：✅ 可以合并", events[0].notes)
            state = json.loads((tmp / "pr_state.json").read_text(encoding="utf-8"))
            entry = state["prs"][pr.url]
            self.assertEqual(entry["last_commented_sha"], pr.head_sha)
            self.assertEqual(entry["last_comment_url"], comment_url)
            self.assertNotIn("pending_review_sha", entry)
            self.assertTrue(Path(result["meta_path"]).exists())

    def test_record_review_rejects_missing_current_head_marker(self):
        pr = self._pr()
        comment_url = "https://github.com/realRoc/my-calendar/pull/42#issuecomment-123"
        body = "\n".join([
            "> 🤖 由 Codex 自动生成",
            pr_watcher.AI_COAUTHOR_METADATA_MARKER,
            "",
            "结论：✅ 可以合并",
        ])

        with mock.patch.object(pr_session_review, "fetch_pr", return_value=pr), \
                mock.patch.object(pr_session_review, "fetch_comment_body", return_value=(comment_url, body)):
            with self.assertRaisesRegex(ValueError, "current head SHA marker"):
                pr_session_review.record_review(
                    pr.url,
                    comment_url,
                    origin_cwd=None,
                    thread_id=None,
                    comment_body_file=None,
                    dry_run=True,
                )


if __name__ == "__main__":
    unittest.main()
