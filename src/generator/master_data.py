"""Master-data generator (Phase 4, step 2).

Builds parent tables first, then children that reference them.

Duration rules (used by the scheduling engine later):
    base_minutes     = fixed_minutes + order_qty * std_minutes_per_unit
    actual_minutes   = base_minutes / rate_factor     (rate_factor > 1 = faster machine)
By design base_minutes <= 301 for a full standard lot, and rate_factor >= 0.85,
so processing <= 355 min. The largest changeover is 77 min. Total <= 432 min,
which always fits inside one 480-minute shift (non-preemptive operations).
"""
from __future__ import annotations

import math

from config.settings import SEED, VOLUMES
from src.generator import ids
from src.generator.rng import make_rng

EFFECTIVE_FROM = "2026-01-01"
CREATED_AT = "2026-01-01 00:00:00"

MACHINE_GROUPS = ["CUTTING", "TURNING", "MILLING", "DRILLING", "GRINDING",
                  "HEAT_TREAT", "WELDING", "ASSEMBLY", "INSPECTION"]
# Machines per group in the full dataset (sums to 40). Scaled for other sizes.
GROUP_WEIGHTS = {"CUTTING": 5, "TURNING": 6, "MILLING": 6, "DRILLING": 4,
                 "GRINDING": 4, "HEAT_TREAT": 3, "WELDING": 3, "ASSEMBLY": 5,
                 "INSPECTION": 4}
FAMILIES = ["SHAFT", "GEAR", "HOUSING", "BRACKET", "VALVE", "FLANGE", "PLATE", "FASTENER"]
LABOR_SKILLS = ["MACHINIST", "WELDER", "ASSEMBLER", "INSPECTOR", "HEAT_TREATER"]

SHIFT_PATTERN_IDS = ["SHP-1S", "SHP-2S", "SHP-3S"]
SHIFT_PATTERN_WEIGHTS = [55, 35, 10]

OP_TO_GROUP = {"CUT": "CUTTING", "TURN": "TURNING", "MILL": "MILLING",
               "DRILL": "DRILLING", "GRIND": "GRINDING", "HEAT_TREAT": "HEAT_TREAT",
               "WELD": "WELDING", "ASSEMBLE": "ASSEMBLY", "INSPECT": "INSPECTION"}
OP_TO_SKILL = {"CUT": "MACHINIST", "TURN": "MACHINIST", "MILL": "MACHINIST",
               "DRILL": "MACHINIST", "GRIND": "MACHINIST", "HEAT_TREAT": "HEAT_TREATER",
               "WELD": "WELDER", "ASSEMBLE": "ASSEMBLER", "INSPECT": "INSPECTOR"}

# Each family has two possible routings (operation sequences of length 3 to 5).
FAMILY_ROUTINGS = {
    "SHAFT": [["CUT", "TURN", "HEAT_TREAT", "GRIND", "INSPECT"],
              ["CUT", "TURN", "GRIND", "INSPECT"]],
    "GEAR": [["CUT", "MILL", "HEAT_TREAT", "GRIND", "INSPECT"],
             ["CUT", "MILL", "DRILL", "INSPECT"]],
    "HOUSING": [["CUT", "MILL", "DRILL", "ASSEMBLE", "INSPECT"],
                ["CUT", "MILL", "DRILL", "INSPECT"]],
    "BRACKET": [["CUT", "DRILL", "WELD", "INSPECT"],
                ["CUT", "DRILL", "INSPECT"]],
    "VALVE": [["TURN", "MILL", "ASSEMBLE", "INSPECT"],
              ["CUT", "TURN", "MILL", "ASSEMBLE", "INSPECT"]],
    "FLANGE": [["CUT", "TURN", "DRILL", "INSPECT"],
               ["CUT", "TURN", "DRILL", "GRIND", "INSPECT"]],
    "PLATE": [["CUT", "DRILL", "INSPECT"],
              ["CUT", "MILL", "DRILL", "INSPECT"]],
    "FASTENER": [["TURN", "HEAT_TREAT", "INSPECT"],
                 ["CUT", "TURN", "HEAT_TREAT", "INSPECT"]],
}

