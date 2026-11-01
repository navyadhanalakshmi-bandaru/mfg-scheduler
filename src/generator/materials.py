"""Inventory lot generator (Phase 4, step 5).

Needs master data (step 2) and orders (step 4). Produces:
    inventory_lot, inventory_transaction (RECEIPT rows only)

Lots are sized from real demand (sum of operation_material_req per material).
Profiles (exact counts, assigned in a seeded random order):
    SHORT        3%  total supply is 60-85% of demand: some orders cannot be built
    LATE        15%  one lot arrives 2-8 days after the first order needing it is released
    REPLENISHED 12%  50% of demand in stock now, 70% more arrives later
    STOCKED     rest 115-150% of demand, received in October
About 2% of STOCKED materials also get a QUARANTINED lot (must never be used).

A lot received by the snapshot time (2026-10-22 08:00) is AVAILABLE, a later one
is EXPECTED. RECEIPT transactions exist for lots already received (AVAILABLE and
QUARANTINED); the scheduler adds RESERVE / CONSUME rows later.
"""
from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timedelta

from config.settings import SEED
from src.generator import ids
from src.generator.calendars import HORIZON_START_DT, TS
from src.generator.orders import ORDER_CREATED_AT
from src.generator.rng import make_rng

SNAPSHOT_DT = datetime.strptime(ORDER_CREATED_AT, TS)
ARRIVAL_CAP = HORIZON_START_DT + timedelta(weeks=5)  # 2026-12-07 00:00

SHARE_SHORT = 0.03
SHARE_LATE = 0.15
SHARE_REPLENISHED = 0.12
SHARE_QUARANTINED = 0.02

INVENTORY_LOAD_ORDER = ["inventory_lot", "inventory_transaction"]


def _fmt(dt: datetime) -> str:
    return dt.strftime(TS)


def _demand_and_first_need(orders: dict) -> tuple[dict, dict]:
    """Total demand per material, and the release time of the first order needing it."""
    release = {o["order_id"]: o["release_time"] for o in orders["production_order"]}
    demand: dict[str, float] = defaultdict(float)
    first_need: dict[str, str] = {}
    for r in orders["operation_material_req"]:
        order, _ = ids.parse_operation_id(r["operation_id"])
        mid = r["material_id"]
        demand[mid] += r["required_qty"]
        when = release[order]
        if mid not in first_need or when < first_need[mid]:
            first_need[mid] = when
    return dict(demand), first_need


def _stock_time(rng) -> datetime:
    """A receipt time in October, before the snapshot."""
    return datetime(2026, 10, rng.randint(5, 21), rng.randint(6, 14))


def _arrival(base: datetime, days: int, rng) -> datetime:
    when = base.replace(hour=rng.randint(6, 14), minute=0, second=0) + timedelta(days=days)
    return min(when, ARRIVAL_CAP)


