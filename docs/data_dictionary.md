# Data Dictionary

## 0. Global conventions

| Topic | Rule |
|---|---|
| IDs | Text primary keys. Generated once, never edited, never reused. Format per table below. |
| Timestamps | ISO-8601 text `YYYY-MM-DD HH:MM:SS`, plant local time, no timezone. Stored as TEXT in SQLite. |
| Dates | `YYYY-MM-DD` text. |
| Durations | Integer **minutes**. Never floats for schedule times. |
| Quantities | Integers for discrete units (EA). Decimals (kg, m) rounded to 3 places. |
| Lifecycle states | `DRAFT -> ACTIVE -> RETIRED`. No backward moves. Retired rows are kept. |
| Effective dating | `effective_from` (required) and `effective_to` (nullable = open ended). `effective_to > effective_from` when set. |
| Changes | Never overwrite history. A change creates a new version row (or an event row). |
| Required | `Y` = NOT NULL. `N` = nullable. |
| Audit columns | Tables that list `created_at` carry it (required). The application always supplies it explicitly and never relies on a default, so generated data stays reproducible. Only `production_order` and `operation` have `updated_at`. Bridge and configuration tables have neither. |
### Enumerations

| Enum | Allowed values |
|---|---|
| product_family | `SHAFT`, `GEAR`, `HOUSING`, `BRACKET`, `VALVE`, `FLANGE`, `PLATE`, `FASTENER` |
| machine_group | `CUTTING`, `TURNING`, `MILLING`, `DRILLING`, `GRINDING`, `HEAT_TREAT`, `WELDING`, `ASSEMBLY`, `INSPECTION` |
| operation_type | `CUT`, `TURN`, `MILL`, `DRILL`, `GRIND`, `HEAT_TREAT`, `WELD`, `ASSEMBLE`, `INSPECT` |
| labor_skill | `MACHINIST`, `WELDER`, `ASSEMBLER`, `INSPECTOR`, `HEAT_TREATER` |
| lifecycle_state | `DRAFT`, `ACTIVE`, `RETIRED` |
| machine_status | `ACTIVE`, `MAINTENANCE`, `RETIRED` |
| downtime_type | `PLANNED_MAINTENANCE`, `BREAKDOWN`, `CALIBRATION`, `HOLIDAY` |

---

## 1. product

One row per SKU version. Master data entity #1.

| Field | Type | Req | Unit | Rules |
|---|---|---|---|---|
| sku_id | TEXT PK | Y | | Format `SKU-001` to `SKU-100`. Immutable. |
| product_name | TEXT | Y | | 3-80 chars. |
| product_family | TEXT | Y | | Enum `product_family`. Drives changeover lookup. |
| unit_of_measure | TEXT | Y | | Always `EA` in this project. |
| standard_lot_size | INTEGER | Y | EA | 1-500. |
| lifecycle_state | TEXT | Y | | Enum `lifecycle_state`. |
| effective_from | TEXT (date) | Y | | |
| effective_to | TEXT (date) | N | | Must be after `effective_from`. |
| created_at | TEXT | Y | | |

Relationships: 1 product to many `routing`, many `product_bom`, many `production_order`.

---

## 2. routing

One row per routing version of a product. Master data entity #2.

| Field | Type | Req | Unit | Rules |
|---|---|---|---|---|
| routing_id | TEXT PK | Y | | Format `ROUTE-<sku_id>-<vv>`, e.g. `ROUTE-SKU-001-01`. |
| sku_id | TEXT FK | Y | | References `product.sku_id`. |
| version_no | INTEGER | Y | | 1, 2, 3... Unique per `sku_id`. Must match the `vv` in the ID. |
| lifecycle_state | TEXT | Y | | Enum. At most one `ACTIVE` routing per SKU at any date. |
| effective_from | TEXT (date) | Y | | |
| effective_to | TEXT (date) | N | | Effective ranges of one SKU must not overlap. |
| created_at | TEXT | Y | | |

Relationships: 1 routing to many `routing_step` (at least 1).

---

## 3. routing_step

The template step in a routing. Each production order copies these into `operation` rows.

