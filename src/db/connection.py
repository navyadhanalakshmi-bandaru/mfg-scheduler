"""Connection helper for the main database (never the ground-truth one)."""
import sqlite3

from config.settings import DB_PATH


def connect(path=DB_PATH) -> sqlite3.Connection:
    """Open the main database with foreign keys on.

    check_same_thread=False because FastAPI may use a connection from a worker
    thread other than the one that opened it (one connection per request).
    """
    con = sqlite3.connect(str(path), check_same_thread=False)
    con.execute("PRAGMA foreign_keys = ON")
    return con


def query(con: sqlite3.Connection, sql: str, params: tuple = ()) -> list[dict]:
    cur = con.execute(sql, params)
    names = [d[0] for d in cur.description]
    return [dict(zip(names, row)) for row in cur.fetchall()]