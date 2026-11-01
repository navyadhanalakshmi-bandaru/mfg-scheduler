# API Endpoint Contract

FastAPI app, base path `/api/v1`. Interactive docs are served at `/docs`.
All timestamps are `YYYY-MM-DD HH:MM:SS` plant-local text. All durations are minutes.

## Rules

1. Endpoints call the **service layer**. Streamlit calls the same services directly.
2. Only `/anomalies/evaluation` and `/anomalies/inject` touch the ground-truth database. No scheduling endpoint ever reads it.
3. Only `POST /runs/{run_id}/validate` can set a run's `feasibility_label` to `FEASIBLE`.
4. Order status changes go through `PATCH /orders/{order_id}/status`, which enforces the lifecycle table in the data dictionary.
5. Nothing is deleted. History tables and past runs are append-only.

## Error format (all errors)

```json
{ "error": "VALIDATION_ERROR", "message": "due_time must be after release_time", "details": {"field": "due_time"} }
```

| HTTP | `error` value | Meaning |
|---|---|---|
| 400 | `VALIDATION_ERROR` | Bad input value or format |
| 404 | `NOT_FOUND` | ID does not exist |
| 409 | `CONFLICT` | Illegal lifecycle transition, duplicate ID, or run already running |
| 422 | `UNPROCESSABLE` | Request is well-formed but breaks a business rule |
| 500 | `INTERNAL_ERROR` | Unexpected failure (logged) |

## 1. System and data pipeline

| Method | Path | Purpose | Key inputs | Returns |
|---|---|---|---|---|
| GET | `/health` | Liveness check | none | `{status, db_ok, version}` |
| POST | `/data/generate` | Run the synthetic generator | `profile` = `starter` or `full` | Row counts per table, checksums |
| POST | `/data/load` | Load generated files into SQLite | `profile` | Rows loaded per table |
| POST | `/data/validate` | Run the 11 data-quality checks | none | List of checks with pass/fail and offending IDs |
| GET | `/data/manifest` | Row counts and SHA-256 per exported file | none | Manifest JSON |
| GET | `/data/dictionary` | Data dictionary as JSON | none | Tables, fields, types |

The seed is fixed at 20261022 and cannot be changed through the API.

## 2. Master data (read-only except where noted)

| Method | Path | Purpose | Filters |
|---|---|---|---|
| GET | `/products` | List SKUs | `family`, `lifecycle_state` |
| GET | `/products/{sku_id}` | One SKU with its active routing, steps, eligible machines, BOM | none |
| GET | `/routings/{routing_id}` | Routing with ordered steps | none |
| GET | `/machines` | List machines | `group`, `status` |
| GET | `/machines/{machine_id}` | One machine with shift pattern and status history | none |
| GET | `/machines/{machine_id}/calendar` | Working windows | `from`, `to` (dates) |
| GET | `/machines/{machine_id}/downtime` | Downtime rows | `from`, `to` |
| POST | `/machines/{machine_id}/downtime` | Record a breakdown or maintenance (append-only). Used to trigger rescheduling | `downtime_type`, `down_start`, `down_end`, `reason` |
| GET | `/materials` | List materials | `uom`, `lifecycle_state` |
| GET | `/materials/availability` | Per material: received, reserved, consumed, remaining, next arrival, and which operations wait on it | `run_id`, `material_id` |
| GET | `/inventory-lots` | Lots | `material_id`, `status` |
| GET | `/inventory-transactions` | Stock movements | `lot_id`, `run_id`, `txn_type` |
| GET | `/changeover-matrix` | Setup minutes | `machine_group`, `from_family`, `to_family` |
| GET | `/tools` | Tools and unit counts | none |
| GET | `/labor/capacity` | Headcount per skill, date, shift | `skill`, `from`, `to` |

## 3. Production orders

| Method | Path | Purpose | Notes |
|---|---|---|---|
| GET | `/orders` | List orders | Filters: `status`, `priority_class`, `sku_id`, `due_before`, `late_in_run` (run_id). Paging: `limit`, `offset` |
| GET | `/orders/{order_id}` | One order with its operations | none |
| POST | `/orders` | Create an order (for example a rush order) | Generates `PROD-2026-######`, copies routing steps into `operation` rows and BOM into `operation_material_req`. Status starts `NEW` |
| PATCH | `/orders/{order_id}/status` | Change status | 409 if the transition is not allowed. Writes `order_status_history` |
| GET | `/orders/{order_id}/operations` | Operations with the schedule rows of a run | `run_id` |
| GET | `/orders/{order_id}/explanation` | Why this order was scheduled where it was, or why it is unscheduled | `run_id` |

