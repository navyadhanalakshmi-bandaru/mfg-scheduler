"""Tests for the KPI calculation.

Some tests compare against independent SQL totals. Others build a small known
case by editing a copy of the starter dataset, so the right answer can be
worked out by hand.
"""
import ast
import json
import sqlite3
from datetime import datetime
from pathlib import Path

import pytest

from config.settings import SCHEMA_SQL
from src.db.loader import load_tables
from src.engine.baseline import write_baseline
from src.engine.dispatcher import dispatch, prepare
from src.generator.export import ALL_TABLES, build_dataset
from src.metrics import kpis
from src.metrics.kpis import compute_metrics, record_metrics
from src.validation.constraints import validate_and_record

TS = "%Y-%m-%d %H:%M:%S"


def build(profile):
    con = sqlite3.connect(":memory:")
    con.executescript(SCHEMA_SQL.read_text(encoding="utf-8"))
    load_tables(con, build_dataset(profile), ALL_TABLES)
    con.execute("PRAGMA foreign_keys = ON")
    model, feas, tls, wins = prepare(con)
    result = dispatch(model, feas, tls, wins, "PRIORITY_EDD")
    return con, write_baseline(con, model, feas, result)


@pytest.fixture(scope="module", params=["starter", "full"])
def built(request):
    con, run_id = build(request.param)
    yield con, run_id
    con.close()


@pytest.fixture(scope="module")
def starter_base():
    con, run_id = build("starter")
    yield con, run_id
    con.close()


@pytest.fixture()
def copy(starter_base):
    src, run_id = starter_base
    dst = sqlite3.connect(":memory:")
    src.backup(dst)
    dst.execute("PRAGMA foreign_keys = ON")
    yield dst, run_id
    dst.close()


def one(con, sql, *args):
    return con.execute(sql, args).fetchone()[0]


def minutes(a, b):
    return int(round((datetime.strptime(b, TS) - datetime.strptime(a, TS)).total_seconds() / 60))


def test_order_counts_reconcile(built):
    con, run_id = built
    m = compute_metrics(con, run_id)
    assert m["orders_scheduled"] + m["orders_unscheduled"] == m["orders_total"]
    assert m["orders_total"] == one(con, "SELECT COUNT(*) FROM production_order")
    assert m["operations_scheduled"] == one(con, "SELECT COUNT(*) FROM operation_schedule WHERE run_id = ?", run_id)
    assert m["orders_unscheduled"] == one(con, "SELECT COUNT(*) FROM production_order WHERE status = 'UNSCHEDULED'")


def test_time_totals_and_makespan_match_sql(built):
    con, run_id = built
    m = compute_metrics(con, run_id)
    prod, setup, first, last = con.execute(
        "SELECT SUM(processing_minutes), SUM(setup_minutes), MIN(setup_start), MAX(end_time)"
        " FROM operation_schedule WHERE run_id = ?", (run_id,)).fetchone()
    assert m["total_production_minutes"] == prod
    assert m["total_changeover_minutes"] == setup
    assert (m["first_start"], m["last_end"]) == (first, last)
    assert m["makespan_minutes"] == minutes(first, last)


def test_on_time_and_late_cover_every_scheduled_order(built):
    con, run_id = built
    m = compute_metrics(con, run_id)
    assert m["on_time_orders"] + m["late_orders"] == m["orders_scheduled"]
    assert sum(m["late_by_priority"].values()) == m["late_orders"]
    assert m["on_time_delivery_pct"] == round(100 * m["on_time_orders"] / m["orders_scheduled"], 2)
    assert m["total_tardiness_minutes"] >= m["late_orders"]  # each late order is at least 1 minute late
    if m["late_orders"] == 0:
        assert m["total_tardiness_minutes"] == 0


def test_utilization_is_a_sensible_percentage(built):
    con, run_id = built
    m = compute_metrics(con, run_id)
    assert set(m["utilization_by_machine_pct"]) == {r[0] for r in con.execute("SELECT machine_id FROM machine")}
    assert all(0 <= v <= 100 for v in m["utilization_by_machine_pct"].values())
    assert 0 < m["utilization_overall_pct"] <= 100
    assert m["utilization_active_window_pct"] >= m["utilization_overall_pct"] - 0.01


def test_unvalidated_run_reports_violation_metrics_as_none(built):
    con, run_id = built
    m = compute_metrics(con, run_id)
    assert m["feasibility_label"] == "NOT_VALIDATED"
    assert m["hard_violations"] is None
    assert m["material_violations"] is None
    assert m["feasibility_rate_pct"] is None


def test_tardiness_known_case(copy):
    con, run_id = copy
    con.execute("UPDATE production_order SET due_time = '2030-01-01 00:00:00'")
    m = compute_metrics(con, run_id)
    assert (m["late_orders"], m["total_tardiness_minutes"], m["on_time_delivery_pct"]) == (0, 0, 100.0)

    order_id, done = con.execute(
        "SELECT o.order_id, MAX(s.end_time) FROM operation_schedule s"
        " JOIN operation o ON o.operation_id = s.operation_id WHERE s.run_id = ?"
        " GROUP BY o.order_id"
        " HAVING COUNT(*) = (SELECT COUNT(*) FROM operation x WHERE x.order_id = o.order_id)"
        " ORDER BY o.order_id LIMIT 1", (run_id,)).fetchone()
    con.execute("UPDATE production_order SET due_time = datetime(?, '-90 minutes') WHERE order_id = ?",
                (done, order_id))
    m = compute_metrics(con, run_id)
    assert m["late_orders"] == 1
    assert m["total_tardiness_minutes"] == 90
    assert m["on_time_orders"] == m["orders_scheduled"] - 1


