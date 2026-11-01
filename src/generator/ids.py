"""Deterministic ID builders and validators.

IDs are pure functions of a number, with no randomness. The same input always
gives the same ID, so IDs are immutable and reproducible.

Formats (from the challenge):
    SKU-001                    product
    ROUTE-SKU-001-01           routing (sku + 2-digit version)
    PROD-2026-000001           production order
    OP-PROD-2026-000001-01     operation (order id + 2-digit sequence)
    MACH-001                   machine
    SCH-2026-000001            schedule row
Assumed (verify against schema.sql):
    MAT-001                    material
    LOT-000001                 inventory lot
"""
import re

ID_YEAR = 2026

_PATTERNS = {
    "sku": re.compile(r"^SKU-\d{3}$"),
    "routing": re.compile(r"^ROUTE-SKU-\d{3}-\d{2}$"),
    "order": re.compile(rf"^PROD-{ID_YEAR}-\d{{6}}$"),
    "operation": re.compile(rf"^OP-PROD-{ID_YEAR}-\d{{6}}-\d{{2}}$"),
    "machine": re.compile(r"^MACH-\d{3}$"),
    "schedule": re.compile(rf"^SCH-{ID_YEAR}-\d{{6}}$"),
    "material": re.compile(r"^MAT-\d{3}$"),
    "lot": re.compile(r"^LOT-\d{6}$"),
}


def _check(n: int, low: int, high: int, name: str) -> int:
    if not isinstance(n, int) or isinstance(n, bool) or not (low <= n <= high):
        raise ValueError(f"{name} must be an integer between {low} and {high}, got {n!r}")
    return n


def sku_id(n: int) -> str:
    return f"SKU-{_check(n, 1, 999, 'sku number'):03d}"


def routing_id(sku: str, version: int = 1) -> str:
    if not is_valid("sku", sku):
        raise ValueError(f"not a valid SKU id: {sku!r}")
    return f"ROUTE-{sku}-{_check(version, 1, 99, 'routing version'):02d}"


def order_id(n: int) -> str:
    return f"PROD-{ID_YEAR}-{_check(n, 1, 999999, 'order number'):06d}"


def operation_id(order: str, seq: int) -> str:
    if not is_valid("order", order):
        raise ValueError(f"not a valid order id: {order!r}")
    return f"OP-{order}-{_check(seq, 1, 99, 'operation sequence'):02d}"


def machine_id(n: int) -> str:
    return f"MACH-{_check(n, 1, 999, 'machine number'):03d}"


def schedule_id(n: int) -> str:
    return f"SCH-{ID_YEAR}-{_check(n, 1, 999999, 'schedule number'):06d}"


def material_id(n: int) -> str:
    return f"MAT-{_check(n, 1, 999, 'material number'):03d}"


def lot_id(n: int) -> str:
    return f"LOT-{_check(n, 1, 999999, 'lot number'):06d}"


def is_valid(kind: str, value: str) -> bool:
    """True if value matches the format for this kind of ID."""
    return bool(_PATTERNS[kind].match(value))


def parse_operation_id(op: str) -> tuple[str, int]:
    """Split an operation id into (order_id, sequence_no)."""
    if not is_valid("operation", op):
        raise ValueError(f"not a valid operation id: {op!r}")
    order, seq = op[3:].rsplit("-", 1)
    return order, int(seq)