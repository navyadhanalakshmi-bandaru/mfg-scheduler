# Manufacturing Production Scheduling System: Project Plan

Stack: Python 3.13 (use the `py` launcher), SQLite + SQLAlchemy, FastAPI, Streamlit, pandas, Plotly, pytest.
Seed: 20261022. Work one phase at a time.

---

## 1. Business Problem

A factory receives **production orders** (product, quantity, due date, priority). Each product has a **routing**: an ordered list of operations (e.g. cut, drill, heat-treat, assemble, inspect). Each operation can run only on certain **machines**, and each machine works only during its **shift calendar**, minus **downtime**.

For every operation the scheduler decides **which machine** and **what start time**, so that:

- The operation starts only after its predecessor finishes.
- The needed material is physically available.
- The machine is free and inside a working window.
- The sequence-dependent **changeover** from the previous job on that machine is accounted for.
- Enough **operators** and **tools** are available.

Some orders cannot be scheduled (no eligible machine, material never arrives, no capacity). The system must say **why**. Urgent orders must be handled sensibly. When conditions change (breakdown, rush order, late material), the system **reschedules** and shows what changed.

We also deliberately inject five kinds of bad schedules (overlap, precedence violation, material before availability, outside shift, missing changeover) with ground-truth labels, and show an independent validator catches them.

| Concept | Meaning |
|---|---|
| Valid baseline schedule | Produced by the engine, passes all hard-constraint checks |
| Infeasible order | A legitimate scheduling outcome, with a reason code |
| Injected anomaly | A deliberately corrupted copy with ground-truth labels |

---

## 2. Requirements

### Functional
| ID | Area | Summary |
|---|---|---|
| FR-A | Master data | 10 required entities, PK/FK, types, units, enums, validation, effective dates, lifecycle states, bridge tables, event history, data dictionary |
| FR-B | Data generator | Seeded, parent-first, deterministic IDs, starter and full modes, CSV + JSON + SQL export, baseline schedule before anomalies |
| FR-C | Scheduler | Routing, eligibility, durations, calendars, precedence, materials, changeovers, labor, priority, infeasibility explanations, rescheduling |
| FR-D | Exceptions | 5 labelled anomaly types, separation of valid/infeasible/injected/exception/corrective |
| FR-E | UI | Dashboard, orders, master data, calendars, Gantt, materials, engine controls, exceptions, comparison/rescheduling, export |
| FR-F | Metrics | 11 required metrics, hard constraints vs objectives |
| FR-G | Data quality | 11 validation categories, separate ground truth, reproducibility checksums |

### Non-functional
- Reproducibility: same seed gives identical output (row counts and SHA-256 checksums).
- Determinism: same input and config gives the same schedule.
- Performance: full dataset schedules in under about 60 seconds on a laptop.
- Explainability: every placement stores candidate machines and the reason for the choice.
- Traceability: every exception links to order, operation, machine, and constraint.
- Runs from a clean clone on Windows PowerShell, Python only, no external services.
- Core logic is pure Python, separate from UI and DB.
- The engine physically cannot read ground-truth labels.

---

## 3. Technology Stack

| Layer | Choice | Why |
|---|---|---|
| Language | Python 3.13 | One language for generator, engine, API, UI |
| Database | SQLite via SQLAlchemy 2.x | Zero install, single file, full FK support |
| Backend | FastAPI | Auto docs at `/docs`, Pydantic validation, easy to test |
| Frontend | Streamlit | Interactive dashboards in pure Python, works with Plotly |
| Data | pandas | Loading, exporting, KPI aggregation |
| Charts | Plotly (`px.timeline`) | Interactive Gantt |
| Optimization | Custom heuristic (primary), OR-Tools CP-SAT (optional benchmark) | See section 6 |
| Testing | pytest | Standard |

Streamlit calls the service layer directly (same process). FastAPI exposes the same services over REST.

---

## 4. Architecture

