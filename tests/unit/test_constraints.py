"""Tests for the independent hard-constraint validator.

Each negative test copies a valid starter baseline, corrupts one thing the way an
anomaly injector would (keeping setup + processing = end consistent so the
database CHECKs still pass), and expects the validator to catch it.
"""
import ast
import json
import sqlite3
from pathlib import Path

import pytest

from config.settings import SCHEMA_SQL
from src.db.loader import load_tables
from src.engine.baseline import write_baseline
from src.engine.dispatcher import dispatch, prepare
from src.generator.export import ALL_TABLES, build_dataset
from src.validation import constraints
from src.validation.constraints import validate_and_record, validate_run


def build(profile):
    con = sqlite3.connect(":memory:")
    con.executescript(SCHEMA_SQL.read_text(encoding="utf-8"))
    load_tables(con, build_dataset(profile), ALL_TABLES)
    con.execute("PRAGMA foreign_keys = ON")
    model, feas, tls, wins = prepare(con)
    result = dispatch(model, feas, tls, wins, "PRIORITY_EDD")
    return con, write_baseline(con, model, feas, result)


@pytest.fixture(scope="module")
def starter():
    con, run_id = build("starter")
    yield con, run_id
    con.close()


@pytest.fixture()
def copy(starter):
    src, run_id = starter
    dst = sqlite3.connect(":memory:")
    src.backup(dst)
    dst.execute("PRAGMA foreign_keys = ON")
    yield dst, run_id
    dst.close()


def shift(con, schedule_id, minutes):
    """Move a whole job (setup, start, end) by some minutes, keeping it consistent."""
    mod = f"{'+' if minutes >= 0 else '-'}{abs(minutes)} minutes"
    con.execute(
        "UPDATE operation_schedule SET setup_start = datetime(setup_start, ?),"
        " start_time = datetime(start_time, ?), end_time = datetime(end_time, ?)"
        " WHERE schedule_id = ?", (mod, mod, mod, schedule_id))


def minutes_between(con, a, b):
    """Minutes from timestamp a to timestamp b."""
    return con.execute("SELECT CAST(ROUND((julianday(?) - julianday(?)) * 1440) AS INTEGER)",
                       (b, a)).fetchone()[0]


def flagged(con, run_id, code):
    return {v.schedule_id for v in validate_run(con, run_id) if v.code == code}


@pytest.mark.parametrize("profile", ["starter", "full"])
def test_clean_baseline_has_no_violations(profile):
    con, run_id = build(profile)
    found = validate_run(con, run_id)
    assert found == [], [f"{v.code}: {v.message}" for v in found[:5]]
    con.close()


def test_overlap_is_detected(copy):
    con, run_id = copy
    machine = con.execute(
        "SELECT machine_id FROM operation_schedule WHERE run_id = ? GROUP BY machine_id"
        " HAVING COUNT(*) >= 2 ORDER BY machine_id LIMIT 1", (run_id,)).fetchone()[0]
    first, second = con.execute(
        "SELECT schedule_id, setup_start FROM operation_schedule WHERE run_id = ? AND machine_id = ?"
        " ORDER BY setup_start LIMIT 2", (run_id, machine)).fetchall()
    shift(con, second[0], minutes_between(con, second[1], first[1]) + 1)
    assert second[0] in flagged(con, run_id, "MACHINE_OVERLAP")


def test_precedence_violation_is_detected(copy):
    con, run_id = copy
    a_id, b_id, a_end, b_start = con.execute(
        "SELECT a.schedule_id, b.schedule_id, a.end_time, b.start_time FROM operation_schedule a"
        " JOIN operation oa ON oa.operation_id = a.operation_id"
        " JOIN operation ob ON ob.order_id = oa.order_id AND ob.sequence_no = oa.sequence_no + 1"
        " JOIN operation_schedule b ON b.operation_id = ob.operation_id AND b.run_id = a.run_id"
        " WHERE a.run_id = ? ORDER BY a.schedule_id LIMIT 1", (run_id,)).fetchone()
    shift(con, b_id, minutes_between(con, b_start, a_end) - 10)  # start 10 min before predecessor ends
    assert b_id in flagged(con, run_id, "PRECEDENCE_VIOLATION")


def test_outside_shift_is_detected(copy):
    con, run_id = copy
    sid = con.execute("SELECT schedule_id FROM operation_schedule WHERE run_id = ?"
                      " ORDER BY schedule_id LIMIT 1", (run_id,)).fetchone()[0]
    shift(con, sid, -60 * 1440)  # 60 days earlier: before any calendar window exists
    assert sid in flagged(con, run_id, "OUTSIDE_SHIFT")


