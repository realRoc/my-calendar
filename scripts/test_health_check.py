"""Regression tests for scripts/health_check.py (pure logic only — no EventKit writes).

Run with:
    .venv/bin/python scripts/test_health_check.py
or:
    .venv/bin/python -m unittest scripts.test_health_check
"""

from __future__ import annotations

import sys
import tempfile
import unittest
from datetime import date, datetime, time, timedelta
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import health_check  # noqa: E402


def _write(path: Path, text: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


WEEK_MD = """---
updated: 2026-07-06
days:
  "2026-07-07":
    meal1: "MEAL1-TUESDAY"
    meal2: "MEAL2-TUESDAY"
    workout: "WORKOUT-TUESDAY"
    workout_time: "08:30"
---
body
"""

MEDS_MD = """---
medications:
  - id: alpha
    name: 药A
    dose: 1 片 / 日
    schedule: daily
    time: "09:00"
    status: active
  - id: beta
    name: 药B
    dose: 1 片 / 日
    schedule: daily
    status: stopped
---
"""


class WeekPlanTests(unittest.TestCase):
    def test_load_week_plan_normalizes_date_keys(self):
        with tempfile.TemporaryDirectory() as td:
            p = _write(Path(td) / "week.md", WEEK_MD)
            plan = health_check.load_week_plan(p)
        self.assertIn("2026-07-07", plan)
        self.assertEqual(plan["2026-07-07"]["meal1"], "MEAL1-TUESDAY")

    def test_missing_file_returns_empty(self):
        self.assertEqual(health_check.load_week_plan(Path("/nonexistent/week.md")), {})


class MedicationTests(unittest.TestCase):
    def test_only_active_meds_loaded(self):
        with tempfile.TemporaryDirectory() as td:
            p = _write(Path(td) / "medications.md", MEDS_MD)
            meds = health_check.load_medications(p)
        self.assertEqual([m["id"] for m in meds], ["alpha"])


class ScheduledEventTests(unittest.TestCase):
    def setUp(self):
        with tempfile.TemporaryDirectory() as td:
            self.plan = health_check.load_week_plan(_write(Path(td) / "w.md", WEEK_MD))
            self.meds = health_check.load_medications(_write(Path(td) / "m.md", MEDS_MD))

    def test_planned_day_uses_week_md_content_and_time(self):
        events = health_check.build_scheduled_events(date(2026, 7, 7), self.plan, self.meds)
        by_key = {e.key.rsplit(":", 1)[-1]: e for e in events}
        self.assertEqual(set(by_key), {"med-alpha", "meal1", "meal2", "workout"})
        self.assertIn("MEAL1-TUESDAY", by_key["meal1"].notes)
        self.assertIn("MEAL2-TUESDAY", by_key["meal2"].notes)
        self.assertIn("WORKOUT-TUESDAY", by_key["workout"].notes)
        self.assertEqual(by_key["workout"].start_at.time(), time(8, 30))
        # med title must not leak the drug name (lock-screen privacy)
        self.assertNotIn("药A", by_key["med-alpha"].title)
        self.assertIn("药A", by_key["med-alpha"].notes)
        # all scheduled events alarm at event time, not at creation
        for e in events:
            self.assertEqual(e.alarm_offset_min, 0)

    def test_unplanned_weekday_and_weekend_fall_back_to_defaults(self):
        weekday = health_check.build_scheduled_events(date(2026, 7, 8), {}, [])
        weekend = health_check.build_scheduled_events(date(2026, 7, 11), {}, [])
        wd = {e.key.rsplit(":", 1)[-1]: e for e in weekday}
        we = {e.key.rsplit(":", 1)[-1]: e for e in weekend}
        self.assertNotIn("med-alpha", wd)  # no meds configured → no med event
        self.assertEqual(wd["workout"].start_at.time(), health_check.WORKOUT_WEEKDAY_TIME)
        self.assertEqual(we["workout"].start_at.time(), health_check.WORKOUT_WEEKEND_TIME)
        self.assertIn(health_check.DEFAULT_MEAL1, wd["meal1"].notes)
        self.assertIn(health_check.DEFAULT_WORKOUT_WEEKEND, we["workout"].notes)

    def test_deterministic_keys(self):
        events = health_check.build_scheduled_events(date(2026, 7, 7), self.plan, self.meds)
        keys = {e.key for e in events}
        self.assertIn("my-calendar:health:2026-07-07:meal1", keys)
        self.assertIn("my-calendar:health:2026-07-07:med-alpha", keys)


class NagTests(unittest.TestCase):
    def _nags(self, hhmm: tuple[int, int], log_fm: dict, state: dict,
              last_weight: date | None = date(2026, 7, 7)):
        now = datetime(2026, 7, 7, *hhmm)
        return health_check.build_nags(now, log_fm, state, last_weight)

    def test_no_nag_before_thresholds(self):
        create, remove = self._nags((11, 0), {}, {})
        self.assertEqual([e.key for e in create], [])
        self.assertEqual(remove, [])

    def test_meal1_nag_after_1230_when_unrecorded(self):
        create, _ = self._nags((12, 31), {}, {})
        self.assertEqual([e.key.rsplit(":", 1)[-1] for e in create], ["nag-meal1"])

    def test_meal2_nag_after_1930(self):
        log = {"meals": [{"slot": 1}]}
        create, _ = self._nags((19, 31), log, {})
        self.assertEqual([e.key.rsplit(":", 1)[-1] for e in create], ["nag-meal2"])

    def test_recorded_meal_suppresses_and_removes_existing_nag(self):
        log = {"meals": [{"slot": 1}]}
        state = {"my-calendar:health:2026-07-07:nag-meal1": {"event_id": "x"}}
        create, remove = self._nags((13, 0), log, state)
        self.assertEqual(create, [])
        self.assertEqual(remove, ["my-calendar:health:2026-07-07:nag-meal1"])

    def test_nag_not_recreated_when_already_in_state(self):
        state = {"my-calendar:health:2026-07-07:nag-meal1": {"event_id": "x"}}
        create, _ = self._nags((13, 0), {}, state)
        self.assertEqual(create, [])

    def test_weight_nag_when_stale(self):
        create, _ = self._nags((13, 0), {"meals": [{"slot": 1}]}, {},
                               last_weight=date(2026, 7, 3))
        self.assertEqual([e.key.rsplit(":", 1)[-1] for e in create], ["nag-weight"])

    def test_weight_nag_suppressed_by_todays_weight(self):
        log = {"meals": [{"slot": 1}], "weight_kg": 78.5}
        create, _ = self._nags((13, 0), log, {}, last_weight=None)
        self.assertEqual(create, [])


class LogLookupTests(unittest.TestCase):
    def test_last_weight_date_scans_backwards(self):
        with tempfile.TemporaryDirectory() as td:
            log_dir = Path(td)
            _write(log_dir / "2026" / "2026-07-05.md",
                   "---\ndate: 2026-07-05\nweight_kg: 70.0\n---\n")
            _write(log_dir / "2026" / "2026-07-06.md",
                   "---\ndate: 2026-07-06\nweight_kg:\n---\n")
            self.assertEqual(health_check.last_weight_date(date(2026, 7, 7), log_dir),
                             date(2026, 7, 5))

    def test_has_meal(self):
        self.assertTrue(health_check.has_meal({"meals": [{"slot": 1}]}, 1))
        self.assertFalse(health_check.has_meal({"meals": [{"slot": 1}]}, 2))
        self.assertFalse(health_check.has_meal({}, 1))


class CacheAndPruneTests(unittest.TestCase):
    def test_filter_changed_skips_unchanged(self):
        events = health_check.build_scheduled_events(date(2026, 7, 7), {}, [])
        cache: dict[str, str] = {}
        first = health_check.filter_changed(events, cache)
        self.assertEqual(len(first), len(events))
        for e in events:
            cache[e.key] = health_check._event_hash(e)
        self.assertEqual(health_check.filter_changed(events, cache), [])

    def test_content_change_invalidates_hash(self):
        [e] = [x for x in health_check.build_scheduled_events(date(2026, 7, 7), {}, [])
               if x.key.endswith(":meal2")]
        cache = {e.key: health_check._event_hash(e)}
        plan = {"2026-07-07": {"meal2": "ADJUSTED-AFTER-MEAL1"}}
        [e2] = [x for x in health_check.build_scheduled_events(date(2026, 7, 7), plan, [])
                if x.key.endswith(":meal2")]
        self.assertEqual(health_check.filter_changed([e2], cache), [e2])

    def test_prune_old_drops_only_expired(self):
        today = date(2026, 7, 7)
        old = (today - timedelta(days=health_check.STATE_RETENTION_DAYS + 1)).isoformat()
        state = {
            f"my-calendar:health:{old}:meal1": {},
            "my-calendar:health:2026-07-07:meal1": {},
        }
        cache = dict.fromkeys(state, "h")
        health_check.prune_old(state, cache, today)
        self.assertEqual(list(state), ["my-calendar:health:2026-07-07:meal1"])
        self.assertEqual(list(cache), ["my-calendar:health:2026-07-07:meal1"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
