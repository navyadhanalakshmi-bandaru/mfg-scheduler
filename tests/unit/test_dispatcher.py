"""Tests for the dispatcher."""
import json
import sqlite3
from datetime import datetime

import pytest

from config.settings import SCHEMA_SQL
from src.db.loader import load_tables
from src.engine.dispatcher import RULES, dispatch, prepare
from src.engine.model import Candidate, FeasibilityResult, Model, Op, Order
from src.engine.timeline import MachineTimeline, parse
from src.generator.export import ALL_TABLES, build_dataset


def d(day, h, m=0):
    return datetime(2026, 11, day, h, m)


# ---------- tiny hand-checked schedules ----------

def tiny(rank_b=3, machines=("M1",), cap=5, base=60):
    def order(oid, fam, rank, due):
        return Order(oid, "SKU", fam, 10, "NORMAL", rank, "2026-11-02 06:00:00", due)
    orders = {"A": order("A", "SHAFT", 3, "2026-11-03 17:00:00"),
              "B": order("B", "GEAR", rank_b, "2026-11-04 17:00:00")}
    ops = {o: [Op(f"OP-{o}-01", o, "STEP-1", 1, "CUTTING", base, "MACHINIST", 1)] for o in orders}
    cands = {"STEP-1": [Candidate(m, 1.0, i + 1) for i, m in enumerate(machines)]}
    labor = {("MACHINIST", f"2026-11-0{day}", "S1"): cap for day in (2, 3)}
    change = {("CUTTING", "SHAFT", "GEAR"): 30, ("CUTTING", "GEAR", "SHAFT"): 30}
    model = Model(orders, ops, cands, {}, {}, labor, {}, {}, change)
    wins = {m: [(d(2, 6), d(2, 14), "2026-11-02", "S1"), (d(3, 6), d(3, 14), "2026-11-03", "S1")]
            for m in machines}
    tls = {m: MachineTimeline([(w[0], w[1]) for w in wins[m]]) for m in machines}
    return model, FeasibilityResult(), tls, wins


def by_op(result):
    return {p.operation_id: p for p in result.placements}


def test_edd_places_a_first_and_applies_changeover():
    res = by_op(dispatch(*tiny(), rule="EDD"))
    a, b = res["OP-A-01"], res["OP-B-01"]
    assert (a.setup_minutes, a.start, a.end) == (0, d(2, 6), d(2, 7))
    assert (b.setup_start, b.setup_minutes, b.start, b.end) == (d(2, 7), 30, d(2, 7, 30), d(2, 8, 30))
    assert b.previous_operation_id == "OP-A-01"


def test_priority_edd_lets_urgent_order_go_first():
    res = by_op(dispatch(*tiny(rank_b=1), rule="PRIORITY_EDD"))
    assert res["OP-B-01"].start == d(2, 6) and res["OP-B-01"].setup_minutes == 0
    assert res["OP-A-01"].setup_minutes == 30 and res["OP-A-01"].end == d(2, 8, 30)
    edd = by_op(dispatch(*tiny(rank_b=1), rule="EDD"))
    assert edd["OP-A-01"].start == d(2, 6)


def test_labor_limit_pushes_second_job_to_other_machine_later():
    res = by_op(dispatch(*tiny(machines=("M1", "M2"), cap=1), rule="EDD"))
    assert res["OP-A-01"].machine_id == "M1" and res["OP-A-01"].start == d(2, 6)
    b = res["OP-B-01"]
    assert (b.machine_id, b.start, b.end) == ("M2", d(2, 7), d(2, 8))
    assert len(b.candidates) == 2 and json.dumps(b.candidates)


def test_infeasible_orders_are_skipped():
    model, feas, tls, wins = tiny()
    feas.infeasible_orders = {"B"}
    result = dispatch(model, feas, tls, wins, "EDD")
    assert {p.order_id for p in result.placements} == {"A"}
    assert result.skipped_orders == {"B"}


def test_operation_longer_than_any_window_has_no_capacity():
    result = dispatch(*tiny(base=600), rule="EDD")
    assert not result.placements
    assert {i.code for i in result.issues} == {"NO_CAPACITY_IN_HORIZON"}


def test_unknown_rule_rejected():
    with pytest.raises(ValueError):
        dispatch(*tiny(), rule="FIFO")


def test_dispatch_does_not_modify_input_timelines():
    model, feas, tls, wins = tiny()
    before = {m: list(t.free) for m, t in tls.items()}
    dispatch(model, feas, tls, wins, "EDD")
    assert {m: t.free for m, t in tls.items()} == before


# ---------- generated data ----------

@pytest.fixture(scope="module", params=[(p, r) for p in ("starter", "full") for r in RULES],
                ids=lambda x: x if isinstance(x, str) else None)