def test_missing_changeover_is_detected(copy):
    con, run_id = copy
    row = con.execute("SELECT schedule_id FROM operation_schedule WHERE run_id = ?"
                      " AND setup_minutes > 0 ORDER BY schedule_id LIMIT 1", (run_id,)).fetchone()
    if row is None:
        pytest.skip("this dataset has no changeovers")
    con.execute("UPDATE operation_schedule SET setup_minutes = 0, setup_start = start_time"
                " WHERE schedule_id = ?", (row[0],))
    assert row[0] in flagged(con, run_id, "MISSING_CHANGEOVER")


def test_material_before_availability_is_detected(copy):
    con, run_id = copy
    row = con.execute(
        "SELECT s.schedule_id FROM operation_schedule s JOIN operation_material_req q"
        " ON q.operation_id = s.operation_id WHERE s.run_id = ? ORDER BY s.schedule_id LIMIT 1",
        (run_id,)).fetchone()
    shift(con, row[0], -120 * 1440)  # 120 days earlier: before every lot arrives
    assert row[0] in flagged(con, run_id, "MATERIAL_BEFORE_AVAILABILITY")


def test_ineligible_machine_is_detected(copy):
    con, run_id = copy
    sid, group = con.execute(
        "SELECT s.schedule_id, m.machine_group FROM operation_schedule s"
        " JOIN machine m ON m.machine_id = s.machine_id WHERE s.run_id = ?"
        " ORDER BY s.schedule_id LIMIT 1", (run_id,)).fetchone()
    other = con.execute("SELECT machine_id FROM machine WHERE machine_group <> ?"
                        " ORDER BY machine_id LIMIT 1", (group,)).fetchone()[0]
    con.execute("UPDATE operation_schedule SET machine_id = ? WHERE schedule_id = ?", (other, sid))
    assert sid in flagged(con, run_id, "INELIGIBLE_MACHINE")


def test_labor_capacity_is_checked(copy):
    con, run_id = copy
    con.execute("UPDATE labor_shift_capacity SET headcount_available = 0")
    assert flagged(con, run_id, "LABOR_CAPACITY_EXCEEDED")


def test_tool_capacity_is_checked(copy):
    con, run_id = copy
    uses_tool = con.execute(
        "SELECT COUNT(*) FROM operation_schedule s JOIN operation o ON o.operation_id = s.operation_id"
        " JOIN routing_step_tool t ON t.step_id = o.step_id WHERE s.run_id = ?", (run_id,)).fetchone()[0]
    if uses_tool == 0:
        pytest.skip("no scheduled operation uses a tool in this dataset")
    con.execute("UPDATE routing_step_tool SET units_required = 10")
    assert flagged(con, run_id, "TOOL_CAPACITY_EXCEEDED")


def test_clean_run_is_marked_feasible(copy):
    con, run_id = copy
    before = con.execute("SELECT COUNT(*) FROM scheduling_exception").fetchone()[0]
    assert validate_and_record(con, run_id) == []
    label, metrics = con.execute("SELECT feasibility_label, metrics_json FROM schedule_run"
                                 " WHERE run_id = ?", (run_id,)).fetchone()
    assert label == "FEASIBLE"
    assert json.loads(metrics)["hard_violations"] == 0
    assert con.execute("SELECT COUNT(*) FROM scheduling_exception").fetchone()[0] == before


def test_corrupted_run_is_never_marked_feasible(copy):
    con, run_id = copy
    sid = con.execute("SELECT schedule_id FROM operation_schedule WHERE run_id = ?"
                      " ORDER BY schedule_id LIMIT 1", (run_id,)).fetchone()[0]
    shift(con, sid, -60 * 1440)
    found = validate_and_record(con, run_id)
    assert found
    label = con.execute("SELECT feasibility_label FROM schedule_run WHERE run_id = ?",
                        (run_id,)).fetchone()[0]
    assert label == "INFEASIBLE_VIOLATIONS"
    rows = con.execute("SELECT category, severity FROM scheduling_exception"
                       " WHERE run_id = ? AND detected_by = 'VALIDATOR'", (run_id,)).fetchall()
    assert len(rows) == len(found)
    assert all(r == ("CONSTRAINT_VIOLATION", "HARD") for r in rows)


def test_a_run_cannot_be_validated_twice(copy):
    con, run_id = copy
    validate_and_record(con, run_id)
    with pytest.raises(ValueError):
        validate_and_record(con, run_id)


def test_unknown_run_is_rejected(copy):
    con, _ = copy
    with pytest.raises(ValueError):
        validate_run(con, "RUN-2026-999999")


def test_validator_does_not_import_engine_or_labels():
    tree = ast.parse(Path(constraints.__file__).read_text(encoding="utf-8"))
    imported = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported += [a.name for a in node.names]
        elif isinstance(node, ast.ImportFrom):
            imported.append(node.module or "")
    joined = " ".join(imported).lower()
    for banned in ("engine", "anomal", "ground"):
        assert banned not in joined