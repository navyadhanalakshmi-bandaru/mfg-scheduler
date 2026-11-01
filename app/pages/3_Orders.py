"""Production orders and whether each is fully scheduled in the selected run."""
import common
import streamlit as st

st.set_page_config(page_title="Orders", layout="wide")
st.title("Orders")
run_id = common.pick_run()

orders = common.frame(
    "SELECT p.order_id, p.sku_id, p.quantity, p.priority_class, p.release_time, p.due_time,"
    " p.status,"
    " CASE WHEN (SELECT COUNT(*) FROM operation_schedule s JOIN operation o"
    "            ON o.operation_id = s.operation_id"
    "            WHERE s.run_id = ? AND o.order_id = p.order_id)"
    "  = (SELECT COUNT(*) FROM operation o2 WHERE o2.order_id = p.order_id)"
    " THEN 'YES' ELSE 'NO' END AS scheduled_in_run"
    " FROM production_order p ORDER BY p.order_id", (run_id,))

priorities = sorted(orders.priority_class.unique())
chosen = st.sidebar.multiselect("Priority", priorities, default=priorities)
scheduled = st.sidebar.selectbox("Scheduled in this run", ["All", "YES", "NO"])

view = orders[orders.priority_class.isin(chosen)]
if scheduled != "All":
    view = view[view.scheduled_in_run == scheduled]

st.caption(f"{len(view)} of {len(orders)} orders")
st.dataframe(view, width="stretch", hide_index=True)