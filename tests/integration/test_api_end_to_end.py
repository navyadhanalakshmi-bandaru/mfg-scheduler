"""Phase 8: whole workflow through the HTTP API only (starter profile)."""
import pytest
from fastapi.testclient import TestClient

from config.settings import GROUND_TRUTH_DB_PATH
from src.api.app import create_app
from src.services.bootstrap import build_database

RULES = ("EDD", "PRIORITY_EDD", "CRITICAL_RATIO")


def _gt_state():
    if GROUND_TRUTH_DB_PATH.exists():
        return True, GROUND_TRUTH_DB_PATH.stat().st_mtime_ns
    return False, None


@pytest.fixture(scope="module")
def flow(tmp_path_factory):
    gt_before = _gt_state()
    folder = tmp_path_factory.mktemp("e2e")
    path = folder / "app.db"
    base = build_database(path, "starter")["run_id"]
    with TestClient(create_app(path)) as client:
        baselines = {}
        for rule in RULES:
            resp = client.post("/runs/baseline", json={"rule": rule})
            assert resp.status_code == 201, resp.text
            baselines[rule] = resp.json()["run_id"]
        machine = client.get(f"/runs/{base}/schedule",
                             params={"limit": 1}).json()[0]["machine_id"]
        resp = client.post(f"/runs/{base}/reschedule", json={
            "trigger_time": "2026-11-02 00:00:00", "why": "e2e breakdown",
            "downtimes": [{"machine_id": machine, "down_start": "2026-11-03 06:00:00",
                           "down_end": "2026-11-03 18:00:00"}]})
        assert resp.status_code == 201, resp.text
        child = resp.json()["run_id"]
        yield {"client": client, "base": base, "baselines": baselines,
               "child": child, "machine": machine, "folder": folder,
               "gt_before": gt_before}


def _all_run_ids(flow):
    return [r["run_id"] for r in flow["client"].get("/runs").json()]


def test_all_runs_exist_and_are_feasible(flow):
    client = flow["client"]
    ids = _all_run_ids(flow)
    expected = {flow["base"], flow["child"], *flow["baselines"].values()}
    assert expected <= set(ids)
    assert len(set(ids)) == len(ids)
    for run_id in ids:
        detail = client.get(f"/runs/{run_id}").json()
        assert detail["status"] == "COMPLETED", run_id
        assert detail["feasibility_label"] == "FEASIBLE", run_id
        assert detail["scheduled_operations"] > 0, run_id
        hard = client.get(f"/runs/{run_id}/exceptions",
                          params={"category": "CONSTRAINT_VIOLATION"}).json()
        assert hard == [], run_id


def test_parent_links(flow):
    client = flow["client"]
    runs = {r["run_id"]: r for r in client.get("/runs").json()}
    for run_id, row in runs.items():
        if row["run_type"] == "BASELINE":
            assert row["parent_run_id"] is None
        else:
            assert row["parent_run_id"] in runs, run_id
    assert runs[flow["child"]]["run_type"] == "RESCHEDULE"
    assert runs[flow["child"]]["parent_run_id"] == flow["base"]


def test_each_rule_is_recorded(flow):
    client = flow["client"]
    for rule, run_id in flow["baselines"].items():
        assert client.get(f"/runs/{run_id}").json()["scheduling_rule"] == rule


def test_breakdown_machine_is_idle_during_downtime(flow):
    client = flow["client"]
    rows = []
    offset = 0
    while True:
        page = client.get(f"/runs/{flow['child']}/schedule",
                          params={"machine_id": flow["machine"], "limit": 1000,
                                  "offset": offset}).json()
        rows += page
        if len(page) < 1000:
            break
        offset += 1000
    down_start, down_end = "2026-11-03 06:00:00", "2026-11-03 18:00:00"
    clashes = [r["schedule_id"] for r in rows
               if r["setup_start"] < down_end and r["end_time"] > down_start]
    assert clashes == []


def test_compare_is_consistent_in_both_directions(flow):
    client = flow["client"]
    a, b = flow["base"], flow["child"]
    forward = client.get("/runs/compare", params={"a": a, "b": b})
    assert forward.status_code == 200
    body = forward.json()
    assert body["base_run_id"] == a and body["other_run_id"] == b
    for name, m in body["metrics"].items():
        if isinstance(m["base"], (int, float)) and isinstance(m["other"], (int, float)):
            assert m["delta"] == pytest.approx(m["other"] - m["base"]), name
    same = client.get("/runs/compare", params={"a": a, "b": a}).json()
    for name, m in same["metrics"].items():
        if isinstance(m["delta"], (int, float)):
            assert m["delta"] == 0, name


def test_metrics_match_run_detail(flow):
    client = flow["client"]
    for run_id in (flow["base"], flow["child"]):
        metrics = client.get(f"/runs/{run_id}/metrics").json()
        detail = client.get(f"/runs/{run_id}").json()
        assert metrics["run_id"] == run_id
        assert metrics["operations_scheduled"] == detail["scheduled_operations"]
        assert metrics["orders_scheduled"] + metrics["orders_unscheduled"] == metrics["orders_total"]


def test_validate_route_is_stable_for_every_run(flow):
    client = flow["client"]
    for run_id in _all_run_ids(flow):
        first = client.post(f"/runs/{run_id}/validate").json()
        second = client.post(f"/runs/{run_id}/validate").json()
        assert first == second
        assert first["violation_count"] == 0


def test_unscheduled_orders_have_an_explanation(flow):
    """Every order the engine could not place should have an INFEASIBLE_ORDER row."""
    client = flow["client"]
    for run_id in (flow["base"], flow["child"], *flow["baselines"].values()):
        unscheduled = client.get(f"/runs/{run_id}/metrics").json()["orders_unscheduled"]
        rows = client.get(f"/runs/{run_id}/exceptions",
                          params={"category": "INFEASIBLE_ORDER"}).json()
        explained = {r["order_id"] for r in rows if r["order_id"]}
        assert len(explained) == unscheduled, (
            f"{run_id}: {unscheduled} unscheduled orders but {len(explained)} explained")


def test_reschedule_chain_keeps_lineage(flow):
    client = flow["client"]
    resp = client.post(f"/runs/{flow['child']}/reschedule", json={
        "trigger_time": "2026-11-09 00:00:00", "why": "e2e second breakdown",
        "downtimes": [{"machine_id": flow["machine"], "down_start": "2026-11-10 06:00:00",
                       "down_end": "2026-11-10 14:00:00"}]})
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["parent_run_id"] == flow["child"]
    assert body["feasibility_label"] == "FEASIBLE" and body["violation_count"] == 0


def test_api_never_touches_the_ground_truth_database(flow):
    assert _gt_state() == flow["gt_before"]
    assert [p.name for p in flow["folder"].iterdir()] == ["app.db"]