# Minutes to process one standard lot (min, max), and fixed minutes (min, max).
OP_LOT_MINUTES = {"CUT": (60, 160), "TURN": (90, 240), "MILL": (90, 260),
                  "DRILL": (60, 150), "GRIND": (90, 240), "HEAT_TREAT": (150, 300),
                  "WELD": (80, 200), "ASSEMBLE": (60, 180), "INSPECT": (30, 90)}
OP_FIXED_MINUTES = {"CUT": (5, 20), "TURN": (10, 30), "MILL": (10, 30),
                    "DRILL": (5, 20), "GRIND": (10, 30), "HEAT_TREAT": (20, 60),
                    "WELD": (10, 30), "ASSEMBLE": (5, 20), "INSPECT": (5, 15)}

LOT_SIZES = [10, 20, 25, 40, 50, 60, 80, 100]
ADJECTIVES = ["Heavy", "Light", "Precision", "Standard", "Compact", "Extended"]

MATERIAL_KINDS = [("Steel Bar", "M"), ("Alloy Bar", "M"), ("Hex Stock", "M"),
                  ("Tube", "M"), ("Steel Plate", "KG"), ("Aluminium Plate", "KG"),
                  ("Weld Wire", "KG"), ("Iron Casting", "EA"), ("Forged Blank", "EA"),
                  ("Bearing", "EA"), ("Seal Kit", "EA"), ("Gasket", "EA")]
GRADES = ["A36", "4140", "1045", "6061", "304", "316", "8620"]
LEAD_TIMES = [0, 2, 3, 5, 7, 10, 14, 21, 30, 45]
LEAD_WEIGHTS = [5, 10, 10, 15, 15, 15, 10, 10, 6, 4]

# Changeover model: base setup per machine group, plus a "size distance"
# between product families. Going to a bigger family costs a little extra,
# so the matrix is sequence-dependent (A->B differs from B->A).
GROUP_SETUP_BASE = {"CUTTING": 15, "TURNING": 20, "MILLING": 25, "DRILLING": 15,
                    "GRINDING": 30, "HEAT_TREAT": 45, "WELDING": 20,
                    "ASSEMBLY": 10, "INSPECTION": 5}
FAMILY_SIZE = {"FASTENER": 0, "PLATE": 1, "BRACKET": 2, "SHAFT": 3,
               "GEAR": 4, "FLANGE": 4, "VALVE": 5, "HOUSING": 6}

TOOL_TYPES = ["Jig", "Fixture", "Gauge", "Die", "Chuck", "Mandrel",
              "Collet", "Vise", "Template", "Clamp"]

# Parent-first order. Loading in this order never violates a foreign key.
MASTER_LOAD_ORDER = ["shift_pattern", "machine", "product", "routing", "routing_step",
                     "routing_step_machine", "material", "product_bom",
                     "changeover_matrix", "tool", "routing_step_tool", "labor_pool"]


def allocate_machines(n: int) -> dict[str, int]:
    """Split n machines across the 9 groups (at least 1 each), in proportion to weights."""
    if n < len(MACHINE_GROUPS):
        raise ValueError(f"need at least {len(MACHINE_GROUPS)} machines, got {n}")
    total = sum(GROUP_WEIGHTS.values())
    ideal = {g: GROUP_WEIGHTS[g] * n / total for g in MACHINE_GROUPS}
    counts = {g: max(1, math.floor(ideal[g])) for g in MACHINE_GROUPS}
    while sum(counts.values()) < n:
        g = max(MACHINE_GROUPS, key=lambda x: ideal[x] - counts[x])
        counts[g] += 1
    while sum(counts.values()) > n:
        candidates = [g for g in MACHINE_GROUPS if counts[g] > 1]
        g = min(candidates, key=lambda x: ideal[x] - counts[x])
        counts[g] -= 1
    return counts


