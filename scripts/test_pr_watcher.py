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


if __name__ == "__main__":
    unittest.main()
