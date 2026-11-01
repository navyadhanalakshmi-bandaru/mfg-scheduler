"""Independent hard-constraint validator (Phase 5e).

Reads ONLY the main database. It never imports the scheduling engine and never
opens the ground-truth database. Rules come from docs/data_dictionary.md.

Checks a saved run against eight hard constraints:
    MACHINE_OVERLAP, PRECEDENCE_VIOLATION, MATERIAL_BEFORE_AVAILABILITY,
    OUTSIDE_SHIFT, MISSING_CHANGEOVER, INELIGIBLE_MACHINE,
    LABOR_CAPACITY_EXCEEDED, TOOL_CAPACITY_EXCEEDED

Conventions (the same ones the baseline run records in its config):
  * A job occupies its machine, its operators and its tools from setup_start
    to end_time (end exclusive).
  * The whole block must lie inside ONE machine_calendar window and must not
    touch any machine_downtime row.
  * Material is consumed at start_time. Material check: by every start time,
    the total demand of jobs started so far must not exceed the usable supply
    (non-QUARANTINED lots) that has arrived. A job that fails this check is
    reported and left out of the running total, so one bad job does not make
    its neighbours look bad.
  * The first job on a machine needs no changeover. Later jobs need at least
    the changeover_matrix minutes for (machine group, previous family, family).
  * Labor capacity is looked up by (skill, work date, shift). The work date is
    the date the machine's shift window STARTS.
"""
from __future__ import annotations

import bisect
import json
import sqlite3
from collections import Counter, defaultdict
from dataclasses import dataclass

from src.generator import ids

VALIDATION_TIME = "2026-10-22 10:00:00"  # fixed so output is reproducible
EPS = 1e-6

VIOLATION_CODES = (
    "MACHINE_OVERLAP",
    "PRECEDENCE_VIOLATION",
    "MATERIAL_BEFORE_AVAILABILITY",
    "OUTSIDE_SHIFT",
    "MISSING_CHANGEOVER",
    "INELIGIBLE_MACHINE",
    "LABOR_CAPACITY_EXCEEDED",
    "TOOL_CAPACITY_EXCEEDED",
)


@dataclass(frozen=True)
class Violation:
    code: str
    schedule_id: str
    operation_id: str
    order_id: str
    machine_id: str
    message: str
    related_schedule_id: str | None = None


def _rows(con: sqlite3.Connection, sql: str, params: tuple = ()) -> list[dict]:
    cur = con.execute(sql, params)
    names = [d[0] for d in cur.description]
    return [dict(zip(names, r)) for r in cur.fetchall()]


def _violation(code: str, row: dict, message: str, related: str | None = None) -> Violation:
    return Violation(code, row["schedule_id"], row["operation_id"], row["order_id"],
                     row["machine_id"], f"{row['schedule_id']}: {message}", related)


def _load_rows(con: sqlite3.Connection, run_id: str) -> list[dict]:
    return _rows(
        con,
        "SELECT s.schedule_id, s.operation_id, s.machine_id, s.setup_start, s.setup_minutes,"
        " s.start_time, s.processing_minutes, s.end_time,"
        " o.order_id, o.step_id, o.sequence_no, o.required_machine_group,"
        " o.labor_skill, o.labor_headcount, p.product_family, m.machine_group"
        " FROM operation_schedule s"
        " JOIN operation o ON o.operation_id = s.operation_id"
        " JOIN production_order po ON po.order_id = o.order_id"
        " JOIN product p ON p.sku_id = po.sku_id"
        " JOIN machine m ON m.machine_id = s.machine_id"
        " WHERE s.run_id = ? ORDER BY s.schedule_id", (run_id,))


# ---------------------------------------------------------------- 1. overlap
def _check_overlap(rows: list[dict]) -> list[Violation]:
    by_machine: dict[str, list[dict]] = defaultdict(list)
    for r in rows:
        by_machine[r["machine_id"]].append(r)
    out = []
    for mid in sorted(by_machine):
        latest = None  # the job seen so far that ends last
        for r in sorted(by_machine[mid], key=lambda x: (x["setup_start"], x["schedule_id"])):
            if latest is not None and r["setup_start"] < latest["end_time"]:
                out.append(_violation(
                    "MACHINE_OVERLAP", r,
                    f"{mid} is busy with {latest['schedule_id']} until {latest['end_time']}, "
                    f"but this job starts at {r['setup_start']}", latest["schedule_id"]))
            if latest is None or r["end_time"] > latest["end_time"]:
                latest = r
    return out