## 4. Scheduling runs

| Method | Path | Purpose | Notes |
|---|---|---|---|
| POST | `/runs` | Start the scheduling engine | Body: `scheduling_rule` (`EDD`, `PRIORITY_EDD`, `CRITICAL_RATIO`), optional `order_ids`, optional `horizon_start`, `horizon_end`. Creates a `BASELINE` run. 409 if a run is already `RUNNING` |
| GET | `/runs` | List runs | Filters: `run_type`, `status`, `feasibility_label` |
| GET | `/runs/{run_id}` | Run details, config, metrics snapshot | none |
| GET | `/runs/{run_id}/schedule` | All `operation_schedule` rows (Gantt data) | Filters: `machine_id`, `order_id`, `from`, `to` |
| GET | `/runs/{run_id}/metrics` | The 11 metrics, each with its inputs | Computed from schedule rows, never typed in |
| POST | `/runs/{run_id}/validate` | Independent constraint check | Writes `CONSTRAINT_VIOLATION` rows to `scheduling_exception`. Sets `feasibility_label`: `FEASIBLE` only if zero hard violations |
| GET | `/runs/{run_id}/exceptions` | Unscheduled orders and violations | Filters: `category`, `code`, `severity`, `order_id`, `machine_id` |
| GET | `/runs/{run_id}/events` | `schedule_event` log | `event_type` |
| GET | `/runs/{run_id}/decisions/{operation_id}` | The `placement_decision`: every candidate machine tried, chosen one, plain-English reason | none |
| POST | `/runs/{run_id}/reschedule` | Corrective run | Body: `trigger_time`, `trigger_reason`, optional `new_downtime_ids`, `new_order_ids`. Operations starting before `trigger_time` are locked. Creates a `RESCHEDULE` run with `parent_run_id` |
| GET | `/runs/compare` | Compare two runs | Query: `base_run_id`, `other_run_id`. Returns moved operations, tardiness delta, changeover delta, newly unscheduled orders |

## 5. Anomaly injection and evaluation

These are the only endpoints allowed to open the ground-truth database.

| Method | Path | Purpose | Notes |
|---|---|---|---|
| POST | `/anomalies/inject` | Copy a valid baseline run and corrupt chosen rows | Body: `base_run_id`, `counts` per issue type (`OVERLAP`, `PRECEDENCE`, `MATERIAL_EARLY`, `OUTSIDE_SHIFT`, `MISSING_CHANGEOVER`), `control_rows`. Creates an `INJECTED` run and writes labels to the ground-truth DB. Unrelated rows stay unchanged |
| GET | `/anomalies/evaluation` | Compare validator findings with ground truth | Query: `run_id`. Returns per issue type: injected, detected, missed, false positives (using control rows), precision, recall |
| GET | `/anomalies/labels/export` | Download `ground_truth.csv` | Evaluation use only |

## 6. Export and reporting

| Method | Path | Output |
|---|---|---|
| GET | `/export/schedule.csv` | Schedule rows for a run (`run_id`) |
| GET | `/export/exceptions.csv` | Exceptions for a run |
| GET | `/export/metrics.json` | Metrics for a run |
| GET | `/export/orders.csv` | Orders with scheduled start, end, tardiness |
| GET | `/export/report.html` | One-page summary report for a run |

## 7. UI page to endpoint map

| Streamlit page | Main endpoints |
|---|---|
| 1 Dashboard | `/runs/{id}/metrics`, `/runs/{id}/exceptions` |
| 2 Production orders | `/orders`, `/orders/{id}/explanation`, `POST /orders` |
| 3 Master data | `/products`, `/machines`, `/materials`, `/changeover-matrix` |
| 4 Machine calendars | `/machines/{id}/calendar`, `/machines/{id}/downtime` |
| 5 Gantt | `/runs/{id}/schedule`, `/runs/{id}/decisions/{op}` |
| 6 Materials | `/materials/availability`, `/inventory-lots` |
| 7 Engine controls | `POST /runs`, `POST /runs/{id}/validate`, `/data/*` |
| 8 Exceptions | `/runs/{id}/exceptions`, `/anomalies/evaluation` |
| 9 Compare and reschedule | `/runs/compare`, `POST /runs/{id}/reschedule`, `POST /machines/{id}/downtime` |
| 10 Export | `/export/*` |