"""Deterministic ID builders and validators.

IDs are pure functions of numbers, with no randomness. The same input always
gives the same ID, so IDs are immutable and reproducible.

Every format below matches a GLOB check in docs/schema.sql.
    SKU-001                      product
    ROUTE-SKU-001-01             routing (sku + 2-digit version)
    ROUTE-SKU-001-01-S01         routing step (routing + sequence)
    PROD-2026-000001             production order
    OP-PROD-2026-000001-01       operation (order id + 2-digit sequence)
    MACH-001                     machine
    MAT-001                      material
    LOT-2026-000001              inventory lot
    TOOL-01                      tool
    CAL-MACH-001-20261102-S1     machine calendar window
    DT-2026-000001               machine downtime
    RUN-2026-000001              schedule run
    SCH-2026-000001              schedule row
    PD-2026-000001               placement decision
    EVT-2026-000001              schedule event
    EXC-2026-000001              scheduling exception
    ITX-2026-000001              inventory transaction
    MSH-2026-000001              machine status history
    OSH-2026-000001              order status history
"""
import re

ID_YEAR = 2026

_PATTERNS = {
    "sku": re.compile(r"^SKU-\d{3}$"),
    "routing": re.compile(r"^ROUTE-SKU-\d{3}-\d{2}$"),
    "step": re.compile(r"^ROUTE-SKU-\d{3}-\d{2}-S\d{2}$"),
    "order": re.compile(rf"^PROD-{ID_YEAR}-\d{{6}}$"),
    "operation": re.compile(rf"^OP-PROD-{ID_YEAR}-\d{{6}}-\d{{2}}$"),
    "machine": re.compile(r"^MACH-\d{3}$"),
    "material": re.compile(r"^MAT-\d{3}$"),
    "lot": re.compile(rf"^LOT-{ID_YEAR}-\d{{6}}$"),
    "tool": re.compile(r"^TOOL-\d{2}$"),
    "calendar": re.compile(r"^CAL-MACH-\d{3}-\d{8}-S[1-3]$"),
    "downtime": re.compile(rf"^DT-{ID_YEAR}-\d{{6}}$"),
    "run": re.compile(rf"^RUN-{ID_YEAR}-\d{{6}}$"),
    "schedule": re.compile(rf"^SCH-{ID_YEAR}-\d{{6}}$"),
    "decision": re.compile(rf"^PD-{ID_YEAR}-\d{{6}}$"),
    "event": re.compile(rf"^EVT-{ID_YEAR}-\d{{6}}$"),
    "exception": re.compile(rf"^EXC-{ID_YEAR}-\d{{6}}$"),
    "txn": re.compile(rf"^ITX-{ID_YEAR}-\d{{6}}$"),
    "machine_history": re.compile(rf"^MSH-{ID_YEAR}-\d{{6}}$"),
    "order_history": re.compile(rf"^OSH-{ID_YEAR}-\d{{6}}$"),
}

_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def _check(n: int, low: int, high: int, name: str) -> int:
    if not isinstance(n, int) or isinstance(n, bool) or not (low <= n <= high):
        raise ValueError(f"{name} must be an integer between {low} and {high}, got {n!r}")
    return n


def _serial(prefix: str, n: int, name: str) -> str:
    return f"{prefix}-{ID_YEAR}-{_check(n, 1, 999999, name):06d}"


def sku_id(n: int) -> str:
    return f"SKU-{_check(n, 1, 999, 'sku number'):03d}"


def routing_id(sku: str, version: int = 1) -> str:
    if not is_valid("sku", sku):
        raise ValueError(f"not a valid SKU id: {sku!r}")
    return f"ROUTE-{sku}-{_check(version, 1, 99, 'routing version'):02d}"


def step_id(routing: str, seq: int) -> str:
    if not is_valid("routing", routing):
        raise ValueError(f"not a valid routing id: {routing!r}")
    return f"{routing}-S{_check(seq, 1, 99, 'step sequence'):02d}"


def order_id(n: int) -> str:
    return _serial("PROD", n, "order number")


def operation_id(order: str, seq: int) -> str:
    if not is_valid("order", order):
        raise ValueError(f"not a valid order id: {order!r}")
    return f"OP-{order}-{_check(seq, 1, 99, 'operation sequence'):02d}"


def machine_id(n: int) -> str:
    return f"MACH-{_check(n, 1, 999, 'machine number'):03d}"


def material_id(n: int) -> str:
    return f"MAT-{_check(n, 1, 999, 'material number'):03d}"


def lot_id(n: int) -> str:
    return _serial("LOT", n, "lot number")


def tool_id(n: int) -> str:
    return f"TOOL-{_check(n, 1, 99, 'tool number'):02d}"


def calendar_id(machine: str, work_date: str, shift: int) -> str:
    """work_date is 'YYYY-MM-DD'; shift is 1, 2 or 3."""
    if not is_valid("machine", machine):
        raise ValueError(f"not a valid machine id: {machine!r}")
    if not _DATE.match(work_date):
        raise ValueError(f"work_date must be YYYY-MM-DD, got {work_date!r}")
    return f"CAL-{machine}-{work_date.replace('-', '')}-S{_check(shift, 1, 3, 'shift')}"


def downtime_id(n: int) -> str:
    return _serial("DT", n, "downtime number")


def run_id(n: int) -> str:
    return _serial("RUN", n, "run number")


def schedule_id(n: int) -> str:
    return _serial("SCH", n, "schedule number")


def decision_id(n: int) -> str:
    return _serial("PD", n, "decision number")


def event_id(n: int) -> str:
    return _serial("EVT", n, "event number")


def exception_id(n: int) -> str:
    return _serial("EXC", n, "exception number")


def txn_id(n: int) -> str:
    return _serial("ITX", n, "transaction number")


def machine_history_id(n: int) -> str:
    return _serial("MSH", n, "machine history number")


def order_history_id(n: int) -> str:
    return _serial("OSH", n, "order history number")


def is_valid(kind: str, value: str) -> bool:
    """True if value matches the format for this kind of ID."""
    return bool(_PATTERNS[kind].match(value))


def parse_operation_id(op: str) -> tuple[str, int]:
    """Split an operation id into (order_id, sequence_no)."""
    if not is_valid("operation", op):
        raise ValueError(f"not a valid operation id: {op!r}")
    order, seq = op[3:].rsplit("-", 1)
    return order, int(seq)