# ------------------------------------------------------------- 2. precedence
def _check_precedence(rows: list[dict]) -> list[Violation]:
    by_order: dict[str, list[dict]] = defaultdict(list)
    for r in rows:
        by_order[r["order_id"]].append(r)
    out = []
    for oid in sorted(by_order):
        ops = sorted(by_order[oid], key=lambda x: x["sequence_no"])
        for prev, cur in zip(ops, ops[1:]):
            if cur["start_time"] < prev["end_time"]:
                out.append(_violation(
                    "PRECEDENCE_VIOLATION", cur,
                    f"operation {cur['sequence_no']} starts at {cur['start_time']} before "
                    f"operation {prev['sequence_no']} ends at {prev['end_time']}",
                    prev["schedule_id"]))
    return out


# --------------------------------------------------------------- 3. material
def _check_materials(con: sqlite3.Connection, run_id: str) -> list[Violation]:
    times: dict[str, list[str]] = defaultdict(list)
    cumulative: dict[str, list[float]] = defaultdict(list)
    running: dict[str, float] = defaultdict(float)
    for mid, when, qty in con.execute(
            "SELECT material_id, available_from, quantity_received FROM inventory_lot"
            " WHERE status <> 'QUARANTINED' ORDER BY material_id, available_from, lot_id"):
        running[mid] += qty
        times[mid].append(when)
        cumulative[mid].append(running[mid])

    reqs = _rows(
        con,
        "SELECT q.material_id, q.required_qty, s.schedule_id, s.operation_id, s.machine_id,"
        " s.start_time, o.order_id FROM operation_material_req q"
        " JOIN operation_schedule s ON s.operation_id = q.operation_id AND s.run_id = ?"
        " JOIN operation o ON o.operation_id = q.operation_id"
        " ORDER BY q.material_id, s.start_time, s.schedule_id", (run_id,))
    used: dict[str, float] = defaultdict(float)
    out = []
    for r in reqs:
        mid = r["material_id"]
        k = bisect.bisect_right(times[mid], r["start_time"])
        supply = cumulative[mid][k - 1] if k else 0.0
        if used[mid] + r["required_qty"] > supply + EPS:
            out.append(_violation(
                "MATERIAL_BEFORE_AVAILABILITY", r,
                f"needs {r['required_qty']:.3f} of {mid} at {r['start_time']} but only "
                f"{max(supply - used[mid], 0.0):.3f} has arrived and is unused by then"))
        else:
            used[mid] += r["required_qty"]
    return out


# ------------------------------------------------- 4. shifts and downtime
def _check_calendar(con: sqlite3.Connection, rows: list[dict]):
    windows: dict[str, list[tuple]] = defaultdict(list)
    for mid, start, end, shift in con.execute(
            "SELECT machine_id, window_start, window_end, shift_code FROM machine_calendar"
            " ORDER BY machine_id, window_start"):
        windows[mid].append((start, end, shift))
    starts = {mid: [w[0] for w in ws] for mid, ws in windows.items()}
    downs: dict[str, list[tuple]] = defaultdict(list)
    for mid, did, s, e in con.execute(
            "SELECT machine_id, downtime_id, down_start, down_end FROM machine_downtime"
            " ORDER BY machine_id, down_start"):
        downs[mid].append((did, s, e))

    out: list[Violation] = []
    window_of: dict[str, tuple[str, str]] = {}  # schedule_id -> (work_date, shift_code)
    for r in rows:
        mid = r["machine_id"]
        block_start, block_end = r["setup_start"], r["end_time"]
        i = bisect.bisect_right(starts.get(mid, []), block_start) - 1
        win = windows[mid][i] if i >= 0 else None
        if win is None or win[1] < block_end:
            out.append(_violation(
                "OUTSIDE_SHIFT", r,
                f"block {block_start} to {block_end} does not fit inside one working shift "
                f"window of {mid}"))
            continue
        window_of[r["schedule_id"]] = (win[0][:10], win[2])
        for did, ds, de in downs.get(mid, []):
            if ds < block_end and de > block_start:
                out.append(_violation(
                    "OUTSIDE_SHIFT", r,
                    f"block {block_start} to {block_end} overlaps downtime {did} ({ds} to {de})"))
                break
    return out, window_of


