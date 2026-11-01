"""Read-only views of master data, calendars and materials."""
import common
import streamlit as st

st.set_page_config(page_title="Data", layout="wide")
st.title("Data")

QUERIES = {
    "Machines": "SELECT machine_id, machine_name, machine_group, status, default_shift_pattern"
                " FROM machine ORDER BY machine_id",
    "Products": "SELECT sku_id, product_name, product_family, standard_lot_size, lifecycle_state"
                " FROM product ORDER BY sku_id",
    "Calendar": "SELECT machine_id, shift_code, window_start, window_end, available_minutes"
                " FROM machine_calendar ORDER BY machine_id, window_start",
    "Downtime": "SELECT machine_id, downtime_type, down_start, down_end, reason"
                " FROM machine_downtime ORDER BY machine_id, down_start",
    "Materials": "SELECT m.material_id, m.material_name, m.lead_time_days,"
                 " COALESCE(SUM(CASE WHEN l.status <> 'QUARANTINED' THEN l.quantity_received END), 0)"
                 " AS usable_supply FROM material m LEFT JOIN inventory_lot l"
                 " ON l.material_id = m.material_id GROUP BY m.material_id ORDER BY m.material_id",
    "Inventory lots": "SELECT lot_id, material_id, quantity_received, available_from, status"
                      " FROM inventory_lot ORDER BY available_from, lot_id",
}

for tab, (name, sql) in zip(st.tabs(list(QUERIES)), QUERIES.items()):
    with tab:
        data = common.frame(sql)
        st.caption(f"{len(data)} rows")
        st.dataframe(data, width="stretch", hide_index=True)