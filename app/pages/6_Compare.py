"""Compare two runs: KPIs side by side and the raw comparison result."""
import common
import pandas as pd
import streamlit as st

from src.db.connection import connect
from src.services import runs as run_service

st.set_page_config(page_title="Compare runs", layout="wide")
st.title("Compare runs")

runs = common.frame("SELECT run_id, run_type, scheduling_rule, feasibility_label"
                    " FROM schedule_run ORDER BY run_id")
if len(runs) < 2:
    st.info("Need at least two runs. Build another baseline on the Engine page.")
    st.stop()

labels = {r.run_id: f"{r.run_id[-6:]} {r.run_type} {r.scheduling_rule} {r.feasibility_label}"
          for r in runs.itertuples()}
ids = list(labels)
base = st.selectbox("Base run", ids, index=0, format_func=labels.get)
other = st.selectbox("Other run", ids, index=len(ids) - 1, format_func=labels.get)
if base == other:
    st.warning("Pick two different runs.")
    st.stop()

con = connect(common.require_db())
try:
    left = run_service.metrics(con, base)
    right = run_service.metrics(con, other)
    result = run_service.compare(con, base, other)
finally:
    con.close()

st.subheader("KPIs side by side")
keys = list(dict.fromkeys(list(left) + list(right)))
table = pd.DataFrame({"KPI": keys,
                      base[-6:]: [str(left.get(k, "")) for k in keys],
                      other[-6:]: [str(right.get(k, "")) for k in keys]})
st.dataframe(table, width="stretch", hide_index=True)

st.subheader("Comparison result")
if isinstance(result, dict) and all(not isinstance(v, (dict, list)) for v in result.values()):
    st.dataframe(pd.DataFrame([(k, str(v)) for k, v in result.items()],
                              columns=["Field", "Value"]), width="stretch", hide_index=True)
else:
    st.json(result)