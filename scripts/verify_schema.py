"""Verify that the SQL schema files load and that key constraints reject bad data.

Run from the project root:  py scripts\\verify_schema.py
Uses only Python's built-in sqlite3, so no packages are needed yet.
"""
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
MAIN_SQL = ROOT / "docs" / "schema.sql"
GT_SQL = ROOT / "docs" / "schema_ground_truth.sql"

EXPECTED_TABLES = {
    "shift_pattern", "machine", "product", "routing", "routing_step",
    "routing_step_machine", "machine_calendar", "machine_downtime",
    "machine_status_history", "material", "inventory_lot", "product_bom",
    "production_order", "operation", "operation_material_req",
    "changeover_matrix", "tool", "routing_step_tool", "labor_pool",
    "labor_shift_capacity", "schedule_run", "operation_schedule",
    "inventory_transaction", "placement_decision", "schedule_event",
    "scheduling_exception", "order_status_history",
}

results = []


def check(label, ok, detail=""):
    results.append(ok)
    tag = "PASS" if ok else "FAIL"
    suffix = f"  ({detail})" if detail else ""
    print(f"  {tag}  {label}{suffix}")


def ins(con, table, **row):
    cols = ", ".join(row)
    marks = ", ".join("?" for _ in row)
    con.execute(f"INSERT INTO {table} ({cols}) VALUES ({marks})", list(row.values()))


def must_accept(con, label, table, **row):
    try:
        ins(con, table, **row)
    except sqlite3.IntegrityError as exc:
        check(label, False, f"wrongly rejected: {exc}")
    else:
        check(label, True)


def must_reject(con, label, table, **row):
    try:
        ins(con, table, **row)
    except sqlite3.IntegrityError as exc:
        check(label, True, f"rejected: {exc}")
    else:
        check(label, False, "bad row was ACCEPTED")


