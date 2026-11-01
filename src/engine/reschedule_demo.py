"""Demo: break the busiest machine, reschedule, and compare with the original plan.

Usage:  python -m src.engine.reschedule_demo [profile] [trigger_time] [hours]
Defaults: full  "2026-11-10 06:00:00"  72
Builds a CRITICAL_RATIO baseline, then two RESCHEDULE runs from it:
  control   re-plan the future with no change (shows the effect of re-planning alone)
  breakdown the busiest remaining machine is down for the given hours
"""
from __future__ import annotations

import sqlite3
import sys
from datetime import timedelta

from config.settings import SCHEMA_SQL
from src.db.loader import load_tables
from src.engine.baseline import write_baseline
from src.engine.dispatcher import _ts, dispatch, prepare
from src.engine.reschedule import reschedule
from src.engine.timeline import parse
from src.generator.export import ALL_TABLES, build_dataset
from src.metrics.compare import compare_runs
from src.metrics.kpis import record_metrics
from src.validation.constraints import validate_and_record


def pick_breakdown(con: sqlite3.Connection, run_id: str, trigger: str, hours: int) -> dict | None:
    """The machine with the most jobs after the trigger goes down once its running job ends."""
    row = con.execute(
        "SELECT machine_id FROM operation_schedule WHERE run_id = ? AND setup_start >= ?"
        " GROUP BY machine_id ORDER BY COUNT(*) DESC, machine_id LIMIT 1", (run_id, trigger)).fetchone()
    if row is None:
        return None
    machine_id = row[0]
    busy_until = con.execute(
        "SELECT MAX(end_time) FROM operation_schedule WHERE run_id = ? AND machine_id = ?"
        " AND setup_start < ?", (run_id, machine_id, trigger)).fetchone()[0]
    start = max(trigger, busy_until or trigger)
    return {"machine_id": machine_id, "down_start": start,
            "down_end": _ts(parse(start) + timedelta(hours=hours)), "downtime_type": "BREAKDOWN",
            "reason": f"Simulated breakdown of {machine_id}"}


def _f(value) -> str:
    if value is None:
        return "-"
    return f"{value:,.2f}" if isinstance(value, float) else f"{value:,}"


def _check(con: sqlite3.Connection, run_id: str, name: str) -> None:
    found = validate_and_record(con, run_id)
    record_metrics(con, run_id)
    label = con.execute("SELECT feasibility_label FROM schedule_run WHERE run_id = ?",
                        (run_id,)).fetchone()[0]
    locked = con.execute("SELECT COUNT(*) FROM operation_schedule WHERE run_id = ? AND status = 'LOCKED'",
                         (run_id,)).fetchone()[0]
    placed = con.execute("SELECT COUNT(*) FROM operation_schedule WHERE run_id = ?", (run_id,)).fetchone()[0]
    print(f"{name:10s} {run_id}  operations {placed}  (locked {locked})  "
          f"violations {len(found)}  label {label}")
    for v in found[:3]:
        print("   e.g.", v.message)


def _show(title: str, cmp: dict) -> None:
    print(f"\n{title}  ({cmp['base_run_id']} -> {cmp['other_run_id']})")
    print(f"  {'metric':28s} {'before':>14s} {'after':>14s} {'change':>12s}")
    for key, v in cmp["metrics"].items():
        print(f"  {key:28s} {_f(v['base']):>14s} {_f(v['other']):>14s} {_f(v['delta']):>12s}")
    print(f"  operations: {cmp['unchanged']} unchanged, {cmp['moved_count']} moved "
          f"({cmp['machine_changed_count']} to another machine), "
          f"{len(cmp['removed'])} removed, {len(cmp['added'])} added")
    print(f"  orders: newly unscheduled {len(cmp['newly_unscheduled_orders'])}, "
          f"newly scheduled {len(cmp['newly_scheduled_orders'])}, "
          f"became late {len(cmp['became_late_orders'])}, "
          f"no longer late {len(cmp['no_longer_late_orders'])}")


def main() -> None:
    profile = sys.argv[1] if len(sys.argv) > 1 else "full"
    trigger = sys.argv[2] if len(sys.argv) > 2 else "2026-11-10 06:00:00"
    hours = int(sys.argv[3]) if len(sys.argv) > 3 else 72

    con = sqlite3.connect(":memory:")
    con.executescript(SCHEMA_SQL.read_text(encoding="utf-8"))
    load_tables(con, build_dataset(profile), ALL_TABLES)
    model, feas, tls, wins = prepare(con)
    base_id = write_baseline(con, model, feas, dispatch(model, feas, tls, wins, "CRITICAL_RATIO"))

    breakdown = pick_breakdown(con, base_id, trigger, hours)
    if breakdown is None:
        print("No operation starts after the trigger time; choose an earlier trigger.")
        return
    print(f"profile {profile}, trigger {trigger}")
    print(f"breakdown: {breakdown['machine_id']} from {breakdown['down_start']} "
          f"to {breakdown['down_end']} ({hours} h)\n")

    _check(con, base_id, "baseline")
    control_id = reschedule(con, base_id, trigger, "Control: re-plan with no change")
    _check(con, control_id, "control")
    broken_id = reschedule(con, base_id, trigger, f"{breakdown['machine_id']} breakdown",
                           downtimes=[breakdown])
    _check(con, broken_id, "breakdown")

    _show("RE-PLAN ONLY (control)", compare_runs(con, base_id, control_id))
    _show("WITH BREAKDOWN", compare_runs(con, base_id, broken_id))
    on_machine = con.execute(
        "SELECT COUNT(*) FROM operation_schedule WHERE run_id = ? AND machine_id = ?"
        " AND setup_start < ? AND end_time > ?",
        (broken_id, breakdown["machine_id"], breakdown["down_end"], breakdown["down_start"])).fetchone()[0]
    print(f"\njobs planned on {breakdown['machine_id']} during its breakdown: {on_machine}")


if __name__ == "__main__":
    main()