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


def test_lot_id_matches_schema():
    assert ids.lot_id(1) == "LOT-2026-000001"
    assert ids.is_valid("lot", ids.lot_id(250))


def test_step_id_matches_schema():
    # schema.sql requires step_id = routing_id || '-S' || printf('%02d', sequence_no)
    assert ids.step_id("ROUTE-SKU-001-01", 1) == "ROUTE-SKU-001-01-S01"
    assert ids.is_valid("step", ids.step_id(ids.routing_id("SKU-100"), 6))


def test_tool_id_matches_schema():
    assert ids.tool_id(1) == "TOOL-01"


def test_calendar_id_matches_schema():
    assert ids.calendar_id("MACH-001", "2026-11-02", 1) == "CAL-MACH-001-20261102-S1"
    with pytest.raises(ValueError):
        ids.calendar_id("MACH-001", "2026-11-02", 4)
    with pytest.raises(ValueError):
        ids.calendar_id("MACH-001", "02/11/2026", 1)


def test_downtime_id_matches_schema():
    assert ids.downtime_id(1) == "DT-2026-000001"


@pytest.mark.parametrize(
    "builder, kind, expected",
    [
        (ids.run_id, "run", "RUN-2026-000007"),
        (ids.decision_id, "decision", "PD-2026-000007"),
        (ids.event_id, "event", "EVT-2026-000007"),
        (ids.exception_id, "exception", "EXC-2026-000007"),
        (ids.txn_id, "txn", "ITX-2026-000007"),
        (ids.machine_history_id, "machine_history", "MSH-2026-000007"),
        (ids.order_history_id, "order_history", "OSH-2026-000007"),
    ],
)
def test_serial_ids_match_schema(builder, kind, expected):
    assert builder(7) == expected
    assert ids.is_valid(kind, expected)