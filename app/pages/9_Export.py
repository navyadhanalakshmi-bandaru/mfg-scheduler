"""Download tables from the current database as CSV."""
import common
import streamlit as st

st.set_page_config(page_title="Export", layout="wide")
st.title("Export")

tables = common.frame("SELECT name FROM sqlite_master WHERE type = 'table'"
                      " AND name NOT LIKE 'sqlite_%' ORDER BY name").name.tolist()
table = st.selectbox("Table", tables)
data = common.frame(f'SELECT * FROM "{table}"')  # name comes from sqlite_master, not user text
st.caption(f"{len(data)} rows")
st.dataframe(data.head(200), width="stretch", hide_index=True)
st.download_button(f"Download {table}.csv", data.to_csv(index=False).encode("utf-8"),
                   file_name=f"{table}.csv", mime="text/csv")