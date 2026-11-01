"""Tests for inventory transactions (RESERVE / CONSUME / RELEASE), starter dataset."""
import sqlite3

import pytest

from config.settings import SCHEMA_SQL
from src.db.loader import load_tables
from src.engine.baseline import write_baseline
from src.engine.dispatcher import dispatch, prepare
from src.engine.reschedule import reschedule
from src.engine.transactions import write_transactions
from src.generator.export import ALL_TABLES, build_dataset
from src.validation.inventory import check_inventory


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
    yield con, run_id, starts[len(starts) // 2]
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


def codes(issues):
    return {i.code for i in issues}


def test_allocations_cover_every_requirement(base):
    con, _, _ = base
    model, feas, _, _ = prepare(con)
    assert feas.allocations
    for op_id, allocs in feas.allocations.items():
        need = sum(q for _, q in model.requirements[op_id])
        assert sum(q for _, q in allocs) == pytest.approx(need, abs=1e-6)
    assert set(feas.material_ready) <= set(feas.allocations)


def test_baseline_writes_balanced_transactions(base):
    con, run, _ = base
    kinds = dict(con.execute("SELECT txn_type, COUNT(*) FROM inventory_transaction"
                             " WHERE run_id = ? GROUP BY txn_type", (run,)))
    assert kinds.get("RESERVE", 0) > 0
    assert kinds.get("CONSUME", 0) > 0
    assert kinds["RESERVE"] == kinds.get("CONSUME", 0) + kinds.get("RELEASE", 0)
    assert check_inventory(con, run) == []


def test_consume_happens_at_operation_start(base):
    con, run, _ = base
    assert one(con, "SELECT COUNT(*) FROM inventory_transaction WHERE run_id = ?"
                    " AND txn_type = 'CONSUME'", run) > 0
    assert one(con, "SELECT COUNT(*) FROM inventory_transaction t JOIN operation_schedule s"
                    " ON s.operation_id = t.operation_id AND s.run_id = t.run_id"
                    " WHERE t.run_id = ? AND t.txn_type = 'CONSUME' AND t.txn_time <> s.start_time",
               run) == 0


def test_reschedule_has_its_own_transactions(db):
    con, parent, trigger = db
    before = one(con, "SELECT COUNT(*) FROM inventory_transaction WHERE run_id = ?", parent)
    new = reschedule(con, parent, trigger, "control")
    assert one(con, "SELECT COUNT(*) FROM inventory_transaction WHERE run_id = ?", new) > 0
    assert one(con, "SELECT COUNT(*) FROM inventory_transaction WHERE run_id = ?", parent) == before
    assert check_inventory(con, new) == []
    assert con.execute("PRAGMA foreign_key_check").fetchall() == []


def test_unplaced_operations_are_released(db):
    con, _, _ = db
    _, feas, _, _ = prepare(con)
    con.execute("INSERT INTO schedule_run (run_id, run_type, parent_run_id, scheduling_rule,"
                " config_json, started_at, status) VALUES ('RUN-2026-000901', 'BASELINE', NULL,"
                " 'EDD', '{}', '2026-10-22 09:00:00', 'COMPLETED')")
    assert write_transactions(con, "RUN-2026-000901", feas, {}, "2026-10-22 09:00:00") > 0
    kinds = dict(con.execute("SELECT txn_type, COUNT(*) FROM inventory_transaction"
                             " WHERE run_id = 'RUN-2026-000901' GROUP BY txn_type"))
    assert kinds["RESERVE"] == kinds["RELEASE"] and "CONSUME" not in kinds
    assert check_inventory(con, "RUN-2026-000901") == []


def first_consume(con, run):
    return con.execute("SELECT txn_id, lot_id FROM inventory_transaction WHERE run_id = ?"
                       " AND txn_type = 'CONSUME' ORDER BY txn_id LIMIT 1", (run,)).fetchone()


def test_detects_consume_before_arrival(db):
    con, run, _ = db
    tid, lot = first_consume(con, run)
    con.execute("UPDATE inventory_transaction SET txn_time = (SELECT datetime(available_from,"
                " '-1 hours') FROM inventory_lot WHERE lot_id = ?) WHERE txn_id = ?", (lot, tid))
    assert "CONSUME_BEFORE_AVAILABLE" in codes(check_inventory(con, run))


def test_detects_overdraw(db):
    con, run, _ = db
    tid, lot = first_consume(con, run)
    held = one(con, "SELECT quantity_received FROM inventory_lot WHERE lot_id = ?", lot)
    con.execute("UPDATE inventory_transaction SET quantity = ? WHERE txn_id = ?", (-(held + 10), tid))
    assert "INVENTORY_OVERDRAW" in codes(check_inventory(con, run))


def test_detects_time_mismatch(db):
    con, run, _ = db
    tid, _ = first_consume(con, run)
    con.execute("UPDATE inventory_transaction SET txn_time = datetime(txn_time, '+1 minutes')"
                " WHERE txn_id = ?", (tid,))
    assert "CONSUME_TIME_MISMATCH" in codes(check_inventory(con, run))


def test_detects_unclosed_reservation_and_missing_consumption(db):
    con, run, _ = db
    tid, _ = first_consume(con, run)
    con.execute("DELETE FROM inventory_transaction WHERE txn_id = ?", (tid,))
    found = codes(check_inventory(con, run))
    assert "RESERVATION_UNBALANCED" in found
    assert "REQUIREMENT_MISMATCH" in found