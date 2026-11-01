"""Rescheduling (Phase 5g).

Takes a finished run and a change (for example a new breakdown), then plans the
future again around the work that has already started.

Rules, each in one place:
  * LOCKED: an operation whose setup started before the trigger time keeps its
    machine and times exactly. Only a leading run of each order's operations can
    be locked (precedence). Locked rows are copied into the new run.
  * Everything else is planned again from scratch (rolling horizon) by the same
    dispatcher and rule, starting no earlier than the trigger time. Orders that
    were unscheduled get another chance.
  * A new downtime must start at or after the trigger time and must not touch a
    locked job. A breakdown that interrupts a running job is not modelled.
  * The new run is a complete schedule: run_type RESCHEDULE, parent_run_id set.
    Validation and KPIs are separate steps (validate_and_record, record_metrics).
  * Everything is written in one transaction. On any error nothing is kept.
"""
from __future__ import annotations

import json
import sqlite3
from collections import Counter

from src.engine.dispatcher import DISPATCH_WINDOW, RULES, Placement, _ts, dispatch, prepare
from src.engine.timeline import parse
from src.generator import ids

DOWNTIME_TYPES = ("PLANNED_MAINTENANCE", "BREAKDOWN", "CALIBRATION", "HOLIDAY")


def _count(con: sqlite3.Connection, table: str) -> int:
    return con.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]


def _parent_rows(con: sqlite3.Connection, run_id: str) -> list[dict]:
    cur = con.execute(
        "SELECT s.operation_id, o.order_id, o.sequence_no, s.machine_id, s.setup_start,"
        " s.setup_minutes, s.start_time, s.processing_minutes, s.end_time"
        " FROM operation_schedule s JOIN operation o ON o.operation_id = s.operation_id"
        " WHERE s.run_id = ? ORDER BY o.order_id, o.sequence_no", (run_id,))
    names = [d[0] for d in cur.description]
    return [dict(zip(names, r)) for r in cur.fetchall()]


def _locked_rows(rows: list[dict], trigger_text: str) -> list[dict]:
    """Per order, the leading operations whose setup started before the trigger."""
    locked, stopped = [], set()
    for r in rows:  # ordered by order, then sequence
        if r["order_id"] in stopped:
            continue
        if r["setup_start"] < trigger_text:
            locked.append(r)
        else:
            stopped.add(r["order_id"])
    return locked


def _where(machine_id: str, setup_start: str, start: str, end: str) -> dict:
    return {"machine_id": machine_id, "setup_start": setup_start, "start": start, "end": end}


def _check_downtimes(con: sqlite3.Connection, downtimes: list[dict], trigger_text: str,
                     locked_rows: list[dict]) -> list[tuple]:
    known = {m for (m,) in con.execute("SELECT machine_id FROM machine")}
    clean = []
    for d in downtimes:
        mid = d["machine_id"]
        kind = d.get("downtime_type", "BREAKDOWN")
        if mid not in known:
            raise ValueError(f"unknown machine {mid}")
        if kind not in DOWNTIME_TYPES:
            raise ValueError(f"unknown downtime type {kind!r}; choose from {DOWNTIME_TYPES}")
        start, end = _ts(parse(d["down_start"])), _ts(parse(d["down_end"]))
        if not end > start:
            raise ValueError(f"downtime on {mid} must end after it starts")
        if start < trigger_text:
            raise ValueError(f"downtime on {mid} starts at {start}, before the trigger time "
                             f"{trigger_text}; it could hit work that has already started")
        for r in locked_rows:
            if r["machine_id"] == mid and r["setup_start"] < end and r["end_time"] > start:
                raise ValueError(
                    f"downtime on {mid} ({start} to {end}) overlaps locked job {r['operation_id']} "
                    f"({r['setup_start']} to {r['end_time']}); start it after that job ends")
        clean.append((mid, kind, start, end, d.get("reason")))
    return clean


def reschedule(con: sqlite3.Connection, parent_run_id: str, trigger_time: str, why: str,
               downtimes=(), rule: str | None = None, run_time: str | None = None) -> str:
    """Create a RESCHEDULE run from a finished run. Returns the new run_id.

    downtimes: dicts with machine_id, down_start, down_end and optionally
    downtime_type (default BREAKDOWN) and reason. They are inserted (append-only).
    run_time: when the reschedule happens; defaults to the trigger time.
    """
    parent = con.execute("SELECT run_type, scheduling_rule, status FROM schedule_run"
                         " WHERE run_id = ?", (parent_run_id,)).fetchone()
    if parent is None:
        raise ValueError(f"unknown run {parent_run_id}")
    if parent[0] == "INJECTED":
        raise ValueError("an INJECTED run is a labelled test copy and cannot be rescheduled")
    if parent[2] != "COMPLETED":
        raise ValueError(f"run {parent_run_id} is {parent[2]}; only COMPLETED runs can be rescheduled")
    rule = rule or parent[1]
    if rule not in RULES:
        raise ValueError(f"unknown rule {rule!r}; choose from {RULES}")
    trigger = parse(trigger_time)
    try:
        run_id = _reschedule(con, parent_run_id, _ts(trigger), why, list(downtimes), rule,
                             run_time or _ts(trigger))
    except Exception:
        con.rollback()
        raise
    con.commit()
    return run_id


