"""Phase 9: the full profile (500 orders) through the HTTP API."""
import pytest
from fastapi.testclient import TestClient

from src.api.app import create_app
from src.services.bootstrap import build_database


@pytest.fixture(scope="module")
def full(tmp_path_factory):
    path = tmp_path_factory.mktemp("full") / "app.db"
    base = build_database(path, "full")["run_id"]
    with TestClient(create_app(path)) as client:
        yield client, base


def test_full_baseline_is_feasible_and_accounted_for(full):
    client, base = full
    detail = client.get(f"/runs/{base}").json()
    assert detail["feasibility_label"] == "FEASIBLE"
    metrics = client.get(f"/runs/{base}/metrics").json()
    assert metrics["orders_total"] == 500
    assert metrics["orders_scheduled"] + metrics["orders_unscheduled"] == 500
    assert metrics["operations_scheduled"] == detail["scheduled_operations"]
    hard = client.get(f"/runs/{base}/exceptions",
                      params={"category": "CONSTRAINT_VIOLATION"}).json()
    assert hard == []


def test_full_unscheduled_orders_are_explained(full):
    client, base = full
    unscheduled = client.get(f"/runs/{base}/metrics").json()["orders_unscheduled"]
    rows = client.get(f"/runs/{base}/exceptions",
                      params={"category": "INFEASIBLE_ORDER"}).json()
    explained = {r["order_id"] for r in rows if r["order_id"]}
    assert len(explained) == unscheduled


def test_full_reschedule_with_breakdown(full):
    client, base = full
    machine = client.get(f"/runs/{base}/schedule", params={"limit": 1}).json()[0]["machine_id"]
    resp = client.post(f"/runs/{base}/reschedule", json={
        "trigger_time": "2026-11-02 00:00:00", "why": "full-profile breakdown",
        "downtimes": [{"machine_id": machine, "down_start": "2026-11-03 06:00:00",
                       "down_end": "2026-11-03 18:00:00"}]})
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["parent_run_id"] == base
    assert body["feasibility_label"] == "FEASIBLE" and body["violation_count"] == 0
    cmp = client.get("/runs/compare", params={"a": base, "b": body["run_id"]})
    assert cmp.status_code == 200