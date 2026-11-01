"""Tests for the calendar / downtime / status history / labor generator."""
import sqlite3
from datetime import datetime

import pytest

from config.settings import SCHEMA_SQL
from src.db.loader import load_tables
from src.generator import ids
from src.generator.calendars import (
    BREAKDOWN_SHARE, CALENDAR_LOAD_ORDER, GROUP_TO_SKILL, HOLIDAY_END, HOLIDAY_START,
    HORIZON_END_DT, HORIZON_START_DT, TS, generate_calendar_data,
)
from src.generator.master_data import MASTER_LOAD_ORDER, generate_master_data

WORKING_DAYS = 40  # 8 weeks x 5 days


def _dt(text):
    return datetime.strptime(text, TS)


def _shift_codes(pattern):
    return [f"S{n}" for n in (1, 2, 3) if pattern[f"s{n}_start"] is not None]


@pytest.fixture(scope="module", params=["starter", "full"])
def data(request):
    master = generate_master_data(request.param)
    return master, generate_calendar_data(master)


def _by_machine(rows, key):
    grouped = {}
    for r in rows:
        grouped.setdefault(r["machine_id"], []).append(r)
    for rs in grouped.values():
        rs.sort(key=lambda r: r[key])
    return grouped


def test_calendar_row_count_and_ids(data):
    master, cal = data
    patterns = {p["pattern_id"]: p for p in master["shift_pattern"]}
    expected = sum(len(_shift_codes(patterns[m["default_shift_pattern"]])) * WORKING_DAYS
                   for m in master["machine"])
    rows = cal["machine_calendar"]
    assert len(rows) == expected
    assert len({r["calendar_id"] for r in rows}) == len(rows)
    assert all(ids.is_valid("calendar", r["calendar_id"]) for r in rows)
    for r in rows:
        assert r["calendar_id"] == ids.calendar_id(
            r["machine_id"], r["window_start"][:10], int(r["shift_code"][1]))


def test_calendar_windows_never_overlap_and_minutes_match(data):
    _, cal = data
    for rows in _by_machine(cal["machine_calendar"], "window_start").values():
        for r in rows:
            start, end = _dt(r["window_start"]), _dt(r["window_end"])
            assert (end - start).total_seconds() / 60 == r["available_minutes"]
            assert start.weekday() < 5
            assert HORIZON_START_DT <= start < HORIZON_END_DT
        for a, b in zip(rows, rows[1:]):
            assert a["window_end"] <= b["window_start"]


def test_calendar_shifts_match_machine_pattern(data):
    master, cal = data
    patterns = {p["pattern_id"]: p for p in master["shift_pattern"]}
    allowed = {m["machine_id"]: set(_shift_codes(patterns[m["default_shift_pattern"]]))
               for m in master["machine"]}
    for r in cal["machine_calendar"]:
        assert r["shift_code"] in allowed[r["machine_id"]]


def test_downtime_never_overlaps_for_a_machine(data):
    _, cal = data
    for rows in _by_machine(cal["machine_downtime"], "down_start").values():
        for a, b in zip(rows, rows[1:]):
            assert a["down_end"] <= b["down_start"]


def test_downtime_ids_types_and_order(data):
    _, cal = data
    rows = cal["machine_downtime"]
    assert [r["downtime_id"] for r in rows] == [ids.downtime_id(i) for i in range(1, len(rows) + 1)]
    assert all(ids.is_valid("downtime", r["downtime_id"]) for r in rows)
    assert {r["downtime_type"] for r in rows} <= {
        "PLANNED_MAINTENANCE", "BREAKDOWN", "CALIBRATION", "HOLIDAY"}
    assert all(r["down_end"] > r["down_start"] for r in rows)


def test_each_machine_has_one_planned_maintenance_inside_horizon(data):
    master, cal = data
    rows = [r for r in cal["machine_downtime"] if r["downtime_type"] == "PLANNED_MAINTENANCE"]
    assert sorted(r["machine_id"] for r in rows) == sorted(m["machine_id"] for m in master["machine"])
    for r in rows:
        assert HORIZON_START_DT <= _dt(r["down_start"]) < HORIZON_END_DT


