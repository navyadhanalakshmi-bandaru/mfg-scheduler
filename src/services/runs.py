"""Run-level actions shared by the API and the bootstrap script."""
import sqlite3
from collections import Counter

from src.engine.baseline import write_baseline
from src.engine.dispatcher import dispatch, prepare
from src.validation.constraints import validate_and_record

RULES = ("EDD", "PRIORITY_EDD", "CRITICAL_RATIO")


def summarize(con: sqlite3.Connection, run_id: str, violations) -> dict:
    label = con.execute("SELECT feasibility_label FROM schedule_run WHERE run_id = ?",
                        (run_id,)).fetchone()[0]
    return {"run_id": run_id, "feasibility_label": label,
            "violation_count": len(violations),
            "violations_by_code": dict(sorted(Counter(v.code for v in violations).items()))}


def stored_summary(con: sqlite3.Connection, run_id: str) -> dict:
    """Summary of an already validated run, read from the saved exception rows."""
    label = con.execute("SELECT feasibility_label FROM schedule_run WHERE run_id = ?",
                        (run_id,)).fetchone()[0]
    rows = con.execute("SELECT code FROM scheduling_exception WHERE run_id = ?"
                       " AND category = 'CONSTRAINT_VIOLATION'", (run_id,)).fetchall()
    return {"run_id": run_id, "feasibility_label": label,
            "violation_count": len(rows),
            "violations_by_code": dict(sorted(Counter(r[0] for r in rows).items()))}


def create_baseline(con: sqlite3.Connection, rule: str = "CRITICAL_RATIO") -> dict:
    if rule not in RULES:
        raise ValueError(f"unknown rule {rule}; choose from {RULES}")
    model, feas, tls, wins = prepare(con)
    run_id = write_baseline(con, model, feas, dispatch(model, feas, tls, wins, rule))
    violations = validate_and_record(con, run_id)
    con.commit()
    return summarize(con, run_id, violations)


def validate(con: sqlite3.Connection, run_id: str) -> dict:
    """Validate a run once. A run that is already validated returns its stored result."""
    label = con.execute("SELECT feasibility_label FROM schedule_run WHERE run_id = ?",
                        (run_id,)).fetchone()[0]
    if label != "NOT_VALIDATED":
        return stored_summary(con, run_id)
    violations = validate_and_record(con, run_id)
    con.commit()
    return summarize(con, run_id, violations)