def run(request):
    profile, rule = request.param
    con = sqlite3.connect(":memory:")
    con.executescript(SCHEMA_SQL.read_text(encoding="utf-8"))
    tables = build_dataset(profile)
    load_tables(con, tables, ALL_TABLES)
    model, feas, tls, wins = prepare(con)
    con.close()
    return model, feas, wins, tables, dispatch(model, feas, tls, wins, rule)


def test_every_feasible_order_is_accounted_for(run):
    model, feas, _, _, res = run
    placed = {p.order_id for p in res.placements}
    failed = {i.order_id for i in res.issues}
    assert placed | failed | feas.infeasible_orders == set(model.orders)
    assert len(failed) <= 0.05 * len(model.orders)
    expected_ops = sum(len(model.ops_by_order[o]) for o in placed - failed)
    assert len([p for p in res.placements if p.order_id not in failed]) == expected_ops


def test_machines_never_overlap_and_chain_is_correct(run):
    _, _, _, _, res = run
    per_machine = {}
    for p in res.placements:
        per_machine.setdefault(p.machine_id, []).append(p)
    for plist in per_machine.values():
        plist.sort(key=lambda p: p.setup_start)
        assert plist[0].previous_operation_id is None
        for prev, cur in zip(plist, plist[1:]):
            assert cur.setup_start >= prev.end
            assert cur.previous_operation_id == prev.operation_id


def test_precedence_release_and_material(run):
    model, feas, _, _, res = run
    placed = {p.operation_id: p for p in res.placements}
    for oid, ops in model.ops_by_order.items():
        release = parse(model.orders[oid].release_time)
        prev_end = release
        for op in ops:
            p = placed.get(op.operation_id)
            if p is None:
                break
            assert p.setup_start >= prev_end
            if op.operation_id in feas.material_ready:
                assert p.setup_start >= parse(feas.material_ready[op.operation_id])
            prev_end = p.end


def test_blocks_stay_inside_one_calendar_window(run):
    _, _, wins, _, res = run
    for p in res.placements:
        assert any(ws <= p.setup_start and p.end <= we for ws, we, _, _ in wins[p.machine_id])


def test_machine_eligibility_downtime_and_setup(run):
    model, _, _, tables, res = run
    op_of = {o.operation_id: o for ops in model.ops_by_order.values() for o in ops}
    downs = {}
    for r in tables["machine_downtime"]:
        downs.setdefault(r["machine_id"], []).append((parse(r["down_start"]), parse(r["down_end"])))
    family_of = {}
    for p in sorted(res.placements, key=lambda p: (p.machine_id, p.setup_start)):
        op = op_of[p.operation_id]
        assert p.machine_id in {c.machine_id for c in model.candidates[op.step_id]}
        assert all(p.end <= s or p.setup_start >= e for s, e in downs.get(p.machine_id, []))
        prev = family_of.get(p.machine_id)
        fam = model.orders[p.order_id].family
        expected = model.changeover.get((op.machine_group, prev, fam), 0) if prev else 0
        assert p.setup_minutes == expected
        family_of[p.machine_id] = fam


def _peak(items):
    return max((sum(n for a, b, n in items if a <= s < b) for s, _, _ in items), default=0)


def test_labor_and_tools_never_exceed_capacity(run):
    model, _, wins, _, res = run
    op_of = {o.operation_id: o for ops in model.ops_by_order.values() for o in ops}
    labor, tools = {}, {}
    for p in res.placements:
        op = op_of[p.operation_id]
        w = next(w for w in wins[p.machine_id] if w[0] <= p.setup_start < w[1])
        labor.setdefault((op.labor_skill, w[2], w[3]), []).append((p.setup_start, p.end, op.labor_headcount))
        for tool_id, need in model.step_tools.get(op.step_id, []):
            tools.setdefault(tool_id, []).append((p.setup_start, p.end, need))
    for key, items in labor.items():
        assert _peak(items) <= model.labor_capacity.get(key, 0)
    for tool_id, items in tools.items():
        assert _peak(items) <= model.tool_units[tool_id][0]


def test_dispatch_is_deterministic(run):
    model, feas, wins, tables, res = run
    con = sqlite3.connect(":memory:")
    con.executescript(SCHEMA_SQL.read_text(encoding="utf-8"))
    load_tables(con, tables, ALL_TABLES)
    _, _, tls, _ = prepare(con)
    con.close()
    again = dispatch(model, feas, tls, wins, res.rule)
    assert [(p.operation_id, p.machine_id, p.setup_start) for p in again.placements] == \
           [(p.operation_id, p.machine_id, p.setup_start) for p in res.placements]