| Field | Type | Req | Unit | Rules |
|---|---|---|---|---|
| step_id | TEXT PK | Y | | Format `<routing_id>-S<nn>`, e.g. `ROUTE-SKU-001-01-S01`. |
| routing_id | TEXT FK | Y | | References `routing.routing_id`. |
| sequence_no | INTEGER | Y | | 1..N, contiguous, unique per routing. Defines precedence. |
| operation_type | TEXT | Y | | Enum `operation_type`. |
| required_machine_group | TEXT | Y | | Enum `machine_group`. Every eligible machine must be in this group. |
| std_minutes_per_unit | REAL | Y | min/EA | 0.1-60.0. |
| fixed_minutes | INTEGER | Y | min | 0-120. Per-run overhead, independent of quantity. |
| labor_skill | TEXT | Y | | Enum `labor_skill`. |
| labor_headcount | INTEGER | Y | persons | 1-3. |
| created_at | TEXT | Y | | |

**Derived duration** (calculated, not stored in the master table):
`processing_minutes = ceil((fixed_minutes + std_minutes_per_unit * order_qty) / rate_factor)`
where `rate_factor` comes from `routing_step_machine` for the chosen machine.

---

## 4. routing_step_machine (bridge, many-to-many)

Which machines may run which step.

| Field | Type | Req | Unit | Rules |
|---|---|---|---|---|
| step_id | TEXT FK | Y | | Part of composite PK. |
| machine_id | TEXT FK | Y | | Part of composite PK. Machine group must equal the step's `required_machine_group`. |
| rate_factor | REAL | Y | ratio | 0.5-2.0. 1.0 = standard speed, 2.0 = twice as fast. |
| preference_rank | INTEGER | Y | | 1 = most preferred. Used as a scheduling tie-break. |
| effective_from | TEXT (date) | Y | | |
| effective_to | TEXT (date) | N | | |

Rule: every step has at least 2 eligible machines in the baseline data (so rescheduling after a breakdown is possible), except a few deliberate single-machine steps.

---

## 5. machine

Master data entity #4.

| Field | Type | Req | Unit | Rules |
|---|---|---|---|---|
| machine_id | TEXT PK | Y | | Format `MACH-001` to `MACH-040`. Immutable. |
| machine_name | TEXT | Y | | |
| machine_group | TEXT | Y | | Enum `machine_group`. |
| status | TEXT | Y | | Enum `machine_status`. |
| default_shift_pattern | TEXT FK | Y | | References `shift_pattern.pattern_id`. |
| effective_from | TEXT (date) | Y | | |
| effective_to | TEXT (date) | N | | |
| created_at | TEXT | Y | | |

Capacity: each machine runs **one operation at a time** (capacity = 1).

---

## 6. shift_pattern

Defines the shift design a machine follows. Used to generate dated calendar rows.

| Field | Type | Req | Unit | Rules |
|---|---|---|---|---|
| pattern_id | TEXT PK | Y | | `SHP-1S`, `SHP-2S`, `SHP-3S`. |
| pattern_name | TEXT | Y | | |
| shift_codes | TEXT | Y | | Comma list, e.g. `S1,S2`. |
| s1_start / s1_minutes | TEXT / INTEGER | Y | HH:MM / min | S1 = 06:00, 480. |
| s2_start / s2_minutes | TEXT / INTEGER | N | HH:MM / min | S2 = 14:00, 480. |
| s3_start / s3_minutes | TEXT / INTEGER | N | HH:MM / min | S3 = 22:00, 480. Crosses midnight. |
| working_weekdays | TEXT | Y | | `MON,TUE,WED,THU,FRI`. |

---

## 7. machine_calendar

Dated working windows per machine. Master data entity #5. The scheduler only places work **inside** these windows.

| Field | Type | Req | Unit | Rules |
|---|---|---|---|---|
| calendar_id | TEXT PK | Y | | Format `CAL-<machine_id>-<yyyymmdd>-<shift>`, e.g. `CAL-MACH-001-20261102-S1`. |
| machine_id | TEXT FK | Y | | |
| shift_code | TEXT | Y | | `S1`, `S2`, `S3`. |
| window_start | TEXT (timestamp) | Y | | |
| window_end | TEXT (timestamp) | Y | | Must be after `window_start`. |
| available_minutes | INTEGER | Y | min | **Derived**: equals the minutes between start and end. Validator checks this. |

