"""Sanity tests for the Phase 3 environment."""
import importlib
import sqlite3
import sys

import pytest


def test_python_version():
    assert sys.version_info >= (3, 11)


@pytest.mark.parametrize(
    "module",
    ["pandas", "numpy", "sqlalchemy", "fastapi", "uvicorn",
     "streamlit", "plotly", "pydantic", "httpx"],
)
def test_package_imports(module):
    importlib.import_module(module)


def test_seed_constant():
    from config.settings import SEED
    assert SEED == 20261022


def test_full_volumes_match_challenge():
    from config.settings import VOLUMES
    assert VOLUMES["full"] == {
        "skus": 100, "machines": 40, "orders": 500, "operations": 2000, "materials": 200,
    }


def _table_names(con):
    rows = con.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
    )
    return {r[0] for r in rows}


def test_main_schema_loads_with_27_tables():
    from config.settings import SCHEMA_SQL
    con = sqlite3.connect(":memory:")
    con.executescript(SCHEMA_SQL.read_text(encoding="utf-8"))
    assert len(_table_names(con)) == 27
    assert "anomaly_ground_truth" not in _table_names(con)


def test_ground_truth_schema_is_separate():
    from config.settings import GROUND_TRUTH_SCHEMA_SQL
    con = sqlite3.connect(":memory:")
    con.executescript(GROUND_TRUTH_SCHEMA_SQL.read_text(encoding="utf-8"))
    assert _table_names(con) == {"anomaly_ground_truth"}