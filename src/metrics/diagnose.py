"""Explain why orders of one priority class are late in a baseline run.

Read-only. Usage:  python -m src.metrics.diagnose [profile] [RULE] [PRIORITY]
Defaults: full CRITICAL_RATIO URGENT
"""
from __future__ import annotations

import sqlite3
import sys
from datetime import datetime
from statistics import median

from config.settings import SCHEMA_SQL
from src.db.loader import load_tables
from src.engine.baseline import write_baseline
from src.engine.dispatcher import dispatch, prepare
from src.generator.export import ALL_TABLES, build_dataset

TS = "%Y-%m-%d %H:%M:%S"


def _minutes(a: str, b: str) -> float:
    return (datetime.strptime(b, TS) - datetime.strptime(a, TS)).total_seconds() / 60


def main() -> None:
    profile = sys.argv[1] if len(sys.argv) > 1 else "full"
    rule = sys.argv[2] if len(sys.argv) > 2 else "CRITICAL_RATIO"
    priority = sys.argv[3] if len(sys.argv) > 3 else "URGENT"

    con = sqlite3.connect(":memory:")
    con.executescript(SCHEMA_SQL.read_text(encoding="utf-8"))
    load_tables(con, build_dataset(profile), ALL_TABLES)
    model, feas, tls, wins = prepare(con)
    run_id = write_baseline(con, model, feas, dispatch(model, feas, tls, wins, rule))

    rows = con.execute(
        "SELECT po.order_id, po.release_time, po.due_time, MIN(s.setup_start), MAX(s.end_time),"
        " SUM(s.setup_minutes + s.processing_minutes), COUNT(*)"
        " FROM production_order po JOIN operation o ON o.order_id = po.order_id"
        " JOIN operation_schedule s ON s.operation_id = o.operation_id AND s.run_id = ?"
        " WHERE po.priority_class = ? GROUP BY po.order_id"
        " HAVING COUNT(*) = (SELECT COUNT(*) FROM operation x WHERE x.order_id = po.order_id)"
        " ORDER BY po.order_id", (run_id, priority)).fetchall()

    data = []
    for order_id, release, due, first, done, work, n_ops in rows:
        data.append({"order": order_id, "ops": n_ops,
                     "allowed": _minutes(release, due) / 1440,
                     "work": work / 1440,
                     "wait_first": _minutes(release, first) / 1440,
                     "flow": _minutes(release, done) / 1440,
                     "late": max(0.0, _minutes(due, done) / 1440)})
    late = [d for d in data if d["late"] > 0]

    print(f"{run_id} {rule} ({profile}): {len(data)} scheduled {priority} orders, {len(late)} late")
    if not data:
        return
    for label, subset in (("all", data), ("late", late)):
        if subset:
            print(f"{label:5s} median days: allowed {median(d['allowed'] for d in subset):6.2f}"
                  f"  machine-work {median(d['work'] for d in subset):6.2f}"
                  f"  wait-for-first-op {median(d['wait_first'] for d in subset):6.2f}"
                  f"  release-to-done {median(d['flow'] for d in subset):6.2f}")
    print()
    print(f"{'order':18s} {'ops':>3s} {'allowed':>8s} {'work':>6s} {'wait1':>6s} {'flow':>6s} {'late':>6s}  (days)")
    for d in sorted(late, key=lambda x: -x["late"])[:12]:
        print(f"{d['order']:18s} {d['ops']:3d} {d['allowed']:8.2f} {d['work']:6.2f} "
              f"{d['wait_first']:6.2f} {d['flow']:6.2f} {d['late']:6.2f}")


if __name__ == "__main__":
    main()