Rules: windows of one machine must not overlap. Generated for the full 8-week horizon.

---

## 8. machine_downtime

Planned maintenance and breakdowns. Reduces working time inside the calendar.

| Field | Type | Req | Unit | Rules |
|---|---|---|---|---|
| downtime_id | TEXT PK | Y | | Format `DT-<yyyy>-<nnnnnn>`, e.g. `DT-2026-000001`. |
| machine_id | TEXT FK | Y | | |
| downtime_type | TEXT | Y | | Enum `downtime_type`. |
| down_start | TEXT (timestamp) | Y | | |
| down_end | TEXT (timestamp) | Y | | After `down_start`. |
| reason | TEXT | N | | Free text. |
| created_at | TEXT | Y | | |

Rule: downtime rows are append-only. A shortened breakdown is a new row, not an edit.

---

## 9. Additional enumerations

| Enum | Allowed values |
|---|---|
| material_uom | `EA`, `KG`, `M` |
| lot_status | `EXPECTED`, `AVAILABLE`, `EXHAUSTED`, `QUARANTINED` |
| txn_type | `RECEIPT`, `RESERVE`, `CONSUME`, `RELEASE`, `ADJUST` |
| priority_class | `URGENT` (rank 1), `HIGH` (2), `NORMAL` (3), `LOW` (4) |
| order_status | `NEW`, `RELEASED`, `SCHEDULED`, `UNSCHEDULED`, `IN_PROGRESS`, `COMPLETED`, `CANCELLED` |
| operation_status | `PENDING`, `SCHEDULED`, `IN_PROGRESS`, `DONE`, `CANCELLED` |
| run_type | `BASELINE`, `INJECTED`, `RESCHEDULE` |
| run_status | `RUNNING`, `COMPLETED`, `FAILED` |
| feasibility_label | `NOT_VALIDATED`, `FEASIBLE`, `INFEASIBLE_VIOLATIONS` |
| scheduling_rule | `EDD`, `PRIORITY_EDD`, `CRITICAL_RATIO` |
| schedule_status | `PLANNED`, `LOCKED`, `SUPERSEDED` |
| event_type | `PLACED`, `MOVED`, `LOCKED`, `UNSCHEDULED`, `REMOVED` |
| exception_category | `INFEASIBLE_ORDER`, `CONSTRAINT_VIOLATION` |
| infeasible_code | `NO_ELIGIBLE_MACHINE`, `MATERIAL_UNAVAILABLE`, `NO_CAPACITY_IN_HORIZON`, `LABOR_UNAVAILABLE`, `TOOL_UNAVAILABLE` |
| violation_code | `MACHINE_OVERLAP`, `PRECEDENCE_VIOLATION`, `MATERIAL_BEFORE_AVAILABILITY`, `OUTSIDE_SHIFT`, `MISSING_CHANGEOVER`, `INELIGIBLE_MACHINE`, `LABOR_CAPACITY_EXCEEDED`, `TOOL_CAPACITY_EXCEEDED` |
| severity | `HARD`, `WARNING` |
| detected_by | `ENGINE`, `VALIDATOR` |
| issue_type (ground truth) | `OVERLAP`, `PRECEDENCE`, `MATERIAL_EARLY`, `OUTSIDE_SHIFT`, `MISSING_CHANGEOVER`, `NONE` |

---

## 10. material

Master data entity #6.

| Field | Type | Req | Unit | Rules |
|---|---|---|---|---|
| material_id | TEXT PK | Y | | Format `MAT-001` to `MAT-200`. Immutable. |
| material_name | TEXT | Y | | 3-80 chars. |
| unit_of_measure | TEXT | Y | | Enum `material_uom`. |
| lead_time_days | INTEGER | Y | days | 0-60. |
| lifecycle_state | TEXT | Y | | Enum. |
| effective_from / effective_to | TEXT (date) | Y / N | | |
| created_at | TEXT | Y | | |

---

## 11. inventory_lot

Master data entity #7. One row per received batch of a material.

| Field | Type | Req | Unit | Rules |
|---|---|---|---|---|
| lot_id | TEXT PK | Y | | Format `LOT-2026-000001`. |
| material_id | TEXT FK | Y | | |
| quantity_received | REAL | Y | material UoM | > 0. |
| available_from | TEXT (timestamp) | Y | | Material cannot be reserved or consumed before this moment. |
| status | TEXT | Y | | Enum `lot_status`. |
| created_at | TEXT | Y | | |

