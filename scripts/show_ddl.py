"""Print the full CREATE TABLE statements (with CHECK constraints) for chosen tables."""
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from config.settings import SCHEMA_SQL

TABLES = [
    "shift_pattern", "machine", "product", "routing", "routing_step",
    "routing_step_machine", "material", "product_bom", "changeover_matrix",
    "tool", "routing_step_tool", "labor_pool", "labor_shift_capacity",
]

con = sqlite3.connect(":memory:")
con.executescript(SCHEMA_SQL.read_text(encoding="utf-8"))
for name in TABLES:
    row = con.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name=?", (name,)
    ).fetchone()
    print(row[0] if row else f"-- table {name} not found")
    print()