def test_utilization_known_case(copy):
    con, run_id = copy
    con.execute("DELETE FROM machine_downtime")
    m = compute_metrics(con, run_id)
    busy_all = one(con, "SELECT SUM(setup_minutes + processing_minutes) FROM operation_schedule WHERE run_id = ?", run_id)
    assert m["utilization_overall_pct"] == round(100 * busy_all / one(con, "SELECT SUM(available_minutes) FROM machine_calendar"), 2)

    machine_id, busy = con.execute(
        "SELECT machine_id, SUM(setup_minutes + processing_minutes) FROM operation_schedule"
        " WHERE run_id = ? GROUP BY machine_id ORDER BY machine_id LIMIT 1", (run_id,)).fetchone()
    cal = one(con, "SELECT SUM(available_minutes) FROM machine_calendar WHERE machine_id = ?", machine_id)
    assert m["utilization_by_machine_pct"][machine_id] == round(100 * busy / cal, 2)


def test_downtime_reduces_available_minutes(copy):
    con, run_id = copy
    con.execute("DELETE FROM machine_downtime")
    machine_id, busy = con.execute(
        "SELECT machine_id, SUM(setup_minutes + processing_minutes) FROM operation_schedule"
        " WHERE run_id = ? GROUP BY machine_id ORDER BY machine_id LIMIT 1", (run_id,)).fetchone()
    cal = one(con, "SELECT SUM(available_minutes) FROM machine_calendar WHERE machine_id = ?", machine_id)
    start, end, window_minutes = con.execute(
        "SELECT window_start, window_end, available_minutes FROM machine_calendar"
        " WHERE machine_id = ? ORDER BY window_start LIMIT 1", (machine_id,)).fetchone()
    con.execute(
        "INSERT INTO machine_downtime (downtime_id, machine_id, downtime_type, down_start, down_end,"
        " reason, created_at) VALUES ('DT-2026-999999', ?, 'PLANNED_MAINTENANCE', ?, ?, 'test', ?)",
        (machine_id, start, end, start))
    m = compute_metrics(con, run_id)
    assert m["utilization_by_machine_pct"][machine_id] == round(100 * busy / (cal - window_minutes), 2)


def test_validated_clean_run(copy):
    con, run_id = copy
    assert validate_and_record(con, run_id) == []
    m = compute_metrics(con, run_id)
    assert m["feasibility_label"] == "FEASIBLE"
    assert (m["hard_violations"], m["material_violations"]) == (0, 0)
    assert m["feasibility_rate_pct"] == round(100 * m["orders_scheduled"] / m["orders_total"], 2)


def test_corrupted_run_lowers_the_feasibility_rate(copy):
    con, run_id = copy
    clean_rate = compute_metrics(con, run_id)["orders_scheduled"] / compute_metrics(con, run_id)["orders_total"] * 100
    schedule_id = con.execute(
        "SELECT s.schedule_id FROM operation_schedule s JOIN operation o ON o.operation_id = s.operation_id"
        " WHERE s.run_id = ? AND o.order_id IN (SELECT o2.order_id FROM operation_schedule s2"
        " JOIN operation o2 ON o2.operation_id = s2.operation_id WHERE s2.run_id = ? GROUP BY o2.order_id"
        " HAVING COUNT(*) = (SELECT COUNT(*) FROM operation x WHERE x.order_id = o2.order_id))"
        " ORDER BY s.schedule_id LIMIT 1", (run_id, run_id)).fetchone()[0]
    mod = "-86400 minutes"  # 60 days earlier, same trick as the validator tests
    con.execute("UPDATE operation_schedule SET setup_start = datetime(setup_start, ?),"
                " start_time = datetime(start_time, ?), end_time = datetime(end_time, ?)"
                " WHERE schedule_id = ?", (mod, mod, mod, schedule_id))
    found = validate_and_record(con, run_id)
    m = compute_metrics(con, run_id)
    assert found and m["feasibility_label"] == "INFEASIBLE_VIOLATIONS"
    assert m["hard_violations"] == len(found)
    assert m["material_violations"] == sum(1 for v in found if v.code == "MATERIAL_BEFORE_AVAILABILITY")
    assert m["material_violations"] <= m["hard_violations"]
    assert m["feasibility_rate_pct"] < clean_rate


def test_record_metrics_keeps_existing_keys(copy):
    con, run_id = copy
    validate_and_record(con, run_id)
    m = record_metrics(con, run_id)
    stored = json.loads(one(con, "SELECT metrics_json FROM schedule_run WHERE run_id = ?", run_id))
    assert stored["kpis"] == m
    assert stored["hard_violations"] == 0
    assert stored["orders_total"] == m["orders_total"]


def test_unknown_run_is_rejected(copy):
    con, _ = copy
    with pytest.raises(ValueError):
        compute_metrics(con, "RUN-2026-999999")


def test_metrics_do_not_import_engine_or_labels():
    tree = ast.parse(Path(kpis.__file__).read_text(encoding="utf-8"))
    names = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names += [a.name for a in node.names]
        elif isinstance(node, ast.ImportFrom):
            names.append(node.module or "")
    joined = " ".join(names).lower()
    for banned in ("engine", "anomal", "ground"):
        assert banned not in joined