```
Presentation:  Streamlit pages (dashboard, orders, master data, calendars,
               Gantt, materials, engine controls, exceptions, compare, export)
                    |  direct calls                    |  HTTP (REST)
Service layer: schedule_service, order_service, metrics_service, export_service
                    |
   Scheduling Engine        Validation (independent)        Anomaly Injector
   (heuristic, timelines,   (hard-constraint checker +      (writes labels to a
   explanations)            data-quality checks)            separate store)
                    |
Persistence:   SQLAlchemy models + repositories -> SQLite (data/app.db)
                    ^
Synthetic data generator (seed 20261022) -> CSV / JSON / SQL + manifest
ground_truth.csv kept OUTSIDE the engine's input path
```

Two design rules:
1. The validator is written independently from the engine.
2. The engine reads only a defined set of input tables, never the ground-truth tables.

---

## 5. Database Entities

Resolution of an ambiguity: **RoutingStep** is the template step on a product routing. **Operation** is the instance of a step for a specific production order (the 2,000 operations).

### Core entities
1. `product`: SKU, family, unit, lifecycle_state, effective_from/to
2. `routing`: per product version, status, effective dates
3. `operation`: per order, linked to routing_step, sequence_no, qty, planned minutes
4. `machine`: group, status, effective dates
5. `machine_calendar`: dated working windows per machine
6. `material`: unit of measure, lead time, status
7. `inventory_lot`: material, quantity, available_from, status
8. `production_order`: product, qty, priority, release/due dates, status
9. `operation_schedule`: operation, machine, setup start, start, end, run id, status
10. `changeover_matrix`: from_family, to_family, machine group, minutes

### Supporting and bridge tables
- `routing_step`, `routing_step_machine` (M:N eligibility), `product_bom` (M:N), `operation_material_req`
- `tool`, `routing_step_tool` (M:N), `labor_pool`, `labor_shift_capacity`
- `shift_pattern`, `machine_downtime`

### Event and history tables (append-only)
- `schedule_run`, `schedule_event`, `inventory_transaction`
- `order_status_history`, `machine_status_history`
- `scheduling_exception`, `placement_decision`

### Ground truth (separate)
- `anomaly_ground_truth` / `ground_truth.csv`: record_id, entity, issue_type, ground_truth, injected_run_id, description. NOT in the engine's input tables.

### Key relationships
| Parent | Child | Cardinality |
|---|---|---|
| product | routing | 1:N (one active) |
| routing | routing_step | 1:N (ordered) |
| routing_step | machine (bridge) | M:N |
| product | material (BOM) | M:N |
| material | inventory_lot | 1:N |
| product | production_order | 1:N |
| production_order | operation | 1:N (ordered) |
| operation | operation_schedule | 1:N across runs |
| machine | machine_calendar / machine_downtime | 1:N |
| schedule_run | operation_schedule | 1:N |

### ID formats
`PROD-2026-000001` (order), `SKU-001`, `ROUTE-SKU-001-01`, `OP-PROD-2026-000001-01`, `MACH-001`, `SCH-2026-000001`.

---

## 6. Folder Structure

```
mfg-scheduler/
  README.md, requirements.txt, pyproject.toml, .gitignore, CLAUDE.md
  config/settings.py
  src/
    db/          models.py, session.py, repositories.py
    generator/   master_data.py, calendars.py, orders.py, materials.py,
                 baseline.py, anomalies.py, export.py, ids.py
    engine/      timelines.py, eligibility.py, materials.py, changeover.py,
                 priority.py, scheduler.py, explain.py, reschedule.py
    validation/  data_quality.py, constraints.py
    metrics/     kpis.py
    services/    schedule_service.py, order_service.py, export_service.py
    api/         main.py, routes/
  app/           Home.py, pages/ (10 pages)
  data/          generated/, ground_truth/, app.db
  tests/         unit/, integration/, e2e/
  docs/          PROJECT_PLAN.md, architecture, data dictionary, ERD, algorithm
```

---

## 7. Scheduling Algorithm

| Approach | Strengths | Weaknesses here |
|---|---|---|
| Constructive heuristic | Fast, deterministic, easy to explain, scales to 2,000 ops | Not provably optimal |
| CP-SAT | Strong on small and medium instances | Calendars, downtime, lots, setups, labor are heavy to model; hard to explain per decision |
| MIP | Exact | Worst scaling here |

