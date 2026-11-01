"""Engine controls: pick a rule and build a new baseline run."""
import common
import streamlit as st

from src.db.connection import connect
from src.services import runs as run_service

st.set_page_config(page_title="Engine controls", layout="wide")
st.title("Engine controls")

rule = st.selectbox("Scheduling rule", list(run_service.RULES), index=2)
if st.button("Build new baseline"):
    con = connect(common.require_db())
    try:
        with st.spinner("Scheduling..."):
            summary = run_service.create_baseline(con, rule)
    finally:
        con.close()
    if summary["feasibility_label"] == "FEASIBLE":
        st.success(f"Created {summary['run_id']}: FEASIBLE")
    else:
        st.error(f"Created {summary['run_id']}: {summary['feasibility_label']}, "
                 f"{summary['violation_count']} violations")
    st.json(summary)