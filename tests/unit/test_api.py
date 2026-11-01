"""Tests for the API (Phase 6 and Phase 7a)."""
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from src.api.app import create_app
from src.services.bootstrap import build_database


@pytest.fixture(scope="module")
def api(tmp_path_factory):
    path = tmp_path_factory.mktemp("db") / "app.db"
    summary = build_database(path, "starter")
    with TestClient(create_app(path)) as client:
        yield client, summary["run_id"]


def _first_machine(client, run_id):
    return client.get(f"/runs/{run_id}/schedule", params={"limit": 1}).json()[0]["machine_id"]


def test_health(api):
    client, _ = api
    body = client.get("/health").json()
    assert body["status"] == "ok" and body["runs"] >= 1


def test_runs_list_and_detail(api):
    client, base = api
    runs = client.get("/runs").json()
    assert base in {r["run_id"] for r in runs}
    detail = client.get(f"/runs/{base}").json()
    assert detail["run_type"] == "BASELINE"
    assert detail["scheduled_operations"] > 0
    assert isinstance(detail["config"], dict)
    assert "config_json" not in detail


def test_unknown_and_malformed_run_ids(api):
    client, _ = api
    assert client.get("/runs/RUN-2026-999999").status_code == 404
    assert client.get("/runs/not-a-run").status_code == 422
    assert client.get("/runs/RUN-2026-999999/schedule").status_code == 404


def test_schedule_paging_and_filters(api):
    client, base = api
    page = client.get(f"/runs/{base}/schedule", params={"limit": 5}).json()
    assert len(page) == 5
    nxt = client.get(f"/runs/{base}/schedule", params={"limit": 5, "offset": 5}).json()
    assert {r["schedule_id"] for r in page}.isdisjoint({r["schedule_id"] for r in nxt})
    machine = page[0]["machine_id"]
    only = client.get(f"/runs/{base}/schedule", params={"machine_id": machine}).json()
    assert only and all(r["machine_id"] == machine for r in only)
    assert client.get(f"/runs/{base}/schedule", params={"limit": 0}).status_code == 422


def test_orders(api):
    client, _ = api
    orders = client.get("/orders", params={"limit": 10}).json()
    assert 0 < len(orders) <= 10
    status = orders[0]["status"]
    filtered = client.get("/orders", params={"status": status}).json()
    assert filtered and all(o["status"] == status for o in filtered)


def test_validate_returns_stored_result_for_validated_run(api):
    client, base = api
    n_before = len(client.get(f"/runs/{base}/exceptions").json())
    first = client.post(f"/runs/{base}/validate")
    second = client.post(f"/runs/{base}/validate")
    n_after = len(client.get(f"/runs/{base}/exceptions").json())
    assert first.status_code == second.status_code == 200
    assert first.json() == second.json()
    assert n_before == n_after, "an already validated run must not get new exception rows"
    assert first.json()["violation_count"] == 0
    assert first.json()["feasibility_label"] == "FEASIBLE"


def test_start_baseline_creates_new_run(api):
    client, base = api
    resp = client.post("/runs/baseline", json={"rule": "EDD"})
    assert resp.status_code == 201
    new_id = resp.json()["run_id"]
    assert new_id != base
    assert client.get(f"/runs/{new_id}").json()["scheduling_rule"] == "EDD"
    assert client.post("/runs/baseline", json={"rule": "BOGUS"}).status_code == 422


def test_api_and_services_never_reference_the_ground_truth_db():
    src = Path(__file__).resolve().parents[2] / "src"
    for folder in ("api", "services", "db"):
        for path in (src / folder).glob("*.py"):
            text = path.read_text(encoding="utf-8")
            assert "anomaly_ground_truth" not in text, path
            assert "GROUND_TRUTH" not in text, path


def test_metrics_route(api):
    client, base = api
    body = client.get(f"/runs/{base}/metrics").json()
    assert isinstance(body, dict)
    assert "makespan_minutes" in body and "late_orders" in body
    assert client.get("/runs/RUN-2026-999999/metrics").status_code == 404
    assert client.get("/runs/not-a-run/metrics").status_code == 422


def test_reschedule_with_breakdown_creates_validated_child_run(api):
    client, base = api
    machine = _first_machine(client, base)
    resp = client.post(f"/runs/{base}/reschedule", json={
        "trigger_time": "2026-11-02 00:00:00",
        "why": "API test breakdown",
        "downtimes": [{"machine_id": machine,
                       "down_start": "2026-11-03 06:00:00",
                       "down_end": "2026-11-03 18:00:00"}]})
    assert resp.status_code == 201, resp.text
    body = resp.json()
    new_id = body["run_id"]
    assert new_id != base and body["parent_run_id"] == base
    assert body["violation_count"] == 0
    assert body["feasibility_label"] == "FEASIBLE"
    detail = client.get(f"/runs/{new_id}").json()
    assert detail["run_type"] == "RESCHEDULE" and detail["parent_run_id"] == base
    assert client.get(f"/runs/{new_id}/schedule", params={"limit": 1}).json()


def test_reschedule_rejects_bad_input(api):
    client, base = api
    machine = _first_machine(client, base)
    assert client.post("/runs/RUN-2026-999999/reschedule", json={
        "trigger_time": "2026-11-02 00:00:00", "why": "x"}).status_code == 404
    assert client.post(f"/runs/{base}/reschedule", json={
        "trigger_time": "tomorrow", "why": "x"}).status_code == 422
    assert client.post(f"/runs/{base}/reschedule", json={
        "trigger_time": "2026-11-02 00:00:00", "why": ""}).status_code == 422
    # a downtime that starts before the trigger time is refused by the engine
    early = client.post(f"/runs/{base}/reschedule", json={
        "trigger_time": "2026-11-05 00:00:00", "why": "x",
        "downtimes": [{"machine_id": machine, "down_start": "2026-11-03 06:00:00",
                       "down_end": "2026-11-03 18:00:00"}]})
    assert early.status_code == 400, early.text


def test_compare_route(api):
    client, base = api
    other = client.post("/runs/baseline", json={"rule": "PRIORITY_EDD"}).json()["run_id"]
    body = client.get("/runs/compare", params={"a": base, "b": other})
    assert body.status_code == 200 and isinstance(body.json(), dict) and body.json()
    assert client.get("/runs/compare", params={"a": base, "b": base}).status_code == 200
    assert client.get("/runs/compare", params={"a": base, "b": "RUN-2026-999999"}).status_code == 404
    assert client.get("/runs/compare", params={"a": base, "b": "nope"}).status_code == 422
    assert client.get("/runs/compare", params={"a": base}).status_code == 422


def test_ui_page_is_served(api):
    client, _ = api
    resp = client.get("/")
    assert resp.status_code == 200
    assert "text/html" in resp.headers["content-type"]
    assert "Manufacturing Scheduler" in resp.text