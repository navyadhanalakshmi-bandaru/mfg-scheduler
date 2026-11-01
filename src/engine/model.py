"""Scheduling model and static feasibility (Phase 5b).

load_model() reads everything the dispatcher needs from the database.
find_issues() flags orders that can never be scheduled (engine-side
INFEASIBLE_ORDER codes) and computes when each operation's material is ready.
"""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field

from config.settings import HORIZON_START

HORIZON_DATE = HORIZON_START[:10]
EPS = 1e-6


@dataclass(frozen=True)
class Candidate:
    machine_id: str
    rate_factor: float
    preference_rank: int


@dataclass(frozen=True)
class Order:
    order_id: str
    sku_id: str
    family: str
    quantity: int
    priority_class: str
    priority_rank: int
    release_time: str
    due_time: str


@dataclass(frozen=True)
class Op:
    operation_id: str
    order_id: str
    step_id: str
    sequence_no: int
    machine_group: str
    base_minutes: int
    labor_skill: str
    labor_headcount: int


@dataclass(frozen=True)
class Lot:
    lot_id: str
    material_id: str
    available_from: str
    quantity: float


@dataclass(frozen=True)
class Issue:
    order_id: str
    operation_id: str
    code: str
    message: str
    material_id: str | None = None


@dataclass
class FeasibilityResult:
    issues: list[Issue] = field(default_factory=list)
    infeasible_orders: set[str] = field(default_factory=set)
    material_ready: dict[str, str] = field(default_factory=dict)  # operation_id -> time
    allocations: dict[str, list[tuple[str, float]]] = field(default_factory=dict)  # operation_id -> (lot_id, qty)


@dataclass
class Model:
    orders: dict[str, Order]
    ops_by_order: dict[str, list[Op]]
    candidates: dict[str, list[Candidate]]            # step_id -> machines, best first
    requirements: dict[str, list[tuple[str, float]]]  # operation_id -> (material, qty)
    lots: dict[str, list[Lot]]                        # material_id -> usable lots by arrival
    labor_capacity: dict[tuple[str, str, str], int]   # (skill, date, shift) -> headcount
    step_tools: dict[str, list[tuple[str, int]]]      # step_id -> (tool_id, units needed)
    tool_units: dict[str, tuple[int, str]]            # tool_id -> (units, status)
    changeover: dict[tuple[str, str, str], int]       # (group, from, to) -> minutes


def _effective(frm: str, to: str | None) -> bool:
    return frm <= HORIZON_DATE and (to is None or to > HORIZON_DATE)


def load_model(con: sqlite3.Connection) -> Model:
    con.row_factory = sqlite3.Row
    try:
        orders = {}
        for r in con.execute(
                "SELECT o.order_id, o.sku_id, p.product_family, o.quantity, o.priority_class,"
                " o.priority_rank, o.release_time, o.due_time"
                " FROM production_order o JOIN product p ON p.sku_id = o.sku_id"
                " WHERE o.status NOT IN ('COMPLETED','CANCELLED') ORDER BY o.order_id"):
            orders[r["order_id"]] = Order(r["order_id"], r["sku_id"], r["product_family"],
                                          r["quantity"], r["priority_class"], r["priority_rank"],
                                          r["release_time"], r["due_time"])
        ops_by_order: dict[str, list[Op]] = {oid: [] for oid in orders}
        for r in con.execute(
                "SELECT operation_id, order_id, step_id, sequence_no, required_machine_group,"
                " base_minutes, labor_skill, labor_headcount FROM operation"
                " WHERE status <> 'CANCELLED' ORDER BY order_id, sequence_no"):
            if r["order_id"] in ops_by_order:
                ops_by_order[r["order_id"]].append(
                    Op(r["operation_id"], r["order_id"], r["step_id"], r["sequence_no"],
                       r["required_machine_group"], r["base_minutes"], r["labor_skill"],
                       r["labor_headcount"]))

        candidates: dict[str, list[Candidate]] = {}
        for r in con.execute(
                "SELECT rsm.step_id, rsm.machine_id, rsm.rate_factor, rsm.preference_rank,"
                " rsm.effective_from AS rf, rsm.effective_to AS rt,"
                " m.effective_from AS mf, m.effective_to AS mt"
                " FROM routing_step_machine rsm"
                " JOIN routing_step s ON s.step_id = rsm.step_id"
                " JOIN machine m ON m.machine_id = rsm.machine_id"
                " WHERE m.status = 'ACTIVE' AND m.machine_group = s.required_machine_group"
                " ORDER BY rsm.step_id, rsm.preference_rank, rsm.machine_id"):
            if _effective(r["rf"], r["rt"]) and _effective(r["mf"], r["mt"]):
                candidates.setdefault(r["step_id"], []).append(
                    Candidate(r["machine_id"], r["rate_factor"], r["preference_rank"]))

        requirements: dict[str, list[tuple[str, float]]] = {}
        for r in con.execute("SELECT operation_id, material_id, required_qty"
                             " FROM operation_material_req ORDER BY req_id"):
            requirements.setdefault(r["operation_id"], []).append((r["material_id"], r["required_qty"]))

        lots: dict[str, list[Lot]] = {}
        for r in con.execute("SELECT lot_id, material_id, available_from, quantity_received"
                             " FROM inventory_lot WHERE status IN ('AVAILABLE','EXPECTED')"
                             " ORDER BY available_from, lot_id"):
            lots.setdefault(r["material_id"], []).append(
                Lot(r["lot_id"], r["material_id"], r["available_from"], r["quantity_received"]))

        labor = {}
        for r in con.execute("SELECT pool_id, work_date, shift_code, headcount_available"
                             " FROM labor_shift_capacity"):
            labor[(r["pool_id"].removeprefix("LAB-"), r["work_date"], r["shift_code"])] = \
                r["headcount_available"]

        step_tools: dict[str, list[tuple[str, int]]] = {}
        for r in con.execute("SELECT step_id, tool_id, units_required FROM routing_step_tool"
                             " ORDER BY step_id, tool_id"):
            step_tools.setdefault(r["step_id"], []).append((r["tool_id"], r["units_required"]))
        tool_units = {r["tool_id"]: (r["units_available"], r["status"])
                      for r in con.execute("SELECT tool_id, units_available, status FROM tool")}

        changeover = {}
        for r in con.execute("SELECT machine_group, from_family, to_family, setup_minutes,"
                             " effective_from, effective_to FROM changeover_matrix"):
            if _effective(r["effective_from"], r["effective_to"]):
                changeover[(r["machine_group"], r["from_family"], r["to_family"])] = r["setup_minutes"]
    finally:
        con.row_factory = None
    return Model(orders, ops_by_order, candidates, requirements, lots, labor,
                 step_tools, tool_units, changeover)


