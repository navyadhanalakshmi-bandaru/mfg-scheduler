"""Inventory transactions for one run (Phase 5d-2).

Per allocated (operation, lot):
  RESERVE  negative, at run time          the plan holds the stock
  CONSUME  negative, at the operation's   the operation is in the schedule
           start_time
  RELEASE  positive, at run time          the operation is NOT in the schedule, so the hold ends
Every RESERVE is closed by exactly one CONSUME or RELEASE. Transactions belong to a run
(run_id); a reschedule writes its own and leaves the parent's untouched.
"""
from __future__ import annotations

import sqlite3

from src.engine.model import FeasibilityResult
from src.generator import ids


def write_transactions(con: sqlite3.Connection, run_id: str, feas: FeasibilityResult,
                       placed_start: dict[str, str], run_time: str) -> int:
    """placed_start: operation_id -> start_time text, for operations in this run's schedule.

    The run row and its schedule rows must already exist. Returns rows written.
    """
    n = con.execute("SELECT COUNT(*) FROM inventory_transaction").fetchone()[0] + 1
    rows = []

    def add(lot_id: str, kind: str, qty: float, op_id: str, when: str) -> None:
        nonlocal n
        rows.append((ids.txn_id(n), lot_id, kind, qty, op_id, run_id, when, run_time))
        n += 1

    for op_id in sorted(feas.allocations):
        for lot_id, qty in feas.allocations[op_id]:
            q = round(qty, 3)
            if q <= 0:
                continue
            add(lot_id, "RESERVE", -q, op_id, run_time)
            if op_id in placed_start:
                add(lot_id, "CONSUME", -q, op_id, placed_start[op_id])
            else:
                add(lot_id, "RELEASE", q, op_id, run_time)
    con.executemany(
        "INSERT INTO inventory_transaction (txn_id, lot_id, txn_type, quantity, operation_id,"
        " run_id, txn_time, created_at) VALUES (?,?,?,?,?,?,?,?)", rows)
    return len(rows)