def _build_shift_patterns() -> list[dict]:
    weekdays = "MON,TUE,WED,THU,FRI"
    return [
        {"pattern_id": "SHP-1S", "pattern_name": "Single shift (day)", "shift_codes": "S1",
         "s1_start": "06:00", "s1_minutes": 480, "s2_start": None, "s2_minutes": None,
         "s3_start": None, "s3_minutes": None, "working_weekdays": weekdays},
        {"pattern_id": "SHP-2S", "pattern_name": "Two shifts (day, evening)",
         "shift_codes": "S1,S2", "s1_start": "06:00", "s1_minutes": 480,
         "s2_start": "14:00", "s2_minutes": 480, "s3_start": None, "s3_minutes": None,
         "working_weekdays": weekdays},
        {"pattern_id": "SHP-3S", "pattern_name": "Three shifts (24h)",
         "shift_codes": "S1,S2,S3", "s1_start": "06:00", "s1_minutes": 480,
         "s2_start": "14:00", "s2_minutes": 480, "s3_start": "22:00", "s3_minutes": 480,
         "working_weekdays": weekdays},
    ]


def _build_machines(n: int, seed: int) -> list[dict]:
    rng = make_rng("machines", seed)
    counts = allocate_machines(n)
    rows, number = [], 1
    for group in MACHINE_GROUPS:
        for k in range(1, counts[group] + 1):
            rows.append({
                "machine_id": ids.machine_id(number),
                "machine_name": f"{group.replace('_', ' ').title()} Machine {k}",
                "machine_group": group,
                "status": "ACTIVE",
                "default_shift_pattern": rng.choices(
                    SHIFT_PATTERN_IDS, weights=SHIFT_PATTERN_WEIGHTS)[0],
                "effective_from": EFFECTIVE_FROM,
                "effective_to": None,
                "created_at": CREATED_AT,
            })
            number += 1
    return rows


def _build_products(n: int, seed: int) -> list[dict]:
    rng = make_rng("products", seed)
    rows = []
    for i in range(1, n + 1):
        family = FAMILIES[(i - 1) % len(FAMILIES)]
        adjective = rng.choice(ADJECTIVES)
        rows.append({
            "sku_id": ids.sku_id(i),
            "product_name": f"{adjective} {family.title()} {i:03d}",
            "product_family": family,
            "unit_of_measure": "EA",
            "standard_lot_size": rng.choice(LOT_SIZES),
            "lifecycle_state": "ACTIVE",
            "effective_from": EFFECTIVE_FROM,
            "effective_to": None,
            "created_at": CREATED_AT,
        })
    return rows


def _build_routings(products: list[dict], seed: int):
    """Returns (routing rows, routing_step rows, {sku_id: [operation types]})."""
    rng = make_rng("routings", seed)
    routings, steps, sku_ops = [], [], {}
    for product in products:
        sku = product["sku_id"]
        rid = ids.routing_id(sku, 1)
        ops = rng.choice(FAMILY_ROUTINGS[product["product_family"]])
        sku_ops[sku] = ops
        routings.append({
            "routing_id": rid, "sku_id": sku, "version_no": 1,
            "lifecycle_state": "ACTIVE", "effective_from": EFFECTIVE_FROM,
            "effective_to": None, "created_at": CREATED_AT,
        })
        for seq, op in enumerate(ops, start=1):
            target = rng.randint(*OP_LOT_MINUTES[op])
            fixed = rng.randint(*OP_FIXED_MINUTES[op])
            per_unit = round((target - fixed) / product["standard_lot_size"], 2)
            per_unit = min(60.0, max(0.1, per_unit))
            headcount = rng.choice([1, 2]) if op in ("WELD", "ASSEMBLE") else 1
            steps.append({
                "step_id": ids.step_id(rid, seq),
                "routing_id": rid,
                "sequence_no": seq,
                "operation_type": op,
                "required_machine_group": OP_TO_GROUP[op],
                "std_minutes_per_unit": per_unit,
                "fixed_minutes": fixed,
                "labor_skill": OP_TO_SKILL[op],
                "labor_headcount": headcount,
                "created_at": CREATED_AT,
            })
    return routings, steps, sku_ops


