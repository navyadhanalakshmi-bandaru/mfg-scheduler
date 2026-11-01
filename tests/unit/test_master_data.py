"""Tests for the master-data generator."""
import sqlite3

import pytest

from config.settings import SCHEMA_SQL, VOLUMES
from src.db.loader import load_tables
from src.generator import ids
from src.generator.master_data import (
    FAMILIES, GROUP_WEIGHTS, MACHINE_GROUPS, MASTER_LOAD_ORDER,
    allocate_machines, generate_master_data,
)


@pytest.fixture(scope="module", params=["starter", "full"])
def data(request):
    return request.param, generate_master_data(request.param)


def test_volumes_match_profile(data):
    profile, t = data
    vol = VOLUMES[profile]
    assert len(t["product"]) == vol["skus"]
    assert len(t["machine"]) == vol["machines"]
    assert len(t["material"]) == vol["materials"]
    assert len(t["routing"]) == vol["skus"]


def test_every_machine_group_has_a_machine(data):
    _, t = data
    assert {m["machine_group"] for m in t["machine"]} == set(MACHINE_GROUPS)


def test_ids_have_valid_formats_and_are_unique(data):
    _, t = data
    checks = [("product", "sku_id", "sku"), ("machine", "machine_id", "machine"),
              ("material", "material_id", "material"), ("tool", "tool_id", "tool"),
              ("routing", "routing_id", "routing"), ("routing_step", "step_id", "step")]
    for table, column, kind in checks:
        values = [r[column] for r in t[table]]
        assert len(values) == len(set(values)), f"duplicate ids in {table}"
        assert all(ids.is_valid(kind, v) for v in values), f"bad id in {table}"


def test_routing_lengths_are_between_3_and_5(data):
    _, t = data
    lengths = {}
    for s in t["routing_step"]:
        lengths[s["routing_id"]] = lengths.get(s["routing_id"], 0) + 1
    assert set(lengths) == {r["routing_id"] for r in t["routing"]}
    assert all(3 <= n <= 5 for n in lengths.values())


def test_every_step_has_an_eligible_machine_in_the_right_group(data):
    _, t = data
    machine_group = {m["machine_id"]: m["machine_group"] for m in t["machine"]}
    step_group = {s["step_id"]: s["required_machine_group"] for s in t["routing_step"]}
    eligible = {}
    for row in t["routing_step_machine"]:
        eligible.setdefault(row["step_id"], []).append(row)
    assert set(eligible) == set(step_group)
    for step_id, rows in eligible.items():
        for row in rows:
            assert machine_group[row["machine_id"]] == step_group[step_id]
        ranks = sorted(r["preference_rank"] for r in rows)
        assert ranks == list(range(1, len(rows) + 1))


def test_step_durations_fit_inside_a_shift(data):
    _, t = data
    lot = {p["sku_id"]: p["standard_lot_size"] for p in t["product"]}
    routing_sku = {r["routing_id"]: r["sku_id"] for r in t["routing"]}
    for s in t["routing_step"]:
        base = s["fixed_minutes"] + s["std_minutes_per_unit"] * lot[routing_sku[s["routing_id"]]]
        assert 1 <= base <= 301, f"{s['step_id']} base minutes {base}"


def test_bom_covers_every_sku_and_material(data):
    _, t = data
    assert {r["sku_id"] for r in t["product_bom"]} == {p["sku_id"] for p in t["product"]}
    assert {r["material_id"] for r in t["product_bom"]} == {m["material_id"] for m in t["material"]}
    lengths = {}
    for s in t["routing_step"]:
        lengths[s["routing_id"]] = lengths.get(s["routing_id"], 0) + 1
    for row in t["product_bom"]:
        assert 1 <= row["consumed_at_sequence"] <= lengths[ids.routing_id(row["sku_id"])]


def test_changeover_matrix_is_complete_and_sequence_dependent(data):
    _, t = data
    rows = t["changeover_matrix"]
    combos = {(r["machine_group"], r["from_family"], r["to_family"]) for r in rows}
    assert len(rows) == len(combos) == len(MACHINE_GROUPS) * len(FAMILIES) ** 2
    minutes = {(r["machine_group"], r["from_family"], r["to_family"]): r["setup_minutes"]
               for r in rows}
    asymmetric = any(minutes[(g, a, b)] != minutes[(g, b, a)]
                     for g in MACHINE_GROUPS for a in FAMILIES for b in FAMILIES if a < b)
    assert asymmetric


def test_rows_load_into_real_schema_without_violations(data):
    _, t = data
    con = sqlite3.connect(":memory:")
    con.executescript(SCHEMA_SQL.read_text(encoding="utf-8"))
    con.execute("PRAGMA foreign_keys = ON")
    loaded = load_tables(con, t, MASTER_LOAD_ORDER)
    con.commit()
    assert loaded == {name: len(rows) for name, rows in t.items()}
    assert con.execute("PRAGMA foreign_key_check").fetchall() == []
    con.close()


def test_allocate_machines_full_matches_plan():
    assert allocate_machines(40) == GROUP_WEIGHTS
    assert sum(allocate_machines(12).values()) == 12


def test_allocate_machines_rejects_too_few():
    with pytest.raises(ValueError):
        allocate_machines(8)


def test_generation_is_deterministic():
    assert generate_master_data("starter") == generate_master_data("starter")


def test_different_seed_changes_output():
    assert generate_master_data("starter", seed=1) != generate_master_data("starter")