def _reschedule(con, parent_id, trigger_text, why, downtimes, rule, run_time) -> str:
    trigger = parse(trigger_text)
    parent_rows = _parent_rows(con, parent_id)
    locked_rows = _locked_rows(parent_rows, trigger_text)
    clean = _check_downtimes(con, downtimes, trigger_text, locked_rows)

    # ---- new downtime (append-only)
    last = con.execute("SELECT MAX(downtime_id) FROM machine_downtime").fetchone()[0]
    first_number = int(last.rsplit("-", 1)[1]) + 1 if last else 1
    downtime_ids = []
    for i, (mid, kind, start, end, reason_text) in enumerate(clean):
        did = f"DT-2026-{first_number + i:06d}"
        con.execute(
            "INSERT INTO machine_downtime (downtime_id, machine_id, downtime_type, down_start,"
            " down_end, reason, created_at) VALUES (?,?,?,?,?,?,?)",
            (did, mid, kind, start, end, reason_text, run_time))
        downtime_ids.append(did)

    # ---- plan again around the locked work
    model, feas, timelines, windows = prepare(con)
    for r in locked_rows:
        if r["order_id"] not in model.orders:
            raise ValueError(f"order {r['order_id']} is no longer open, so its started work "
                             "cannot be kept")
    locked = [Placement(
        r["operation_id"], r["order_id"], r["machine_id"], parse(r["setup_start"]),
        r["setup_minutes"], parse(r["start_time"]), r["processing_minutes"], parse(r["end_time"]),
        None, "LOCKED", [],
        f"Locked: setup started {r['setup_start']}, before the trigger time {trigger_text}; "
        f"kept exactly as in run {parent_id}") for r in locked_rows]
    result = dispatch(model, feas, timelines, windows, rule, locked=locked, not_before=trigger)

    # ---- the run row
    run_id = ids.run_id(_count(con, "schedule_run") + 1)
    config = {"rule": rule, "mode": "reschedule", "locked_operations": len(locked),
              "replanned_operations": len(result.placements), "new_downtime_ids": downtime_ids,
              "future_work_planned_from_scratch": True,
              "dispatch_window_minutes": int(DISPATCH_WINDOW.total_seconds() // 60)}
    con.execute(
        "INSERT INTO schedule_run (run_id, run_type, parent_run_id, scheduling_rule, config_json,"
        " trigger_time, trigger_reason, started_at, finished_at, status, feasibility_label,"
        " metrics_json) VALUES (?, 'RESCHEDULE', ?, ?, ?, ?, ?, ?, NULL, 'RUNNING',"
        " 'NOT_VALIDATED', NULL)",
        (run_id, parent_id, rule, json.dumps(config, sort_keys=True), trigger_text, why, run_time))

    # ---- schedule rows: chronological, so each job's predecessor is inserted first
    entries = [(p, "LOCKED") for p in locked] + [(p, "PLANNED") for p in result.placements]
    entries.sort(key=lambda e: (e[0].setup_start, e[0].machine_id, e[0].operation_id))
    base = _count(con, "operation_schedule") + 1
    last_on: dict[str, str] = {}
    rows = []
    for i, (p, status) in enumerate(entries):
        sid = ids.schedule_id(base + i)
        rows.append((sid, run_id, p.operation_id, p.machine_id, _ts(p.setup_start), p.setup_minutes,
                     _ts(p.start), p.processing_minutes, _ts(p.end), last_on.get(p.machine_id),
                     status, run_time))
        last_on[p.machine_id] = sid
    con.executemany(
        "INSERT INTO operation_schedule (schedule_id, run_id, operation_id, machine_id, setup_start,"
        " setup_minutes, start_time, processing_minutes, end_time, previous_schedule_id, status,"
        " created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)", rows)

    # ---- order outcome
    issues = list(feas.issues) + list(result.issues)
    placed_count = Counter(p.order_id for p, _ in entries)
    scheduled = {o for o, ops in model.ops_by_order.items() if ops and placed_count[o] == len(ops)}
    unscheduled = sorted(set(model.orders) - scheduled)
    reason: dict[str, str] = {}
    for issue in issues:
        reason.setdefault(issue.order_id, issue.code)

    # ---- placement decisions
    n = _count(con, "placement_decision") + 1
    decisions = []
    for p, status in entries:
        decisions.append((ids.decision_id(n + len(decisions)), run_id, p.operation_id, p.machine_id,
                          "[]" if status == "LOCKED" else json.dumps(p.candidates, sort_keys=True),
                          "LOCKED" if status == "LOCKED" else p.rule, p.explanation, run_time))
    for issue in result.issues:
        decisions.append((ids.decision_id(n + len(decisions)), run_id, issue.operation_id, None,
                          "[]", rule, issue.message, run_time))
    con.executemany(
        "INSERT INTO placement_decision (decision_id, run_id, operation_id, chosen_machine_id,"
        " candidates_json, rule_applied, explanation, created_at) VALUES (?,?,?,?,?,?,?,?)", decisions)

    # ---- events: what changed compared with the parent run
    parent_by_op = {r["operation_id"]: r for r in parent_rows}
    events = []
    for p, status in entries:
        new = _where(p.machine_id, _ts(p.setup_start), _ts(p.start), _ts(p.end))
        if status == "LOCKED":
            events.append(("LOCKED", p.order_id, p.operation_id, None, new))
            continue
        old_row = parent_by_op.get(p.operation_id)
        if old_row is None:
            events.append(("PLACED", p.order_id, p.operation_id, None, new))
            continue
        old = _where(old_row["machine_id"], old_row["setup_start"], old_row["start_time"],
                     old_row["end_time"])
        if old != new:
            events.append(("MOVED", p.order_id, p.operation_id, old, new))
    in_new = {p.operation_id for p, _ in entries}
    for r in parent_rows:
        if r["operation_id"] not in in_new:
            events.append(("REMOVED", r["order_id"], r["operation_id"],
                           _where(r["machine_id"], r["setup_start"], r["start_time"], r["end_time"]),
                           None))
    for oid in unscheduled:
        events.append(("UNSCHEDULED", oid, None, None, {"reason": reason.get(oid, "INCOMPLETE")}))
    n = _count(con, "schedule_event") + 1
    con.executemany(
        "INSERT INTO schedule_event (event_id, run_id, event_type, order_id, operation_id,"
        " old_value_json, new_value_json, event_time) VALUES (?,?,?,?,?,?,?,?)",
        [(ids.event_id(n + i), run_id, kind, order_id, op_id,
          json.dumps(old, sort_keys=True) if old is not None else None,
          json.dumps(new, sort_keys=True) if new is not None else None, run_time)
         for i, (kind, order_id, op_id, old, new) in enumerate(events)])

    # ---- exceptions raised by the engine
    n = _count(con, "scheduling_exception") + 1
    con.executemany(
        "INSERT INTO scheduling_exception (exception_id, run_id, category, code, severity, order_id,"
        " operation_id, machine_id, message, detected_by, created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        [(ids.exception_id(n + i), run_id, "INFEASIBLE_ORDER", x.code, "HARD", x.order_id,
          x.operation_id, None, x.message, "ENGINE", run_time) for i, x in enumerate(issues)])

    # ---- statuses and history (only where something changed)
    current = dict(con.execute("SELECT order_id, status FROM production_order"))
    changed = [(o, current[o], "SCHEDULED" if o in scheduled else "UNSCHEDULED")
               for o in sorted(model.orders)
               if current[o] != ("SCHEDULED" if o in scheduled else "UNSCHEDULED")]
    con.executemany("UPDATE production_order SET status = ?, updated_at = ? WHERE order_id = ?",
                    [(new, run_time, o) for o, _, new in changed])
    n = _count(con, "order_status_history") + 1
    con.executemany(
        "INSERT INTO order_status_history (history_id, order_id, from_status, to_status, changed_at,"
        " run_id, reason) VALUES (?,?,?,?,?,?,?)",
        [(ids.order_history_id(n + i), o, old, new, run_time, run_id, f"Reschedule {run_id}: {why}")
         for i, (o, old, new) in enumerate(changed)])
    con.execute("UPDATE operation SET status = 'PENDING', updated_at = ? WHERE status = 'SCHEDULED'",
                (run_time,))
    con.executemany("UPDATE operation SET status = 'SCHEDULED', updated_at = ? WHERE operation_id = ?",
                    [(run_time, op_id) for op_id in sorted(in_new)])

    metrics = {"orders_total": len(model.orders), "orders_scheduled": len(scheduled),
               "orders_unscheduled": len(unscheduled), "operations_placed": len(entries),
               "locked_operations": len(locked), "replanned_operations": len(result.placements),
               "exceptions": len(issues)}
    con.execute("UPDATE schedule_run SET status = 'COMPLETED', finished_at = ?, metrics_json = ?"
                " WHERE run_id = ?", (run_time, json.dumps(metrics, sort_keys=True), run_id))
    return run_id