**Derived (not stored):** `quantity_remaining = quantity_received + ADJUST - CONSUME` (reservations tracked separately). Must never be negative.

---

## 12. inventory_transaction (append-only)

Every stock movement is a new row. Rows are never edited or deleted.

| Field | Type | Req | Unit | Rules |
|---|---|---|---|---|
| txn_id | TEXT PK | Y | | Format `ITX-2026-000001`. |
| lot_id | TEXT FK | Y | | |
| txn_type | TEXT | Y | | Enum `txn_type`. |
| quantity | REAL | Y | material UoM | Signed. `RECEIPT` > 0, `CONSUME` < 0, `RESERVE` < 0, `RELEASE` > 0. |
| operation_id | TEXT FK | N | | Required for `RESERVE`, `CONSUME`, `RELEASE`. |
| run_id | TEXT FK | N | | The schedule run that created it. |
| txn_time | TEXT (timestamp) | Y | | For `CONSUME`: must be >= the lot's `available_from`. |
| created_at | TEXT | Y | | |

---

## 13. product_bom (bridge, many-to-many)

Which materials a product consumes.

| Field | Type | Req | Unit | Rules |
|---|---|---|---|---|
| sku_id | TEXT FK | Y | | Composite PK part. |
| material_id | TEXT FK | Y | | Composite PK part. |
| qty_per_unit | REAL | Y | material UoM / EA | > 0. |
| consumed_at_sequence | INTEGER | Y | | Which routing step consumes it. Default 1. Must exist in the SKU's routing. |
| effective_from / effective_to | TEXT (date) | Y / N | | |

---

## 14. production_order

Master data entity #8.

| Field | Type | Req | Unit | Rules |
|---|---|---|---|---|
| order_id | TEXT PK | Y | | Format `PROD-2026-000001` to `PROD-2026-000500`. |
| sku_id | TEXT FK | Y | | |
| routing_id | TEXT FK | Y | | Must be the `ACTIVE` routing of the SKU at `release_time`. |
| quantity | INTEGER | Y | EA | 1-500. |
| priority_class | TEXT | Y | | Enum `priority_class`. |
| priority_rank | INTEGER | Y | | 1-4. **Derived** from `priority_class`. |
| release_time | TEXT (timestamp) | Y | | Earliest the order may start. |
| due_time | TEXT (timestamp) | Y | | Must be after `release_time`. Must allow at least the order's total base processing time. |
| status | TEXT | Y | | Enum `order_status`. |
| created_at / updated_at | TEXT | Y | | |

**Lifecycle transitions (anything else is rejected):**

| From | Allowed next states |
|---|---|
| NEW | RELEASED, CANCELLED |
| RELEASED | SCHEDULED, UNSCHEDULED, CANCELLED |
| UNSCHEDULED | SCHEDULED, CANCELLED |
| SCHEDULED | IN_PROGRESS, UNSCHEDULED, CANCELLED |
| IN_PROGRESS | COMPLETED, CANCELLED |
| COMPLETED, CANCELLED | none (final) |

---

## 15. operation

One row per routing step per order. The 2,000 scheduled operations are these rows. (500 orders with 4 steps each gives exactly 2,000.)

| Field | Type | Req | Unit | Rules |
|---|---|---|---|---|
| operation_id | TEXT PK | Y | | Format `OP-<order_id>-<nn>`, e.g. `OP-PROD-2026-000001-01`. |
| order_id | TEXT FK | Y | | |
| step_id | TEXT FK | Y | | The routing step it was copied from. |
| sequence_no | INTEGER | Y | | Same as the step. Operation `n` cannot start before operation `n-1` of the same order ends. The `nn` in the ID must equal this. |
| operation_type | TEXT | Y | | Copied from step. |
| required_machine_group | TEXT | Y | | Copied from step. |
| quantity | INTEGER | Y | EA | Equals the order quantity. |
| base_minutes | INTEGER | Y | min | **Derived**: `ceil(fixed_minutes + std_minutes_per_unit * quantity)` at rate 1.0. Must be at most 450 so it fits inside one 480 minute shift together with a changeover. |
| labor_skill | TEXT | Y | | Copied from step. |
| labor_headcount | INTEGER | Y | persons | Copied from step. |
| status | TEXT | Y | | Enum `operation_status`. |
| created_at / updated_at | TEXT | Y | | |

