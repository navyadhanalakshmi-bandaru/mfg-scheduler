"""Tests for the scheduling model and static feasibility."""
import copy
import sqlite3

import pytest

from config.settings import SCHEMA_SQL, VOLUMES
from src.db.loader import load_tables
from src.engine.model import EPS, find_issues, load_model
from src.generator.export import ALL_TABLES, build_dataset
from src.generator.materials import material_coverage


@pytest.fixture(scope="module", params=["starter", "full"])
def built(request):
    tables = build_dataset(request.param)
    con = sqlite3.connect(":memory:")
    con.executescript(SCHEMA_SQL.read_text(encoding="utf-8"))
    load_tables(con, tables, ALL_TABLES)
    model = load_model(con)
    con.close()
    return request.param, tables, model, find_issues(model)


def test_model_sizes(built):
    profile, tables, model, _ = built
    assert len(model.orders) == VOLUMES[profile]["orders"]
    assert sum(len(v) for v in model.ops_by_order.values()) == VOLUMES[profile]["operations"]
    assert all(len(v) >= 1 for v in model.ops_by_order.values())


def test_candidates_are_active_and_best_first(built):
    _, tables, model, _ = built
    active = {m["machine_id"] for m in tables["machine"] if m["status"] == "ACTIVE"}
    for cands in model.candidates.values():
        assert all(c.machine_id in active for c in cands)
        ranks = [c.preference_rank for c in cands]
        assert ranks == sorted(ranks)


def test_lots_exclude_quarantined(built):
    _, tables, model, _ = built
    quarantined = {l["lot_id"] for l in tables["inventory_lot"] if l["status"] == "QUARANTINED"}
    used = {lot.lot_id for lots in model.lots.values() for lot in lots}
    assert quarantined and not (quarantined & used)


def test_generated_data_has_no_structural_issues(built):
    _, _, _, result = built
    structural = {"NO_ELIGIBLE_MACHINE", "LABOR_UNAVAILABLE", "TOOL_UNAVAILABLE"}
    assert not [i for i in result.issues if i.code in structural]


def test_material_issues_only_for_short_materials(built):
    _, tables, _, result = built
    coverage = material_coverage(tables, tables)
    short = {m for m, c in coverage.items() if c["usable_supply"] < c["demand"]}
    material_issues = [i for i in result.issues if i.code == "MATERIAL_UNAVAILABLE"]
    assert material_issues
    assert all(i.material_id in short for i in material_issues)
    assert len({i.order_id for i in material_issues}) == len(material_issues)


def test_feasible_orders_never_overdraw_supply(built):
    _, _, model, result = built
    supply = {m: sum(l.quantity for l in lots) for m, lots in model.lots.items()}
    drawn = {}
    for oid, ops in model.ops_by_order.items():
        if oid in result.infeasible_orders:
            continue
        for op in ops:
            for material_id, qty in model.requirements.get(op.operation_id, []):
                drawn[material_id] = drawn.get(material_id, 0.0) + qty
    for material_id, qty in drawn.items():
        assert qty <= supply.get(material_id, 0.0) + EPS


def test_no_eligible_machine_is_detected(built):
    _, _, model, _ = built
    op = model.ops_by_order[next(iter(model.orders))][0]
    broken = copy.deepcopy(model)
    broken.candidates[op.step_id] = []
    result = find_issues(broken)
    affected = {o.order_id for ops in model.ops_by_order.values() for o in ops
                if o.step_id == op.step_id}
    assert affected <= result.infeasible_orders
    assert any(i.code == "NO_ELIGIBLE_MACHINE" and i.order_id == op.order_id
               for i in result.issues)


def test_missing_labor_is_detected(built):
    _, _, model, _ = built
    op = model.ops_by_order[next(iter(model.orders))][0]
    broken = copy.deepcopy(model)
    broken.labor_capacity = {k: (0 if k[0] == op.labor_skill else v)
                             for k, v in model.labor_capacity.items()}
    result = find_issues(broken)
    affected = {o.order_id for ops in model.ops_by_order.values() for o in ops
                if o.labor_skill == op.labor_skill}
    assert affected <= result.infeasible_orders
    assert any(i.code == "LABOR_UNAVAILABLE" for i in result.issues)


def test_retired_tool_is_detected(built):
    _, _, model, _ = built
    target = next(((op, tool_id) for ops in model.ops_by_order.values() for op in ops
                   for tool_id, _ in model.step_tools.get(op.step_id, [])), None)
    if target is None:
        pytest.skip("no order uses a tool in this profile")
    op, tool_id = target
    broken = copy.deepcopy(model)
    broken.tool_units[tool_id] = (broken.tool_units[tool_id][0], "RETIRED")
    result = find_issues(broken)
    assert op.order_id in result.infeasible_orders
    assert any(i.code == "TOOL_UNAVAILABLE" and i.order_id == op.order_id for i in result.issues)