def _build_eligibility(steps: list[dict], machines: list[dict], seed: int) -> list[dict]:
    """Bridge table: which machines may run each routing step."""
    rng = make_rng("eligibility", seed)
    by_group = {g: sorted(m["machine_id"] for m in machines if m["machine_group"] == g)
                for g in MACHINE_GROUPS}
    rows = []
    for step in steps:
        pool = by_group[step["required_machine_group"]]
        if not pool:
            raise ValueError(f"no machines in group {step['required_machine_group']}")
        count = 1 if len(pool) == 1 else rng.randint(2, len(pool))
        chosen = sorted(rng.sample(pool, count))
        rated = [(round(rng.uniform(0.85, 1.25), 2), mid) for mid in chosen]
        ranked = sorted(rated, key=lambda t: (-t[0], t[1]))  # fastest machine = rank 1
        for rank, (rate, mid) in enumerate(ranked, start=1):
            rows.append({
                "step_id": step["step_id"], "machine_id": mid,
                "rate_factor": rate, "preference_rank": rank,
                "effective_from": EFFECTIVE_FROM, "effective_to": None,
            })
    rows.sort(key=lambda r: (r["step_id"], r["machine_id"]))
    return rows


def _build_materials(n: int, seed: int) -> list[dict]:
    rng = make_rng("materials", seed)
    rows = []
    for i in range(1, n + 1):
        kind, uom = rng.choice(MATERIAL_KINDS)
        grade = rng.choice(GRADES)
        rows.append({
            "material_id": ids.material_id(i),
            "material_name": f"{kind} {grade} {i:03d}",
            "unit_of_measure": uom,
            "lead_time_days": rng.choices(LEAD_TIMES, weights=LEAD_WEIGHTS)[0],
            "lifecycle_state": "ACTIVE",
            "effective_from": EFFECTIVE_FROM,
            "effective_to": None,
            "created_at": CREATED_AT,
        })
    return rows


def _bom_quantity(uom: str, rng) -> float:
    if uom == "EA":
        return float(rng.randint(1, 4))
    if uom == "KG":
        return round(rng.uniform(0.2, 5.0), 2)
    return round(rng.uniform(0.1, 2.0), 2)  # metres


def _build_bom(products, materials, sku_ops, seed) -> list[dict]:
    """Bridge table product <-> material. Every SKU gets 1-3 materials and every
    material is used by at least one SKU."""
    rng = make_rng("bom", seed)
    n_skus, n_mat = len(products), len(materials)
    if n_mat < 3 or n_mat > 3 * n_skus:
        raise ValueError("materials must be between 3 and 3 x number of SKUs")
    counts = [rng.choice([1, 2, 2, 3]) for _ in products]
    deficit = n_mat - sum(counts)
    while deficit > 0:
        idx = rng.randrange(n_skus)
        if counts[idx] < 3:
            counts[idx] += 1
            deficit -= 1
    pool = [m["material_id"] for m in materials]
    rng.shuffle(pool)
    uom = {m["material_id"]: m["unit_of_measure"] for m in materials}
    pointer, rows = 0, []
    for product, count in zip(products, counts):
        sku = product["sku_id"]
        ops = sku_ops[sku]
        chosen = []
        while len(chosen) < count:
            mid = pool[pointer % len(pool)]
            pointer += 1
            if mid not in chosen:
                chosen.append(mid)
        # First material is consumed at step 1; others at assembly (or step 2).
        second_seq = ops.index("ASSEMBLE") + 1 if "ASSEMBLE" in ops else min(2, len(ops))
        for j, mid in enumerate(chosen):
            rows.append({
                "sku_id": sku, "material_id": mid,
                "qty_per_unit": _bom_quantity(uom[mid], rng),
                "consumed_at_sequence": 1 if j == 0 else second_seq,
                "effective_from": EFFECTIVE_FROM, "effective_to": None,
            })
    rows.sort(key=lambda r: (r["sku_id"], r["material_id"]))
    return rows


