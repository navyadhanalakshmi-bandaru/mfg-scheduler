"""Build data/app.db from scratch: schema, generated data, one validated baseline.

    python -m src.services.bootstrap            # full profile into data/app.db
    python -m src.services.bootstrap starter    # small profile
"""
import sqlite3
import sys
from pathlib import Path

from config.settings import DB_PATH, SCHEMA_SQL
from src.db.loader import load_tables
from src.generator.export import ALL_TABLES, build_dataset
from src.services.runs import create_baseline


def build_database(path=DB_PATH, profile: str = "full", rule: str = "CRITICAL_RATIO") -> dict:
    """Create a fresh database file (an existing one is replaced). Returns the baseline summary."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        path.unlink()
    con = sqlite3.connect(str(path))
    try:
        con.executescript(SCHEMA_SQL.read_text(encoding="utf-8"))
        load_tables(con, build_dataset(profile), ALL_TABLES)
        con.commit()
        con.execute("PRAGMA foreign_keys = ON")
        return create_baseline(con, rule)
    finally:
        con.close()


if __name__ == "__main__":
    chosen = sys.argv[1] if len(sys.argv) > 1 else "full"
    summary = build_database(DB_PATH, chosen)
    print(f"built {DB_PATH} ({chosen})")
    print(summary)