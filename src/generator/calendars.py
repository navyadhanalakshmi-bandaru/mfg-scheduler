"""Calendar, downtime, status-history and labor generator (Phase 4, step 3).

Needs the master data from step 2 (machines, shift patterns, labor pools).

Rules:
  * Each ACTIVE machine gets one calendar window per shift of its shift pattern,
    on every working weekday (Mon-Fri) of the 8-week horizon. Shift S3 starts at
    22:00 and ends at 06:00 the next morning (the window crosses midnight).
  * Downtime (machine_downtime):
      - one PLANNED_MAINTENANCE per machine, in weeks 1-4;
      - one CALIBRATION (60 min) for about 30% of machines, in weeks 5-8;
      - one plant-wide HOLIDAY (2026-12-25 06:00 to 2026-12-26 06:00);
      - a few past BREAKDOWNs before the horizon (history only).
    Downtime of one machine never overlaps.
  * machine_status_history records the commissioning row for each machine, and
    ACTIVE -> MAINTENANCE -> ACTIVE for every breakdown (history is never overwritten).
  * Labor: per skill, date and shift, headcount = 80% of the machines of that
    skill running in that shift (minimum 2, maximum 30). It is 0 when no machine
    runs that shift and on the holiday. Labor can therefore be a real constraint.
"""
from __future__ import annotations

import math
from datetime import date, datetime, timedelta

from config.settings import HORIZON_START, HORIZON_WEEKS, SEED
from src.generator import ids
from src.generator.master_data import (
    CREATED_AT, OP_TO_GROUP, OP_TO_SKILL, generate_master_data,
)
from src.generator.rng import make_rng

TS = "%Y-%m-%d %H:%M:%S"
HORIZON_START_DT = datetime.strptime(HORIZON_START, TS)
HORIZON_END_DT = HORIZON_START_DT + timedelta(weeks=HORIZON_WEEKS)

HOLIDAY_DATE = date(2026, 12, 25)  # a Friday inside the horizon
HOLIDAY_START = "2026-12-25 06:00:00"
HOLIDAY_END = "2026-12-26 06:00:00"

WEEKDAY_INDEX = {"MON": 0, "TUE": 1, "WED": 2, "THU": 3, "FRI": 4, "SAT": 5, "SUN": 6}
GROUP_TO_SKILL = {OP_TO_GROUP[op]: OP_TO_SKILL[op] for op in OP_TO_GROUP}

LABOR_COVERAGE = 0.8
BREAKDOWN_SHARE = 0.12
CALIBRATION_SHARE = 0.30
PLANNED_CREATED_AT = "2026-10-01 00:00:00"

CALENDAR_LOAD_ORDER = ["machine_calendar", "machine_downtime",
                       "machine_status_history", "labor_shift_capacity"]


def _fmt(dt: datetime) -> str:
    return dt.strftime(TS)


def horizon_dates() -> list[date]:
    first = HORIZON_START_DT.date()
    return [first + timedelta(days=i) for i in range(HORIZON_WEEKS * 7)]


def _pattern_shifts(pattern: dict) -> list[tuple[int, str, int]]:
    """List of (shift number, 'HH:MM' start, minutes) for a shift pattern row."""
    shifts = []
    for n in (1, 2, 3):
        start = pattern[f"s{n}_start"]
        if start is not None:
            shifts.append((n, start, pattern[f"s{n}_minutes"]))
    return shifts


def _working_days(pattern: dict) -> list[date]:
    allowed = {WEEKDAY_INDEX[d] for d in pattern["working_weekdays"].split(",")}
    return [d for d in horizon_dates() if d.weekday() in allowed]


def _shift_start(day: date, hhmm: str) -> datetime:
    hour, minute = hhmm.split(":")
    return datetime(day.year, day.month, day.day, int(hour), int(minute))


def _build_calendar(machines: list[dict], patterns: list[dict]) -> list[dict]:
    by_id = {p["pattern_id"]: p for p in patterns}
    rows = []
    for m in sorted(machines, key=lambda r: r["machine_id"]):
        if m["status"] != "ACTIVE":
            continue
        pattern = by_id[m["default_shift_pattern"]]
        for day in _working_days(pattern):
            for n, hhmm, minutes in _pattern_shifts(pattern):
                start = _shift_start(day, hhmm)
                rows.append({
                    "calendar_id": ids.calendar_id(m["machine_id"], day.isoformat(), n),
                    "machine_id": m["machine_id"],
                    "shift_code": f"S{n}",
                    "window_start": _fmt(start),
                    "window_end": _fmt(start + timedelta(minutes=minutes)),
                    "available_minutes": minutes,
                })
    return rows