---

## 16. operation_material_req

Material needed by a specific operation.

| Field | Type | Req | Unit | Rules |
|---|---|---|---|---|
| req_id | TEXT PK | Y | | Format `REQ-<operation_id>-<mmm>`, e.g. `REQ-OP-PROD-2026-000001-01-001`. |
| operation_id | TEXT FK | Y | | The operation whose sequence equals the BOM's `consumed_at_sequence`. |
| material_id | TEXT FK | Y | | |
| required_qty | REAL | Y | material UoM | **Derived**: `qty_per_unit * order quantity`. |

---

## 17. changeover_matrix

Master data entity #10. Setup time when a machine switches from one product family to another.

| Field | Type | Req | Unit | Rules |
|---|---|---|---|---|
| changeover_id | TEXT PK | Y | | Format `CHG-<machine_group>-<from>-<to>`, e.g. `CHG-MILLING-GEAR-HOUSING`. |
| machine_group | TEXT | Y | | Enum. |
| from_family | TEXT | Y | | Enum `product_family`. |
| to_family | TEXT | Y | | Enum `product_family`. |
| setup_minutes | INTEGER | Y | min | 0-180. Same family to same family is 0-10. |
| effective_from / effective_to | TEXT (date) | Y / N | | |

Rules: unique on (`machine_group`, `from_family`, `to_family`). The matrix must be **complete** (8 x 8 = 64 rows per group, 576 rows total) and **asymmetric** (A to B need not equal B to A). The first job on a machine in the horizon has setup 0.

---

## 18. Tooling and labor

### tool
| Field | Type | Req | Rules |
|---|---|---|---|
| tool_id | TEXT PK | Y | Format `TOOL-01` to `TOOL-12`. |
| tool_name | TEXT | Y | |
| units_available | INTEGER | Y | 1-3. One unit is used by one operation at a time. |
| status | TEXT | Y | `ACTIVE` or `RETIRED`. |

### routing_step_tool (bridge)
| Field | Type | Req | Rules |
|---|---|---|---|
| step_id | TEXT FK | Y | Composite PK part. |
| tool_id | TEXT FK | Y | Composite PK part. |
| units_required | INTEGER | Y | Usually 1. |

### labor_pool
| Field | Type | Req | Rules |
|---|---|---|---|
| pool_id | TEXT PK | Y | Format `LAB-<skill>`, e.g. `LAB-MACHINIST`. |
| labor_skill | TEXT | Y | Enum `labor_skill`. Unique. |

### labor_shift_capacity
| Field | Type | Req | Rules |
|---|---|---|---|
| pool_id | TEXT FK | Y | Composite PK part. |
| work_date | TEXT (date) | Y | Composite PK part. |
| shift_code | TEXT | Y | Composite PK part. `S1`, `S2`, `S3`. |
| headcount_available | INTEGER | Y | 0-30. Operators in use during a shift must never exceed this. |

---

## 19. schedule_run (append-only)

One row per engine run, anomaly injection, or reschedule.

| Field | Type | Req | Rules |
|---|---|---|---|
| run_id | TEXT PK | Y | Format `RUN-2026-000001`. |
| run_type | TEXT | Y | Enum `run_type`. `BASELINE` = engine output. `INJECTED` = corrupted copy. `RESCHEDULE` = corrective run. |
| parent_run_id | TEXT FK | N | Required for `INJECTED` and `RESCHEDULE`. Points to the run it came from. |
| scheduling_rule | TEXT | Y | Enum `scheduling_rule`. |
| config_json | TEXT | Y | Exact settings used, so a run can be reproduced. |
| trigger_time | TEXT (timestamp) | N | Reschedule only. Operations starting before it are locked. |
| trigger_reason | TEXT | N | e.g. `MACH-007 breakdown`. |
| started_at / finished_at | TEXT (timestamp) | Y / N | |
| status | TEXT | Y | Enum `run_status`. |
| feasibility_label | TEXT | Y | Enum. Starts as `NOT_VALIDATED`. **Only the validator may set `FEASIBLE`**, and only when it finds zero hard violations. |
| metrics_json | TEXT | N | Snapshot of the 11 metrics at run end. |

