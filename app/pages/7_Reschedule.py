"""Reschedule from a trigger time, optionally with a new rule."""
import common
import streamlit as st

from src.db.connection import connect
from src.services import runs as run_service

st.set_page_config(page_title="Reschedule", layout="wide")
st.title("Reschedule")
parent = common.pick_run()

trigger = st.text_input("Trigger time (YYYY-MM-DD HH:MM:SS)", "2026-11-09 06:00:00")
why = st.text_input("Reason", "Manual reschedule from the UI")
choice = st.selectbox("Rule", ["Keep parent rule"] + list(run_service.RULES))

if st.button("Reschedule"):
    try:
        con = connect(common.require_db())
        try:
            with st.spinner("Rescheduling..."):
                summary = run_service.reschedule_run(
                    con, parent, trigger, why,
                    rule=None if choice == "Keep parent rule" else choice)
        finally:
            con.close()
    except Exception as exc:  # show the engine's message instead of a stack trace
        st.error(f"Reschedule failed: {exc}")
    else:
        if summary["feasibility_label"] == "FEASIBLE":
            st.success(f"Created {summary['run_id']}: FEASIBLE")
        else:
            st.error(f"Created {summary['run_id']}: {summary['feasibility_label']}, "
                     f"{summary.get('violation_count')} violations")
        st.json(summary)