def _build_downtime(machines: list[dict], patterns: list[dict], seed: int) -> list[dict]:
    rng = make_rng("downtime", seed)
    by_id = {p["pattern_id"]: p for p in patterns}
    active = sorted((m for m in machines if m["status"] == "ACTIVE"),
                    key=lambda r: r["machine_id"])
    machine_ids = [m["machine_id"] for m in active]
    pattern_of = {m["machine_id"]: by_id[m["default_shift_pattern"]] for m in active}
    week4_end = HORIZON_START_DT.date() + timedelta(weeks=4)

    events = []  # (start, machine_id, type, end, reason, created_at)

    # 1. Planned maintenance for every machine, in weeks 1-4.
    for mid in machine_ids:
        pattern = pattern_of[mid]
        day = rng.choice([d for d in _working_days(pattern) if d < week4_end])
        _, hhmm, _ = rng.choice(_pattern_shifts(pattern))
        start = _shift_start(day, hhmm) + timedelta(minutes=rng.choice([0, 60, 120]))
        end = start + timedelta(minutes=rng.choice([120, 180, 240]))
        events.append((start, mid, "PLANNED_MAINTENANCE", end,
                       "Preventive maintenance", PLANNED_CREATED_AT))

    # 2. Calibration for about 30% of machines, in weeks 5-8 (never on the holiday).
    n_calibration = round(CALIBRATION_SHARE * len(machine_ids))
    for mid in sorted(rng.sample(machine_ids, n_calibration)):
        pattern = pattern_of[mid]
        days = [d for d in _working_days(pattern) if d >= week4_end and d != HOLIDAY_DATE]
        day = rng.choice(days)
        _, hhmm, _ = rng.choice(_pattern_shifts(pattern))
        start = _shift_start(day, hhmm) + timedelta(minutes=60)
        events.append((start, mid, "CALIBRATION", start + timedelta(minutes=60),
                       "Scheduled calibration", PLANNED_CREATED_AT))

    # 3. Plant-wide holiday.
    holiday_start = datetime.strptime(HOLIDAY_START, TS)
    holiday_end = datetime.strptime(HOLIDAY_END, TS)
    for mid in machine_ids:
        events.append((holiday_start, mid, "HOLIDAY", holiday_end,
                       "Plant holiday (Christmas Day)", PLANNED_CREATED_AT))

    # 4. Past breakdowns (before the horizon, so they are history only).
    n_breakdowns = max(1, round(BREAKDOWN_SHARE * len(machine_ids)))
    for mid in sorted(rng.sample(machine_ids, n_breakdowns)):
        day = date(2026, 9, 1) + timedelta(days=rng.randint(0, 59))  # Sep 1 - Oct 30
        start = datetime(day.year, day.month, day.day, rng.randint(6, 18), 0)
        end = start + timedelta(minutes=rng.choice([120, 180, 240, 360, 480, 600]))
        events.append((start, mid, "BREAKDOWN", end,
                       "Spindle or drive failure", _fmt(start)))

    events.sort(key=lambda e: (e[0], e[1], e[2]))
    return [{
        "downtime_id": ids.downtime_id(i),
        "machine_id": mid,
        "downtime_type": dtype,
        "down_start": _fmt(start),
        "down_end": _fmt(end),
        "reason": reason,
        "created_at": created,
    } for i, (start, mid, dtype, end, reason, created) in enumerate(events, start=1)]


def _build_status_history(machines: list[dict], downtime: list[dict]) -> list[dict]:
    events = []  # (changed_at, machine_id, from_status, to_status, reason)
    for m in sorted(machines, key=lambda r: r["machine_id"]):
        events.append((CREATED_AT, m["machine_id"], None, "ACTIVE", "Commissioned"))
    for d in downtime:
        if d["downtime_type"] == "BREAKDOWN":
            events.append((d["down_start"], d["machine_id"], "ACTIVE", "MAINTENANCE",
                           "Breakdown: " + d["reason"]))
            events.append((d["down_end"], d["machine_id"], "MAINTENANCE", "ACTIVE",
                           "Repaired and returned to service"))
    events.sort(key=lambda e: (e[0], e[1]))
    return [{
        "history_id": ids.machine_history_id(i),
        "machine_id": mid, "from_status": frm, "to_status": to,
        "changed_at": at, "reason": reason,
    } for i, (at, mid, frm, to, reason) in enumerate(events, start=1)]


def _build_labor(machines: list[dict], patterns: list[dict], pools: list[dict]) -> list[dict]:
    by_id = {p["pattern_id"]: p for p in patterns}
    running: dict[tuple[str, str], int] = {}
    for m in machines:
        skill = GROUP_TO_SKILL[m["machine_group"]]
        for n, _, _ in _pattern_shifts(by_id[m["default_shift_pattern"]]):
            key = (skill, f"S{n}")
            running[key] = running.get(key, 0) + 1

    rows = []
    for pool in sorted(pools, key=lambda p: p["pool_id"]):
        skill = pool["labor_skill"]
        for day in horizon_dates():
            if day.weekday() >= 5:
                continue
            for code in ("S1", "S2", "S3"):
                n_running = running.get((skill, code), 0)
                if day == HOLIDAY_DATE or n_running == 0:
                    headcount = 0
                else:
                    headcount = min(30, max(2, math.ceil(LABOR_COVERAGE * n_running)))
                rows.append({"pool_id": pool["pool_id"], "work_date": day.isoformat(),
                             "shift_code": code, "headcount_available": headcount})
    return rows


def generate_calendar_data(master: dict, seed: int = SEED) -> dict[str, list[dict]]:
    """Generate calendar, downtime, status history and labor tables from master data."""
    machines, patterns = master["machine"], master["shift_pattern"]
    downtime = _build_downtime(machines, patterns, seed)
    tables = {
        "machine_calendar": _build_calendar(machines, patterns),
        "machine_downtime": downtime,
        "machine_status_history": _build_status_history(machines, downtime),
        "labor_shift_capacity": _build_labor(machines, patterns, master["labor_pool"]),
    }
    assert list(tables) == CALENDAR_LOAD_ORDER
    return tables


if __name__ == "__main__":
    import sys

    chosen = sys.argv[1] if len(sys.argv) > 1 else "full"
    for table_name, table_rows in generate_calendar_data(generate_master_data(chosen)).items():
        print(f"{table_name:24s} {len(table_rows):5d}")