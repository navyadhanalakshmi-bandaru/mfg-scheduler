"""Production order generator (Phase 4, step 4).

Needs the master data from step 2. Produces:
    production_order, operation, operation_material_req, order_status_history

Rules:
  * Exactly VOLUMES[profile]['orders'] orders and VOLUMES[profile]['operations']
    operations. SKUs are chosen per order, then swapped for SKUs with a shorter
    or longer routing until the operation total hits the target exactly.
  * Quantity is between 40% and 100% of the SKU standard lot, so every operation
    fits inside one shift (the guarantee made in master_data.py).
  * base_minutes = ceil(fixed_minutes + quantity * std_minutes_per_unit).
  * Priority mix: 5% URGENT, 15% HIGH, 60% NORMAL, 20% LOW (at least one URGENT).
  * Release: a random weekday in the first 5 weeks, between 06:00 and 13:00.
    Order ids are numbered in release order.
  * Due = release + (days per operation by priority x routing length + 2-6 buffer
    days), at 17:00, never later than 2026-12-24 17:00.
  * Material requirement = BOM qty_per_unit x order quantity, attached to the
    operation named by consumed_at_sequence.
  * Orders start as RELEASED (status history: NEW -> RELEASED). The scheduler
    changes them to SCHEDULED or UNSCHEDULED later.
"""
from __future__ import annotations

import math
from datetime import datetime, timedelta

from config.settings import SEED, VOLUMES
from src.generator import ids
from src.generator.calendars import HORIZON_END_DT, HORIZON_START_DT, TS
from src.generator.master_data import MACHINE_GROUPS
from src.generator.rng import make_rng

ORDER_CREATED_AT = "2026-10-22 08:00:00"
ORDER_RELEASED_AT = "2026-10-22 08:30:00"

PRIORITY_RANK = {"URGENT": 1, "HIGH": 2, "NORMAL": 3, "LOW": 4}
PRIORITY_SHARE = {"URGENT": 0.05, "HIGH": 0.15, "LOW": 0.20}  # NORMAL = the rest
DAYS_PER_OP = {"URGENT": 0.6, "HIGH": 1.0, "NORMAL": 1.4, "LOW": 2.0}
RELEASE_WEEKS = 2
DUE_CAP = (HORIZON_END_DT - timedelta(days=4)).replace(hour=17, minute=0, second=0)

ORDER_LOAD_ORDER = ["production_order", "operation", "operation_material_req",
                    "order_status_history"]


def _fmt(dt: datetime) -> str:
    return dt.strftime(TS)


def _priority_list(n: int, rng) -> list[str]:
    counts = {pc: max(1, round(share * n)) for pc, share in PRIORITY_SHARE.items()}
    counts["NORMAL"] = n - sum(counts.values())
    if counts["NORMAL"] < 1:
        raise ValueError(f"too few orders ({n}) for the priority mix")
    classes = [pc for pc in PRIORITY_RANK for _ in range(counts[pc])]
    rng.shuffle(classes)
    return classes


def _release_times(n: int, rng) -> list[datetime]:
    first = HORIZON_START_DT.date()
    days = [first + timedelta(days=d) for d in range(RELEASE_WEEKS * 7)]
    days = [d for d in days if d.weekday() < 5]
    times = []
    for _ in range(n):
        d = rng.choice(days)
        times.append(datetime(d.year, d.month, d.day, 6) + timedelta(minutes=60 * rng.randint(0, 7)))
    return sorted(times)


def _choose_skus(n_orders: int, target_ops: int, route_len: dict[str, int], rng) -> list[str]:
    """Pick one SKU per order so the routing lengths add up to exactly target_ops."""
    skus = sorted(route_len)
    if not n_orders * min(route_len.values()) <= target_ops <= n_orders * max(route_len.values()):
        raise ValueError(f"cannot reach {target_ops} operations with {n_orders} orders")
    chosen: list[str] = []
    while len(chosen) < n_orders:  # every SKU is used about equally often
        block = skus[:]
        rng.shuffle(block)
        chosen.extend(block)
    chosen = chosen[:n_orders]

    by_len: dict[int, list[str]] = {}
    for s in skus:
        by_len.setdefault(route_len[s], []).append(s)
    total = sum(route_len[s] for s in chosen)
    for _ in range(200000):
        if total == target_ops:
            return chosen
        i = rng.randrange(n_orders)
        cur = route_len[chosen[i]]
        diff = total - target_ops
        if diff > 0:
            options = [L for L in by_len if L < cur and cur - L <= diff]
        else:
            options = [L for L in by_len if L > cur and L - cur <= -diff]
        if not options:
            continue
        new_len = rng.choice(sorted(options))
        chosen[i] = rng.choice(by_len[new_len])
        total += new_len - cur
    raise RuntimeError("could not reach the operation target")


