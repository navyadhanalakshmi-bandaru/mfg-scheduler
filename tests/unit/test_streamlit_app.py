"""Smoke tests: each Streamlit page renders on real data without an exception."""
import sys
from pathlib import Path

import pytest
from streamlit.testing.v1 import AppTest

from src.services.bootstrap import build_database

APP = Path(__file__).resolve().parents[2] / "app"
sys.path.insert(0, str(APP))

PAGES = ["Home.py", "pages/2_Gantt.py"]


@pytest.fixture(scope="module")
def db(tmp_path_factory):
    path = tmp_path_factory.mktemp("st") / "app.db"
    build_database(path, "starter")
    return path


@pytest.mark.parametrize("page", PAGES)
def test_page_renders(page, db, monkeypatch):
    monkeypatch.setenv("MFG_DB", str(db))
    at = AppTest.from_file(str(APP / page), default_timeout=30).run()
    assert not at.exception, at.exception


def test_missing_database_shows_message(tmp_path, monkeypatch):
    monkeypatch.setenv("MFG_DB", str(tmp_path / "nope.db"))
    at = AppTest.from_file(str(APP / "Home.py"), default_timeout=30).run()
    assert at.error and "bootstrap" in at.error[0].value