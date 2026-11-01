"""Compare two runs (Phase 5g). Read-only; uses only the main database."""
from __future__ import annotations

import sqlite3
from collections import Counter
from datetime import datetime

from src.metrics.kpis import compute_metrics

TS = "%Y-%m-%d %H:%M:%S"
COMPARED = ("orders_scheduled", "orders_unscheduled", "late_orders", "on_time_delivery_pct",
            "total_tardiness_minutes", "makespan_minutes", "total_changeover_minutes",
            "total_production_minutes", "utilization_overall_pct", "hard_violations")


def _minutes(a: str, b: str) -> int:
    return int(round((datetime.strptime(b, TS) - datetime.strptime(a, TS)).total_seconds() / 60))


def _placements(con: sqlite3.Connection, run_id: str) -> dict[str, tuple]:
    return {op: (machine, setup, end, order) for op, machine, setup, end, order in con.execute(
        "SELECT s.operation_id, s.machine_id, s.setup_start, s.end_time, o.order_id"
        " FROM operation_schedule s JOIN operation o ON o.operation_id = s.operation_id"
        " WHERE s.run_id = ?", (run_id,))}


def _order_state(con: sqlite3.Connection, run_id: str) -> dict[str, dict]:
    total = dict(con.execute("SELECT order_id, COUNT(*) FROM operation GROUP BY order_id"))
    due = dict(con.execute("SELECT order_id, due_time FROM production_order"))
    placed = {oid: (n, last) for oid, n, last in con.execute(
        "SELECT o.order_id, COUNT(*), MAX(s.end_time) FROM operation_schedule s"
        " JOIN operation o ON o.operation_id = s.operation_id WHERE s.run_id = ?"
        " GROUP BY o.order_id", (run_id,))}
    state = {}
    for oid, count in total.items():
        n, last = placed.get(oid, (0, None))
        done = n == count
        state[oid] = {"scheduled": done,
                      "tardiness": max(0, _minutes(due[oid], last)) if done else 0}
    return state


def compare_runs(con: sqlite3.Connection, base_run_id: str, other_run_id: str,
                 sample: int = 10) -> dict:
    """What changed from the base run to the other run."""
    base_m = compute_metrics(con, base_run_id)    # raises ValueError for an unknown run
    other_m = compute_metrics(con, other_run_id)
    metrics = {}
    for key in COMPARED:
        b, o = base_m[key], other_m[key]
        metrics[key] = {"base": b, "other": o,
                        "delta": None if b is None or o is None else round(o - b, 2)}

    bp, op_ = _placements(con, base_run_id), _placements(con, other_run_id)
    moved, unchanged = [], 0
    for op_id in sorted(set(bp) & set(op_)):
        b, o = bp[op_id], op_[op_id]
        if b[:3] == o[:3]:
            unchanged += 1
        else:
            moved.append({"operation_id": op_id, "order_id": b[3], "from_machine": b[0],
                          "to_machine": o[0], "end_shift_minutes": _minutes(b[2], o[2])})
    moved.sort(key=lambda m: (-abs(m["end_shift_minutes"]), m["operation_id"]))

    bs, os_ = _order_state(con, base_run_id), _order_state(con, other_run_id)
    both = sorted(set(bs) & set(os_))
    return {
        "base_run_id": base_run_id, "other_run_id": other_run_id, "metrics": metrics,
        "unchanged": unchanged, "moved_count": len(moved),
        "machine_changed_count": sum(1 for m in moved if m["from_machine"] != m["to_machine"]),
        "moved": moved[:sample],
        "removed": sorted(set(bp) - set(op_)), "added": sorted(set(op_) - set(bp)),
        "newly_unscheduled_orders": [o for o in both if bs[o]["scheduled"] and not os_[o]["scheduled"]],
        "newly_scheduled_orders": [o for o in both if not bs[o]["scheduled"] and os_[o]["scheduled"]],
        "became_late_orders": [o for o in both if bs[o]["scheduled"] and os_[o]["scheduled"]
                               and bs[o]["tardiness"] == 0 and os_[o]["tardiness"] > 0],
        "no_longer_late_orders": [o for o in both if bs[o]["scheduled"] and os_[o]["scheduled"]
                                  and bs[o]["tardiness"] > 0 and os_[o]["tardiness"] == 0],
    }