# ------------------------------------------------------------ 5. changeover
def _check_changeover(con: sqlite3.Connection, rows: list[dict]) -> list[Violation]:
    matrix = {(g, a, b): m for g, a, b, m in con.execute(
        "SELECT machine_group, from_family, to_family, setup_minutes FROM changeover_matrix")}
    by_machine: dict[str, list[dict]] = defaultdict(list)
    for r in rows:
        by_machine[r["machine_id"]].append(r)
    out = []
    for mid in sorted(by_machine):
        prev = None
        for r in sorted(by_machine[mid], key=lambda x: (x["setup_start"], x["schedule_id"])):
            if prev is not None:
                need = matrix.get((r["machine_group"], prev["product_family"], r["product_family"]), 0)
                if r["setup_minutes"] < need:
                    out.append(_violation(
                        "MISSING_CHANGEOVER", r,
                        f"changeover {prev['product_family']} to {r['product_family']} on "
                        f"{r['machine_group']} needs {need} min, only {r['setup_minutes']} scheduled",
                        prev["schedule_id"]))
            prev = r
    return out


# ------------------------------------------------------------ 6. eligibility
def _check_eligibility(con: sqlite3.Connection, rows: list[dict]) -> list[Violation]:
    eligible = {(s, m): (f, t) for s, m, f, t in con.execute(
        "SELECT step_id, machine_id, effective_from, effective_to FROM routing_step_machine")}
    out = []
    for r in rows:
        key = (r["step_id"], r["machine_id"])
        if key not in eligible:
            out.append(_violation("INELIGIBLE_MACHINE", r,
                                  f"{r['machine_id']} is not an eligible machine for {r['step_id']}"))
            continue
        if r["machine_group"] != r["required_machine_group"]:
            out.append(_violation(
                "INELIGIBLE_MACHINE", r,
                f"{r['machine_id']} is in group {r['machine_group']}, step needs "
                f"{r['required_machine_group']}"))
            continue
        eff_from, eff_to = eligible[key]
        day = r["start_time"][:10]
        if day < eff_from or (eff_to is not None and day >= eff_to):
            out.append(_violation("INELIGIBLE_MACHINE", r,
                                  f"eligibility of {r['machine_id']} is not effective on {day}"))
    return out


# --------------------------------------------------- 7 and 8. labor, tools
def _overload(blocks: list[tuple[dict, int]], capacity: int) -> list[tuple[dict, int]]:
    """Sweep through time. Return (row, total in use) each time a block starts
    and pushes concurrent use above capacity. Ends are processed before starts
    at the same instant (end exclusive)."""
    events = []
    for row, amount in blocks:
        events.append((row["setup_start"], 1, row["schedule_id"], row, amount))
        events.append((row["end_time"], 0, row["schedule_id"], row, amount))
    events.sort(key=lambda e: (e[0], e[1], e[2]))
    total = 0
    over = []
    for _, kind, _, row, amount in events:
        if kind == 1:
            total += amount
            if total > capacity:
                over.append((row, total))
        else:
            total -= amount
    return over


def _check_labor(con: sqlite3.Connection, rows: list[dict], window_of: dict) -> list[Violation]:
    capacity = {(skill, day, shift): hc for skill, day, shift, hc in con.execute(
        "SELECT lp.labor_skill, c.work_date, c.shift_code, c.headcount_available"
        " FROM labor_shift_capacity c JOIN labor_pool lp ON lp.pool_id = c.pool_id")}
    groups: dict[tuple, list] = defaultdict(list)
    for r in rows:
        w = window_of.get(r["schedule_id"])
        if w is not None:
            groups[(r["labor_skill"], w[0], w[1])].append((r, r["labor_headcount"]))
    out = []
    for key in sorted(groups):
        cap = capacity.get(key)
        for r, total in _overload(groups[key], cap or 0):
            note = "" if cap is not None else " (no labor capacity row exists for that shift)"
            out.append(_violation(
                "LABOR_CAPACITY_EXCEEDED", r,
                f"{total} {key[0]} operators needed at once on {key[1]} {key[2]}, "
                f"only {cap or 0} available{note}"))
    return out