def generate_order_data(master: dict, profile: str = "full", seed: int = SEED) -> dict[str, list[dict]]:
    """Generate orders, operations, material requirements and status history."""
    if profile not in VOLUMES:
        raise ValueError(f"unknown profile {profile!r}; choose from {sorted(VOLUMES)}")
    n_orders = VOLUMES[profile]["orders"]
    target_ops = VOLUMES[profile]["operations"]
    rng = make_rng("orders", seed)

    routing_of = {r["sku_id"]: r["routing_id"] for r in master["routing"]}
    lot_of = {p["sku_id"]: p["standard_lot_size"] for p in master["product"]}
    steps_of: dict[str, list[dict]] = {}
    for s in master["routing_step"]:
        steps_of.setdefault(s["routing_id"], []).append(s)
    for rows in steps_of.values():
        rows.sort(key=lambda r: r["sequence_no"])
    route_len = {sku: len(steps_of[rid]) for sku, rid in routing_of.items()}
    bom_of: dict[str, list[dict]] = {}
    for b in master["product_bom"]:
        bom_of.setdefault(b["sku_id"], []).append(b)

    skus = _choose_skus(n_orders, target_ops, route_len, rng)
    priorities = _priority_list(n_orders, rng)
    releases = _release_times(n_orders, rng)

    orders, operations, requirements, history = [], [], [], []
    for i in range(n_orders):
        oid = ids.order_id(i + 1)
        sku, pc, release = skus[i], priorities[i], releases[i]
        rid = routing_of[sku]
        lot = lot_of[sku]
        qty = rng.randint(max(1, math.ceil(0.4 * lot)), lot)
        n_ops = route_len[sku]

        lead_days = DAYS_PER_OP[pc] * n_ops + rng.randint(2, 6)
        due = (release + timedelta(days=lead_days)).replace(hour=17, minute=0, second=0)
        due = min(due, DUE_CAP)

        orders.append({
            "order_id": oid, "sku_id": sku, "routing_id": rid, "quantity": qty,
            "priority_class": pc, "priority_rank": PRIORITY_RANK[pc],
            "release_time": _fmt(release), "due_time": _fmt(due), "status": "RELEASED",
            "created_at": ORDER_CREATED_AT, "updated_at": ORDER_RELEASED_AT,
        })

        for step in steps_of[rid]:
            minutes = step["fixed_minutes"] + qty * step["std_minutes_per_unit"]
            operations.append({
                "operation_id": ids.operation_id(oid, step["sequence_no"]),
                "order_id": oid, "step_id": step["step_id"],
                "sequence_no": step["sequence_no"],
                "operation_type": step["operation_type"],
                "required_machine_group": step["required_machine_group"],
                "quantity": qty,
                "base_minutes": min(450, max(1, math.ceil(round(minutes, 6)))),
                "labor_skill": step["labor_skill"],
                "labor_headcount": step["labor_headcount"],
                "status": "PENDING",
                "created_at": ORDER_CREATED_AT, "updated_at": ORDER_CREATED_AT,
            })

        for bom in bom_of[sku]:
            op_id = ids.operation_id(oid, bom["consumed_at_sequence"])
            requirements.append({
                "req_id": f"REQ-{op_id}-{bom['material_id']}",
                "operation_id": op_id, "material_id": bom["material_id"],
                "required_qty": round(bom["qty_per_unit"] * qty, 3),
            })

    counter = 0
    for o in orders:
        for frm, to, at, reason in [(None, "NEW", ORDER_CREATED_AT, "Order created"),
                                    ("NEW", "RELEASED", ORDER_RELEASED_AT, "Released to planning")]:
            counter += 1
            history.append({
                "history_id": ids.order_history_id(counter), "order_id": o["order_id"],
                "from_status": frm, "to_status": to, "changed_at": at,
                "run_id": None, "reason": reason,
            })

    tables = {"production_order": orders, "operation": operations,
              "operation_material_req": requirements, "order_status_history": history}
    assert list(tables) == ORDER_LOAD_ORDER
    return tables


def load_by_group(master: dict, calendar: dict, orders: dict) -> dict[str, dict]:
    """Planned work minutes versus calendar minutes per machine group.

    Planned = sum of base_minutes (ignores machine speed, changeovers, downtime).
    load_pct       = planned / calendar minutes of the whole 8-week horizon.
    early_load_pct = planned / calendar minutes of weeks 1-5, when work arrives.
    Rough guides, not scheduling results.
    """
    group_of = {m["machine_id"]: m["machine_group"] for m in master["machine"]}
    cutoff = _fmt(HORIZON_START_DT + timedelta(weeks=RELEASE_WEEKS))
    available = dict.fromkeys(MACHINE_GROUPS, 0)
    early = dict.fromkeys(MACHINE_GROUPS, 0)
    for r in calendar["machine_calendar"]:
        g = group_of[r["machine_id"]]
        available[g] += r["available_minutes"]
        if r["window_start"] < cutoff:
            early[g] += r["available_minutes"]
    planned = dict.fromkeys(MACHINE_GROUPS, 0)
    for op in orders["operation"]:
        planned[op["required_machine_group"]] += op["base_minutes"]
    return {g: {"planned_minutes": planned[g], "available_minutes": available[g],
                "load_pct": round(100 * planned[g] / available[g], 1) if available[g] else 0.0,
                "early_available_minutes": early[g],
                "early_load_pct": round(100 * planned[g] / early[g], 1) if early[g] else 0.0}
            for g in MACHINE_GROUPS}


if __name__ == "__main__":
    import sys

    from src.generator.calendars import generate_calendar_data
    from src.generator.master_data import generate_master_data

    chosen = sys.argv[1] if len(sys.argv) > 1 else "full"
    master_data = generate_master_data(chosen)
    calendar_data = generate_calendar_data(master_data)
    order_data = generate_order_data(master_data, chosen)
    for table_name, table_rows in order_data.items():
        print(f"{table_name:24s} {len(table_rows):5d}")
    print()
    print(f"{'machine group':14s} {'planned min':>12s} {'available min':>14s} "
          f"{'load %':>7s} {'wk1-5 load %':>13s}")
    for group, info in load_by_group(master_data, calendar_data, order_data).items():
        print(f"{group:14s} {info['planned_minutes']:12d} {info['available_minutes']:14d} "
              f"{info['load_pct']:7.1f} {info['early_load_pct']:13.1f}")