---

## 20. operation_schedule

Master data entity #9. One row per operation per run.

| Field | Type | Req | Unit | Rules |
|---|---|---|---|---|
| schedule_id | TEXT PK | Y | | Format `SCH-2026-000001`. |
| run_id | TEXT FK | Y | | |
| operation_id | TEXT FK | Y | | Unique together with `run_id`. |
| machine_id | TEXT FK | Y | | Must be in `routing_step_machine` for the step. |
| setup_start | TEXT (timestamp) | Y | | Equals `start_time` when `setup_minutes` = 0. |
| setup_minutes | INTEGER | Y | min | Changeover applied. Validator recomputes the required value from `changeover_matrix`. |
| start_time | TEXT (timestamp) | Y | | |
| processing_minutes | INTEGER | Y | min | **Derived**: `ceil(base_minutes / rate_factor)`. |
| end_time | TEXT (timestamp) | Y | | **Derived**: `start_time + processing_minutes`. |
| previous_schedule_id | TEXT FK | N | | The job before this one on the same machine. Null for the first job. |
| status | TEXT | Y | | Enum `schedule_status`. |
| created_at | TEXT | Y | | |

Rules: `setup_start + setup_minutes = start_time`. Setup and processing must lie inside **one** working window, with no downtime inside it.

---

## 21. placement_decision (append-only)

The "why this machine" explanation.

| Field | Type | Req | Rules |
|---|---|---|---|
| decision_id | TEXT PK | Y | Format `PD-2026-000001`. |
| run_id | TEXT FK | Y | |
| operation_id | TEXT FK | Y | |
| chosen_machine_id | TEXT FK | N | Null when the operation could not be placed. |
| candidates_json | TEXT | Y | Every eligible machine tried, with earliest start, setup, end, and rejection reason if any. |
| rule_applied | TEXT | Y | e.g. `EARLIEST_COMPLETION`, tie-break used. |
| explanation | TEXT | Y | Plain-English sentence shown in the UI. |
| created_at | TEXT | Y | |

---

## 22. schedule_event (append-only)

| Field | Type | Req | Rules |
|---|---|---|---|
| event_id | TEXT PK | Y | Format `EVT-2026-000001`. |
| run_id | TEXT FK | Y | |
| event_type | TEXT | Y | Enum `event_type`. |
| order_id / operation_id | TEXT FK | N / N | |
| old_value_json / new_value_json | TEXT | N | Before and after, for `MOVED`. |
| event_time | TEXT (timestamp) | Y | |

---

## 23. scheduling_exception

Unscheduled orders and constraint violations, each with an explanation.

| Field | Type | Req | Rules |
|---|---|---|---|
| exception_id | TEXT PK | Y | Format `EXC-2026-000001`. |
| run_id | TEXT FK | Y | |
| category | TEXT | Y | Enum `exception_category`. |
| code | TEXT | Y | An `infeasible_code` or `violation_code`, matching the category. |
| severity | TEXT | Y | Enum. Violations are `HARD`. |
| order_id | TEXT FK | N | |
| operation_id | TEXT FK | N | |
| machine_id | TEXT FK | N | |
| message | TEXT | Y | Plain-English explanation. |
| detected_by | TEXT | Y | `ENGINE` for infeasible orders, `VALIDATOR` for violations. |
| created_at | TEXT | Y | |

---

## 24. History tables (append-only)

### order_status_history
| Field | Type | Req | Rules |
|---|---|---|---|
| history_id | TEXT PK | Y | Format `OSH-2026-000001`. |
| order_id | TEXT FK | Y | |
| from_status / to_status | TEXT | N / Y | Must follow the lifecycle table in section 14. |
| changed_at | TEXT (timestamp) | Y | |
| run_id | TEXT FK | N | |
| reason | TEXT | N | |

### machine_status_history
| Field | Type | Req | Rules |
|---|---|---|---|
| history_id | TEXT PK | Y | Format `MSH-2026-000001`. |
| machine_id | TEXT FK | Y | |
| from_status / to_status | TEXT | N / Y | Enum `machine_status`. |
| changed_at | TEXT (timestamp) | Y | |
| reason | TEXT | N | |