def generate_inventory_data(master: dict, orders: dict, seed: int = SEED) -> dict[str, list[dict]]:
    """Generate inventory lots and RECEIPT transactions from material demand."""
    rng = make_rng("inventory", seed)
    demand, first_need = _demand_and_first_need(orders)
    lead = {m["material_id"]: m["lead_time_days"] for m in master["material"]}
    material_ids = sorted(lead)

    needed = [m for m in material_ids if demand.get(m, 0.0) > 0]
    shuffled = needed[:]
    rng.shuffle(shuffled)
    n_short = max(1, round(SHARE_SHORT * len(needed)))
    n_late = round(SHARE_LATE * len(needed))
    n_repl = round(SHARE_REPLENISHED * len(needed))

    category = {m: "STOCKED" for m in material_ids}
    for m in shuffled[:n_short]:
        category[m] = "SHORT"
    for m in shuffled[n_short:n_short + n_late]:
        category[m] = "LATE"
    for m in shuffled[n_short + n_late:n_short + n_late + n_repl]:
        category[m] = "REPLENISHED"

    stocked = [m for m in needed if category[m] == "STOCKED"]
    n_quarantined = max(1, round(SHARE_QUARANTINED * len(needed)))
    quarantined = set(rng.sample(stocked, min(n_quarantined, len(stocked))))

    pending = []  # (available_from, material_id, quantity, forced_status)
    for mid in material_ids:
        d = demand.get(mid, 0.0)
        if d <= 0:  # not used by any order: a small stock lot
            pending.append((_stock_time(rng), mid, round(rng.uniform(5, 50), 3), None))
            continue
        need_dt = datetime.strptime(first_need[mid], TS)
        cat = category[mid]
        if cat == "STOCKED":
            total = d * rng.uniform(1.15, 1.5)
            if rng.random() < 0.4:
                first = round(total * 0.6, 3)
                pending.append((_stock_time(rng), mid, first, None))
                pending.append((_stock_time(rng), mid, round(total - first, 3), None))
            else:
                pending.append((_stock_time(rng), mid, round(total, 3), None))
            if mid in quarantined:
                pending.append((_stock_time(rng), mid, round(d * 0.3, 3), "QUARANTINED"))
        elif cat == "SHORT":
            pending.append((_stock_time(rng), mid, round(d * rng.uniform(0.6, 0.85), 3), None))
        elif cat == "LATE":
            when = _arrival(need_dt, rng.randint(2, 8), rng)
            pending.append((when, mid, round(d * rng.uniform(1.15, 1.4), 3), None))
        else:  # REPLENISHED
            pending.append((_stock_time(rng), mid, round(d * 0.5, 3), None))
            wait = max(3, min(lead[mid], 14))
            pending.append((_arrival(need_dt, wait, rng), mid, round(d * 0.7, 3), None))

    pending.sort(key=lambda p: (p[0], p[1], p[2]))
    lots, transactions = [], []
    for i, (when, mid, qty, forced) in enumerate(pending, start=1):
        status = forced or ("AVAILABLE" if when <= SNAPSHOT_DT else "EXPECTED")
        lots.append({
            "lot_id": ids.lot_id(i), "material_id": mid, "quantity_received": qty,
            "available_from": _fmt(when), "status": status,
            "created_at": _fmt(min(when, SNAPSHOT_DT)),
        })

    for lot in lots:
        if lot["status"] in ("AVAILABLE", "QUARANTINED"):
            transactions.append({
                "txn_id": ids.txn_id(len(transactions) + 1), "lot_id": lot["lot_id"],
                "txn_type": "RECEIPT", "quantity": lot["quantity_received"],
                "operation_id": None, "run_id": None,
                "txn_time": lot["available_from"], "created_at": lot["available_from"],
            })

    tables = {"inventory_lot": lots, "inventory_transaction": transactions}
    assert list(tables) == INVENTORY_LOAD_ORDER
    return tables


def material_coverage(orders: dict, inventory: dict) -> dict[str, dict]:
    """Per material used by orders: demand, usable supply (QUARANTINED excluded)
    and the first and last time usable stock becomes available."""
    demand, _ = _demand_and_first_need(orders)
    supply: dict[str, float] = defaultdict(float)
    first: dict[str, str] = {}
    last: dict[str, str] = {}
    for lot in inventory["inventory_lot"]:
        if lot["status"] == "QUARANTINED":
            continue
        mid = lot["material_id"]
        supply[mid] += lot["quantity_received"]
        first[mid] = min(first.get(mid, lot["available_from"]), lot["available_from"])
        last[mid] = max(last.get(mid, lot["available_from"]), lot["available_from"])
    return {mid: {"demand": round(d, 3), "usable_supply": round(supply.get(mid, 0.0), 3),
                  "first_available": first.get(mid), "last_available": last.get(mid)}
            for mid, d in sorted(demand.items())}


if __name__ == "__main__":
    import sys
    from collections import Counter

    from src.generator.calendars import generate_calendar_data
    from src.generator.master_data import generate_master_data
    from src.generator.orders import generate_order_data

    chosen = sys.argv[1] if len(sys.argv) > 1 else "full"
    master_data = generate_master_data(chosen)
    order_data = generate_order_data(master_data, chosen)
    inventory_data = generate_inventory_data(master_data, order_data)
    for table_name, table_rows in inventory_data.items():
        print(f"{table_name:24s} {len(table_rows):5d}")
    print()
    for status, count in sorted(Counter(l["status"] for l in inventory_data["inventory_lot"]).items()):
        print(f"lots {status:12s} {count:5d}")
    coverage = material_coverage(order_data, inventory_data)
    short = sum(1 for c in coverage.values() if c["usable_supply"] < c["demand"])
    late = sum(1 for c in coverage.values() if c["first_available"] >= _fmt(HORIZON_START_DT))
    print(f"materials used by orders        {len(coverage):5d}")
    print(f"materials short of demand       {short:5d}")
    print(f"materials first stocked in horizon {late:2d}")