**Recommendation: heuristic first, CP-SAT optional benchmark.**

1. Rank orders: urgent first, then priority class, then due-date rule (EDD or critical ratio).
2. For each order, for each operation in routing sequence:
   - Earliest start = latest of predecessor end, order release, material-ready time (FIFO lots).
   - For each eligible machine, find the first slot where the machine is free, the block is inside a working window and outside downtime, changeover fits first, operators are available, and tools are free.
   - Choose the machine with the earliest completion time. Tie-break: preferred machine, lowest load, machine ID.
   - Record all candidates in `placement_decision`.
3. If nothing works, mark the whole order unscheduled with a reason code and roll back partial reservations. Codes: `NO_ELIGIBLE_MACHINE`, `MATERIAL_UNAVAILABLE`, `NO_CAPACITY_IN_HORIZON`, `LABOR_UNAVAILABLE`, `TOOL_UNAVAILABLE`.
4. Lateness is an objective, not a hard constraint.

**Rescheduling:** pick trigger time T. Started or completed operations are locked. Remaining ones are re-run with changed inputs. Store as a new `schedule_run` and diff against the previous one.

**Assumptions:** operations are non-preemptive and contiguous; setup occupies the machine and needs an operator; material is consumed at operation start (FIFO); one machine per operation.

---

## 8. Roadmap

| # | Milestone | Done when |
|---|---|---|
| M0 | Plan approved | You confirm |
| M1 | Design: data dictionary, ERD, API list, DDL | Reviewed |
| M2 | Environment | `pytest` runs green |
| M3 | Generator | Volumes correct, double-run checksums match |
| M4 | Engine | Small dataset hand-verified, full run feasible |
| M5 | DB + API | Data round-trips, integrity checks pass |
| M6 | UI (10 pages) | All pages render real data |
| M7 | Integration | Generate, load, schedule, validate, display |
| M8 | Testing | All acceptance criteria checked |
| M9 | Docs + GitHub | Another machine runs it |

---

## 9. Risks and Mitigations

| Risk | Mitigation |
|---|---|
| Baseline data infeasible or too loose | Generate orders against computed capacity (70-85% load), let the engine produce the baseline |
| Heuristic too slow | Sorted interval lists with binary search, profile in Phase 9 |
| Engine and validator share bugs | Write the validator separately with its own time-window logic |
| Label leakage | Separate store, loader whitelist, test that fails if labels reach the engine |
| Non-determinism | One seeded RNG, sorted iteration, stable tie-breaks, checksum test |
| Material logic complexity | FIFO lots plus a reconciliation check |
| Beginner overwhelm | One phase at a time, small testable steps |
| Streamlit slow with 2,000 ops | Cache data, limit Gantt window |
| Scope creep (CP-SAT) | Keep optional and last |

---

## 10. Traceability Matrix

