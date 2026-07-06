"""Health module calendar entrypoint. Run from launchd every 30 min.

Two responsibilities per tick:
  1. Schedule side — upsert today's AND tomorrow's planned events from
     health/plans/week.md + health/medications.md: med reminder (09:00),
     meal-1 suggestion (10:00), meal-2 suggestion (17:00), workout slot.
     Events are timed, carry a relative alarm (fires AT event time), and sync
     to iCloud, so the iPhone still notifies while the Mac sleeps.
     Pre-creating tomorrow keeps morning alarms alive across overnight sleep.
  2. Nag side — if today's log (health/log/YYYY/YYYY-MM-DD.md) is missing an
     expected record past a threshold time, create a one-shot nag event.
     Once the record lands, the next tick removes the nag from the calendar.

Design mirrors daily_check.py: markdown + YAML frontmatter as the source of
truth, deterministic event UIDs (my-calendar:health:<date>:<slot>), a dedicated
calendar ("健康提醒") and dedicated state (scripts/health_state.json). A small
content-hash cache (scripts/health_plan_cache.json) skips EventKit writes when
nothing changed, so the 30-min tick doesn't churn iCloud sync.

Usage:
  python health_check.py             # real run
  python health_check.py --dry-run   # plan only, no EventKit writes
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from datetime import date, datetime, time, timedelta
from pathlib import Path
from typing import Any

import yaml

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(HERE))

from log_setup import redirect_stdio_to_log  # noqa: E402

HEALTH_DIR = ROOT / "health"
WEEK_PLAN_PATH = HEALTH_DIR / "plans" / "week.md"
MEDS_PATH = HEALTH_DIR / "medications.md"
LOG_DIR = HEALTH_DIR / "log"
STATE_PATH = HERE / "health_state.json"
PLAN_CACHE_PATH = HERE / "health_plan_cache.json"

HEALTH_CALENDAR_NAME = "健康提醒"

# ─── daily slots（时间都是本地时区；改这里即可整体平移） ───────────────────────
MED_DEFAULT_TIME = time(9, 0)
MEAL1_TIME = time(10, 0)          # 16+8 窗口第一顿 10:00–11:00
MEAL2_TIME = time(17, 0)          # 第二顿 17:00–18:00
MEAL_DURATION_MIN = 60
WORKOUT_WEEKDAY_TIME = time(9, 15)   # 上班(11:00)前的运动窗口
WORKOUT_WEEKEND_TIME = time(15, 0)   # 双休日游泳，两餐之间
WORKOUT_DURATION_MIN = 60

# ─── nag thresholds ────────────────────────────────────────────────────────────
NAG_MEAL1_AFTER = time(12, 30)    # 第一顿窗口结束 1.5h 后还没记录 → 催
NAG_MEAL2_AFTER = time(19, 30)    # 第二顿同理
NAG_WEIGHT_AFTER = time(12, 30)
WEIGHT_STALE_DAYS = 3             # 体重连续 N 天没记才催（不要求天天称）

STATE_RETENTION_DAYS = 30

# ─── fallback advice：week.md 没覆盖到的日期用这套通用建议 ─────────────────────
DEFAULT_MEAL1 = (
    "蛋白质优先开窗：鸡蛋 2 个 + 无糖酸奶或牛奶一杯 + 全麦面包/玉米 1 份；"
    "便利店版：茶叶蛋×2 + 即食鸡胸半包 + 香蕉 1 根。"
)
DEFAULT_MEAL2 = (
    "一掌半瘦肉蛋白（鸡胸/牛肉/鱼虾任选）+ 双手捧满的蔬菜 + 一拳主食（杂粮饭优先）；"
    "外卖优先：轻食碗，或清汤麻辣烫（多菜多肉、主食减半）。含糖饮料换无糖/气泡水。"
)
DEFAULT_WORKOUT_WEEKDAY = (
    "上班前快走 35 分钟（平路，护右膝）；或家庭力量 20 分钟：臀桥 15×3、"
    "哑铃罗马尼亚硬拉 10×3、弹力带划船 15×3、前臂平板 30s×3（全程不压手腕）。"
)
DEFAULT_WORKOUT_WEEKEND = (
    "游泳 45–60 分钟：自由泳/仰泳为主，蛙泳强推水手腕有感觉就停；"
    "可加浮板夹腿纯打腿组（完全不用手）。"
)

MEAL1_FOOTER = (
    "—\n吃完随手拍张照，对 Claude 说“记一下第一顿”（record-health）。"
    "记录之后，17:00 的第二顿建议会根据这一顿的内容针对性调整。"
)
MEAL2_FOOTER = (
    "—\n如果上午记录过第一顿，这条建议已经是按第一顿调整后的版本。"
    "吃完同样记一下，18:00 后进食窗口关闭。"
)
WORKOUT_FOOTER = "—\n伤处红线：右腕不承重不扭转、右膝不爬楼不跳跃；疼痛加重立即停止并记一条 symptom。"


_FRONTMATTER_RE = re.compile(r"^---\n(.*?)\n---\n?(.*)$", re.DOTALL)


def _parse_frontmatter(text: str) -> tuple[dict[str, Any], str]:
    m = _FRONTMATTER_RE.match(text)
    if not m:
        return {}, text
    fm = yaml.safe_load(m.group(1)) or {}
    return fm, m.group(2)


# ─── data loading ──────────────────────────────────────────────────────────────


def load_week_plan(path: Path = WEEK_PLAN_PATH) -> dict[str, dict[str, Any]]:
    """{iso-date: {meal1, meal2, workout, workout_time?}} — missing file → {}."""
    if not path.exists():
        return {}
    fm, _ = _parse_frontmatter(path.read_text(encoding="utf-8"))
    days = fm.get("days") or {}
    out: dict[str, dict[str, Any]] = {}
    for k, v in days.items():
        key = k.isoformat() if isinstance(k, date) else str(k)
        if isinstance(v, dict):
            out[key] = v
    return out


def load_medications(path: Path = MEDS_PATH) -> list[dict[str, Any]]:
    """Active daily medications from health/medications.md frontmatter."""
    if not path.exists():
        return []
    fm, _ = _parse_frontmatter(path.read_text(encoding="utf-8"))
    meds = fm.get("medications") or []
    return [m for m in meds if isinstance(m, dict) and m.get("status") == "active"]


def load_day_log(d: date, log_dir: Path = LOG_DIR) -> dict[str, Any]:
    path = log_dir / str(d.year) / f"{d.isoformat()}.md"
    if not path.exists():
        return {}
    fm, _ = _parse_frontmatter(path.read_text(encoding="utf-8"))
    return fm


def has_meal(log_fm: dict[str, Any], slot: int) -> bool:
    return any(
        isinstance(m, dict) and m.get("slot") == slot
        for m in (log_fm.get("meals") or [])
    )


def last_weight_date(today: date, log_dir: Path = LOG_DIR) -> date | None:
    """Most recent day (within lookback) that has a weight_kg entry."""
    for i in range(WEIGHT_STALE_DAYS + 11):
        d = today - timedelta(days=i)
        fm = load_day_log(d, log_dir)
        if fm.get("weight_kg") not in (None, ""):
            return d
    return None


def _parse_hhmm(value: Any, fallback: time) -> time:
    if isinstance(value, time):
        return value
    if isinstance(value, str) and re.fullmatch(r"\d{1,2}:\d{2}", value.strip()):
        h, m = value.strip().split(":")
        return time(int(h), int(m))
    return fallback


# ─── event building（纯函数，可单测） ──────────────────────────────────────────


def build_scheduled_events(d: date, plan: dict[str, dict[str, Any]],
                           meds: list[dict[str, Any]]) -> list:
    from calendar_sync import ReminderEvent  # lazy: keeps pure logic importable sans EventKit

    day = plan.get(d.isoformat(), {})
    is_weekend = d.weekday() >= 5
    events = []

    for med in meds:
        med_id = str(med.get("id") or "med")
        med_time = _parse_hhmm(med.get("time"), MED_DEFAULT_TIME)
        notes = f"{med.get('name', med_id)} · {med.get('dose', '')}".strip(" ·")
        if med.get("notes"):
            notes += f"\n{med['notes']}"
        notes += "\n—\n漏服规则：当天想起就补服，第二天不要双倍。"
        events.append(ReminderEvent(
            key=f"my-calendar:health:{d.isoformat()}:med-{med_id}",
            title="💊 用药提醒",     # 标题不带药名，锁屏通知不暴露隐私；详情在描述里
            notes=notes,
            on_date=d,
            start_at=datetime.combine(d, med_time),
            duration_min=15,
            alarm_offset_min=0,
        ))

    meal1 = str(day.get("meal1") or DEFAULT_MEAL1)
    events.append(ReminderEvent(
        key=f"my-calendar:health:{d.isoformat()}:meal1",
        title="🍳 第一顿（10–11 点）· 今日建议",
        notes=f"{meal1}\n{MEAL1_FOOTER}",
        on_date=d,
        start_at=datetime.combine(d, MEAL1_TIME),
        duration_min=MEAL_DURATION_MIN,
        alarm_offset_min=0,
    ))

    meal2 = str(day.get("meal2") or DEFAULT_MEAL2)
    events.append(ReminderEvent(
        key=f"my-calendar:health:{d.isoformat()}:meal2",
        title="🥗 第二顿（17–18 点）· 今日建议",
        notes=f"{meal2}\n{MEAL2_FOOTER}",
        on_date=d,
        start_at=datetime.combine(d, MEAL2_TIME),
        duration_min=MEAL_DURATION_MIN,
        alarm_offset_min=0,
    ))

    workout = str(day.get("workout")
                  or (DEFAULT_WORKOUT_WEEKEND if is_weekend else DEFAULT_WORKOUT_WEEKDAY))
    workout_time = _parse_hhmm(day.get("workout_time"),
                               WORKOUT_WEEKEND_TIME if is_weekend else WORKOUT_WEEKDAY_TIME)
    events.append(ReminderEvent(
        key=f"my-calendar:health:{d.isoformat()}:workout",
        title="🏊 今日运动" if is_weekend else "🚶 今日运动",
        notes=f"{workout}\n{WORKOUT_FOOTER}",
        on_date=d,
        start_at=datetime.combine(d, workout_time),
        duration_min=WORKOUT_DURATION_MIN,
        alarm_offset_min=0,
    ))
    return events


def build_nags(now: datetime, today_log: dict[str, Any], state: dict[str, dict],
               last_weight: date | None) -> tuple[list, list[str]]:
    """Returns (nag events to create, nag keys to remove because the record landed)."""
    from calendar_sync import ReminderEvent

    d = now.date()
    to_create, to_remove = [], []

    def nag_key(slot: str) -> str:
        return f"my-calendar:health:{d.isoformat()}:nag-{slot}"

    def nag_event(slot: str, title: str, notes: str):
        return ReminderEvent(
            key=nag_key(slot),
            title=title,
            notes=notes + "\n—\n对 Claude 说一句话就能记录（record-health），记完这条提醒会自动消失。",
            on_date=d,
            start_at=now + timedelta(minutes=2),
            duration_min=15,
            alarm_offset_min=0,
        )

    checks = [
        ("meal1", NAG_MEAL1_AFTER, has_meal(today_log, 1),
         "📝 第一顿还没记录", "上午那顿吃了什么？拍照+一句话发给 Claude 即可。"),
        ("meal2", NAG_MEAL2_AFTER, has_meal(today_log, 2),
         "📝 第二顿还没记录", "晚上那顿吃了什么？拍照+一句话发给 Claude 即可。"),
    ]
    for slot, after, recorded, title, notes in checks:
        if recorded:
            if nag_key(slot) in state:
                to_remove.append(nag_key(slot))
        elif now.time() >= after and nag_key(slot) not in state:
            to_create.append(nag_event(slot, title, notes))

    weight_fresh = (last_weight is not None
                    and (d - last_weight).days < WEIGHT_STALE_DAYS)
    if weight_fresh or today_log.get("weight_kg") not in (None, ""):
        if nag_key("weight") in state:
            to_remove.append(nag_key("weight"))
    elif now.time() >= NAG_WEIGHT_AFTER and nag_key("weight") not in state:
        days = "还" if last_weight is None else f"已经 {(d - last_weight).days} 天"
        to_create.append(nag_event(
            "weight", "⚖️ 该称体重了",
            f"体重{days}没记录了。明早起床空腹称一下，一句话告诉 Claude。"))

    return to_create, to_remove


# ─── plan cache：内容没变就不碰 EventKit，避免 30min tick 造成 iCloud churn ────


def _event_hash(e) -> str:
    blob = "|".join([e.title, e.notes, str(e.start_at), str(e.duration_min)])
    return hashlib.sha1(blob.encode("utf-8")).hexdigest()


def _load_json(path: Path) -> dict:
    if path.exists():
        return json.loads(path.read_text(encoding="utf-8"))
    return {}


def _save_json(path: Path, data: dict) -> None:
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False, sort_keys=True),
                    encoding="utf-8")


def filter_changed(events: list, cache: dict[str, str]) -> list:
    return [e for e in events if cache.get(e.key) != _event_hash(e)]


def prune_old(state: dict[str, dict], cache: dict[str, str], today: date) -> None:
    cutoff = (today - timedelta(days=STATE_RETENTION_DAYS)).isoformat()
    date_re = re.compile(r"^my-calendar:health:(\d{4}-\d{2}-\d{2}):")
    for key in [k for k in list(state) if (m := date_re.match(k)) and m.group(1) < cutoff]:
        state.pop(key, None)
    for key in [k for k in list(cache) if (m := date_re.match(k)) and m.group(1) < cutoff]:
        cache.pop(key, None)


# ─── main ──────────────────────────────────────────────────────────────────────


def main() -> int:
    parser = argparse.ArgumentParser(description="Health calendar tick")
    parser.add_argument("--dry-run", action="store_true", help="plan only, no EventKit writes")
    args = parser.parse_args()

    now = datetime.now()
    today = now.date()
    tomorrow = today + timedelta(days=1)

    plan = load_week_plan()
    meds = load_medications()
    today_log = load_day_log(today)

    scheduled = build_scheduled_events(today, plan, meds) + \
        build_scheduled_events(tomorrow, plan, meds)

    from calendar_sync import upsert_events, remove_event, _load_state

    state = _load_state(STATE_PATH)
    cache = _load_json(PLAN_CACHE_PATH)

    nags, nag_removals = build_nags(now, today_log, state, last_weight_date(today))
    changed = filter_changed(scheduled, cache)
    to_upsert = changed + nags  # nags are never cached: created once, removed on record

    if args.dry_run:
        for e in to_upsert:
            print(f"[dry-run] upsert {e.key}  {e.title}  @ {e.start_at}")
        for key in nag_removals:
            print(f"[dry-run] remove {key}")
        if not to_upsert and not nag_removals:
            print("[dry-run] nothing to do")
        return 0

    actions = upsert_events(to_upsert, STATE_PATH, calendar_name=HEALTH_CALENDAR_NAME) \
        if to_upsert else {}
    for e in changed:
        if actions.get(e.key) in ("created", "updated"):
            cache[e.key] = _event_hash(e)
    for key in nag_removals:
        if remove_event(key, STATE_PATH):
            print(f"[ok] removed {key} (record landed)")

    # prune after upsert so state reflects the post-write world
    state = _load_state(STATE_PATH)
    prune_old(state, cache, today)
    _save_json(STATE_PATH, state)
    _save_json(PLAN_CACHE_PATH, cache)

    for key, action in actions.items():
        print(f"[ok] {action}: {key}")
    if not actions and not nag_removals:
        print(f"[ok] tick {now.isoformat(timespec='minutes')}: nothing to do")
    return 0


if __name__ == "__main__":
    redirect_stdio_to_log()
    sys.exit(main())
