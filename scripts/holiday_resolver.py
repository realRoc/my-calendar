"""Load holiday definitions and resolve their concrete dates within a window.

A holiday's `date_rule` is one of:
  - fixed:        {month, day}
  - lunar:        {month, day}             (Chinese lunar calendar)
  - nth_weekday:  {month, weekday, n}      (weekday: 0=Mon..6=Sun; n: ±1..5, -1 = last)

Public API:
  load_holidays(root) -> list[Holiday]
  upcoming(holidays, today, days) -> list[Hit]   # one Hit per (holiday, lead_day) match
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date, timedelta
from pathlib import Path
from typing import Any

import yaml
import lunardate


@dataclass
class Holiday:
    id: str
    name_cn: str
    name_en: str | None
    category: str
    date_rule: dict[str, Any]
    default_people: list[str] = field(default_factory=list)
    lead_days: list[int] = field(default_factory=lambda: [7, 1])
    notes: str = ""
    disabled: bool = False
    source_path: Path | None = None


@dataclass
class Hit:
    holiday: Holiday
    holiday_date: date                                    # the actual day the holiday falls on
    lead_day: int                                         # primary configured lead value (used in state key)
    consolidated_leads: list[int] = field(default_factory=list)
    # ↑ Other leads merged into this fire (their state keys also marked, so they don't fire later)


_FRONTMATTER_RE = re.compile(r"^---\n(.*?)\n---\n?(.*)$", re.DOTALL)


def _parse_frontmatter(text: str) -> tuple[dict[str, Any], str]:
    m = _FRONTMATTER_RE.match(text)
    if not m:
        raise ValueError("missing YAML frontmatter")
    fm = yaml.safe_load(m.group(1)) or {}
    body = m.group(2)
    return fm, body


def load_holidays(root: Path) -> list[Holiday]:
    out: list[Holiday] = []
    for path in sorted((root / "holidays").rglob("*.md")):
        try:
            fm, _body = _parse_frontmatter(path.read_text(encoding="utf-8"))
        except Exception as e:
            print(f"[warn] skipping {path}: {e}")
            continue
        if fm.get("disabled"):
            continue
        try:
            h = Holiday(
                id=fm["id"],
                name_cn=fm["name_cn"],
                name_en=fm.get("name_en"),
                category=fm.get("category", "custom"),
                date_rule=fm["date_rule"],
                default_people=fm.get("default_people") or [],
                lead_days=fm.get("lead_days") or [7, 1],
                notes=(fm.get("notes") or "").strip(),
                source_path=path,
            )
        except KeyError as e:
            print(f"[warn] {path} missing required field {e}; skipping")
            continue
        out.append(h)
    return out


# ─── date resolution ───────────────────────────────────────────────────────────


def _resolve_fixed(rule: dict, year: int) -> date | None:
    try:
        return date(year, int(rule["month"]), int(rule["day"]))
    except ValueError:
        return None


def _resolve_lunar(rule: dict, lunar_year: int) -> date | None:
    try:
        return lunardate.LunarDate(lunar_year, int(rule["month"]), int(rule["day"])).toSolarDate()
    except Exception:
        return None


def _resolve_nth_weekday(rule: dict, year: int) -> date | None:
    month = int(rule["month"])
    weekday = int(rule["weekday"])    # 0=Mon..6=Sun
    n = int(rule["n"])
    if n > 0:
        first = date(year, month, 1)
        offset = (weekday - first.weekday()) % 7
        day = 1 + offset + 7 * (n - 1)
        try:
            return date(year, month, day)
        except ValueError:
            return None
    if n < 0:
        # Find last day of month
        if month == 12:
            next_month_first = date(year + 1, 1, 1)
        else:
            next_month_first = date(year, month + 1, 1)
        last = next_month_first - timedelta(days=1)
        offset = (last.weekday() - weekday) % 7
        day = last.day - offset - 7 * (abs(n) - 1)
        try:
            return date(year, month, day)
        except ValueError:
            return None
    return None


def _candidate_dates_in_window(
    h: Holiday, today: date, end: date
) -> list[date]:
    """Return all solar dates this holiday could land on within [today, end]."""
    rule = h.date_rule
    rtype = rule.get("type")
    out: list[date] = []
    # Try ±1 year of the window to catch year-boundary cases
    years = sorted({today.year - 1, today.year, today.year + 1, end.year, end.year + 1})
    for y in years:
        if rtype == "fixed":
            d = _resolve_fixed(rule, y)
        elif rtype == "lunar":
            d = _resolve_lunar(rule, y)
        elif rtype == "nth_weekday":
            d = _resolve_nth_weekday(rule, y)
        else:
            print(f"[warn] {h.id}: unknown date_rule type {rtype!r}")
            return []
        if d and today <= d <= end:
            out.append(d)
    # Dedup while preserving order
    seen = set()
    uniq = []
    for d in out:
        if d not in seen:
            seen.add(d)
            uniq.append(d)
    return uniq


def upcoming(holidays: list[Holiday], today: date, days: int) -> list[Hit]:
    """Return Hits for every (holiday, lead_day) pair where today + lead_day == holiday_date.

    Logic: we want to fire a reminder today only if today is exactly `lead_day` days before
    the holiday (for some lead_day in the holiday's lead_days list). `days` argument here
    just bounds how far we look ahead (max(lead_days) is usually enough, but caller sets it).
    """
    end = today + timedelta(days=days)
    hits: list[Hit] = []
    for h in holidays:
        for d in _candidate_dates_in_window(h, today, end):
            delta = (d - today).days
            if delta in h.lead_days:
                hits.append(Hit(holiday=h, holiday_date=d, lead_day=delta))
    return hits


def all_in_window(holidays: list[Holiday], today: date, days: int) -> list[tuple[Holiday, date]]:
    """List every holiday landing in the window, regardless of lead_days. For --dry-run."""
    end = today + timedelta(days=days)
    out = []
    for h in holidays:
        for d in _candidate_dates_in_window(h, today, end):
            out.append((h, d))
    out.sort(key=lambda x: x[1])
    return out


def find_pending_hits(
    holidays: list[Holiday],
    today: date,
    lookahead_days: int,
    state: dict[str, dict],
) -> list[Hit]:
    """Lead-day hits with lazy catch-up + redundancy suppression.

    Rules per (holiday, year):
      - Pending = leads whose target date has passed AND no state entry exists
      - Suppression: if a smaller lead (more recent reminder) was already fired,
        skip backfilling larger leads — no point sending a "7 days before" notice
        when a "1 day before" already went out
      - Consolidation: of remaining pending leads, fire only the SMALLEST
        (most temporally relevant); mark all of them as fired in state
    """
    end = today + timedelta(days=lookahead_days)
    hits: list[Hit] = []
    for h in holidays:
        for hday in _candidate_dates_in_window(h, today, end):
            if hday < today:
                continue                              # past holiday → MISSING flow
            prefix = f"my-calendar:{h.id}:{hday.year}:lead"
            fired_leads = [int(k[len(prefix):]) for k in state if k.startswith(prefix)]
            min_fired = min(fired_leads) if fired_leads else None

            pending: list[int] = []
            for lead in h.lead_days:
                target_date = hday - timedelta(days=lead)
                if target_date > today:
                    continue                          # not yet time for this lead
                if f"{prefix}{lead}" in state:
                    continue                          # already fired
                if min_fired is not None and lead > min_fired:
                    continue                          # a more recent reminder already fired
                pending.append(lead)
            if not pending:
                continue
            primary = min(pending)                    # most temporally relevant
            hits.append(Hit(
                holiday=h,
                holiday_date=hday,
                lead_day=primary,
                consolidated_leads=sorted(pending),
            ))
    return hits


def find_missing_records(
    holidays: list[Holiday],
    today: date,
    lookback_days: int,
    root: Path,
) -> list[tuple[Holiday, date, str]]:
    """Past holidays (within lookback) that have default_people but no history file."""
    start = today - timedelta(days=lookback_days)
    end = today - timedelta(days=1)
    missing: list[tuple[Holiday, date, str]] = []
    for h in holidays:
        if not h.default_people:
            continue
        for hday in _candidate_dates_in_window(h, start, end):
            for pid in h.default_people:
                expected = root / "history" / str(hday.year) / f"{hday.isoformat()}__{h.id}__{pid}.md"
                if expected.exists():
                    continue
                missing.append((h, hday, pid))
    return missing


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Inspect holiday resolution")
    parser.add_argument("--days", type=int, default=14)
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parent.parent)
    args = parser.parse_args()

    today = date.today()
    holidays = load_holidays(args.root)
    print(f"loaded {len(holidays)} holidays")
    print(f"\nholidays in next {args.days} days:")
    for h, d in all_in_window(holidays, today, args.days):
        delta = (d - today).days
        print(f"  {d.isoformat()}  T+{delta:>3}  {h.id:<20}  {h.name_cn}")
    print(f"\nlead-day hits today ({today.isoformat()}):")
    for hit in upcoming(holidays, today, args.days):
        print(f"  lead={hit.lead_day:>3}  {hit.holiday.id:<20}  {hit.holiday.name_cn}  → {hit.holiday_date}")
