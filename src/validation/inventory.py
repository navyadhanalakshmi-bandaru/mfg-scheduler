"""Independent inventory check for one run (Phase 5d-2). Reads saved rows only.

Codes:
  INVENTORY_OVERDRAW          consumed from a lot more than it holds
  CONSUME_BEFORE_AVAILABLE    consumed before the lot's available_from
  CONSUME_TIME_MISMATCH       CONSUME time differs from the operation's start_time
  CONSUME_WITHOUT_SCHEDULE    CONSUME for an operation that is not in the run
  RESERVATION_UNBALANCED      reserved quantity is not closed by CONSUME + RELEASE
  REQUIREMENT_MISMATCH        consumed quantity differs from operation_material_req
"""
from __future__ import annotations

import sqlite3
from collections import defaultdict
from dataclasses import dataclass

TOL = 0.01


@dataclass(frozen=True)
class InventoryIssue:
    code: str
    lot_id: str | None
    operation_id: str | None
    message: str


def check_inventory(con: sqlite3.Connection, run_id: str) -> list[InventoryIssue]:
    issues: list[InventoryIssue] = []
    lots = {lot: (mat, avail, qty) for lot, mat, avail, qty in con.execute(
        "SELECT lot_id, material_id, available_from, quantity_received FROM inventory_lot")}
    adjust = dict(con.execute("SELECT lot_id, SUM(quantity) FROM inventory_transaction"
                              " WHERE txn_type = 'ADJUST' GROUP BY lot_id"))
    start = dict(con.execute("SELECT operation_id, start_time FROM operation_schedule"
                             " WHERE run_id = ?", (run_id,)))

    pair = defaultdict(lambda: {"RESERVE": 0.0, "CONSUME": 0.0, "RELEASE": 0.0})
    consumed_lot: dict[str, float] = defaultdict(float)
    consumed_op_mat: dict[tuple[str, str], float] = defaultdict(float)
    for txn_id, lot, kind, qty, op, when in con.execute(
            "SELECT txn_id, lot_id, txn_type, quantity, operation_id, txn_time"
            " FROM inventory_transaction WHERE run_id = ? ORDER BY txn_id", (run_id,)):
        if kind not in ("RESERVE", "CONSUME", "RELEASE"):
            continue
        size = abs(qty)
        pair[(op, lot)][kind] += size
        if kind != "CONSUME":
            continue
        material, available, _ = lots[lot]
        consumed_lot[lot] += size
        consumed_op_mat[(op, material)] += size
        if when < available:
            issues.append(InventoryIssue(
                "CONSUME_BEFORE_AVAILABLE", lot, op,
                f"{txn_id}: {op} consumes {lot} at {when}, before it arrives at {available}"))
        if op not in start:
            issues.append(InventoryIssue(
                "CONSUME_WITHOUT_SCHEDULE", lot, op,
                f"{txn_id}: {op} consumes stock but is not scheduled in {run_id}"))
        elif start[op] != when:
            issues.append(InventoryIssue(
                "CONSUME_TIME_MISMATCH", lot, op,
                f"{txn_id}: consumed at {when} but {op} starts at {start[op]}"))

    for lot, used in sorted(consumed_lot.items()):
        capacity = lots[lot][2] + (adjust.get(lot) or 0.0)
        if used > capacity + 0.002:
            issues.append(InventoryIssue(
                "INVENTORY_OVERDRAW", lot, None,
                f"{lot}: consumed {used:.3f} but the lot holds {capacity:.3f}"))

    for (op, lot), v in sorted(pair.items()):
        open_qty = v["RESERVE"] - v["CONSUME"] - v["RELEASE"]
        if abs(open_qty) > 0.002:
            issues.append(InventoryIssue(
                "RESERVATION_UNBALANCED", lot, op,
                f"{op} / {lot}: reserved {v['RESERVE']:.3f}, consumed {v['CONSUME']:.3f}, "
                f"released {v['RELEASE']:.3f}"))

    for op, material, required in con.execute(
            "SELECT q.operation_id, q.material_id, q.required_qty FROM operation_material_req q"
            " JOIN operation_schedule s ON s.operation_id = q.operation_id AND s.run_id = ?"
            " ORDER BY q.operation_id, q.material_id", (run_id,)):
        got = consumed_op_mat.get((op, material), 0.0)
        if abs(got - required) > TOL:
            issues.append(InventoryIssue(
                "REQUIREMENT_MISMATCH", None, op,
                f"{op} needs {required:.3f} of {material} but {got:.3f} is consumed"))
    return issues