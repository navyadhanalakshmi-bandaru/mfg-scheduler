"""Small helpers for loading generated rows into SQLite (reused in Phase 6)."""
import sqlite3


def insert_rows(con: sqlite3.Connection, table: str, rows: list[dict]) -> int:
    """Insert a list of dict rows into one table. Returns the number inserted."""
    if not rows:
        return 0
    columns = list(rows[0].keys())
    placeholders = ", ".join("?" for _ in columns)
    sql = f"INSERT INTO {table} ({', '.join(columns)}) VALUES ({placeholders})"
    con.executemany(sql, [tuple(row[c] for c in columns) for row in rows])
    return len(rows)


def load_tables(con: sqlite3.Connection, tables: dict, order: list[str]) -> dict:
    """Insert several tables in the given (parent-first) order."""
    return {name: insert_rows(con, name, tables[name]) for name in order}