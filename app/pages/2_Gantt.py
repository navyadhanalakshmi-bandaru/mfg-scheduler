"""Gantt chart for one run (Plotly)."""
from datetime import date

import common
import plotly.express as px
import streamlit as st

st.set_page_config(page_title="Gantt", layout="wide")
st.title("Gantt")
run_id = common.pick_run()

bounds = common.frame("SELECT MIN(setup_start) AS lo FROM operation_schedule WHERE run_id = ?", (run_id,))
if bounds.empty or bounds.lo.iloc[0] is None:
    st.info("This run has no scheduled operations.")
    st.stop()

groups = common.frame("SELECT DISTINCT machine_group FROM machine ORDER BY machine_group").machine_group.tolist()
chosen = st.sidebar.multiselect("Machine groups", groups, default=groups[:3])
first_day = bounds.lo.iloc[0][:10]
days = st.sidebar.slider("Days to show", 1, 14, 3)
start = st.sidebar.date_input("From", value=date.fromisoformat(first_day))