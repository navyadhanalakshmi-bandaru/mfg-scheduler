"""Tests for the baseline run writer."""
import json
import sqlite3

import pytest

from config.settings import SCHEMA_SQL
from src.db.loader import load_tables
from src.engine.baseline import write_baseline
from src.engine.dispatcher import dispatch, prepare
from src.generator.export import ALL_TABLES, build_dataset


def build(profile, rule="PRIORITY_EDD"):
    con = sqlite3.connect(":memory:")
    con.executescript(SCHEMA_SQL.read_text(encoding="utf-8"))
    load_tables(con, build_dataset(profile), ALL_TABLES)
    con.execute("PRAGMA foreign_keys = ON")
    model, feas, tls, wins = prepare(con)
    res = dispatch(model, feas, tls, wins, rule)
    return con, model, feas, res, write_baseline(con, model, feas, res)


@pytest.fixture(scope="module", params=["starter", "full"])
def built(request):
    con, model, feas, res, run_id = build(request.param)
    yield con, model, feas, res, run_id
    con.close()


def count(con, sql, *args):
    return con.execute(sql, args).fetchone()[0]


def test_run_row(built):
    con, _, _, res, run_id = built
    row = con.execute("SELECT run_type, parent_run_id, scheduling_rule, status, feasibility_label,"
                      " config_json, metrics_json FROM schedule_run").fetchall()
    assert len(row) == 1
    run_type, parent, rule, status, label, config, metrics = row[0]
    assert (run_type, parent, rule, status, label) == (
        "BASELINE", None, "PRIORITY_EDD", "COMPLETED", "NOT_VALIDATED")
    assert json.loads(config)["rule"] == "PRIORITY_EDD"
    assert json.loads(metrics)["operations_placed"] == len(res.placements)


def test_operation_schedule_rows_and_machine_chain(built):
    con, _, _, res, run_id = built
    assert count(con, "SELECT COUNT(*) FROM operation_schedule WHERE run_id = ?", run_id) \
        == len(res.placements)
    last = {}
    for sid, machine, prev in con.execute(
            "SELECT schedule_id, machine_id, previous_schedule_id FROM operation_schedule"
            " ORDER BY machine_id, setup_start"):
        assert prev == last.get(machine)
        last[machine] = sid


def test_placement_decisions(built):
    con, _, _, res, _ = built
    assert count(con, "SELECT COUNT(*) FROM placement_decision") == len(res.placements) + len(res.issues)
    for (cands,) in con.execute("SELECT candidates_json FROM placement_decision"):
        assert isinstance(json.loads(cands), list)
    assert count(con, "SELECT COUNT(*) FROM placement_decision d JOIN operation_schedule s"
                      " ON s.operation_id = d.operation_id WHERE d.chosen_machine_id <> s.machine_id") == 0


def test_events(built):
    con, model, _, res, _ = built
    unscheduled = count(con, "SELECT COUNT(*) FROM production_order WHERE status = 'UNSCHEDULED'")
    assert count(con, "SELECT COUNT(*) FROM schedule_event WHERE event_type = 'PLACED'") == len(res.placements)
    assert count(con, "SELECT COUNT(*) FROM schedule_event WHERE event_type = 'UNSCHEDULED'") == unscheduled


def test_exceptions(built):
    con, _, feas, res, _ = built
    assert count(con, "SELECT COUNT(*) FROM scheduling_exception") == len(feas.issues) + len(res.issues)
    assert count(con, "SELECT COUNT(*) FROM scheduling_exception"
                      " WHERE category <> 'INFEASIBLE_ORDER' OR detected_by <> 'ENGINE'") == 0


def test_order_and_operation_statuses(built):
    con, model, feas, res, _ = built
    assert count(con, "SELECT COUNT(*) FROM production_order WHERE status = 'SCHEDULED'") \
        + count(con, "SELECT COUNT(*) FROM production_order WHERE status = 'UNSCHEDULED'") == len(model.orders)
    unsched = {o for (o,) in con.execute("SELECT order_id FROM production_order WHERE status = 'UNSCHEDULED'")}
    assert feas.infeasible_orders <= unsched
    assert count(con, "SELECT COUNT(*) FROM operation WHERE status = 'SCHEDULED'") == len(res.placements)


def test_status_history_links_to_run(built):
    con, model, _, _, run_id = built
    rows = con.execute("SELECT o.status, h.to_status, h.from_status FROM order_status_history h"
                       " JOIN production_order o ON o.order_id = h.order_id"
                       " WHERE h.run_id = ?", (run_id,)).fetchall()
    assert len(rows) == len(model.orders)
    assert all(s == to and frm == "RELEASED" for s, to, frm in rows)


def test_foreign_keys_clean(built):
    con = built[0]
    assert con.execute("PRAGMA foreign_key_check").fetchall() == []


def test_writing_is_deterministic():
    a = build("starter")[0]
    b = build("starter")[0]
    q = "SELECT * FROM operation_schedule ORDER BY schedule_id"
    assert a.execute(q).fetchall() == b.execute(q).fetchall()
    a.close()
    b.close()


def test_other_rule_is_recorded():
    con, _, _, _, _ = build("starter", "CRITICAL_RATIO")
    assert count(con, "SELECT COUNT(*) FROM schedule_run WHERE scheduling_rule = 'CRITICAL_RATIO'") == 1
    con.close()