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
import os
import sys
import tempfile
import time
import unittest
from datetime import datetime
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


class CollectRunningTests(unittest.TestCase):
    """Regression cover for PR #9 blocker: `collect_running` must surface a
    PR while codex is mid-run.

    Pre-fix behaviour was a heuristic on `.jsonl` mtime + absence of
    `.meta.json`; that never lit up in practice because pr_watcher only
    refreshed the static HTML AFTER `.meta.json` was already written.

    Post-fix contract: pr_watcher.run_codex drops a `.running` JSON sidecar
    before codex starts and removes it after codex finishes; the dashboard
    is regenerated at both moments. These tests pin that contract.
    """

    STEM = "20260520-130000__realRoc_my-calendar_pull_9"

    def _write_running(self, tmp: Path, *, stem: str | None = None, **extra) -> Path:
        stem = stem or self.STEM
        sidecar = tmp / f"{stem}.running"
        payload = {
            "started_at": "2026-05-20T13:00:00",
            "repo": "realRoc/my-calendar",
            "pr_number": 9,
            "pr_url": "https://github.com/realRoc/my-calendar/pull/9",
            "pr_title": "Live dashboard + serialize duplicate pr_watcher triggers",
            "head_sha": "deadbee",
            "jsonl_path": str(tmp / f"{stem}.jsonl"),
        }
        payload.update(extra)
        sidecar.write_text(json.dumps(payload), encoding="utf-8")
        return sidecar

    def test_running_sidecar_is_visible(self):
        # The PR #9 blocker: while codex is running, the dashboard MUST list
        # this PR in the running section. The .jsonl exists (codex is
        # streaming) and no .meta.json exists yet (run not finished).
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            _patch_log_dir(self, tmp)
            self._write_running(tmp)
            (tmp / f"{self.STEM}.jsonl").write_text("{}\n", encoding="utf-8")
            running = dashboard.collect_running(datetime.now())
            self.assertEqual(len(running), 1)
            self.assertEqual(running[0]["pr_number"], 9)
            self.assertEqual(running[0]["repo"], "realRoc/my-calendar")
            self.assertIn("last_active", running[0])

    def test_sidecar_disappears_when_meta_lands(self):
        # When run finishes, pr_watcher writes .meta.json THEN deletes
        # .running. If the deletion races behind the meta write, we must
        # still treat the run as done and hide it from the running list —
        # otherwise the dashboard would briefly double-count it as both
        # "running" and "completed".
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            _patch_log_dir(self, tmp)
            self._write_running(tmp)
            (tmp / f"{self.STEM}.meta.json").write_text(
                json.dumps({
                    "started_at": "2026-05-20T13:00:00",
                    "repo": "realRoc/my-calendar",
                    "pr_number": 9,
                    "pr_url": "https://github.com/realRoc/my-calendar/pull/9",
                    "head_sha": "deadbee",
                    "comment_body": "OK",
                    "codex_exit": 0,
                }),
                encoding="utf-8",
            )
            running = dashboard.collect_running(datetime.now())
            self.assertEqual(running, [])

    def test_stale_sidecar_is_ignored(self):
        # Crashed pr_watcher (SIGKILL, panic) leaves a phantom .running
        # behind. The mtime cap must hide it so the dashboard doesn't
        # show a permanent fake "running" row.
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            _patch_log_dir(self, tmp)
            sidecar = self._write_running(tmp)
            old = time.time() - dashboard.RUNNING_SIDECAR_MAX_AGE_SEC - 60
            os.utime(sidecar, (old, old))
            running = dashboard.collect_running(datetime.now())
            self.assertEqual(running, [])

    def test_running_skipped_when_collect_reviews_runs(self):
        # `.running` files must not leak into the reviews list — they're not
        # finished reviews. collect_reviews only iterates `.meta.json`,
        # `.last.txt`, `.jsonl`; a `.running` sidecar should be invisible to it.
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            _patch_log_dir(self, tmp)
            self._write_running(tmp)
            self.assertEqual(dashboard.collect_reviews(), [])


class RenderHtmlXssTests(unittest.TestCase):
    """PR #3 review blocker: PR titles and comment bodies are
    attacker-controlled HTML text. They must never break out of the
    <script type="application/json"> data island via literal `</script>`.

    HTML_TEMPLATE has exactly three legitimate </script> tags:
    `reviews-data`, `running-data`, and the page's main script close.
    Any fourth occurrence means an attacker payload escaped the JSON
    escaping path.
    """

    LEGITIMATE_SCRIPT_CLOSES = 3

    def test_script_close_in_comment_body_is_neutralized(self):
        reviews = [{
            "timestamp": "2026-05-20T12:00:00",
            "repo": "x/y",
            "pr_number": 1,
            "pr_url": "https://github.com/x/y/pull/1",
            "comment_body": "</script><script>window.PWNED=1</script>",
        }]
        html = dashboard.render_html(reviews, [])
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
        html = dashboard.render_html(reviews, [])
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
            html = dashboard.render_html(reviews, [])
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
        html = dashboard.render_html(reviews, [])
        self.assertIn('"pr_title": "Add feature"', html)
        self.assertIn('"comment_body": "looks good"', html)
        self.assertIn('id="reviews-data"', html)
        self.assertIn("JSON.parse", html)