def _check_tools(con: sqlite3.Connection, rows: list[dict]) -> list[Violation]:
    units = dict(con.execute("SELECT tool_id, units_available FROM tool"))
    needs: dict[str, list[tuple[str, int]]] = defaultdict(list)
    for step, tool, qty in con.execute(
            "SELECT step_id, tool_id, units_required FROM routing_step_tool ORDER BY step_id, tool_id"):
        needs[step].append((tool, qty))
    by_tool: dict[str, list] = defaultdict(list)
    for r in rows:
        for tool, qty in needs.get(r["step_id"], []):
            by_tool[tool].append((r, qty))
    out = []
    for tool in sorted(by_tool):
        for r, total in _overload(by_tool[tool], units.get(tool, 0)):
            out.append(_violation(
                "TOOL_CAPACITY_EXCEEDED", r,
                f"{total} units of {tool} in use at once, only {units.get(tool, 0)} exist"))
    return out


# --------------------------------------------------------------- public API
def validate_run(con: sqlite3.Connection, run_id: str) -> list[Violation]:
    """Return every hard-constraint violation in a run. Reads only; changes nothing."""
    if con.execute("SELECT 1 FROM schedule_run WHERE run_id = ?", (run_id,)).fetchone() is None:
        raise ValueError(f"unknown run {run_id}")
    rows = _load_rows(con, run_id)
    calendar_violations, window_of = _check_calendar(con, rows)
    found = (_check_overlap(rows) + _check_precedence(rows) + _check_materials(con, run_id)
             + calendar_violations + _check_changeover(con, rows) + _check_eligibility(con, rows)
             + _check_labor(con, rows, window_of) + _check_tools(con, rows))
    return sorted(found, key=lambda v: (VIOLATION_CODES.index(v.code), v.schedule_id,
                                        v.related_schedule_id or "", v.message))


def validate_and_record(con: sqlite3.Connection, run_id: str,
                        checked_at: str = VALIDATION_TIME) -> list[Violation]:
    """Validate a run once, write CONSTRAINT_VIOLATION rows, set feasibility_label.

    The label becomes FEASIBLE only when there are zero hard violations.
    A run that is already validated is refused, because exceptions are append-only.
    """
    row = con.execute("SELECT feasibility_label, metrics_json FROM schedule_run WHERE run_id = ?",
                      (run_id,)).fetchone()
    if row is None:
        raise ValueError(f"unknown run {run_id}")
    if row[0] != "NOT_VALIDATED":
        raise ValueError(f"run {run_id} was already validated (label {row[0]})")

    violations = validate_run(con, run_id)
    n = con.execute("SELECT COUNT(*) FROM scheduling_exception").fetchone()[0] + 1
    con.executemany(
        "INSERT INTO scheduling_exception (exception_id, run_id, category, code, severity, order_id,"
        " operation_id, machine_id, message, detected_by, created_at)"
        " VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        [(ids.exception_id(n + i), run_id, "CONSTRAINT_VIOLATION", v.code, "HARD", v.order_id,
          v.operation_id, v.machine_id, v.message, "VALIDATOR", checked_at)
         for i, v in enumerate(violations)])

    metrics = json.loads(row[1]) if row[1] else {}
    metrics["hard_violations"] = len(violations)
    metrics["violations_by_code"] = dict(sorted(Counter(v.code for v in violations).items()))
    label = "FEASIBLE" if not violations else "INFEASIBLE_VIOLATIONS"
    con.execute("UPDATE schedule_run SET feasibility_label = ?, metrics_json = ? WHERE run_id = ?",
                (label, json.dumps(metrics, sort_keys=True), run_id))
    con.commit()
    return violations