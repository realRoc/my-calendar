"""Apple Calendar (EventKit) wrapper.

- Owns a dedicated calendar named "节日提醒" so we never touch the user's main calendars.
- Idempotent upserts: each reminder has a deterministic key (e.g. "mothers-day:2026:lead7");
  state.json maps key → EventKit event_identifier so reruns update instead of duplicate.

First-run authorization: macOS will prompt for Calendar access. If denied, raise with a
clear message pointing the user to System Settings → Privacy → Calendar.
"""

from __future__ import annotations

import json
import sys
import threading
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

import objc
from EventKit import (  # type: ignore[import-not-found]
    EKEventStore,
    EKEvent,
    EKAlarm,
    EKCalendar,
    EKEntityTypeEvent,
    EKSpanThisEvent,
    EKSourceTypeLocal,
    EKSourceTypeCalDAV,
)
from Foundation import NSDate, NSCalendar, NSCalendarUnitDay  # noqa: F401

CALENDAR_NAME = "节日提醒"          # 节日 daily_check 默认写入
PR_CALENDAR_NAME = "PR 监控"        # pr_watcher 默认写入
STATE_FILENAME = "state.json"


@dataclass
class ReminderEvent:
    key: str            # deterministic: e.g. "mothers-day:2026:lead7"
    title: str
    notes: str
    on_date: date       # the calendar day the all-day event sits on


# ─── permission ────────────────────────────────────────────────────────────────


def _request_access(store: EKEventStore) -> bool:
    """Block until macOS resolves the Calendar permission. Returns True on grant."""
    granted = {"value": False, "error": None}
    done = threading.Event()

    def completion(ok: bool, err: Any) -> None:
        granted["value"] = bool(ok)
        granted["error"] = err
        done.set()

    # macOS 14+ has requestFullAccessToEventsWithCompletion:; older has requestAccessToEntityType:completion:.
    if hasattr(store, "requestFullAccessToEventsWithCompletion_"):
        store.requestFullAccessToEventsWithCompletion_(completion)
    else:
        store.requestAccessToEntityType_completion_(EKEntityTypeEvent, completion)

    done.wait(timeout=30)
    if not granted["value"]:
        raise PermissionError(
            "Calendar access denied. Open System Settings → Privacy & Security → Calendar "
            "and grant access to Terminal (or the app you're running this from), then re-run."
        )
    return True


# ─── calendar lookup / creation ────────────────────────────────────────────────


def _find_or_create_calendar(store: EKEventStore, name: str = CALENDAR_NAME) -> EKCalendar:
    for cal in store.calendarsForEntityType_(EKEntityTypeEvent):
        if cal.title() == name:
            return cal

    # Need a writable source. Prefer Local, fall back to iCloud (CalDAV).
    local_source = None
    icloud_source = None
    for src in store.sources():
        if src.sourceType() == EKSourceTypeLocal:
            local_source = src
        elif src.sourceType() == EKSourceTypeCalDAV and "icloud" in (src.title() or "").lower():
            icloud_source = src
    source = local_source or icloud_source
    if source is None:
        # last resort: any CalDAV source
        for src in store.sources():
            if src.sourceType() == EKSourceTypeCalDAV:
                source = src
                break
    if source is None:
        raise RuntimeError("No writable calendar source found (no Local/iCloud).")

    cal = EKCalendar.calendarForEntityType_eventStore_(EKEntityTypeEvent, store)
    cal.setTitle_(name)
    cal.setSource_(source)
    err = objc.nil
    ok, err = store.saveCalendar_commit_error_(cal, True, None)
    if not ok:
        raise RuntimeError(f"Failed to create calendar {name!r}: {err}")
    return cal


# ─── state (key → event_identifier) ────────────────────────────────────────────


def _load_state(state_path: Path) -> dict[str, dict]:
    if state_path.exists():
        return json.loads(state_path.read_text(encoding="utf-8"))
    return {}


