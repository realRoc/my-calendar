"""Pop a one-off test reminder so you can preview the macOS notification UX.

The regular daily_check creates all-day events without alarms — they sit in
the calendar but don't actively pop. This script creates a TIME-based event
with an alarm at start, so you get a real popup ~1 minute from now.

Usage:
  .venv/bin/python scripts/test_notification.py
  .venv/bin/python scripts/test_notification.py --delay 30
  .venv/bin/python scripts/test_notification.py --delete <event_id>
"""

from __future__ import annotations

import argparse
import sys
from datetime import datetime, timedelta
from pathlib import Path

from EventKit import EKEvent, EKAlarm, EKEventStore, EKSpanThisEvent  # type: ignore
from Foundation import NSDate

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
from calendar_sync import _request_access, _find_or_create_calendar  # noqa: E402


def create(delay_seconds: int) -> int:
    store = EKEventStore.alloc().init()
    _request_access(store)
    cal = _find_or_create_calendar(store)

    start = datetime.now() + timedelta(seconds=delay_seconds)
    end = start + timedelta(minutes=15)

    event = EKEvent.eventWithEventStore_(store)
    event.setCalendar_(cal)
    event.setTitle_("🧪 [TEST] 🎁 明天是母亲节 · 5月10日")
    event.setNotes_(
        "这是一个测试提醒，用于预览苹果日历弹窗效果。看完可以删。\n\n"
        "母亲节 · 2026-05-10 (T-1)\n\n"
        "节日备注：\n"
        "5 月第二个周日。\n\n"
        "── mom ──\n"
        "档案：妈妈\n"
        "  偏好: 喜欢吃鱼。 / 喜欢吃内蒙菜（本人就是内蒙人，从小的口味）。\n"
        "历史：\n"
        "  • 2026 · 远程订剧院门票 · 反馈：（未记录）\n"
    )
    event.setStartDate_(NSDate.dateWithTimeIntervalSince1970_(start.timestamp()))
    event.setEndDate_(NSDate.dateWithTimeIntervalSince1970_(end.timestamp()))
    event.setAllDay_(False)

    alarm = EKAlarm.alarmWithRelativeOffset_(0)   # fire at start
    event.addAlarm_(alarm)

    ok, err = store.saveEvent_span_error_(event, EKSpanThisEvent, None)
    if not ok:
        print(f"✗ save failed: {err}", file=sys.stderr)
        return 1

    eid = event.eventIdentifier()
    print(f"✓ test event created in '节日提醒'")
    print(f"  title:    {event.title()}")
    print(f"  fires at: {start.strftime('%H:%M:%S')}  ({delay_seconds}s from now)")
    print(f"  event id: {eid}")
    print()
    print(f"to delete after viewing:")
    print(f"  .venv/bin/python scripts/test_notification.py --delete {eid}")
    return 0


def delete(event_id: str) -> int:
    store = EKEventStore.alloc().init()
    _request_access(store)
    e = store.eventWithIdentifier_(event_id)
    if e is None:
        print(f"✗ event {event_id} not found")
        return 1
    ok, err = store.removeEvent_span_error_(e, EKSpanThisEvent, None)
    if ok:
        print(f"✓ deleted: {event_id}")
        return 0
    print(f"✗ delete failed: {err}", file=sys.stderr)
    return 1


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--delay", type=int, default=60, help="seconds from now to fire (default 60)")
    parser.add_argument("--delete", type=str, metavar="EVENT_ID", help="delete a previously-created test event by id")
    args = parser.parse_args()
    if args.delete:
        return delete(args.delete)
    return create(args.delay)


if __name__ == "__main__":
    sys.exit(main())