---

## 25. anomaly_ground_truth (SEPARATE DATABASE)

Stored in `data/ground_truth/ground_truth.db` and exported as `ground_truth.csv`. **The scheduling engine never opens this file.** It has no foreign keys. It refers to other tables by ID text only.

| Field | Type | Req | Rules |
|---|---|---|---|
| gt_id | TEXT PK | Y | Format `GT-2026-000001`. |
| run_id | TEXT | Y | The `INJECTED` run. |
| record_entity | TEXT | Y | Always `operation_schedule`. |
| record_id | TEXT | Y | The affected `schedule_id`. |
| related_record_id | TEXT | N | Second row involved, e.g. the other job in an overlap. |
| issue_type | TEXT | Y | Enum `issue_type`. |
| ground_truth | INTEGER | Y | 1 = injected anomaly. 0 = untouched control row (`issue_type` = `NONE`). Control rows let us measure false positives. |
| description | TEXT | Y | What was changed and by how much. |
| created_at | TEXT | Y | |

---

## 26. ID format summary

| Entity | Format | Example |
|---|---|---|
| Product | `SKU-###` | `SKU-001` |
| Routing | `ROUTE-<sku>-##` | `ROUTE-SKU-001-01` |
| Routing step | `<routing_id>-S##` | `ROUTE-SKU-001-01-S01` |
| Machine | `MACH-###` | `MACH-001` |
| Calendar | `CAL-<machine>-<yyyymmdd>-<shift>` | `CAL-MACH-001-20261102-S1` |
| Downtime | `DT-2026-######` | `DT-2026-000001` |
| Material | `MAT-###` | `MAT-001` |
| Inventory lot | `LOT-2026-######` | `LOT-2026-000001` |
| Inventory txn | `ITX-2026-######` | `ITX-2026-000001` |
| Production order | `PROD-2026-######` | `PROD-2026-000001` |
| Operation | `OP-<order_id>-##` | `OP-PROD-2026-000001-01` |
| Material req | `REQ-<operation_id>-###` | `REQ-OP-PROD-2026-000001-01-001` |
| Changeover | `CHG-<group>-<from>-<to>` | `CHG-MILLING-GEAR-HOUSING` |
| Tool / labor pool | `TOOL-##` / `LAB-<skill>` | `TOOL-01`, `LAB-MACHINIST` |
| Schedule run | `RUN-2026-######` | `RUN-2026-000001` |
| Operation schedule | `SCH-2026-######` | `SCH-2026-000001` |
| Placement decision | `PD-2026-######` | `PD-2026-000001` |
| Event / exception | `EVT-2026-######` / `EXC-2026-######` | |
| Status history | `OSH-2026-######` / `MSH-2026-######` | |
| Ground truth | `GT-2026-######` | `GT-2026-000001` |

---

## 27. Cross-table validation rules

These feed `validation/data_quality.py` (data checks) and `validation/constraints.py` (schedule checks).

| # | Category | Rule |
|---|---|---|
| 1 | Primary key uniqueness | No duplicate PK in any table. |
| 2 | Foreign key integrity | Every FK points to an existing parent. |
| 3 | Enumerations | Every enum column holds an allowed value. |
| 4 | Date ordering | `effective_to > effective_from`, `due_time > release_time`, `window_end > window_start`, `down_end > down_start`. |
| 5 | Operation precedence | For each order, operation `n` starts at or after operation `n-1` ends. |
| 6 | Machine availability | Each placed block lies inside one calendar window and overlaps no downtime. |
| 7 | Inventory reconciliation | Per lot: received + adjustments - consumed >= 0, and no consumption before `available_from`. |
| 8 | Lifecycle transitions | Every status history row follows the allowed transitions. |
| 9 | Duplicates | No duplicate (`run_id`, `operation_id`); no duplicate natural keys such as (`sku_id`, `version_no`). |
| 10 | Derived consistency | `end_time`, `available_minutes`, `base_minutes`, `required_qty`, `priority_rank` equal their recomputed values. |
| 11 | Schedule constraints | No machine overlap, correct changeover, eligible machine, labor and tool capacity respected, material available at start. |

