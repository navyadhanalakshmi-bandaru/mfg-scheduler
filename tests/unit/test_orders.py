"""Tests for the production order generator."""
import math
import sqlite3
from datetime import datetime

import pytest

from config.settings import SCHEMA_SQL, VOLUMES
from src.db.loader import load_tables
from src.generator import ids
from src.generator.calendars import (
    CALENDAR_LOAD_ORDER, HORIZON_START_DT, TS, generate_calendar_data,
)
from src.generator.master_data import (
    MACHINE_GROUPS, MASTER_LOAD_ORDER, generate_master_data,
)
from src.generator.orders import (
    DUE_CAP, ORDER_LOAD_ORDER, PRIORITY_RANK, generate_order_data, load_by_group,
)


def _dt(text):
    return datetime.strptime(text, TS)


@pytest.fixture(scope="module", params=["starter", "full"])
def data(request):
    master = generate_master_data(request.param)
    calendar = generate_calendar_data(master)
    return request.param, master, calendar, generate_order_data(master, request.param)


def test_exact_volumes(data):
    profile, _, _, t = data
    assert len(t["production_order"]) == VOLUMES[profile]["orders"]
    assert len(t["operation"]) == VOLUMES[profile]["operations"]


def test_order_ids_are_sequential_and_valid(data):
    _, _, _, t = data
    got = [o["order_id"] for o in t["production_order"]]
    assert got == [ids.order_id(i) for i in range(1, len(got) + 1)]
    assert all(ids.is_valid("operation", op["operation_id"]) for op in t["operation"])


def test_orders_are_numbered_in_release_order(data):
    _, _, _, t = data
    releases = [o["release_time"] for o in t["production_order"]]
    assert releases == sorted(releases)


def test_order_references_match_master_data(data):
    _, master, _, t = data
    routing_of = {r["sku_id"]: r["routing_id"] for r in master["routing"]}
    lot_of = {p["sku_id"]: p["standard_lot_size"] for p in master["product"]}
    for o in t["production_order"]:
        assert o["routing_id"] == routing_of[o["sku_id"]]
        assert 1 <= o["quantity"] <= lot_of[o["sku_id"]]


def test_priorities(data):
    profile, _, _, t = data
    counts = {}
    for o in t["production_order"]:
        assert o["priority_rank"] == PRIORITY_RANK[o["priority_class"]]
        counts[o["priority_class"]] = counts.get(o["priority_class"], 0) + 1
    assert counts["URGENT"] >= 1
    assert set(counts) == set(PRIORITY_RANK)
    if profile == "full":
        assert counts == {"URGENT": 25, "HIGH": 75, "NORMAL": 300, "LOW": 100}


def test_release_and_due_dates(data):
    _, _, _, t = data
    for o in t["production_order"]:
        release, due = _dt(o["release_time"]), _dt(o["due_time"])
        assert due > release
        assert due <= DUE_CAP
        assert release.weekday() < 5
        assert 0 <= (release - HORIZON_START_DT).days < 35
        assert 6 <= release.hour <= 13


def test_operations_follow_the_routing(data):
    _, master, _, t = data
    steps = {s["step_id"]: s for s in master["routing_step"]}
    by_order = {}
    for op in t["operation"]:
        by_order.setdefault(op["order_id"], []).append(op)
    assert set(by_order) == {o["order_id"] for o in t["production_order"]}
    qty = {o["order_id"]: o["quantity"] for o in t["production_order"]}
    routing = {o["order_id"]: o["routing_id"] for o in t["production_order"]}
    for oid, ops in by_order.items():
        ops.sort(key=lambda r: r["sequence_no"])
        assert [op["sequence_no"] for op in ops] == list(range(1, len(ops) + 1))
        for op in ops:
            step = steps[op["step_id"]]
            assert step["routing_id"] == routing[oid]
            assert step["sequence_no"] == op["sequence_no"]
            assert op["operation_type"] == step["operation_type"]
            assert op["required_machine_group"] == step["required_machine_group"]
            assert op["quantity"] == qty[oid]
            assert op["status"] == "PENDING"


def test_base_minutes_are_derived_from_the_routing(data):
    _, master, _, t = data
    steps = {s["step_id"]: s for s in master["routing_step"]}
    for op in t["operation"]:
        step = steps[op["step_id"]]
        expected = math.ceil(round(step["fixed_minutes"] + op["quantity"] * step["std_minutes_per_unit"], 6))
        assert op["base_minutes"] == expected
        assert 1 <= op["base_minutes"] <= 301


def test_material_requirements_come_from_the_bom(data):
    _, master, _, t = data
    bom = {(b["sku_id"], b["material_id"]): b for b in master["product_bom"]}
    order = {o["order_id"]: o for o in t["production_order"]}
    expected_count = sum(
        sum(1 for b in master["product_bom"] if b["sku_id"] == o["sku_id"])
        for o in t["production_order"])
    assert len(t["operation_material_req"]) == expected_count
    for r in t["operation_material_req"]:
        oid, seq = ids.parse_operation_id(r["operation_id"])
        b = bom[(order[oid]["sku_id"], r["material_id"])]
        assert seq == b["consumed_at_sequence"]
        assert r["required_qty"] == pytest.approx(b["qty_per_unit"] * order[oid]["quantity"], abs=0.001)
        assert r["req_id"] == f"REQ-{r['operation_id']}-{r['material_id']}"


def test_status_history_chain(data):
    _, _, _, t = data
    rows = t["order_status_history"]
    assert len(rows) == 2 * len(t["production_order"])
    assert [r["history_id"] for r in rows] == [ids.order_history_id(i) for i in range(1, len(rows) + 1)]
    by_order = {}
    for r in rows:
        by_order.setdefault(r["order_id"], []).append(r)
    status = {o["order_id"]: o["status"] for o in t["production_order"]}
    for oid, chain in by_order.items():
        assert chain[0]["from_status"] is None
        assert chain[1]["from_status"] == chain[0]["to_status"]
        assert chain[0]["changed_at"] <= chain[1]["changed_at"]
        assert chain[-1]["to_status"] == status[oid]


def test_load_report_covers_every_group(data):
    _, master, calendar, t = data
    report = load_by_group(master, calendar, t)
    assert set(report) == set(MACHINE_GROUPS)
    assert sum(g["planned_minutes"] for g in report.values()) == sum(
        op["base_minutes"] for op in t["operation"])
    assert all(g["available_minutes"] > 0 for g in report.values())


def test_everything_loads_into_real_schema(data):
    _, master, calendar, t = data
    con = sqlite3.connect(":memory:")
    con.executescript(SCHEMA_SQL.read_text(encoding="utf-8"))
    con.execute("PRAGMA foreign_keys = ON")
    everything = {**master, **calendar, **t}
    loaded = load_tables(con, everything, MASTER_LOAD_ORDER + CALENDAR_LOAD_ORDER + ORDER_LOAD_ORDER)
    con.commit()
    assert loaded == {name: len(everything[name]) for name in loaded}
    assert con.execute("PRAGMA foreign_key_check").fetchall() == []
    con.close()


def test_generation_is_deterministic():
    master = generate_master_data("starter")
    assert generate_order_data(master, "starter") == generate_order_data(master, "starter")


def test_different_seed_changes_orders():
    master = generate_master_data("starter")
    assert generate_order_data(master, "starter", seed=1) != generate_order_data(master, "starter")


def test_unknown_profile_rejected():
    with pytest.raises(ValueError):
        generate_order_data(generate_master_data("starter"), "huge")