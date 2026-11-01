"""Anomaly demo: inject five kinds of bad schedules into a copy and show the validator catches them.

Runs in memory on a fresh starter or full dataset. It never opens the saved
ground-truth files and never touches the main database.
"""
import sqlite3

import common  # noqa: F401  (sets up the import path)
import streamlit as st

from config.settings import SCHEMA_SQL
from src.db.loader import load_tables
from src.engine.baseline import write_baseline
from src.engine.dispatcher import dispatch, prepare
from src.generator.export import ALL_TABLES, build_dataset
from src.validation.anomalies import evaluate, format_report, inject_anomalies, open_ground_truth
from src.validation.constraints import validate_and_record

st.set_page_config(page_title="Anomalies", layout="wide")
st.title("Anomalies")
st.write("Injects five kinds of bad schedules (overlap, precedence, early material, outside "
         "shift, missing changeover) into a copy of a valid run, then checks that the "
         "independent validator finds them.")

profile = st.selectbox("Dataset", ["starter", "full"])
if st.button("Run anomaly demo"):
    with st.spinner("Scheduling, injecting and validating..."):
        con = sqlite3.connect(":memory:")
        con.executescript(SCHEMA_SQL.read_text(encoding="utf-8"))
        load_tables(con, build_dataset(profile), ALL_TABLES)
        model, feas, tls, wins = prepare(con)
        base = write_baseline(con, model, feas, dispatch(model, feas, tls, wins, "CRITICAL_RATIO"))
        gt = open_ground_truth()
        summary = inject_anomalies(con, gt, base)
        found = validate_and_record(con, summary["run_id"])
        report = format_report(evaluate(con, gt, summary["run_id"]))
        rows = con.execute(
            "SELECT code, severity, operation_id, machine_id, message FROM scheduling_exception"
            " WHERE run_id = ? AND category = 'CONSTRAINT_VIOLATION' ORDER BY exception_id",
            (summary["run_id"],)).fetchall()
        con.close()
        gt.close()
    st.success(f"Base run {summary['base_run_id']}, injected run {summary['run_id']}: "
               f"validator reported {len(found)} violations")
    st.subheader("Detection report")
    st.code(report)
    st.subheader("Violations reported by the validator")
    st.dataframe([dict(zip(["code", "severity", "operation_id", "machine_id", "message"], r))
                  for r in rows], width="stretch", hide_index=True)