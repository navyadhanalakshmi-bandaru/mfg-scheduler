"""Tests for deterministic ID builders."""
import pytest

from src.generator import ids


def test_known_examples_match_challenge():
    assert ids.sku_id(1) == "SKU-001"
    assert ids.routing_id("SKU-001") == "ROUTE-SKU-001-01"
    assert ids.order_id(1) == "PROD-2026-000001"
    assert ids.operation_id("PROD-2026-000001", 1) == "OP-PROD-2026-000001-01"
    assert ids.machine_id(1) == "MACH-001"
    assert ids.schedule_id(1) == "SCH-2026-000001"


def test_ids_are_deterministic():
    first = [ids.order_id(n) for n in range(1, 501)]
    second = [ids.order_id(n) for n in range(1, 501)]
    assert first == second
    assert len(set(first)) == 500  # all unique


def test_routing_id_follows_schema_rule():
    # schema.sql requires routing_id = 'ROUTE-' || sku_id || '-' || printf('%02d', version_no)
    assert ids.routing_id("SKU-042", 3) == "ROUTE-SKU-042-03"


def test_operation_id_embeds_order_id():
    op = ids.operation_id(ids.order_id(77), 4)
    assert op == "OP-PROD-2026-000077-04"


def test_parse_operation_id_roundtrip():
    order = ids.order_id(500)
    op = ids.operation_id(order, 5)
    assert ids.parse_operation_id(op) == (order, 5)


@pytest.mark.parametrize(
    "builder, value",
    [
        (ids.sku_id, 0),
        (ids.sku_id, 1000),
        (ids.machine_id, 0),
        (ids.machine_id, -1),
        (ids.order_id, 1_000_000),
    ],
)
def test_out_of_range_numbers_rejected(builder, value):
    with pytest.raises(ValueError):
        builder(value)


def test_is_valid_accepts_good_ids():
    assert ids.is_valid("sku", "SKU-100")
    assert ids.is_valid("machine", "MACH-040")
    assert ids.is_valid("operation", "OP-PROD-2026-000500-04")


def test_is_valid_rejects_bad_ids():
    assert not ids.is_valid("sku", "SKU-1")
    assert not ids.is_valid("machine", "mach-001")
    assert not ids.is_valid("order", "PROD-2025-000001")