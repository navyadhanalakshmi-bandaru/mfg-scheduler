"""Why orders are unscheduled and which constraints were violated."""
import common
import streamlit as st

st.set_page_config(page_title="Exceptions", layout="wide")
st.title("Exceptions")
run_id = common.pick_run()

data = common.frame(
    "SELECT category, code, severity, order_id, operation_id, machine_id, message, detected_by"
    " FROM scheduling_exception WHERE run_id = ? ORDER BY exception_id", (run_id,))

if data.empty:
    st.success("No exceptions recorded for this run.")
    st.stop()

categories = sorted(data.category.unique())
picked = st.sidebar.multiselect("Category", categories, default=categories)
view = data[data.category.isin(picked)]
codes = sorted(view.code.unique())
picked_codes = st.sidebar.multiselect("Code", codes, default=codes)
view = view[view.code.isin(picked_codes)]

st.subheader("Counts")
st.dataframe(view.groupby(["category", "code"]).size().reset_index(name="rows"),
             width="stretch", hide_index=True)
st.subheader("Details")
st.caption(f"{len(view)} rows")
st.dataframe(view, width="stretch", hide_index=True)