def _save_state(state_path: Path, state: dict[str, dict]) -> None:
    state_path.write_text(json.dumps(state, indent=2, ensure_ascii=False, sort_keys=True), encoding="utf-8")


# ─── upsert ────────────────────────────────────────────────────────────────────


def _to_nsdate(d: date) -> NSDate:
    # All-day event: use local-time midnight; EventKit will interpret with setAllDay_
    dt = datetime(d.year, d.month, d.day, 0, 0, 0)
    return NSDate.dateWithTimeIntervalSince1970_(dt.timestamp())


def _next_day_nsdate(d: date) -> NSDate:
    return _to_nsdate(d + timedelta(days=1))


def _make_immediate_alarm() -> EKAlarm:
    """Alarm firing ~10s after now — gives macOS a small buffer to register and pop the notification."""
    fire_at = datetime.now() + timedelta(seconds=10)
    return EKAlarm.alarmWithAbsoluteDate_(NSDate.dateWithTimeIntervalSince1970_(fire_at.timestamp()))


def upsert_events(
    events: list[ReminderEvent],
    state_path: Path,
    dry_run: bool = False,
    calendar_name: str = CALENDAR_NAME,
) -> dict[str, str]:
    """Upsert each event. Returns {key: action} where action is created/updated/unchanged."""
    actions: dict[str, str] = {}
    if dry_run:
        for e in events:
            actions[e.key] = "would-upsert"
        return actions

    store = EKEventStore.alloc().init()
    _request_access(store)
    cal = _find_or_create_calendar(store, calendar_name)
    state = _load_state(state_path)

    for e in events:
        existing_id = state.get(e.key, {}).get("event_id")
        ek_event = None
        if existing_id:
            ek_event = store.eventWithIdentifier_(existing_id)

        is_new = ek_event is None
        if is_new:
            ek_event = EKEvent.eventWithEventStore_(store)
            ek_event.setCalendar_(cal)
            action = "created"
        else:
            action = "updated"

        ek_event.setTitle_(e.title)
        ek_event.setNotes_(e.notes)
        ek_event.setStartDate_(_to_nsdate(e.on_date))
        ek_event.setEndDate_(_next_day_nsdate(e.on_date))
        ek_event.setAllDay_(True)

        if is_new:
            # Pop a system notification ~10s after creation. Only on initial create —
            # re-syncs of an existing event don't re-fire the alarm.
            ek_event.addAlarm_(_make_immediate_alarm())

        ok, err = store.saveEvent_span_error_(ek_event, EKSpanThisEvent, None)
        if not ok:
            print(f"[error] save failed for {e.key}: {err}", file=sys.stderr)
            actions[e.key] = "failed"
            continue

        state[e.key] = {
            "event_id": ek_event.eventIdentifier(),
            "on_date": e.on_date.isoformat(),
            "last_synced": datetime.now().isoformat(timespec="seconds"),
        }
        actions[e.key] = action

    _save_state(state_path, state)
    return actions


def remove_event(key: str, state_path: Path) -> bool:
    """Remove an event from both Calendar and state."""
    store = EKEventStore.alloc().init()
    _request_access(store)
    state = _load_state(state_path)
    entry = state.get(key)
    if not entry:
        return False
    ek_event = store.eventWithIdentifier_(entry["event_id"])
    if ek_event is not None:
        store.removeEvent_span_error_(ek_event, EKSpanThisEvent, None)
    state.pop(key, None)
    _save_state(state_path, state)
    return True


if __name__ == "__main__":
    # Smoke test: ensure we can read calendars and find/create the dedicated one.
    store = EKEventStore.alloc().init()
    _request_access(store)
    cal = _find_or_create_calendar(store)
    print(f"calendar OK: {cal.title()!r}  source={cal.source().title()}")
    print(f"sources available:")
    for src in store.sources():
        print(f"  - {src.title()!r}  type={src.sourceType()}")
