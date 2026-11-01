"""Build a baseline run, validate it, compute and store the KPIs, print a summary.

Usage:  python -m src.metrics.report [full|starter] [RULE]
"""
from __future__ import annotations

import sqlite3
import sys

from config.settings import SCHEMA_SQL
from src.db.loader import load_tables
from src.engine.baseline import write_baseline
from src.engine.dispatcher import dispatch, prepare
from src.generator.export import ALL_TABLES, build_dataset
from src.metrics.kpis import record_metrics
from src.validation.constraints import validate_and_record


def _show(value) -> str:
    return "not validated" if value is None else str(value)


def main() -> None:
    profile = sys.argv[1] if len(sys.argv) > 1 else "full"
    rule = sys.argv[2] if len(sys.argv) > 2 else "PRIORITY_EDD"
    con = sqlite3.connect(":memory:")
    con.executescript(SCHEMA_SQL.read_text(encoding="utf-8"))
    load_tables(con, build_dataset(profile), ALL_TABLES)
    model, feas, tls, wins = prepare(con)
    run_id = write_baseline(con, model, feas, dispatch(model, feas, tls, wins, rule))
    validate_and_record(con, run_id)
    m = record_metrics(con, run_id)

    print(f"{run_id} {rule} ({profile})  label={m['feasibility_label']}")
    print(f"orders total / scheduled / unscheduled  {m['orders_total']:5d} {m['orders_scheduled']:5d} {m['orders_unscheduled']:5d}")
    print(f"operations scheduled                    {m['operations_scheduled']:5d}")
    print(f"makespan                       {m['makespan_minutes']:8d} min ({m['makespan_minutes'] / 1440:.1f} days)")
    print(f"  from {m['first_start']} to {m['last_end']}")
    print(f"total production time          {m['total_production_minutes']:8d} min")
    print(f"total changeover time          {m['total_changeover_minutes']:8d} min")
    print(f"utilization, whole horizon     {m['utilization_overall_pct']:8.2f} %")
    print(f"utilization, makespan window   {m['utilization_active_window_pct']:8.2f} %")
    print(f"on-time delivery               {m['on_time_delivery_pct']:8.2f} %  ({m['on_time_orders']} on time)")
    print(f"late orders                    {m['late_orders']:8d}")
    print(f"total tardiness                {m['total_tardiness_minutes']:8d} min ({m['total_tardiness_minutes'] / 1440:.1f} days)")
    print(f"late by priority               {m['late_by_priority']}")
    print(f"constraint violations          {_show(m['hard_violations']):>8s}")
    print(f"material violations            {_show(m['material_violations']):>8s}")
    print(f"feasibility rate               {_show(m['feasibility_rate_pct']):>8s} %")
    busiest = sorted(m["utilization_by_machine_pct"].items(), key=lambda kv: (-kv[1], kv[0]))[:5]
    print("busiest machines:", ", ".join(f"{k} {v}%" for k, v in busiest))


if __name__ == "__main__":
    main()