class FilterStatePersistenceTests(unittest.TestCase):
    """Issue #12 regression cover: the dashboard reloads itself every 5s via
    `setInterval(() => location.reload(), 5000)`. Before this PR the
    in-DOM filter state (date range / repo / search / tab / expanded
    review cards) was lost on every reload, so the board was unusable for
    leaving open long-term.

    Fix: mirror filter state into `location.hash` via URLSearchParams,
    read it back on load. URL hash survives reload(), so the next render
    starts from the same state. Also makes the view shareable by URL.

    These tests are structural — they assert the plumbing strings exist
    in the rendered HTML. Real behaviour testing would need a JS runtime
    (jsdom/playwright); since the repo has no JS test infra, this is the
    available guardrail. If someone refactors and accidentally drops the
    persistence logic, these tests fail and force the conversation."""

    def _render(self):
        return dashboard.render_html([{
            "timestamp": "2026-05-24T12:00:00",
            "repo": "x/y",
            "pr_number": 1,
            "pr_url": "https://github.com/x/y/pull/1",
            "pr_title": "test",
            "head_sha": "deadbeef",
            "comment_url": "https://github.com/x/y/issues/1#issuecomment-1",
            "comment_body": "hi",
            "codex_exit": 0,
        }], [])

    def test_hash_state_helpers_are_present(self):
        html = self._render()
        # The two persistence directions.
        self.assertIn("readStateFromHash", html, "state restore on load missing")
        self.assertIn("writeStateToHash", html, "state persist on change missing")
        # URL-hash mechanism specifically (not localStorage, which would not
        # be shareable and would not work cross-browser-profile).
        self.assertIn("URLSearchParams", html)
        self.assertIn("location.hash", html)
        # replaceState avoids polluting browser history with one entry per
        # keystroke in the search box.
        self.assertIn("history.replaceState", html)

    def test_state_object_holds_canonical_fields(self):
        html = self._render()
        # The state object must own all five filter dimensions that issue
        # #12 calls out: date range, repo, search, tab, expanded items.
        # Asserts the keys are wired up — a refactor that drops one would
        # silently stop persisting that dimension.
        for key in ("range", "repo", "q", "tab", "expanded"):
            self.assertIn(f"state.{key}", html, f"state.{key} missing — {key} would not persist")

    def test_expanded_set_persists_via_rid(self):
        html = self._render()
        # Review cards need a stable id (data-rid) for the expanded Set to
        # match the same cards after reload. Without data-rid the open/closed
        # state can't be reapplied.
        self.assertIn("data-rid=", html)
        self.assertIn("reviewId(", html, "stable rid function missing")
        # Inline onclick toggler was replaced by delegated handler so the
        # toggle can update state.expanded. Inline form would skip the Set.
        self.assertNotIn("onclick=\"this.classList.toggle", html)

    def test_reload_interval_preserved(self):
        # The 5s reload itself must stay — the URL-hash fix is meant to be
        # invisible to the user, NOT a swap to incremental data fetching.
        # If a future change disables the reload, dashboard staleness comes
        # back and users won't see new pr_watcher reviews until refresh.
        html = self._render()
        self.assertIn("location.reload()", html)
        self.assertIn("setInterval", html)

    def test_tolerates_unknown_hash_values(self):
        # The hash schema is forward-compatible: unknown range/tab values
        # must fall back to defaults so an old bookmark from a future
        # dashboard version (or a typo in a hand-edited URL) doesn't crash
        # the page. Assert the validation enumerations are present.
        html = self._render()
        # range whitelist
        self.assertIn("range === 'today'", html)
        self.assertIn("range === 'week'", html)
        self.assertIn("range === 'all'", html)
        # tab whitelist
        self.assertIn("tab === 'repo'", html)
        self.assertIn("tab === 'pr'", html)
        self.assertIn("tab === 'timeline'", html)


class RenderRunningMarkdownTests(unittest.TestCase):
    def test_empty_running_list_is_terminal_friendly(self):
        out = dashboard.render_running_markdown([], datetime(2026, 5, 20, 13, 0, 0))
        self.assertIn("## PR Review 运行中", out)
        self.assertIn("当前没有正在运行的 Codex PR review", out)
        self.assertNotIn("pr-dashboard.html", out)

    def test_running_task_renders_expandable_clickable_component(self):
        out = dashboard.render_running_markdown([{
            "started_at": "2026-05-20T12:58:30",
            "timestamp": "2026-05-20T12:58:30",
            "repo": "realRoc/my-calendar",
            "pr_number": 9,
            "pr_url": "https://github.com/realRoc/my-calendar/pull/9",
            "pr_title": "Live terminal board",
            "head_sha": "deadbeefcafebabe",
            "jsonl_path": "/tmp/run.jsonl",
            "jsonl_size": 1536,
            "last_active": "2026-05-20T12:59:50",
        }], datetime(2026, 5, 20, 13, 0, 0))

        self.assertIn("<details>", out)
        self.assertIn("<summary>", out)
        self.assertIn("[realRoc/my-calendar #9](https://github.com/realRoc/my-calendar/pull/9)", out)
        self.assertIn("运行 `1m 30s`", out)
        self.assertIn("最近活动 `10s`", out)
        self.assertIn("Live terminal board", out)
        self.assertIn("`deadbee", out)
        self.assertIn("`1.5KB`", out)


if __name__ == "__main__":
    unittest.main()
