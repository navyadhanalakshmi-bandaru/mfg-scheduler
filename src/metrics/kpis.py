"""KPI calculation (Phase 5f).

Reads ONLY the main database. Every number is derived from saved rows
(operation_schedule, production_order, machine_calendar, machine_downtime,
scheduling_exception). Nothing is typed in or estimated.

HARD constraints (pass/fail, set by the validator): hard_violations,
material_violations, feasibility_rate_pct.
OBJECTIVES (we try to improve them, they never make a run infeasible):
makespan, tardiness, late orders, changeover time, utilization.

Violation metrics are None for a run that has not been validated yet,
because "zero violations" and "never checked" are different things.
"""
from __future__ import annotations

import json
import sqlite3
from collections import Counter, defaultdict
from datetime import datetime

TS = "%Y-%m-%d %H:%M:%S"
PRIORITY_ORDER = ("URGENT", "HIGH", "NORMAL", "LOW")


def _dt(text: str) -> datetime:
    return datetime.strptime(text, TS)


def _minutes(start: datetime, end: datetime) -> int:
    return int(round((end - start).total_seconds() / 60))


def _merge(intervals: list[tuple[datetime, datetime]]) -> list[list[datetime]]:
    merged: list[list[datetime]] = []
    for start, end in sorted(intervals):
        if merged and start <= merged[-1][1]:
            if end > merged[-1][1]:
                merged[-1][1] = end
        else:
            merged.append([start, end])
    return merged


def _available_minutes(con: sqlite3.Connection, lo: datetime | None = None,
                       hi: datetime | None = None) -> dict[str, int]:
    """Working minutes per machine: calendar windows minus downtime.

    If lo / hi are given, only the part of each window inside [lo, hi] counts.
    """
    available = {m: 0 for (m,) in con.execute("SELECT machine_id FROM machine ORDER BY machine_id")}
    downtime: dict[str, list] = defaultdict(list)
    for machine_id, start, end in con.execute(
            "SELECT machine_id, down_start, down_end FROM machine_downtime"):
        downtime[machine_id].append((_dt(start), _dt(end)))
    merged = {m: _merge(v) for m, v in downtime.items()}
    for machine_id, start, end in con.execute(
            "SELECT machine_id, window_start, window_end FROM machine_calendar"
            " ORDER BY machine_id, window_start"):
        w_start, w_end = _dt(start), _dt(end)
        if lo is not None and w_start < lo:
            w_start = lo
        if hi is not None and w_end > hi:
            w_end = hi
        if w_end <= w_start:
            continue
        total = _minutes(w_start, w_end)
        for d_start, d_end in merged.get(machine_id, []):
            a, b = max(d_start, w_start), min(d_end, w_end)
            if b > a:
                total -= _minutes(a, b)
        available[machine_id] += total
    return available


def _pct(part: float, whole: float) -> float | None:
    return round(100.0 * part / whole, 2) if whole else None


def compute_metrics(con: sqlite3.Connection, run_id: str) -> dict:
    """Return the KPI dictionary for one run. Reads only; changes nothing."""
    run = con.execute("SELECT run_type, feasibility_label FROM schedule_run WHERE run_id = ?",
                      (run_id,)).fetchone()
    if run is None:
        raise ValueError(f"unknown run {run_id}")
    run_type, label = run

    rows = con.execute(
        "SELECT o.order_id, s.machine_id, s.setup_start, s.setup_minutes,"
        " s.processing_minutes, s.end_time FROM operation_schedule s"
        " JOIN operation o ON o.operation_id = s.operation_id WHERE s.run_id = ?", (run_id,)).fetchall()
    orders = {oid: (due, prio) for oid, due, prio in con.execute(
        "SELECT order_id, due_time, priority_class FROM production_order WHERE status <> 'CANCELLED'")}
    ops_in_order = dict(con.execute("SELECT order_id, COUNT(*) FROM operation GROUP BY order_id"))

    placed: Counter = Counter()
    completion: dict[str, str] = {}
    busy: dict[str, int] = defaultdict(int)
    first_start = last_end = None
    production = changeover = 0
    for order_id, machine_id, setup_start, setup_min, proc_min, end_time in rows:
        placed[order_id] += 1
        if order_id not in completion or end_time > completion[order_id]:
            completion[order_id] = end_time
        busy[machine_id] += setup_min + proc_min
        production += proc_min
        changeover += setup_min
        if first_start is None or setup_start < first_start:
            first_start = setup_start
        if last_end is None or end_time > last_end:
            last_end = end_time

    scheduled = {o for o in orders if placed[o] and placed[o] == ops_in_order.get(o, 0)}

    # ---- lateness (objective)
    on_time = late = tardiness = 0
    late_by_priority = dict.fromkeys(PRIORITY_ORDER, 0)
    for order_id in sorted(scheduled):
        due, priority = orders[order_id]
        delay = _minutes(_dt(due), _dt(completion[order_id]))
        if delay > 0:
            late += 1
            tardiness += delay
            late_by_priority[priority] += 1
        else:
            on_time += 1

    # ---- utilization (objective)
    makespan = _minutes(_dt(first_start), _dt(last_end)) if rows else 0
    available = _available_minutes(con)
    by_machine = {m: _pct(busy.get(m, 0), available[m]) or 0.0 for m in sorted(available)}
    total_busy = sum(busy.values())
    overall = _pct(total_busy, sum(available.values()))
    in_window = None
    if rows:
        window_available = _available_minutes(con, _dt(first_start), _dt(last_end))
        in_window = _pct(total_busy, sum(window_available.values()))

    # ---- hard constraints (from the validator)
    violations = material = feasibility_rate = None
    if label != "NOT_VALIDATED":
        found = con.execute(
            "SELECT code, order_id FROM scheduling_exception"
            " WHERE run_id = ? AND category = 'CONSTRAINT_VIOLATION'", (run_id,)).fetchall()
        violations = len(found)
        material = sum(1 for code, _ in found if code == "MATERIAL_BEFORE_AVAILABILITY")
        bad_orders = {o for _, o in found if o}
        feasibility_rate = _pct(len(scheduled - bad_orders), len(orders))

    return {
        "run_id": run_id, "run_type": run_type, "feasibility_label": label,
        "orders_total": len(orders), "orders_scheduled": len(scheduled),
        "orders_unscheduled": len(orders) - len(scheduled), "operations_scheduled": len(rows),
        "first_start": first_start, "last_end": last_end, "makespan_minutes": makespan,
        "total_production_minutes": production, "total_changeover_minutes": changeover,
        "utilization_overall_pct": overall, "utilization_active_window_pct": in_window,
        "utilization_by_machine_pct": by_machine,
        "on_time_orders": on_time, "on_time_delivery_pct": _pct(on_time, len(scheduled)),
        "late_orders": late, "total_tardiness_minutes": tardiness,
        "late_by_priority": late_by_priority,
        "hard_violations": violations, "material_violations": material,
        "feasibility_rate_pct": feasibility_rate,
    }


def record_metrics(con: sqlite3.Connection, run_id: str) -> dict:
    """Compute the KPIs and store them under "kpis" in schedule_run.metrics_json.

    Existing keys (orders_total, hard_violations, ...) are kept.
    """
    metrics = compute_metrics(con, run_id)
    row = con.execute("SELECT metrics_json FROM schedule_run WHERE run_id = ?", (run_id,)).fetchone()
    stored = json.loads(row[0]) if row[0] else {}
    stored["kpis"] = metrics
    con.execute("UPDATE schedule_run SET metrics_json = ? WHERE run_id = ?",
                (json.dumps(stored, sort_keys=True), run_id))
    con.commit()
    return metrics