def main():
    now = "2026-11-01 00:00:00"

    print(f"Loading main schema: {MAIN_SQL}")
    con = sqlite3.connect(":memory:")
    con.execute("PRAGMA foreign_keys = ON")
    con.executescript(MAIN_SQL.read_text(encoding="utf-8"))

    fk_on = con.execute("PRAGMA foreign_keys").fetchone()[0] == 1
    check("foreign key enforcement is ON", fk_on)

    tables = {
        r[0] for r in con.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
        )
    }
    check(f"{len(EXPECTED_TABLES)} expected tables exist", tables == EXPECTED_TABLES,
          f"missing={sorted(EXPECTED_TABLES - tables)} extra={sorted(tables - EXPECTED_TABLES)}")
    check("ground-truth table is NOT in the main database", "anomaly_ground_truth" not in tables)

    # ---- product / routing ----
    product = dict(sku_id="SKU-001", product_name="Test Shaft", product_family="SHAFT",
                   unit_of_measure="EA", standard_lot_size=10, lifecycle_state="ACTIVE",
                   effective_from="2026-01-01", created_at=now)
    must_reject(con, "unknown product family rejected", "product",
                **{**product, "product_family": "BOGUS"})
    must_accept(con, "valid product accepted", "product", **product)

    routing = dict(routing_id="ROUTE-SKU-001-01", sku_id="SKU-001", version_no=1,
                   lifecycle_state="ACTIVE", effective_from="2026-01-01", created_at=now)
    must_reject(con, "routing for unknown SKU rejected (foreign key)", "routing",
                **{**routing, "routing_id": "ROUTE-SKU-999-01", "sku_id": "SKU-999"})
    must_accept(con, "valid routing accepted", "routing", **routing)
    must_reject(con, "second ACTIVE routing for the same SKU rejected", "routing",
                **{**routing, "routing_id": "ROUTE-SKU-001-02", "version_no": 2})
    must_reject(con, "routing ID that disagrees with version_no rejected", "routing",
                **{**routing, "routing_id": "ROUTE-SKU-001-05", "version_no": 2,
                   "lifecycle_state": "DRAFT"})

    # ---- plant ----
    shift = dict(pattern_id="SHP-1S", pattern_name="Single shift", shift_codes="S1",
                 s1_start="06:00", s1_minutes=480, working_weekdays="MON,TUE,WED,THU,FRI")
    must_accept(con, "valid shift pattern accepted", "shift_pattern", **shift)

    machine = dict(machine_id="MACH-001", machine_name="Saw 1", machine_group="CUTTING",
                   status="ACTIVE", default_shift_pattern="SHP-1S",
                   effective_from="2026-01-01", created_at=now)
    must_reject(con, "unknown machine group rejected", "machine",
                **{**machine, "machine_group": "PAINTING"})
    must_accept(con, "valid machine accepted", "machine", **machine)

    step = dict(step_id="ROUTE-SKU-001-01-S01", routing_id="ROUTE-SKU-001-01", sequence_no=1,
                operation_type="CUT", required_machine_group="CUTTING",
                std_minutes_per_unit=2.5, fixed_minutes=15, labor_skill="MACHINIST",
                labor_headcount=1, created_at=now)
    must_accept(con, "valid routing step accepted", "routing_step", **step)
    must_reject(con, "labor headcount of 9 rejected", "routing_step",
                **{**step, "step_id": "ROUTE-SKU-001-01-S02", "sequence_no": 2,
                   "labor_headcount": 9})

    # ---- orders / operations ----
    order = dict(order_id="PROD-2026-000001", sku_id="SKU-001", routing_id="ROUTE-SKU-001-01",
                 quantity=10, priority_class="NORMAL", priority_rank=3,
                 release_time="2026-11-02 06:00:00", due_time="2026-11-10 14:00:00",
                 status="NEW", created_at=now, updated_at=now)
    must_reject(con, "order due before release rejected", "production_order",
                **{**order, "due_time": "2026-11-01 06:00:00"})
    must_reject(con, "URGENT order with priority_rank 3 rejected", "production_order",
                **{**order, "priority_class": "URGENT"})
    must_accept(con, "valid production order accepted", "production_order", **order)

    operation = dict(operation_id="OP-PROD-2026-000001-01", order_id="PROD-2026-000001",
                     step_id="ROUTE-SKU-001-01-S01", sequence_no=1, operation_type="CUT",
                     required_machine_group="CUTTING", quantity=10, base_minutes=40,
                     labor_skill="MACHINIST", labor_headcount=1, status="PENDING",
                     created_at=now, updated_at=now)
    must_reject(con, "operation longer than 450 minutes rejected", "operation",
                **{**operation, "base_minutes": 500})
    must_accept(con, "valid operation accepted", "operation", **operation)

    # ---- runs / schedule rows ----
    run = dict(run_id="RUN-2026-000001", run_type="BASELINE", scheduling_rule="PRIORITY_EDD",
               config_json='{"seed": 20261022}', started_at=now, status="RUNNING")
    must_accept(con, "valid baseline run accepted", "schedule_run", **run)
    must_reject(con, "INJECTED run without a parent run rejected", "schedule_run",
                **{**run, "run_id": "RUN-2026-000002", "run_type": "INJECTED"})

    sched = dict(schedule_id="SCH-2026-000001", run_id="RUN-2026-000001",
                 operation_id="OP-PROD-2026-000001-01", machine_id="MACH-001",
                 setup_start="2026-11-02 06:00:00", setup_minutes=30,
                 start_time="2026-11-02 06:30:00", processing_minutes=60,
                 end_time="2026-11-02 07:30:00", status="PLANNED", created_at=now)
    must_reject(con, "end_time that disagrees with start + processing rejected",
                "operation_schedule", **{**sched, "end_time": "2026-11-02 08:00:00"})
    must_reject(con, "start_time that disagrees with setup_start + setup rejected",
                "operation_schedule", **{**sched, "setup_minutes": 45})
    must_reject(con, "schedule on unknown machine rejected", "operation_schedule",
                **{**sched, "machine_id": "MACH-999"})
    must_accept(con, "valid schedule row accepted", "operation_schedule", **sched)
    must_reject(con, "second schedule row for same run + operation rejected",
                "operation_schedule", **{**sched, "schedule_id": "SCH-2026-000002"})

    fk_problems = con.execute("PRAGMA foreign_key_check").fetchall()
    check("foreign_key_check finds no orphans", fk_problems == [], str(fk_problems))
    integrity = con.execute("PRAGMA integrity_check").fetchone()[0]
    check("integrity_check is ok", integrity == "ok", integrity)
    con.close()

    # ---- ground-truth database ----
    print(f"\nLoading ground-truth schema: {GT_SQL}")
    gt = sqlite3.connect(":memory:")
    gt.executescript(GT_SQL.read_text(encoding="utf-8"))
    gt_tables = {
        r[0] for r in gt.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
        )
    }
    check("ground-truth DB contains exactly one table", gt_tables == {"anomaly_ground_truth"})

    label = dict(gt_id="GT-2026-000001", run_id="RUN-2026-000002", record_entity="operation_schedule",
                 record_id="SCH-2026-000001", related_record_id="SCH-2026-000002",
                 issue_type="OVERLAP", ground_truth=1,
                 description="Moved start 40 min earlier to overlap the previous job",
                 created_at=now)
    must_accept(gt, "valid anomaly label accepted", "anomaly_ground_truth", **label)
    must_reject(gt, "ground_truth=1 with issue_type NONE rejected", "anomaly_ground_truth",
                **{**label, "gt_id": "GT-2026-000002", "record_id": "SCH-2026-000003",
                   "issue_type": "NONE"})
    must_accept(gt, "valid control row (ground_truth=0, NONE) accepted", "anomaly_ground_truth",
                **{**label, "gt_id": "GT-2026-000003", "record_id": "SCH-2026-000004",
                   "related_record_id": None, "issue_type": "NONE", "ground_truth": 0,
                   "description": "Untouched control row"})
    must_reject(gt, "ground_truth=0 with a real issue_type rejected", "anomaly_ground_truth",
                **{**label, "gt_id": "GT-2026-000004", "record_id": "SCH-2026-000005",
                   "ground_truth": 0})
    gt.close()

    passed = sum(results)
    total = len(results)
    print()
    if passed == total:
        print(f"RESULT: ALL {total} CHECKS PASSED")
        return 0
    print(f"RESULT: {total - passed} OF {total} CHECKS FAILED")
    return 1


if __name__ == "__main__":
    sys.exit(main())