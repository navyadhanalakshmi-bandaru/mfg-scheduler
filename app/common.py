"""Shared helpers for the Streamlit pages. Reads only the main database."""
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import pandas as pd  # noqa: E402
import streamlit as st  # noqa: E402

from config.settings import DB_PATH  # noqa: E402
from src.db.connection import connect  # noqa: E402


def db_path() -> Path:
    return Path(os.environ.get("MFG_DB", DB_PATH))


def require_db() -> Path:
    path = db_path()
    if not path.exists():
        st.error(f"{path} not found. Run: python -m src.services.bootstrap")
        st.stop()
    return path


def frame(sql: str, params: tuple = ()) -> pd.DataFrame:
    con = connect(require_db())
    try:
        return pd.read_sql_query(sql, con, params=params)
    finally:
        con.close()


def pick_run() -> str:
    runs = frame("SELECT run_id, run_type, scheduling_rule, feasibility_label"
                 " FROM schedule_run ORDER BY run_id")
    if runs.empty:
        st.warning("No runs yet. Run: python -m src.services.bootstrap")
        st.stop()
    info = {r.run_id: f"{r.run_id[-6:]} {r.run_type} {r.scheduling_rule} {r.feasibility_label}"
            for r in runs.itertuples()}
    return st.sidebar.selectbox("Run", list(info), format_func=info.get, key="run_pick")