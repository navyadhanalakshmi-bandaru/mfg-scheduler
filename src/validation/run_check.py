"""Build a baseline run in memory, validate it independently, print a summary.

Usage:  python -m src.validation.run_check [full|starter] [RULE]
"""
from __future__ import annotations

import sqlite3
import sys
from collections import Counter

from config.settings import SCHEMA_SQL
from src.db.loader import load_tables
from src.engine.baseline import write_baseline
from src.engine.dispatcher import dispatch, prepare
from src.generator.export import ALL_TABLES, build_dataset
from src.validation.constraints import VIOLATION_CODES, validate_and_record


def main() -> None:
    profile = sys.argv[1] if len(sys.argv) > 1 else "full"
    rule = sys.argv[2] if len(sys.argv) > 2 else "PRIORITY_EDD"
    con = sqlite3.connect(":memory:")
    con.executescript(SCHEMA_SQL.read_text(encoding="utf-8"))
    load_tables(con, build_dataset(profile), ALL_TABLES)
    model, feas, tls, wins = prepare(con)
    run_id = write_baseline(con, model, feas, dispatch(model, feas, tls, wins, rule))
    found = validate_and_record(con, run_id)

    label = con.execute("SELECT feasibility_label FROM schedule_run WHERE run_id = ?",
                        (run_id,)).fetchone()[0]
    placed = con.execute("SELECT COUNT(*) FROM operation_schedule WHERE run_id = ?",
                         (run_id,)).fetchone()[0]
    print(f"{run_id} {rule} ({profile})")
    print(f"operations checked      {placed:6d}")
    print(f"hard violations         {len(found):6d}")
    print(f"feasibility_label       {label}")
    counts = Counter(v.code for v in found)
    for code in VIOLATION_CODES:
        print(f"  {code:30s} {counts.get(code, 0):5d}")
    shown = Counter()
    for v in found:
        if shown[v.code] < 3:
            print(f"  e.g. {v.code}: {v.message}")
            shown[v.code] += 1


if __name__ == "__main__":
    main()