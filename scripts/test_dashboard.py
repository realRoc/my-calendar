"""Regression tests for scripts/dashboard.py.

Run with:
    .venv/bin/python scripts/test_dashboard.py
or:
    .venv/bin/python -m unittest scripts.test_dashboard

Covers:
  - collect_reviews() across the three input shapes (sidecar / .last.txt
    fallback / malformed .meta.json), and the bare-.jsonl skip.
  - render_html() XSS-escaping: attacker-controlled `</script>` in PR
    titles or comment bodies must not break out of the JSON data island.
"""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import dashboard  # noqa: E402


def _patch_log_dir(test: unittest.TestCase, tmp: Path) -> None:
    original = dashboard.LOG_DIR
    dashboard.LOG_DIR = tmp
    test.addCleanup(lambda: setattr(dashboard, "LOG_DIR", original))


class CollectReviewsTests(unittest.TestCase):
    def test_sidecar_full_record(self):
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            _patch_log_dir(self, tmp)
            stem = "20260520-120000__realRoc_my-calendar_pull_3"
            (tmp / f"{stem}.meta.json").write_text(
                json.dumps({
                    "started_at": "2026-05-20T12:00:00",
                    "repo": "realRoc/my-calendar",
                    "pr_number": 3,
                    "pr_url": "https://github.com/realRoc/my-calendar/pull/3",
                    "pr_title": "PR #3",
                    "head_sha": "abc1234",
                    "thread_id": "tid",
                    "comment_url": "https://github.com/x/y/issues/3#issuecomment-1",
                    "comment_body": "OK",
                    "codex_exit": 0,
                }),
                encoding="utf-8",
            )
            (tmp / f"{stem}.jsonl").write_text("{}\n", encoding="utf-8")
            out = dashboard.collect_reviews()
            self.assertEqual(len(out), 1)
            self.assertEqual(out[0]["pr_number"], 3)
            self.assertEqual(out[0]["codex_exit"], 0)
            self.assertEqual(out[0]["timestamp"], "2026-05-20T12:00:00")

    def test_last_txt_only_fallback(self):
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            _patch_log_dir(self, tmp)
            stem = "20260101-090000__owner_repo_pull_99"
            (tmp / f"{stem}.last.txt").write_text("legacy comment body", encoding="utf-8")
            out = dashboard.collect_reviews()
            self.assertEqual(len(out), 1)
            self.assertEqual(out[0]["repo"], "owner/repo")
            self.assertEqual(out[0]["pr_number"], 99)
            self.assertEqual(out[0]["comment_body"], "legacy comment body")
            self.assertNotIn("codex_exit", out[0])

    def test_malformed_meta_json_falls_back_gracefully(self):
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            _patch_log_dir(self, tmp)
            stem = "20260101-080000__owner_repo_pull_1"
            (tmp / f"{stem}.meta.json").write_text("{not valid json", encoding="utf-8")
            (tmp / f"{stem}.last.txt").write_text("fallback", encoding="utf-8")
            out = dashboard.collect_reviews()
            self.assertEqual(len(out), 1)
            self.assertEqual(out[0]["comment_body"], "fallback")
            self.assertEqual(out[0]["pr_number"], 1)

    def test_bare_jsonl_is_skipped(self):
        # codex started but never produced a comment → only .jsonl exists
        # (no sidecar, no .last.txt). Avoid empty/garbage rows.
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            _patch_log_dir(self, tmp)
            stem = "20260101-070000__owner_repo_pull_5"
            (tmp / f"{stem}.jsonl").write_text("{}\n", encoding="utf-8")
            out = dashboard.collect_reviews()
            self.assertEqual(out, [])


class RenderHtmlXssTests(unittest.TestCase):
    """PR #3 review blocker: PR titles and comment bodies are
    attacker-controlled HTML text. They must never break out of the
    <script type="application/json"> data island via literal `</script>`.

    HTML_TEMPLATE has exactly two legitimate </script> tags (the data
    island's close and the page's main script close). Any third occurrence
    means an attacker payload escaped the JSON escaping path.
    """

    LEGITIMATE_SCRIPT_CLOSES = 2

    def test_script_close_in_comment_body_is_neutralized(self):
        reviews = [{
            "timestamp": "2026-05-20T12:00:00",
            "repo": "x/y",
            "pr_number": 1,
            "pr_url": "https://github.com/x/y/pull/1",
            "comment_body": "</script><script>window.PWNED=1</script>",
        }]
        html = dashboard.render_html(reviews)
        self.assertEqual(
            html.count("</script"),
            self.LEGITIMATE_SCRIPT_CLOSES,
            "attacker-controlled </script> must be escaped to <\\/script",
        )

    def test_script_close_in_pr_title_is_neutralized(self):
        reviews = [{
            "timestamp": "2026-05-20T12:00:00",
            "repo": "x/y",
            "pr_number": 1,
            "pr_url": "https://github.com/x/y/pull/1",
            "pr_title": "</script><img src=x onerror=alert(1)>",
            "comment_body": "",
        }]
        html = dashboard.render_html(reviews)
        self.assertEqual(
            html.count("</script"),
            self.LEGITIMATE_SCRIPT_CLOSES,
        )

    def test_uppercase_and_whitespace_variants_neutralized(self):
        # HTML parser end-tag scanner is case-insensitive and allows
        # whitespace inside the close tag. Our "</" → "<\/" substitution
        # catches all of them since they all start with "</".
        for payload in (
            "</SCRIPT>",
            "</Script\n>",
            "</script foo=bar>",
        ):
            reviews = [{
                "timestamp": "2026-05-20T12:00:00",
                "repo": "x/y",
                "pr_number": 1,
                "pr_url": "https://github.com/x/y/pull/1",
                "comment_body": payload,
            }]
            html = dashboard.render_html(reviews)
            # Count case-insensitively
            self.assertEqual(
                html.lower().count("</script"),
                self.LEGITIMATE_SCRIPT_CLOSES,
                f"payload {payload!r} broke out of data island",
            )

    def test_normal_review_round_trips_through_json(self):
        # Plain content must survive — JSON.parse on the JS side will
        # consume "<\/" perfectly fine.
        reviews = [{
            "timestamp": "2026-05-20T12:00:00",
            "repo": "x/y",
            "pr_number": 1,
            "pr_url": "https://github.com/x/y/pull/1",
            "pr_title": "Add feature",
            "comment_body": "looks good",
            "codex_exit": 0,
        }]
        html = dashboard.render_html(reviews)
        self.assertIn('"pr_title": "Add feature"', html)
        self.assertIn('"comment_body": "looks good"', html)
        self.assertIn('id="reviews-data"', html)
        self.assertIn("JSON.parse", html)


if __name__ == "__main__":
    unittest.main()
