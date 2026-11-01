"""Dashboard."""
import common
import pandas as pd
import streamlit as st

from src.db.connection import connect
from src.metrics.kpis import compute_metrics

st.set_page_config(page_title="Manufacturing Scheduler", layout="wide")
st.title("Manufacturing Scheduler: Dashboard")

run_id = common.pick_run()
con = connect(common.require_db())
try:
    metrics = compute_metrics(con, run_id)
finally:
    con.close()

label = metrics.get("feasibility_label")
(st.success if label == "FEASIBLE" else st.error if label == "INFEASIBLE_VIOLATIONS"
 else st.info)(f"{run_id}: {label}")

cols = st.columns(4)
cols[0].metric("Orders scheduled", f"{metrics.get('orders_scheduled')} / {metrics.get('orders_total')}")
cols[1].metric("Late orders", metrics.get("late_orders"))
cols[2].metric("Makespan (min)", metrics.get("makespan_minutes"))
cols[3].metric("Hard violations", metrics.get("hard_violations"))

st.subheader("All KPIs")
st.dataframe(pd.DataFrame([(k, str(v)) for k, v in metrics.items()], columns=["KPI", "Value"]),
             width="stretch", hide_index=True)

st.subheader("Runs")
st.dataframe(common.frame(
    "SELECT run_id, run_type, parent_run_id, scheduling_rule, status, feasibility_label"
    " FROM schedule_run ORDER BY run_id"), width="stretch", hide_index=True)