def test_breakdowns_are_history_before_the_horizon(data):
    master, cal = data
    rows = [r for r in cal["machine_downtime"] if r["downtime_type"] == "BREAKDOWN"]
    assert len(rows) == max(1, round(BREAKDOWN_SHARE * len(master["machine"])))
    assert all(_dt(r["down_end"]) <= HORIZON_START_DT for r in rows)


def test_every_machine_has_the_holiday(data):
    master, cal = data
    rows = [r for r in cal["machine_downtime"] if r["downtime_type"] == "HOLIDAY"]
    assert len(rows) == len(master["machine"])
    assert all(r["down_start"] == HOLIDAY_START and r["down_end"] == HOLIDAY_END for r in rows)


def test_status_history_is_a_consistent_chain(data):
    master, cal = data
    status = {m["machine_id"]: m["status"] for m in master["machine"]}
    grouped = _by_machine(cal["machine_status_history"], "history_id")
    assert set(grouped) == set(status)
    for mid, rows in grouped.items():
        assert rows[0]["from_status"] is None
        for a, b in zip(rows, rows[1:]):
            assert b["from_status"] == a["to_status"]
            assert a["changed_at"] <= b["changed_at"]
        assert rows[-1]["to_status"] == status[mid]


def test_status_history_ids_and_count(data):
    master, cal = data
    rows = cal["machine_status_history"]
    breakdowns = sum(1 for r in cal["machine_downtime"] if r["downtime_type"] == "BREAKDOWN")
    assert len(rows) == len(master["machine"]) + 2 * breakdowns
    assert [r["history_id"] for r in rows] == [ids.machine_history_id(i)
                                               for i in range(1, len(rows) + 1)]


def test_labor_shape_and_holiday(data):
    master, cal = data
    rows = cal["labor_shift_capacity"]
    assert len(rows) == len(master["labor_pool"]) * WORKING_DAYS * 3
    keys = [(r["pool_id"], r["work_date"], r["shift_code"]) for r in rows]
    assert len(set(keys)) == len(keys)
    assert all(r["headcount_available"] == 0 for r in rows if r["work_date"] == "2026-12-25")


def test_labor_headcount_follows_running_machines(data):
    master, cal = data
    patterns = {p["pattern_id"]: p for p in master["shift_pattern"]}
    running = {}
    for m in master["machine"]:
        skill = GROUP_TO_SKILL[m["machine_group"]]
        for code in _shift_codes(patterns[m["default_shift_pattern"]]):
            running[(skill, code)] = running.get((skill, code), 0) + 1
    for r in cal["labor_shift_capacity"]:
        assert 0 <= r["headcount_available"] <= 30
        if r["work_date"] == "2026-12-25":
            continue
        skill = r["pool_id"].replace("LAB-", "")
        n = running.get((skill, r["shift_code"]), 0)
        if n == 0:
            assert r["headcount_available"] == 0
        else:
            assert r["headcount_available"] >= 2


def test_master_plus_calendar_load_into_real_schema(data):
    master, cal = data
    con = sqlite3.connect(":memory:")
    con.executescript(SCHEMA_SQL.read_text(encoding="utf-8"))
    con.execute("PRAGMA foreign_keys = ON")
    everything = {**master, **cal}
    loaded = load_tables(con, everything, MASTER_LOAD_ORDER + CALENDAR_LOAD_ORDER)
    con.commit()
    assert loaded == {name: len(everything[name]) for name in loaded}
    assert con.execute("PRAGMA foreign_key_check").fetchall() == []
    con.close()


def test_generation_is_deterministic():
    master = generate_master_data("starter")
    assert generate_calendar_data(master) == generate_calendar_data(master)


def test_different_seed_changes_downtime():
    master = generate_master_data("starter")
    assert generate_calendar_data(master, seed=1) != generate_calendar_data(master)