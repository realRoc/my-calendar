"""Daily entrypoint. Run from launchd once a day.

Two responsibilities:
  1. Future side — for each upcoming holiday in window, fire a reminder event in
     Apple Calendar (lazy catch-up: if a configured lead_day was missed, fire late).
  2. Past side — scan past `--lookback` days for holidays with default_people that
     have no history file. Write findings to MISSING.md so Claude can proactively
     ask the user to record retroactively.

Flow:
  - load holidays + state
  - upsert pending events (lazy catch-up)
  - rebuild MISSING.md from scratch

Usage:
  python daily_check.py                         # real run
  python daily_check.py --dry-run               # plan only, no writes
  python daily_check.py --days 14 --lookback 90 # custom windows
  python daily_check.py --force                 # ignore lead_days; fire on every upcoming
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path
from typing import Any

import yaml

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(HERE))

from holiday_resolver import (  # noqa: E402
    Holiday,
    Hit,
    load_holidays,
    all_in_window,
    find_pending_hits,
    find_missing_records,
)
from calendar_sync import ReminderEvent, upsert_events  # noqa: E402


_FRONTMATTER_RE = re.compile(r"^---\n(.*?)\n---\n?(.*)$", re.DOTALL)


def _parse_frontmatter(text: str) -> tuple[dict[str, Any], str]:
    m = _FRONTMATTER_RE.match(text)
    if not m:
        return {}, text
    fm = yaml.safe_load(m.group(1)) or {}
    return fm, m.group(2)


# ─── person + history lookups ──────────────────────────────────────────────────


@dataclass
class HistoryEntry:
    date: date
    holiday: str
    person: str
    action: str
    cost: float | None
    feedback: str
    rating: int | None
    body: str
    path: Path


def load_person(root: Path, person_id: str) -> dict[str, Any] | None:
    p = root / "people" / f"{person_id}.md"
    if not p.exists():
        return None
    fm, body = _parse_frontmatter(p.read_text(encoding="utf-8"))
    fm["_body"] = body.strip()
    return fm


def load_history_for(root: Path, holiday_id: str, person_id: str | None = None) -> list[HistoryEntry]:
    out: list[HistoryEntry] = []
    for path in sorted((root / "history").rglob(f"*__{holiday_id}__*.md")):
        if person_id is not None and not path.stem.endswith(f"__{person_id}"):
            continue
        try:
            fm, body = _parse_frontmatter(path.read_text(encoding="utf-8"))
        except Exception as e:
            print(f"[warn] {path}: {e}", file=sys.stderr)
            continue
        try:
            d = fm["date"] if isinstance(fm["date"], date) else date.fromisoformat(str(fm["date"]))
        except Exception:
            continue
        out.append(
            HistoryEntry(
                date=d,
                holiday=fm.get("holiday", ""),
                person=fm.get("person", ""),
                action=fm.get("action", ""),
                cost=fm.get("cost"),
                feedback=(fm.get("feedback") or "").strip(),
                rating=fm.get("rating"),
                body=body.strip(),
                path=path,
            )
        )
    out.sort(key=lambda h: h.date, reverse=True)
    return out


# ─── notes builder ─────────────────────────────────────────────────────────────


def _format_person_brief(person: dict[str, Any]) -> str:
    lines = [f"档案：{person.get('name', person.get('id', ''))}"]
    for key, label in [
        ("preferences", "偏好"),
        ("allergies", "过敏/忌讳"),
        ("sizes", "尺码"),
    ]:
        val = person.get(key)
        if isinstance(val, str) and val.strip():
            collapsed = " / ".join(line.strip() for line in val.splitlines() if line.strip())
            lines.append(f"  {label}: {collapsed}")
    return "\n".join(lines)


def _format_history_line(h: HistoryEntry) -> str:
    parts = [f"{h.date.year}", h.action.strip()]
    if h.cost:
        parts.append(f"¥{h.cost}")
    if h.feedback:
        rating_str = f" ★{h.rating}" if h.rating else ""
        parts.append(f"反馈：{h.feedback}{rating_str}")
    else:
        parts.append("反馈：（未记录）")
    return " · ".join(parts)


def build_notes(root: Path, hit: Hit, person_ids: list[str], today: date) -> str:
    actual = (hit.holiday_date - today).days
    blocks: list[str] = []
    blocks.append(f"{hit.holiday.name_cn} · {hit.holiday_date.isoformat()} (T-{actual})")

    if hit.holiday.notes:
        blocks.append(f"\n节日备注：\n{hit.holiday.notes.strip()}")

    if not person_ids:
        blocks.append("\n（未关联人物。在 holiday 文件 default_people 里加 id 可让历史自动出现。）")
        return "\n".join(blocks)

    for pid in person_ids:
        person = load_person(root, pid)
        history = load_history_for(root, hit.holiday.id, pid)
        section = [f"\n── {pid} ──"]
        if person:
            section.append(_format_person_brief(person))
        else:
            section.append(f"（未找到 people/{pid}.md，建议先 add-person）")
        if history:
            section.append("历史：")
            for h in history[:5]:
                section.append(f"  • {_format_history_line(h)}")
        else:
            section.append("历史：无记录")
        blocks.append("\n".join(section))

    return "\n".join(blocks)


def build_title(hit: Hit, today: date) -> str:
    actual = (hit.holiday_date - today).days
    md = f"{hit.holiday_date.month}月{hit.holiday_date.day}日"
    if actual == 0:
        return f"🎁 今天就是{hit.holiday.name_cn} · {md}"
    if actual == 1:
        return f"🎁 明天是{hit.holiday.name_cn} · {md}"
    return f"🎁 {actual} 天后：{hit.holiday.name_cn} · {md}"


def build_reminder_event(root: Path, hit: Hit, today: date) -> ReminderEvent:
    person_ids = list(hit.holiday.default_people)
    notes = build_notes(root, hit, person_ids, today)
    actual = (hit.holiday_date - today).days
    if actual < hit.lead_day:
        largest = max(hit.consolidated_leads) if hit.consolidated_leads else hit.lead_day
        original = hit.holiday_date - timedelta(days=largest)
        notes = (
            f"⚠️ 补跑：本应在 {largest} 天前（{original.isoformat()}）提醒，被错过了，今天补上。\n\n"
        ) + notes
    return ReminderEvent(
        key=f"my-calendar:{hit.holiday.id}:{hit.holiday_date.year}:lead{hit.lead_day}",
        title=build_title(hit, today),
        notes=notes,
        on_date=today,
    )


def _write_consolidation_state(state_path: Path, hits: list[Hit], events: list[ReminderEvent]) -> None:
    """After upsert, write extra state entries for consolidated leads pointing to the same event_id."""
    if not state_path.exists():
        return
    state = json.loads(state_path.read_text(encoding="utf-8"))
    changed = False
    for hit, event in zip(hits, events):
        primary_entry = state.get(event.key)
        if not primary_entry:
            continue
        for lead in hit.consolidated_leads:
            if lead == hit.lead_day:
                continue
            ck = f"my-calendar:{hit.holiday.id}:{hit.holiday_date.year}:lead{lead}"
            if ck in state:
                continue
            state[ck] = {
                "event_id": primary_entry["event_id"],
                "on_date": primary_entry["on_date"],
                "last_synced": primary_entry["last_synced"],
                "consolidated_into": event.key,
            }
            changed = True
    if changed:
        state_path.write_text(
            json.dumps(state, indent=2, ensure_ascii=False, sort_keys=True),
            encoding="utf-8",
        )


# ─── MISSING.md ────────────────────────────────────────────────────────────────


def write_missing_md(path: Path, missing: list[tuple[Holiday, date, str]], today: date) -> None:
    lines = [
        "# 待补记录",
        "",
        "> Claude 看到这个文件且非空时，请**主动告知用户**有几条节日记录待补，",
        "> 逐项询问「那天/那段时间为这位做了什么」（送了礼/打了电话/没过 都算），",
        "> 通过 `record-history` skill 落盘。落盘后下次 daily_check (06:00) 跑时本文件自动重建。",
        ">",
        "> 如果用户说「那天什么都没做、不想记」，仍要写一个最简 history 文件——",
        "> action 字段填 `未庆祝`，feedback 留空。这样下次不会再被列出。",
        "",
        "## 待补条目",
        "",
    ]
    for h, hday, pid in sorted(missing, key=lambda x: x[1], reverse=True):
        days_ago = (today - hday).days
        lines.append(f"- [ ] {hday.isoformat()} {h.name_cn} (`{h.id}`) — `{pid}` — {days_ago} 天前")
    lines.append("")
    lines.append(f"_由 daily_check.py 自动生成于 {today.isoformat()}_")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


# ─── main ──────────────────────────────────────────────────────────────────────


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--root", type=Path, default=ROOT)
    parser.add_argument("--days", type=int, default=30, help="forward window for reminders")
    parser.add_argument("--lookback", type=int, default=60, help="backward window for missing records")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--force", action="store_true", help="ignore lead_days; fire on every upcoming holiday in window")
    parser.add_argument("--today", type=str, help="override today (ISO) for testing")
    args = parser.parse_args()

    today = date.fromisoformat(args.today) if args.today else date.today()
    print(f"[my-calendar] today={today.isoformat()}  fwd={args.days}d  back={args.lookback}d  dry-run={args.dry_run}  force={args.force}")

    holidays = load_holidays(args.root)
    print(f"  loaded {len(holidays)} holiday definitions")

    state_path = HERE / "state.json"
    state = json.loads(state_path.read_text(encoding="utf-8")) if state_path.exists() else {}

    # ─── future side: reminders ───────────────────────────────────────────────
    upcoming = all_in_window(holidays, today, args.days)
    print(f"\n  upcoming in next {args.days}d:")
    if not upcoming:
        print("    (none)")
    for h, d in upcoming:
        delta = (d - today).days
        marker = " *" if delta in h.lead_days else ""
        print(f"    {d.isoformat()}  T+{delta:>3}  {h.id:<22}{marker}")

    if args.force:
        hits = [Hit(holiday=h, holiday_date=d, lead_day=(d - today).days) for h, d in upcoming]
    else:
        hits = find_pending_hits(holidays, today, args.days, state)

    print(f"\n  pending reminder hits: {len(hits)}")
    events = [build_reminder_event(args.root, hit, today) for hit in hits]
    for e in events:
        print(f"    [{e.key}]  →  {e.title}")

    if events:
        actions = upsert_events(events, state_path, dry_run=args.dry_run)
        if not args.dry_run:
            _write_consolidation_state(state_path, hits, events)
        print("\n  upsert result:")
        for k, a in actions.items():
            print(f"    {a:>14}  {k}")

    # ─── past side: missing history ───────────────────────────────────────────
    missing = find_missing_records(holidays, today, args.lookback, args.root)
    missing_path = args.root / "MISSING.md"
    print(f"\n  scanning past {args.lookback}d for unrecorded holidays...")
    if missing:
        if not args.dry_run:
            write_missing_md(missing_path, missing, today)
        print(f"  ⚠️ {len(missing)} 条待补记录{' (dry-run, 未写文件)' if args.dry_run else ' → MISSING.md'}")
        for h, hday, pid in sorted(missing, key=lambda x: x[1], reverse=True):
            days_ago = (today - hday).days
            print(f"    {hday.isoformat()}  ({days_ago:>3}d ago)  {h.name_cn:<14}  →  {pid}")
    else:
        if missing_path.exists() and not args.dry_run:
            missing_path.unlink()
            print("  ✓ 无待补记录（已删除旧 MISSING.md）")
        else:
            print("  ✓ 无待补记录")

    return 0


if __name__ == "__main__":
    sys.exit(main())
