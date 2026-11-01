"""Baseline run writer (Phase 5d).

Writes one BASELINE run: schedule_run, operation_schedule, placement_decision,
schedule_event, scheduling_exception, inventory_transaction, plus order/operation
status updates.
"""
from __future__ import annotations

import json
import sqlite3
from collections import Counter

from src.engine.dispatcher import DISPATCH_WINDOW, DispatchResult, _ts
from src.engine.model import FeasibilityResult, Model
from src.engine.transactions import write_transactions
from src.generator import ids

RUN_TIME = "2026-10-22 09:00:00"  # fixed so output is reproducible


def _next(con: sqlite3.Connection, table: str) -> int:
    return con.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] + 1


def write_baseline(con: sqlite3.Connection, model: Model, feas: FeasibilityResult,
                   result: DispatchResult, run_time: str = RUN_TIME) -> str:
    """Write the run and return its run_id. Commits when done."""
    run_id = ids.run_id(_next(con, "schedule_run"))
    config = {"rule": result.rule, "placement": "append-only",
              "labor_and_tools_held_during": "setup+processing",
              "dispatch_window_minutes": int(DISPATCH_WINDOW.total_seconds() // 60),
              "block_must_fit_one_shift_window": True}
    con.execute(
        "INSERT INTO schedule_run (run_id, run_type, parent_run_id, scheduling_rule, config_json,"
        " trigger_time, trigger_reason, started_at, finished_at, status, feasibility_label,"
        " metrics_json) VALUES (?, 'BASELINE', NULL, ?, ?, NULL, NULL, ?, NULL, 'RUNNING',"
        " 'NOT_VALIDATED', NULL)",
        (run_id, result.rule, json.dumps(config, sort_keys=True), run_time))

    # ---- operation_schedule
    n = _next(con, "operation_schedule")
    schedule_of: dict[str, str] = {}
    rows = []
    for i, p in enumerate(result.placements):
        sid = ids.schedule_id(n + i)
        schedule_of[p.operation_id] = sid
        rows.append((sid, run_id, p.operation_id, p.machine_id, _ts(p.setup_start),
                     p.setup_minutes, _ts(p.start), p.processing_minutes, _ts(p.end),
                     schedule_of.get(p.previous_operation_id) if p.previous_operation_id else None,
                     "PLANNED", run_time))
    con.executemany(
        "INSERT INTO operation_schedule (schedule_id, run_id, operation_id, machine_id, setup_start,"
        " setup_minutes, start_time, processing_minutes, end_time, previous_schedule_id, status,"
        " created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)", rows)

    # ---- inventory_transaction (RESERVE, then CONSUME or RELEASE)
    write_transactions(con, run_id, feas,
                       {p.operation_id: _ts(p.start) for p in result.placements}, run_time)

    # ---- order outcome
    placed_count = Counter(p.order_id for p in result.placements)
    scheduled = {o for o, ops in model.ops_by_order.items()
                 if ops and placed_count[o] == len(ops)}
    reason: dict[str, str] = {}
    for issue in list(feas.issues) + list(result.issues):
        reason.setdefault(issue.order_id, issue.code)
    unscheduled = sorted(set(model.orders) - scheduled)

    # ---- placement_decision
    n = _next(con, "placement_decision")
    decisions = [(ids.decision_id(n + i), run_id, p.operation_id, p.machine_id,
                  json.dumps(p.candidates, sort_keys=True), p.rule, p.explanation, run_time)
                 for i, p in enumerate(result.placements)]
    n += len(decisions)
    for j, issue in enumerate(result.issues):
        decisions.append((ids.decision_id(n + j), run_id, issue.operation_id, None, "[]",
                          result.rule, issue.message, run_time))
    con.executemany(
        "INSERT INTO placement_decision (decision_id, run_id, operation_id, chosen_machine_id,"
        " candidates_json, rule_applied, explanation, created_at) VALUES (?,?,?,?,?,?,?,?)", decisions)

    # ---- schedule_event
    n = _next(con, "schedule_event")
    events = [(ids.event_id(n + i), run_id, "PLACED", p.order_id, p.operation_id, None,
               json.dumps({"machine_id": p.machine_id, "setup_start": _ts(p.setup_start),
                           "start": _ts(p.start), "end": _ts(p.end)}, sort_keys=True), run_time)
              for i, p in enumerate(result.placements)]
    n += len(events)
    for j, oid in enumerate(unscheduled):
        events.append((ids.event_id(n + j), run_id, "UNSCHEDULED", oid, None, None,
                       json.dumps({"reason": reason.get(oid, "INCOMPLETE")}), run_time))
    con.executemany(
        "INSERT INTO schedule_event (event_id, run_id, event_type, order_id, operation_id,"
        " old_value_json, new_value_json, event_time) VALUES (?,?,?,?,?,?,?,?)", events)

    # ---- scheduling_exception
    n = _next(con, "scheduling_exception")
    exceptions = [(ids.exception_id(n + i), run_id, "INFEASIBLE_ORDER", x.code, "HARD",
                   x.order_id, x.operation_id, None, x.message, "ENGINE", run_time)
                  for i, x in enumerate(list(feas.issues) + list(result.issues))]
    con.executemany(
        "INSERT INTO scheduling_exception (exception_id, run_id, category, code, severity, order_id,"
        " operation_id, machine_id, message, detected_by, created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        exceptions)

    # ---- statuses and history
    new_status = {o: ("SCHEDULED" if o in scheduled else "UNSCHEDULED") for o in model.orders}
    con.executemany("UPDATE production_order SET status = ?, updated_at = ? WHERE order_id = ?",
                    [(s, run_time, o) for o, s in sorted(new_status.items())])
    con.executemany("UPDATE operation SET status = 'SCHEDULED', updated_at = ?"
                    " WHERE operation_id = ?",
                    [(run_time, p.operation_id) for p in result.placements])
    n = _next(con, "order_status_history")
    con.executemany(
        "INSERT INTO order_status_history (history_id, order_id, from_status, to_status, changed_at,"
        " run_id, reason) VALUES (?,?,?,?,?,?,?)",
        [(ids.order_history_id(n + i), o, "RELEASED", s, run_time, run_id,
          f"Baseline run {run_id}" + (f": {reason.get(o, 'INCOMPLETE')}" if s == "UNSCHEDULED" else ""))
         for i, (o, s) in enumerate(sorted(new_status.items()))])

    metrics = {"orders_total": len(model.orders), "orders_scheduled": len(scheduled),
               "orders_unscheduled": len(unscheduled), "operations_placed": len(result.placements),
               "exceptions": len(exceptions)}
    con.execute("UPDATE schedule_run SET status = 'COMPLETED', finished_at = ?, metrics_json = ?"
                " WHERE run_id = ?", (run_time, json.dumps(metrics, sort_keys=True), run_id))
    con.commit()
    return run_id


if __name__ == "__main__":
    import sys

    from config.settings import SCHEMA_SQL
    from src.db.loader import load_tables
    from src.engine.dispatcher import dispatch, prepare
    from src.generator.export import ALL_TABLES, build_dataset

    chosen = sys.argv[1] if len(sys.argv) > 1 else "full"
    chosen_rule = sys.argv[2] if len(sys.argv) > 2 else "PRIORITY_EDD"
    con = sqlite3.connect(":memory:")
    con.executescript(SCHEMA_SQL.read_text(encoding="utf-8"))
    load_tables(con, build_dataset(chosen), ALL_TABLES)
    mdl, fe, tls, wins = prepare(con)
    new_run = write_baseline(con, mdl, fe, dispatch(mdl, fe, tls, wins, chosen_rule))
    print(new_run, chosen_rule)
    for t in ("schedule_run", "operation_schedule", "placement_decision", "schedule_event",
              "scheduling_exception", "order_status_history", "inventory_transaction"):
        print(f"{t:24s} {con.execute(f'SELECT COUNT(*) FROM {t}').fetchone()[0]:6d}")
    for status, count in con.execute("SELECT status, COUNT(*) FROM production_order"
                                     " GROUP BY status ORDER BY status"):
        print(f"orders {status:12s} {count:5d}")