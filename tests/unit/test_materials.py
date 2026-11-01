"""Tests for the inventory lot generator."""
import sqlite3

import pytest

from config.settings import SCHEMA_SQL
from src.db.loader import load_tables
from src.generator import ids
from src.generator.calendars import (
    CALENDAR_LOAD_ORDER, HORIZON_START_DT, generate_calendar_data,
)
from src.generator.master_data import MASTER_LOAD_ORDER, generate_master_data
from src.generator.materials import (
    ARRIVAL_CAP, INVENTORY_LOAD_ORDER, SHARE_LATE, SHARE_QUARANTINED, SHARE_SHORT,
    SNAPSHOT_DT, _fmt, generate_inventory_data, material_coverage,
)
from src.generator.orders import ORDER_LOAD_ORDER, generate_order_data


@pytest.fixture(scope="module", params=["starter", "full"])
def data(request):
    master = generate_master_data(request.param)
    calendar = generate_calendar_data(master)
    orders = generate_order_data(master, request.param)
    inventory = generate_inventory_data(master, orders)
    return master, calendar, orders, inventory


def test_every_material_has_a_lot(data):
    master, _, _, inv = data
    assert {l["material_id"] for l in inv["inventory_lot"]} == {m["material_id"] for m in master["material"]}
    assert all(l["quantity_received"] > 0 for l in inv["inventory_lot"])


def test_lot_ids_are_sequential_and_sorted_by_arrival(data):
    _, _, _, inv = data
    lots = inv["inventory_lot"]
    assert [l["lot_id"] for l in lots] == [ids.lot_id(i) for i in range(1, len(lots) + 1)]
    assert [l["available_from"] for l in lots] == sorted(l["available_from"] for l in lots)


def test_status_follows_arrival_time(data):
    _, _, _, inv = data
    snapshot = _fmt(SNAPSHOT_DT)
    for lot in inv["inventory_lot"]:
        if lot["status"] == "QUARANTINED":
            assert lot["available_from"] <= snapshot
        elif lot["status"] == "AVAILABLE":
            assert lot["available_from"] <= snapshot
        else:
            assert lot["status"] == "EXPECTED"
            assert lot["available_from"] > snapshot


def test_arrivals_stay_inside_the_planning_window(data):
    _, _, _, inv = data
    assert all(l["available_from"] <= _fmt(ARRIVAL_CAP) for l in inv["inventory_lot"])


def test_supply_versus_demand(data):
    _, _, orders, inv = data
    coverage = material_coverage(orders, inv)
    needed = len(coverage)
    expected_short = max(1, round(SHARE_SHORT * needed))
    short = [m for m, c in coverage.items() if c["usable_supply"] < c["demand"]]
    assert len(short) == expected_short
    for mid, c in coverage.items():
        if mid not in short:
            assert c["usable_supply"] >= c["demand"]


def test_late_materials_arrive_after_the_horizon_starts(data):
    _, _, orders, inv = data
    coverage = material_coverage(orders, inv)
    late = [m for m, c in coverage.items() if c["first_available"] >= _fmt(HORIZON_START_DT)]
    assert len(late) == round(SHARE_LATE * len(coverage))


def test_quarantined_lots_exist_and_are_not_counted(data):
    _, _, orders, inv = data
    coverage = material_coverage(orders, inv)
    quarantined = [l for l in inv["inventory_lot"] if l["status"] == "QUARANTINED"]
    assert len(quarantined) == max(1, round(SHARE_QUARANTINED * len(coverage)))
    by_material = {}
    for l in inv["inventory_lot"]:
        if l["status"] != "QUARANTINED":
            by_material[l["material_id"]] = by_material.get(l["material_id"], 0) + l["quantity_received"]
    for mid, c in coverage.items():
        assert c["usable_supply"] == pytest.approx(by_material[mid], abs=0.001)


def test_receipts_reconcile_with_lots(data):
    _, _, _, inv = data
    lots = {l["lot_id"]: l for l in inv["inventory_lot"]}
    txns = inv["inventory_transaction"]
    assert [t["txn_id"] for t in txns] == [ids.txn_id(i) for i in range(1, len(txns) + 1)]
    received = {l["lot_id"] for l in lots.values() if l["status"] in ("AVAILABLE", "QUARANTINED")}
    assert {t["lot_id"] for t in txns} == received
    assert len(txns) == len(received)
    for t in txns:
        lot = lots[t["lot_id"]]
        assert t["txn_type"] == "RECEIPT"
        assert t["quantity"] == lot["quantity_received"]
        assert t["txn_time"] == lot["available_from"]
        assert t["operation_id"] is None and t["run_id"] is None


def test_everything_loads_into_real_schema(data):
    master, calendar, orders, inv = data
    con = sqlite3.connect(":memory:")
    con.executescript(SCHEMA_SQL.read_text(encoding="utf-8"))
    con.execute("PRAGMA foreign_keys = ON")
    everything = {**master, **calendar, **orders, **inv}
    order = MASTER_LOAD_ORDER + CALENDAR_LOAD_ORDER + ORDER_LOAD_ORDER + INVENTORY_LOAD_ORDER
    loaded = load_tables(con, everything, order)
    con.commit()
    assert loaded == {name: len(everything[name]) for name in loaded}
    assert con.execute("PRAGMA foreign_key_check").fetchall() == []
    con.close()


def test_generation_is_deterministic():
    master = generate_master_data("starter")
    orders = generate_order_data(master, "starter")
    assert generate_inventory_data(master, orders) == generate_inventory_data(master, orders)


def test_different_seed_changes_inventory():
    master = generate_master_data("starter")
    orders = generate_order_data(master, "starter")
    assert generate_inventory_data(master, orders, seed=1) != generate_inventory_data(master, orders)