| Req | Requirement | Implementation | Verification |
|---|---|---|---|
| A1 | 10 entities with PK/FK | `db/models.py` | `test_schema_entities` |
| A2 | Types, units, enums, validation | Models + `data_quality.py` | `test_enum_and_range_rules` |
| A3 | Relationships and cardinality | Models + ERD | `test_fk_integrity` |
| A4 | Effective dates and lifecycle | `*_from/to`, state fields | `test_lifecycle_transitions` |
| A5 | Immutable consistent IDs | `generator/ids.py` | `test_id_formats` |
| A6 | Bridge tables | eligibility, BOM, tool tables | `test_bridge_tables` |
| A7 | Event history preserved | history and run/event tables | `test_append_only_history` |
| A8 | Schema and data dictionary | `docs/data_dictionary.md` | Doc review |
| B1 | 100/40/500/2,000/200 volumes | `generator/*` | `test_full_volumes` |
| B2 | Seed 20261022 | `config/settings.py` | `test_seed_reproducible` |
| B3 | Parent-first generation | Generator ordering | `test_no_orphans` |
| B4 | Realistic routings, eligibility | `master_data.py` | `test_routing_has_eligible_machines` |
| B5 | Calendars, shifts, downtime | `calendars.py` | `test_calendar_windows` |
| B6 | Durations and changeovers | `master_data.py` | `test_changeover_matrix_complete` |
| B7 | Materials and inventory dates | `materials.py` | `test_inventory_reconciles` |
| B8 | Orders with priority, due dates | `orders.py` | `test_due_after_release` |
| B9 | Valid baseline, then anomalies | `baseline.py`, `anomalies.py` | `test_baseline_zero_violations` |
| B10 | Derived values calculated | Generator + validators | `test_derived_consistency` |
| B11 | CSV/JSON/SQL export | `export.py` | `test_export_files` |
| B12 | Deterministic IDs | `ids.py` | `test_id_formats` |
| B13 | Starter and full datasets | `generate(profile=...)` | `test_starter_vs_full` |
| C1-C9 | Routing through priority | `engine/*` | Unit tests per constraint |
| C10 | Feasible schedules | `scheduler.py`, `constraints.py` | `test_schedule_is_feasible` |
| C11 | Infeasibility explanations | `explain.py` | `test_unscheduled_has_reason` |
| C12 | Rescheduling | `reschedule.py` | `test_reschedule_locks_started_ops` |
| C13 | Real algorithm, not random | Heuristic | Code review + determinism test |
| D1-D5 | 5 anomaly types | `anomalies.py`, `constraints.py` | `test_each_anomaly_detected` |
| D6 | Labels (issue_type, ground_truth) | `ground_truth.csv` | `test_labels_match_injections` |
| D7 | Distinguish the 5 categories | `run_type`, status fields | `test_category_separation` |
| D8 | No silent corruption | Targeted injection | `test_unrelated_rows_unchanged` |
| E1-E10 | 10 UI areas | `app/pages/*` | e2e smoke tests |
| E11 | Why assigned or unscheduled | `placement_decision`, `scheduling_exception` | `test_explanation_available` |
| G1 | 11 metrics from actual results | `metrics/kpis.py` | `test_metrics_known_case` |
| G2 | Hard vs objectives | `constraints.py` vs `kpis.py` | `test_never_feasible_with_violation` |
| H1 | Validation (11 categories) | `validation/*` | `test_*` per category |
| H2 | Separate ground truth | `data/ground_truth/` | `test_no_label_leakage` |
| H3 | Double-run checksum | `manifest.json` | `test_repro_checksums` |
| H4 | Provenance, lawful data | README | Doc |
| I1 | Clean-env run | README, `requirements.txt` | Clean-clone test |
| I2 | Persist and export | DB + `export_service` | Integration test |
| I3 | Tests pass | `tests/` | `pytest` |
| I4 | Docs and public GitHub | README, `docs/` | Submission checklist |

### Metric definitions
| Metric | Calculation |
|---|---|
| Makespan | max(end) - min(start) over scheduled ops |
| Total production time | Sum of processing minutes of scheduled ops |
| Total changeover time | Sum of setup minutes of scheduled ops |
| Machine utilization | (processing + setup minutes) / available working minutes in horizon |
| On-time delivery % | On-time scheduled orders / scheduled orders x 100 |
| Total tardiness | Sum of max(0, completion - due date) |
| Late orders | Count where tardiness > 0 |
| Unscheduled orders | Count with infeasible status |
| Constraint violations | Count reported by the independent validator |
| Material violations | Subset where consumption precedes availability |
| Feasibility rate | Scheduled orders with zero hard violations / total orders |

**Hard constraints:** no machine overlap, precedence, material availability, shift/calendar windows, required changeovers, labor and tool capacity, machine eligibility.
**Objectives:** tardiness, late orders, makespan, changeover time, utilization balance.

---

## 11. Decisions (defaults, pending approval)

1. Streamlit calls the service layer directly; FastAPI exposes the same services.
2. Exactly 500 orders and 2,000 operations (about 4 per order).
3. Horizon: 8 weeks starting 2026-11-02, 5-day week, 2-3 shifts per machine.
4. Operations are non-preemptive and contiguous.

---

## Progress

- [x] Phase 1: Requirement analysis and plan
- [ ] Phase 2: Architecture and database design
- [ ] Phases 3-10: pending