def _build_changeover(seed: int) -> list[dict]:
    """Every (machine group, from family, to family) combination: 9 x 8 x 8 = 576 rows."""
    rng = make_rng("changeover", seed)
    rows = []
    for group in MACHINE_GROUPS:
        base = GROUP_SETUP_BASE[group]
        for from_f in FAMILIES:
            for to_f in FAMILIES:
                if from_f == to_f:
                    minutes = rng.randint(0, 5)  # same family: quick clean-up only
                else:
                    distance = abs(FAMILY_SIZE[from_f] - FAMILY_SIZE[to_f])
                    minutes = round(base * (0.6 + 0.15 * distance))
                    minutes += 4 if FAMILY_SIZE[to_f] > FAMILY_SIZE[from_f] else 0
                    minutes += rng.randint(0, 5)
                rows.append({
                    "changeover_id": f"CHG-{group}-{from_f}-{to_f}",
                    "machine_group": group, "from_family": from_f, "to_family": to_f,
                    "setup_minutes": min(180, max(0, minutes)),
                    "effective_from": EFFECTIVE_FROM, "effective_to": None,
                })
    return rows


def _tool_group(tool_number: int) -> str:
    return MACHINE_GROUPS[(tool_number - 1) % len(MACHINE_GROUPS)]


def _build_tools(n_tools: int, seed: int) -> list[dict]:
    rng = make_rng("tools", seed)
    rows = []
    for k in range(1, n_tools + 1):
        group = _tool_group(k).replace("_", " ").title()
        rows.append({
            "tool_id": ids.tool_id(k),
            "tool_name": f"{TOOL_TYPES[(k - 1) % len(TOOL_TYPES)]} {k:02d} ({group})",
            "units_available": rng.choice([1, 2, 2, 3]),
            "status": "ACTIVE",
        })
    return rows


def _build_step_tools(steps: list[dict], tools: list[dict], seed: int) -> list[dict]:
    """Bridge table: about 25% of steps need one shared tool from their machine group."""
    rng = make_rng("step_tools", seed)
    by_group: dict[str, list[str]] = {}
    for k, tool in enumerate(tools, start=1):
        by_group.setdefault(_tool_group(k), []).append(tool["tool_id"])
    rows = []
    for step in steps:
        pool = by_group.get(step["required_machine_group"])
        if pool and rng.random() < 0.25:
            rows.append({"step_id": step["step_id"], "tool_id": rng.choice(pool),
                         "units_required": 1})
    return rows


def _build_labor_pools() -> list[dict]:
    return [{"pool_id": f"LAB-{skill}", "labor_skill": skill} for skill in LABOR_SKILLS]


def generate_master_data(profile: str = "full", seed: int = SEED) -> dict[str, list[dict]]:
    """Generate all master-data tables for a profile ('starter' or 'full')."""
    if profile not in VOLUMES:
        raise ValueError(f"unknown profile {profile!r}; choose from {sorted(VOLUMES)}")
    vol = VOLUMES[profile]

    shift_patterns = _build_shift_patterns()
    machines = _build_machines(vol["machines"], seed)
    products = _build_products(vol["skus"], seed)
    routings, steps, sku_ops = _build_routings(products, seed)
    eligibility = _build_eligibility(steps, machines, seed)
    materials = _build_materials(vol["materials"], seed)
    bom = _build_bom(products, materials, sku_ops, seed)
    changeover = _build_changeover(seed)
    tools = _build_tools(max(5, vol["machines"] // 3), seed)
    step_tools = _build_step_tools(steps, tools, seed)
    labor_pools = _build_labor_pools()

    tables = {
        "shift_pattern": shift_patterns, "machine": machines, "product": products,
        "routing": routings, "routing_step": steps, "routing_step_machine": eligibility,
        "material": materials, "product_bom": bom, "changeover_matrix": changeover,
        "tool": tools, "routing_step_tool": step_tools, "labor_pool": labor_pools,
    }
    assert list(tables) == MASTER_LOAD_ORDER
    return tables


if __name__ == "__main__":
    import sys

    chosen = sys.argv[1] if len(sys.argv) > 1 else "full"
    for table_name, table_rows in generate_master_data(chosen).items():
        print(f"{table_name:22s} {len(table_rows):5d}")