def _static_issues(model: Model) -> list[Issue]:
    best_labor: dict[str, int] = {}
    for (skill, _, _), heads in model.labor_capacity.items():
        best_labor[skill] = max(best_labor.get(skill, 0), heads)
    issues = []
    for oid, ops in model.ops_by_order.items():
        for op in ops:
            if not model.candidates.get(op.step_id):
                issues.append(Issue(oid, op.operation_id, "NO_ELIGIBLE_MACHINE",
                                    f"no active eligible machine for step {op.step_id}"))
            if best_labor.get(op.labor_skill, 0) < op.labor_headcount:
                issues.append(Issue(oid, op.operation_id, "LABOR_UNAVAILABLE",
                                    f"{op.labor_skill} never has {op.labor_headcount} people available"))
            for tool_id, need in model.step_tools.get(op.step_id, []):
                units, status = model.tool_units.get(tool_id, (0, "RETIRED"))
                if status != "ACTIVE" or need > units:
                    issues.append(Issue(oid, op.operation_id, "TOOL_UNAVAILABLE",
                                        f"tool {tool_id} needs {need} unit(s), "
                                        f"has {units} ({status})"))
    return issues


def _allocate_materials(model: Model, excluded: set[str],
                        result: FeasibilityResult) -> None:
    """Serve orders by (priority_rank, due_time, order_id); each draws lots earliest-first.
    An order that cannot be fully covered takes nothing and is flagged."""
    remaining = {lot.lot_id: lot.quantity for lots in model.lots.values() for lot in lots}
    for order in sorted(model.orders.values(),
                        key=lambda o: (o.priority_rank, o.due_time, o.order_id)):
        if order.order_id in excluded:
            continue
        taken: list[tuple[str, float]] = []
        ready: dict[str, str] = {}
        per_op: dict[str, list[tuple[str, float]]] = {}
        failure = None
        for op in model.ops_by_order[order.order_id]:
            op_ready = ""
            op_taken: list[tuple[str, float]] = []
            for material_id, qty in model.requirements.get(op.operation_id, []):
                need = qty
                for lot in model.lots.get(material_id, []):
                    if need <= EPS:
                        break
                    available = remaining[lot.lot_id]
                    if available <= EPS:
                        continue
                    use = min(available, need)
                    remaining[lot.lot_id] -= use
                    taken.append((lot.lot_id, use))
                    op_taken.append((lot.lot_id, use))
                    need -= use
                    op_ready = max(op_ready, lot.available_from)
                if need > EPS:
                    failure = (op, material_id, need)
                    break
            if failure:
                break
            if op_ready:
                ready[op.operation_id] = op_ready
            if op_taken:
                per_op[op.operation_id] = op_taken
        if failure:
            for lot_id, use in taken:
                remaining[lot_id] += use
            op, material_id, short = failure
            result.issues.append(Issue(order.order_id, op.operation_id, "MATERIAL_UNAVAILABLE",
                                       f"{material_id} short by {short:.3f}", material_id))
            result.infeasible_orders.add(order.order_id)
        else:
            result.material_ready.update(ready)
            result.allocations.update(per_op)


def find_issues(model: Model) -> FeasibilityResult:
    result = FeasibilityResult(issues=_static_issues(model))
    result.infeasible_orders = {i.order_id for i in result.issues}
    _allocate_materials(model, set(result.infeasible_orders), result)
    return result


if __name__ == "__main__":
    import sys
    from collections import Counter

    from config.settings import SCHEMA_SQL
    from src.db.loader import load_tables
    from src.generator.export import ALL_TABLES, build_dataset

    chosen = sys.argv[1] if len(sys.argv) > 1 else "full"
    connection = sqlite3.connect(":memory:")
    connection.executescript(SCHEMA_SQL.read_text(encoding="utf-8"))
    load_tables(connection, build_dataset(chosen), ALL_TABLES)
    loaded = load_model(connection)
    found = find_issues(loaded)
    print(f"orders {len(loaded.orders)}, operations {sum(len(v) for v in loaded.ops_by_order.values())}")
    print(f"steps with candidates {len(loaded.candidates)}")
    for code, count in sorted(Counter(i.code for i in found.issues).items()):
        print(f"issue {code:22s} {count:4d}")
    print(f"infeasible orders {len(found.infeasible_orders)}")
    print(f"operations with a material-ready time {len(found.material_ready)}")