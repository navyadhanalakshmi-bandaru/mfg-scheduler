"""Tests for machine time availability."""
import sqlite3
from datetime import datetime, timedelta

import pytest

from config.settings import SCHEMA_SQL
from src.db.loader import load_tables
from src.engine.timeline import MachineTimeline, load_timelines, parse, subtract
from src.generator.export import ALL_TABLES, build_dataset


def d(h, m=0, day=2):
    return datetime(2026, 11, day, h, m)


def test_subtract_splits_and_clips():
    assert subtract([(d(6), d(14))], [(d(8), d(9))]) == [(d(6), d(8)), (d(9), d(14))]
    assert subtract([(d(6), d(14))], [(d(5), d(7)), (d(13), d(15))]) == [(d(7), d(13))]
    assert subtract([(d(6), d(14))], [(d(5), d(15))]) == []
    assert subtract([(d(6), d(14))], []) == [(d(6), d(14))]


def test_merge_adjacent_flag():
    windows = [(d(6), d(14)), (d(14), d(22))]
    assert len(subtract(windows, [])) == 2
    assert subtract(windows, [], merge_adjacent=True) == [(d(6), d(22))]


def test_earliest_start_skips_gaps_that_are_too_small():
    t = MachineTimeline([(d(6), d(8)), (d(10), d(14))])
    assert t.earliest_start(d(6), 60) == d(6)
    assert t.earliest_start(d(6), 150) == d(10)
    assert t.earliest_start(d(7), 120) == d(10)
    assert t.earliest_start(d(12), 120) == d(12)
    assert t.earliest_start(d(12), 150) is None


def test_reserve_splits_and_rejects_outside():
    t = MachineTimeline([(d(6), d(14))])
    t.reserve(d(8), d(10))
    assert t.free == [(d(6), d(8)), (d(10), d(14))]
    assert t.earliest_start(d(6), 180) == d(10)
    with pytest.raises(ValueError):
        t.reserve(d(9), d(11))
    t.reserve(d(6), d(8))
    t.reserve(d(10), d(14))
    assert t.free == [] and t.free_minutes() == 0


@pytest.fixture(scope="module", params=["starter", "full"])
def loaded(request):
    tables = build_dataset(request.param)
    con = sqlite3.connect(":memory:")
    con.executescript(SCHEMA_SQL.read_text(encoding="utf-8"))
    load_tables(con, tables, ALL_TABLES)
    yield tables, load_timelines(con)
    con.close()


def test_every_machine_has_a_timeline(loaded):
    tables, timelines = loaded
    assert set(timelines) == {m["machine_id"] for m in tables["machine"]}
    assert all(t.free for t in timelines.values())


def test_free_time_excludes_downtime(loaded):
    tables, timelines = loaded
    for dt in tables["machine_downtime"]:
        ds, de = parse(dt["down_start"]), parse(dt["down_end"])
        for s, e in timelines[dt["machine_id"]].free:
            assert e <= ds or s >= de


def test_free_time_stays_inside_calendar_windows(loaded):
    tables, timelines = loaded
    windows = {}
    for r in tables["machine_calendar"]:
        windows.setdefault(r["machine_id"], []).append((parse(r["window_start"]), parse(r["window_end"])))
    for mid, t in timelines.items():
        for s, e in t.free:
            assert any(ws <= s and e <= we for ws, we in windows[mid])


def test_free_minutes_never_exceed_calendar_minutes(loaded):
    tables, timelines = loaded
    cal = {}
    for r in tables["machine_calendar"]:
        cal[r["machine_id"]] = cal.get(r["machine_id"], 0) + r["available_minutes"]
    for mid, t in timelines.items():
        assert 0 < t.free_minutes() <= cal[mid]