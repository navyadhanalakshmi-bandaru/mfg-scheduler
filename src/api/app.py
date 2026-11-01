"""FastAPI app over the main scheduling database.

Never opens the ground-truth database. Start with:
    uvicorn src.api.app:app --reload
Interactive docs: http://127.0.0.1:8000/docs
"""
import json
import sqlite3
from pathlib import Path
from typing import Annotated, Literal

from fastapi import Depends, FastAPI, HTTPException, Query, Request
from fastapi import Path as PathParam
from pydantic import BaseModel

from config.settings import DB_PATH
from src.db.connection import connect, query
from src.services import runs as run_service

RunId = Annotated[str, PathParam(pattern=r"^RUN-2026-\d{6}$")]


class BaselineRequest(BaseModel):
    rule: Literal["EDD", "PRIORITY_EDD", "CRITICAL_RATIO"] = "CRITICAL_RATIO"


def get_con(request: Request):
    """One connection per request, opened on the app's database file."""
    con = connect(request.app.state.db_path)
    try:
        yield con
    finally:
        con.close()


Con = Annotated[sqlite3.Connection, Depends(get_con)]


def run_row(con: sqlite3.Connection, run_id: str) -> dict:
    rows = query(con, "SELECT * FROM schedule_run WHERE run_id = ?", (run_id,))
    if not rows:
        raise HTTPException(404, f"run {run_id} not found")
    return rows[0]


def create_app(db_path=DB_PATH) -> FastAPI:
    db_path = Path(db_path)
    if not db_path.exists():
        raise RuntimeError(f"{db_path} not found; run: python -m src.services.bootstrap")
    app = FastAPI(title="Manufacturing Scheduler API", version="0.1")
    app.state.db_path = db_path

    @app.get("/health")
    def health(con: Con):
        n = con.execute("SELECT COUNT(*) FROM schedule_run").fetchone()[0]
        return {"status": "ok", "runs": n}

    @app.get("/runs")
    def list_runs(con: Con):
        return query(con, "SELECT run_id, run_type, parent_run_id, scheduling_rule, status,"
                          " feasibility_label, started_at, finished_at"
                          " FROM schedule_run ORDER BY run_id")

    @app.get("/runs/{run_id}")
    def get_run(run_id: RunId, con: Con):
        row = run_row(con, run_id)
        row["config"] = json.loads(row.pop("config_json"))
        metrics = row.pop("metrics_json")
        row["metrics"] = json.loads(metrics) if metrics else None
        row["scheduled_operations"] = con.execute(
            "SELECT COUNT(*) FROM operation_schedule WHERE run_id = ?", (run_id,)).fetchone()[0]
        row["exceptions"] = con.execute(
            "SELECT COUNT(*) FROM scheduling_exception WHERE run_id = ?", (run_id,)).fetchone()[0]
        return row

    @app.get("/runs/{run_id}/schedule")
    def get_schedule(run_id: RunId, con: Con, machine_id: str | None = None,
                     order_id: str | None = None,
                     limit: int = Query(200, ge=1, le=1000), offset: int = Query(0, ge=0)):
        run_row(con, run_id)
        sql = ("SELECT s.schedule_id, s.operation_id, o.order_id, o.sequence_no, s.machine_id,"
               " s.setup_start, s.setup_minutes, s.start_time, s.processing_minutes, s.end_time,"
               " s.status FROM operation_schedule s JOIN operation o"
               " ON o.operation_id = s.operation_id WHERE s.run_id = ?")
        params: list = [run_id]
        if machine_id:
            sql += " AND s.machine_id = ?"
            params.append(machine_id)
        if order_id:
            sql += " AND o.order_id = ?"
            params.append(order_id)
        sql += " ORDER BY s.start_time, s.schedule_id LIMIT ? OFFSET ?"
        return query(con, sql, (*params, limit, offset))

    @app.get("/runs/{run_id}/exceptions")
    def get_exceptions(run_id: RunId, con: Con, category: str | None = None):
        run_row(con, run_id)
        sql = "SELECT * FROM scheduling_exception WHERE run_id = ?"
        params: list = [run_id]
        if category:
            sql += " AND category = ?"
            params.append(category)
        return query(con, sql + " ORDER BY exception_id", tuple(params))

    @app.get("/orders")
    def list_orders(con: Con, status: str | None = None,
                    limit: int = Query(200, ge=1, le=1000), offset: int = Query(0, ge=0)):
        sql = ("SELECT order_id, sku_id, quantity, priority_class, release_time, due_time, status"
               " FROM production_order")
        params: list = []
        if status:
            sql += " WHERE status = ?"
            params.append(status)
        return query(con, sql + " ORDER BY order_id LIMIT ? OFFSET ?", (*params, limit, offset))

    @app.post("/runs/baseline", status_code=201)
    def start_baseline(body: BaselineRequest, con: Con):
        return run_service.create_baseline(con, body.rule)

    @app.post("/runs/{run_id}/validate")
    def validate_run_route(run_id: RunId, con: Con):
        run_row(con, run_id)
        return run_service.validate(con, run_id)

    return app


app = create_app() if DB_PATH.exists() else None