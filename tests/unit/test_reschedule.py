"""Tests for rescheduling and run comparison (starter dataset)."""
import ast
import json
import sqlite3
from pathlib import Path

import pytest

from config.settings import SCHEMA_SQL
from src.db.loader import load_tables
from src.engine.baseline import write_baseline
from src.engine.dispatcher import dispatch, prepare
from src.engine.reschedule import reschedule
from src.engine.reschedule_demo import pick_breakdown
from src.generator.export import ALL_TABLES, build_dataset
from src.metrics import compare as compare_module
from src.metrics.compare import compare_runs
from src.metrics.kpis import compute_metrics
from src.validation.constraints import validate_and_record


@pytest.fixture(scope="module")
def base():
    con = sqlite3.connect(":memory:")
    con.executescript(SCHEMA_SQL.read_text(encoding="utf-8"))
    load_tables(con, build_dataset("starter"), ALL_TABLES)
    con.execute("PRAGMA foreign_keys = ON")
    model, feas, tls, wins = prepare(con)
    run_id = write_baseline(con, model, feas, dispatch(model, feas, tls, wins, "CRITICAL_RATIO"))
    starts = sorted(r[0] for r in con.execute(
        "SELECT setup_start FROM operation_schedule WHERE run_id = ?", (run_id,)))
    trigger = starts[len(starts) // 2]  # splits the baseline into started and future work
    yield con, run_id, trigger
    con.close()


@pytest.fixture()
def db(base):
    src, run_id, trigger = base
    dst = sqlite3.connect(":memory:")
    src.backup(dst)
    dst.execute("PRAGMA foreign_keys = ON")
    yield dst, run_id, trigger
    dst.close()


def one(con, sql, *args):
    return con.execute(sql, args).fetchone()[0]


def rows_of(con, run_id, extra=""):
    return {r[0]: r[1:] for r in con.execute(
        "SELECT operation_id, machine_id, setup_start, start_time, end_time FROM operation_schedule"
        f" WHERE run_id = ? {extra}", (run_id,))}


def test_started_work_is_kept_exactly(db):
    con, parent, trigger = db
    new = reschedule(con, parent, trigger, "control")
    expected = {op: v for op, v in rows_of(con, parent).items() if v[1] < trigger}
    assert expected
    assert rows_of(con, new, "AND status = 'LOCKED'") == expected


def test_nothing_new_starts_before_the_trigger(db):
    con, parent, trigger = db
    new = reschedule(con, parent, trigger, "control")
    assert one(con, "SELECT COUNT(*) FROM operation_schedule WHERE run_id = ? AND status = 'PLANNED'"
                    " AND setup_start < ?", new, trigger) == 0
    assert one(con, "SELECT COUNT(*) FROM operation_schedule WHERE run_id = ? AND status = 'PLANNED'",
               new) > 0


def test_reschedule_passes_the_independent_validator(db):
    con, parent, trigger = db
    new = reschedule(con, parent, trigger, "control")
    found = validate_and_record(con, new)
    assert found == [], [v.message for v in found[:3]]
    assert one(con, "SELECT feasibility_label FROM schedule_run WHERE run_id = ?", new) == "FEASIBLE"


def test_breakdown_blocks_the_machine_and_stays_feasible(db):
    con, parent, trigger = db
    d = pick_breakdown(con, parent, trigger, 48)
    assert d is not None
    new = reschedule(con, parent, trigger, "breakdown", downtimes=[d])
    clash = one(con, "SELECT COUNT(*) FROM operation_schedule WHERE run_id = ? AND machine_id = ?"
                     " AND setup_start < ? AND end_time > ?",
                new, d["machine_id"], d["down_end"], d["down_start"])
    assert clash == 0
    found = validate_and_record(con, new)
    assert found == [], [v.message for v in found[:3]]


def test_downtime_is_recorded_append_only(db):
    con, parent, trigger = db
    d = pick_breakdown(con, parent, trigger, 48)
    before = one(con, "SELECT COUNT(*) FROM machine_downtime")
    new = reschedule(con, parent, trigger, "breakdown", downtimes=[d])
    assert one(con, "SELECT COUNT(*) FROM machine_downtime") == before + 1
    row = con.execute("SELECT downtime_id, machine_id, down_start, down_end, downtime_type"
                      " FROM machine_downtime ORDER BY downtime_id DESC LIMIT 1").fetchone()
    assert row[1:] == (d["machine_id"], d["down_start"], d["down_end"], "BREAKDOWN")
    config = json.loads(one(con, "SELECT config_json FROM schedule_run WHERE run_id = ?", new))
    assert config["new_downtime_ids"] == [row[0]]


def test_parent_run_is_not_modified(db):
    con, parent, trigger = db
    q = "SELECT * FROM operation_schedule WHERE run_id = ? ORDER BY schedule_id"
    before = con.execute(q, (parent,)).fetchall()
    reschedule(con, parent, trigger, "control")
    assert con.execute(q, (parent,)).fetchall() == before


def test_run_metadata(db):
    con, parent, trigger = db
    new = reschedule(con, parent, trigger, "control")
    row = con.execute("SELECT run_type, parent_run_id, trigger_time, trigger_reason, status,"
                      " feasibility_label, scheduling_rule FROM schedule_run WHERE run_id = ?",
                      (new,)).fetchone()
    assert row == ("RESCHEDULE", parent, trigger, "control", "COMPLETED", "NOT_VALIDATED",
                   "CRITICAL_RATIO")


def test_rejects_downtime_before_the_trigger(db):
    con, parent, trigger = db
    d = pick_breakdown(con, parent, trigger, 48)
    early = one(con, "SELECT datetime(?, '-1 hours')", trigger)
    runs, downs = one(con, "SELECT COUNT(*) FROM schedule_run"), one(con, "SELECT COUNT(*) FROM machine_downtime")
    with pytest.raises(ValueError, match="before the trigger"):
        reschedule(con, parent, trigger, "x", downtimes=[{**d, "down_start": early}])
    assert one(con, "SELECT COUNT(*) FROM schedule_run") == runs
    assert one(con, "SELECT COUNT(*) FROM machine_downtime") == downs


def test_rejects_downtime_that_hits_a_started_job(db):
    con, parent, _ = db
    machine, setup = con.execute(
        "SELECT machine_id, setup_start FROM operation_schedule WHERE run_id = ?"
        " ORDER BY julianday(end_time) - julianday(setup_start) DESC, schedule_id LIMIT 1",
        (parent,)).fetchone()
    trig = one(con, "SELECT datetime(?, '+1 minutes')", setup)
    end = one(con, "SELECT datetime(?, '+2 hours')", trig)
    with pytest.raises(ValueError, match="locked job"):
        reschedule(con, parent, trig, "x",
                   downtimes=[{"machine_id": machine, "down_start": trig, "down_end": end}])


def test_rejects_bad_parent_and_bad_rule(db):
    con, parent, trigger = db
    with pytest.raises(ValueError, match="unknown run"):
        reschedule(con, "RUN-2026-999999", trigger, "x")
    with pytest.raises(ValueError, match="unknown rule"):
        reschedule(con, parent, trigger, "x", rule="BOGUS")
    con.execute("INSERT INTO schedule_run (run_id, run_type, parent_run_id, scheduling_rule,"
                " config_json, started_at, status) VALUES ('RUN-2026-000900', 'INJECTED', ?, 'EDD',"
                " '{}', '2026-10-22 09:00:00', 'COMPLETED')", (parent,))
    with pytest.raises(ValueError, match="INJECTED"):
        reschedule(con, "RUN-2026-000900", trigger, "x")


def test_events_and_decisions_cover_the_run(db):
    con, parent, trigger = db
    new = reschedule(con, parent, trigger, "control")
    n_rows = one(con, "SELECT COUNT(*) FROM operation_schedule WHERE run_id = ?", new)
    n_locked = one(con, "SELECT COUNT(*) FROM operation_schedule WHERE run_id = ? AND status = 'LOCKED'", new)
    assert one(con, "SELECT COUNT(*) FROM schedule_event WHERE run_id = ? AND event_type = 'LOCKED'",
               new) == n_locked
    assert one(con, "SELECT COUNT(*) FROM placement_decision WHERE run_id = ?"
                    " AND chosen_machine_id IS NOT NULL", new) == n_rows


def test_statuses_match_the_new_run_and_keys_are_clean(db):
    con, parent, trigger = db
    new = reschedule(con, parent, trigger, "control")
    assert one(con, "SELECT COUNT(*) FROM production_order WHERE status = 'SCHEDULED'") \
        == compute_metrics(con, new)["orders_scheduled"]
    assert con.execute("PRAGMA foreign_key_check").fetchall() == []


def test_reschedule_is_deterministic(base):
    src, parent, trigger = base

    def once():
        dst = sqlite3.connect(":memory:")
        src.backup(dst)
        dst.execute("PRAGMA foreign_keys = ON")
        new = reschedule(dst, parent, trigger, "control")
        out = dst.execute("SELECT * FROM operation_schedule WHERE run_id = ? ORDER BY schedule_id",
                          (new,)).fetchall()
        dst.close()
        return out

    assert once() == once()


def test_a_reschedule_can_be_rescheduled(db):
    con, parent, trigger = db
    first = reschedule(con, parent, trigger, "first")
    later = one(con, "SELECT datetime(?, '+1 days')", trigger)
    second = reschedule(con, first, later, "second")
    assert one(con, "SELECT parent_run_id FROM schedule_run WHERE run_id = ?", second) == first
    assert validate_and_record(con, second) == []


def test_compare_run_with_itself_is_empty(db):
    con, parent, _ = db
    c = compare_runs(con, parent, parent)
    assert c["moved_count"] == 0 and not c["removed"] and not c["added"]
    assert all(v["delta"] in (0, 0.0, None) for v in c["metrics"].values())


def test_compare_accounts_for_every_operation(db):
    con, parent, trigger = db
    new = reschedule(con, parent, trigger, "control")
    c = compare_runs(con, parent, new)
    assert c["unchanged"] + c["moved_count"] + len(c["removed"]) \
        == one(con, "SELECT COUNT(*) FROM operation_schedule WHERE run_id = ?", parent)
    assert c["unchanged"] + c["moved_count"] + len(c["added"]) \
        == one(con, "SELECT COUNT(*) FROM operation_schedule WHERE run_id = ?", new)
    for v in c["metrics"].values():
        if v["delta"] is not None:
            assert v["delta"] == pytest.approx(v["other"] - v["base"], abs=0.01)


def test_compare_rejects_unknown_run(db):
    con, parent, _ = db
    with pytest.raises(ValueError):
        compare_runs(con, parent, "RUN-2026-999999")


def test_compare_module_is_independent_of_engine_and_labels():
    tree = ast.parse(Path(compare_module.__file__).read_text(encoding="utf-8"))
    names = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names += [a.name for a in node.names]
        elif isinstance(node, ast.ImportFrom):
            names.append(node.module or "")
    joined = " ".join(names).lower()
    for banned in ("engine", "anomal", "ground"):
        assert banned not in joined