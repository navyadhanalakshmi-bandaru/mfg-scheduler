-- =====================================================================
-- Manufacturing Production Scheduling System: main database schema
-- Target: SQLite 3.40+ (ships with Python 3.13)
-- Source of truth: docs/data_dictionary.md
--
-- What the database enforces: types, enums, ranges, ID formats,
-- primary/foreign keys, uniqueness, date ordering, derived values.
-- What the independent validator enforces (needs several tables):
--   machine group of eligible machines, calendar overlap, downtime,
--   inventory reconciliation, precedence, labor/tool capacity.
-- Ground-truth labels are NOT here: see schema_ground_truth.sql.
-- =====================================================================

PRAGMA foreign_keys = ON;

-- ---------------------------------------------------------------------
-- 1. Plant: shift patterns and machines
-- ---------------------------------------------------------------------
CREATE TABLE shift_pattern (
    pattern_id        TEXT PRIMARY KEY
                      CHECK (pattern_id IN ('SHP-1S','SHP-2S','SHP-3S')),
    pattern_name      TEXT NOT NULL,
    shift_codes       TEXT NOT NULL,
    s1_start          TEXT NOT NULL CHECK (s1_start GLOB '[0-2][0-9]:[0-5][0-9]'),
    s1_minutes        INTEGER NOT NULL CHECK (s1_minutes BETWEEN 1 AND 720),
    s2_start          TEXT CHECK (s2_start IS NULL OR s2_start GLOB '[0-2][0-9]:[0-5][0-9]'),
    s2_minutes        INTEGER CHECK (s2_minutes IS NULL OR s2_minutes BETWEEN 1 AND 720),
    s3_start          TEXT CHECK (s3_start IS NULL OR s3_start GLOB '[0-2][0-9]:[0-5][0-9]'),
    s3_minutes        INTEGER CHECK (s3_minutes IS NULL OR s3_minutes BETWEEN 1 AND 720),
    working_weekdays  TEXT NOT NULL,
    CHECK ((s2_start IS NULL) = (s2_minutes IS NULL)),
    CHECK ((s3_start IS NULL) = (s3_minutes IS NULL))
);

CREATE TABLE machine (
    machine_id            TEXT PRIMARY KEY
                          CHECK (machine_id GLOB 'MACH-[0-9][0-9][0-9]'),
    machine_name          TEXT NOT NULL,
    machine_group         TEXT NOT NULL
                          CHECK (machine_group IN ('CUTTING','TURNING','MILLING','DRILLING',
                                 'GRINDING','HEAT_TREAT','WELDING','ASSEMBLY','INSPECTION')),
    status                TEXT NOT NULL CHECK (status IN ('ACTIVE','MAINTENANCE','RETIRED')),
    default_shift_pattern TEXT NOT NULL REFERENCES shift_pattern(pattern_id),
    effective_from        TEXT NOT NULL
                          CHECK (effective_from GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]'),
    effective_to          TEXT
                          CHECK (effective_to IS NULL OR
                                 effective_to GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]'),
    created_at            TEXT NOT NULL,
    CHECK (effective_to IS NULL OR effective_to > effective_from)
);

-- ---------------------------------------------------------------------
-- 2. Product, routing, eligibility (master data)
-- ---------------------------------------------------------------------
CREATE TABLE product (
    sku_id             TEXT PRIMARY KEY CHECK (sku_id GLOB 'SKU-[0-9][0-9][0-9]'),
    product_name       TEXT NOT NULL CHECK (length(product_name) BETWEEN 3 AND 80),
    product_family     TEXT NOT NULL
                       CHECK (product_family IN ('SHAFT','GEAR','HOUSING','BRACKET',
                                                 'VALVE','FLANGE','PLATE','FASTENER')),
    unit_of_measure    TEXT NOT NULL CHECK (unit_of_measure = 'EA'),
    standard_lot_size  INTEGER NOT NULL CHECK (standard_lot_size BETWEEN 1 AND 500),
    lifecycle_state    TEXT NOT NULL CHECK (lifecycle_state IN ('DRAFT','ACTIVE','RETIRED')),
    effective_from     TEXT NOT NULL
                       CHECK (effective_from GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]'),
    effective_to       TEXT
                       CHECK (effective_to IS NULL OR
                              effective_to GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]'),
    created_at         TEXT NOT NULL,
    CHECK (effective_to IS NULL OR effective_to > effective_from)
);

CREATE TABLE routing (
    routing_id       TEXT PRIMARY KEY
                     CHECK (routing_id GLOB 'ROUTE-SKU-[0-9][0-9][0-9]-[0-9][0-9]'),
    sku_id           TEXT NOT NULL REFERENCES product(sku_id),
    version_no       INTEGER NOT NULL CHECK (version_no >= 1),
    lifecycle_state  TEXT NOT NULL CHECK (lifecycle_state IN ('DRAFT','ACTIVE','RETIRED')),
    effective_from   TEXT NOT NULL
                     CHECK (effective_from GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]'),
    effective_to     TEXT
                     CHECK (effective_to IS NULL OR
                            effective_to GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]'),
    created_at       TEXT NOT NULL,
    UNIQUE (sku_id, version_no),
    CHECK (routing_id = 'ROUTE-' || sku_id || '-' || printf('%02d', version_no)),
    CHECK (effective_to IS NULL OR effective_to > effective_from)
);
-- At most one ACTIVE routing per SKU
CREATE UNIQUE INDEX ux_routing_one_active ON routing(sku_id) WHERE lifecycle_state = 'ACTIVE';

CREATE TABLE routing_step (
    step_id                 TEXT PRIMARY KEY
                            CHECK (step_id GLOB 'ROUTE-SKU-[0-9][0-9][0-9]-[0-9][0-9]-S[0-9][0-9]'),
    routing_id              TEXT NOT NULL REFERENCES routing(routing_id),
    sequence_no             INTEGER NOT NULL CHECK (sequence_no >= 1),
    operation_type          TEXT NOT NULL
                            CHECK (operation_type IN ('CUT','TURN','MILL','DRILL','GRIND',
                                   'HEAT_TREAT','WELD','ASSEMBLE','INSPECT')),
    required_machine_group  TEXT NOT NULL
                            CHECK (required_machine_group IN ('CUTTING','TURNING','MILLING',
                                   'DRILLING','GRINDING','HEAT_TREAT','WELDING','ASSEMBLY','INSPECTION')),
    std_minutes_per_unit    REAL NOT NULL CHECK (std_minutes_per_unit BETWEEN 0.1 AND 60.0),
    fixed_minutes           INTEGER NOT NULL CHECK (fixed_minutes BETWEEN 0 AND 120),
    labor_skill             TEXT NOT NULL
                            CHECK (labor_skill IN ('MACHINIST','WELDER','ASSEMBLER',
                                                   'INSPECTOR','HEAT_TREATER')),
    labor_headcount         INTEGER NOT NULL CHECK (labor_headcount BETWEEN 1 AND 3),
    created_at              TEXT NOT NULL,
    UNIQUE (routing_id, sequence_no),
    CHECK (step_id = routing_id || '-S' || printf('%02d', sequence_no))
);

CREATE TABLE routing_step_machine (
    step_id          TEXT NOT NULL REFERENCES routing_step(step_id),
    machine_id       TEXT NOT NULL REFERENCES machine(machine_id),
    rate_factor      REAL NOT NULL CHECK (rate_factor BETWEEN 0.5 AND 2.0),
    preference_rank  INTEGER NOT NULL CHECK (preference_rank >= 1),
    effective_from   TEXT NOT NULL
                     CHECK (effective_from GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]'),
    effective_to     TEXT
                     CHECK (effective_to IS NULL OR
                            effective_to GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]'),
    PRIMARY KEY (step_id, machine_id),
    CHECK (effective_to IS NULL OR effective_to > effective_from)
);

-- ---------------------------------------------------------------------
-- 3. Machine calendars, downtime, status history
-- ---------------------------------------------------------------------
CREATE TABLE machine_calendar (
    calendar_id        TEXT PRIMARY KEY
                       CHECK (calendar_id GLOB 'CAL-MACH-[0-9][0-9][0-9]-[0-9][0-9][0-9][0-9][0-9][0-9][0-9][0-9]-S[1-3]'),
    machine_id         TEXT NOT NULL REFERENCES machine(machine_id),
    shift_code         TEXT NOT NULL CHECK (shift_code IN ('S1','S2','S3')),
    window_start       TEXT NOT NULL
                       CHECK (window_start GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9] [0-9][0-9]:[0-9][0-9]:[0-9][0-9]'),
    window_end         TEXT NOT NULL
                       CHECK (window_end GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9] [0-9][0-9]:[0-9][0-9]:[0-9][0-9]'),
    available_minutes  INTEGER NOT NULL CHECK (available_minutes > 0),
    UNIQUE (machine_id, window_start),
    CHECK (window_end > window_start),
    CHECK (available_minutes =
           CAST(ROUND((julianday(window_end) - julianday(window_start)) * 1440) AS INTEGER))
);

CREATE TABLE machine_downtime (
    downtime_id    TEXT PRIMARY KEY CHECK (downtime_id GLOB 'DT-2026-[0-9][0-9][0-9][0-9][0-9][0-9]'),
    machine_id     TEXT NOT NULL REFERENCES machine(machine_id),
    downtime_type  TEXT NOT NULL
                   CHECK (downtime_type IN ('PLANNED_MAINTENANCE','BREAKDOWN','CALIBRATION','HOLIDAY')),
    down_start     TEXT NOT NULL
                   CHECK (down_start GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9] [0-9][0-9]:[0-9][0-9]:[0-9][0-9]'),
    down_end       TEXT NOT NULL
                   CHECK (down_end GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9] [0-9][0-9]:[0-9][0-9]:[0-9][0-9]'),
    reason         TEXT,
    created_at     TEXT NOT NULL,
    CHECK (down_end > down_start)
);
CREATE INDEX ix_downtime_machine ON machine_downtime(machine_id, down_start);

CREATE TABLE machine_status_history (
    history_id   TEXT PRIMARY KEY CHECK (history_id GLOB 'MSH-2026-[0-9][0-9][0-9][0-9][0-9][0-9]'),
    machine_id   TEXT NOT NULL REFERENCES machine(machine_id),
    from_status  TEXT CHECK (from_status IS NULL OR from_status IN ('ACTIVE','MAINTENANCE','RETIRED')),
    to_status    TEXT NOT NULL CHECK (to_status IN ('ACTIVE','MAINTENANCE','RETIRED')),
    changed_at   TEXT NOT NULL
                 CHECK (changed_at GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9] [0-9][0-9]:[0-9][0-9]:[0-9][0-9]'),
    reason       TEXT
);

-- ---------------------------------------------------------------------
-- 4. Materials and inventory
-- ---------------------------------------------------------------------
CREATE TABLE material (
    material_id      TEXT PRIMARY KEY CHECK (material_id GLOB 'MAT-[0-9][0-9][0-9]'),
    material_name    TEXT NOT NULL CHECK (length(material_name) BETWEEN 3 AND 80),
    unit_of_measure  TEXT NOT NULL CHECK (unit_of_measure IN ('EA','KG','M')),
    lead_time_days   INTEGER NOT NULL CHECK (lead_time_days BETWEEN 0 AND 60),
    lifecycle_state  TEXT NOT NULL CHECK (lifecycle_state IN ('DRAFT','ACTIVE','RETIRED')),
    effective_from   TEXT NOT NULL
                     CHECK (effective_from GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]'),
    effective_to     TEXT
                     CHECK (effective_to IS NULL OR
                            effective_to GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]'),
    created_at       TEXT NOT NULL,
    CHECK (effective_to IS NULL OR effective_to > effective_from)
);

CREATE TABLE inventory_lot (
    lot_id             TEXT PRIMARY KEY CHECK (lot_id GLOB 'LOT-2026-[0-9][0-9][0-9][0-9][0-9][0-9]'),
    material_id        TEXT NOT NULL REFERENCES material(material_id),
    quantity_received  REAL NOT NULL CHECK (quantity_received > 0),
    available_from     TEXT NOT NULL
                       CHECK (available_from GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9] [0-9][0-9]:[0-9][0-9]:[0-9][0-9]'),
    status             TEXT NOT NULL
                       CHECK (status IN ('EXPECTED','AVAILABLE','EXHAUSTED','QUARANTINED')),
    created_at         TEXT NOT NULL
);
CREATE INDEX ix_lot_material ON inventory_lot(material_id, available_from);

CREATE TABLE product_bom (
    sku_id                TEXT NOT NULL REFERENCES product(sku_id),
    material_id           TEXT NOT NULL REFERENCES material(material_id),
    qty_per_unit          REAL NOT NULL CHECK (qty_per_unit > 0),
    consumed_at_sequence  INTEGER NOT NULL DEFAULT 1 CHECK (consumed_at_sequence >= 1),
    effective_from        TEXT NOT NULL
                          CHECK (effective_from GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]'),
    effective_to          TEXT
                          CHECK (effective_to IS NULL OR
                                 effective_to GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]'),
    PRIMARY KEY (sku_id, material_id),
    CHECK (effective_to IS NULL OR effective_to > effective_from)
);

-- ---------------------------------------------------------------------
-- 5. Production orders and operations
-- ---------------------------------------------------------------------
CREATE TABLE production_order (
    order_id       TEXT PRIMARY KEY CHECK (order_id GLOB 'PROD-2026-[0-9][0-9][0-9][0-9][0-9][0-9]'),
    sku_id         TEXT NOT NULL REFERENCES product(sku_id),
    routing_id     TEXT NOT NULL REFERENCES routing(routing_id),
    quantity       INTEGER NOT NULL CHECK (quantity BETWEEN 1 AND 500),
    priority_class TEXT NOT NULL CHECK (priority_class IN ('URGENT','HIGH','NORMAL','LOW')),
    priority_rank  INTEGER NOT NULL CHECK (priority_rank BETWEEN 1 AND 4),
    release_time   TEXT NOT NULL
                   CHECK (release_time GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9] [0-9][0-9]:[0-9][0-9]:[0-9][0-9]'),
    due_time       TEXT NOT NULL
                   CHECK (due_time GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9] [0-9][0-9]:[0-9][0-9]:[0-9][0-9]'),
    status         TEXT NOT NULL
                   CHECK (status IN ('NEW','RELEASED','SCHEDULED','UNSCHEDULED',
                                     'IN_PROGRESS','COMPLETED','CANCELLED')),
    created_at     TEXT NOT NULL,
    updated_at     TEXT NOT NULL,
    CHECK (due_time > release_time),
    CHECK ((priority_class = 'URGENT' AND priority_rank = 1) OR
           (priority_class = 'HIGH'   AND priority_rank = 2) OR
           (priority_class = 'NORMAL' AND priority_rank = 3) OR
           (priority_class = 'LOW'    AND priority_rank = 4))
);
CREATE INDEX ix_order_status ON production_order(status);

CREATE TABLE operation (
    operation_id            TEXT PRIMARY KEY
                            CHECK (operation_id GLOB 'OP-PROD-2026-[0-9][0-9][0-9][0-9][0-9][0-9]-[0-9][0-9]'),
    order_id                TEXT NOT NULL REFERENCES production_order(order_id),
    step_id                 TEXT NOT NULL REFERENCES routing_step(step_id),
    sequence_no             INTEGER NOT NULL CHECK (sequence_no >= 1),
    operation_type          TEXT NOT NULL
                            CHECK (operation_type IN ('CUT','TURN','MILL','DRILL','GRIND',
                                   'HEAT_TREAT','WELD','ASSEMBLE','INSPECT')),
    required_machine_group  TEXT NOT NULL
                            CHECK (required_machine_group IN ('CUTTING','TURNING','MILLING',
                                   'DRILLING','GRINDING','HEAT_TREAT','WELDING','ASSEMBLY','INSPECTION')),
    quantity                INTEGER NOT NULL CHECK (quantity BETWEEN 1 AND 500),
    base_minutes            INTEGER NOT NULL CHECK (base_minutes BETWEEN 1 AND 450),
    labor_skill             TEXT NOT NULL
                            CHECK (labor_skill IN ('MACHINIST','WELDER','ASSEMBLER',
                                                   'INSPECTOR','HEAT_TREATER')),
    labor_headcount         INTEGER NOT NULL CHECK (labor_headcount BETWEEN 1 AND 3),
    status                  TEXT NOT NULL
                            CHECK (status IN ('PENDING','SCHEDULED','IN_PROGRESS','DONE','CANCELLED')),
    created_at              TEXT NOT NULL,
    updated_at              TEXT NOT NULL,
    UNIQUE (order_id, sequence_no),
    CHECK (operation_id = 'OP-' || order_id || '-' || printf('%02d', sequence_no))
);

CREATE TABLE operation_material_req (
    req_id        TEXT PRIMARY KEY,
    operation_id  TEXT NOT NULL REFERENCES operation(operation_id),
    material_id   TEXT NOT NULL REFERENCES material(material_id),
    required_qty  REAL NOT NULL CHECK (required_qty > 0),
    UNIQUE (operation_id, material_id),
    CHECK (req_id LIKE 'REQ-' || operation_id || '-%')
);

-- ---------------------------------------------------------------------
-- 6. Changeover matrix, tooling, labor
-- ---------------------------------------------------------------------
CREATE TABLE changeover_matrix (
    changeover_id   TEXT PRIMARY KEY,
    machine_group   TEXT NOT NULL
                    CHECK (machine_group IN ('CUTTING','TURNING','MILLING','DRILLING',
                           'GRINDING','HEAT_TREAT','WELDING','ASSEMBLY','INSPECTION')),
    from_family     TEXT NOT NULL
                    CHECK (from_family IN ('SHAFT','GEAR','HOUSING','BRACKET',
                                           'VALVE','FLANGE','PLATE','FASTENER')),
    to_family       TEXT NOT NULL
                    CHECK (to_family IN ('SHAFT','GEAR','HOUSING','BRACKET',
                                         'VALVE','FLANGE','PLATE','FASTENER')),
    setup_minutes   INTEGER NOT NULL CHECK (setup_minutes BETWEEN 0 AND 180),
    effective_from  TEXT NOT NULL
                    CHECK (effective_from GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]'),
    effective_to    TEXT
                    CHECK (effective_to IS NULL OR
                           effective_to GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]'),
    UNIQUE (machine_group, from_family, to_family),
    CHECK (changeover_id = 'CHG-' || machine_group || '-' || from_family || '-' || to_family),
    CHECK (from_family <> to_family OR setup_minutes <= 10),
    CHECK (effective_to IS NULL OR effective_to > effective_from)
);

CREATE TABLE tool (
    tool_id          TEXT PRIMARY KEY CHECK (tool_id GLOB 'TOOL-[0-9][0-9]'),
    tool_name        TEXT NOT NULL,
    units_available  INTEGER NOT NULL CHECK (units_available BETWEEN 1 AND 3),
    status           TEXT NOT NULL CHECK (status IN ('ACTIVE','RETIRED'))
);

CREATE TABLE routing_step_tool (
    step_id         TEXT NOT NULL REFERENCES routing_step(step_id),
    tool_id         TEXT NOT NULL REFERENCES tool(tool_id),
    units_required  INTEGER NOT NULL CHECK (units_required >= 1),
    PRIMARY KEY (step_id, tool_id)
);

CREATE TABLE labor_pool (
    pool_id      TEXT PRIMARY KEY,
    labor_skill  TEXT NOT NULL UNIQUE
                 CHECK (labor_skill IN ('MACHINIST','WELDER','ASSEMBLER',
                                        'INSPECTOR','HEAT_TREATER')),
    CHECK (pool_id = 'LAB-' || labor_skill)
);

CREATE TABLE labor_shift_capacity (
    pool_id              TEXT NOT NULL REFERENCES labor_pool(pool_id),
    work_date            TEXT NOT NULL
                         CHECK (work_date GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]'),
    shift_code           TEXT NOT NULL CHECK (shift_code IN ('S1','S2','S3')),
    headcount_available  INTEGER NOT NULL CHECK (headcount_available BETWEEN 0 AND 30),
    PRIMARY KEY (pool_id, work_date, shift_code)
);

-- ---------------------------------------------------------------------
-- 7. Schedule runs and results
-- ---------------------------------------------------------------------
CREATE TABLE schedule_run (
    run_id             TEXT PRIMARY KEY CHECK (run_id GLOB 'RUN-2026-[0-9][0-9][0-9][0-9][0-9][0-9]'),
    run_type           TEXT NOT NULL CHECK (run_type IN ('BASELINE','INJECTED','RESCHEDULE')),
    parent_run_id      TEXT REFERENCES schedule_run(run_id),
    scheduling_rule    TEXT NOT NULL CHECK (scheduling_rule IN ('EDD','PRIORITY_EDD','CRITICAL_RATIO')),
    config_json        TEXT NOT NULL CHECK (json_valid(config_json)),
    trigger_time       TEXT
                       CHECK (trigger_time IS NULL OR
                              trigger_time GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9] [0-9][0-9]:[0-9][0-9]:[0-9][0-9]'),
    trigger_reason     TEXT,
    started_at         TEXT NOT NULL,
    finished_at        TEXT,
    status             TEXT NOT NULL CHECK (status IN ('RUNNING','COMPLETED','FAILED')),
    feasibility_label  TEXT NOT NULL DEFAULT 'NOT_VALIDATED'
                       CHECK (feasibility_label IN ('NOT_VALIDATED','FEASIBLE','INFEASIBLE_VIOLATIONS')),
    metrics_json       TEXT CHECK (metrics_json IS NULL OR json_valid(metrics_json)),
    CHECK (run_type = 'BASELINE' OR parent_run_id IS NOT NULL),
    CHECK (run_type <> 'RESCHEDULE' OR trigger_time IS NOT NULL)
);

CREATE TABLE operation_schedule (
    schedule_id            TEXT PRIMARY KEY CHECK (schedule_id GLOB 'SCH-2026-[0-9][0-9][0-9][0-9][0-9][0-9]'),
    run_id                 TEXT NOT NULL REFERENCES schedule_run(run_id),
    operation_id           TEXT NOT NULL REFERENCES operation(operation_id),
    machine_id             TEXT NOT NULL REFERENCES machine(machine_id),
    setup_start            TEXT NOT NULL
                           CHECK (setup_start GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9] [0-9][0-9]:[0-9][0-9]:[0-9][0-9]'),
    setup_minutes          INTEGER NOT NULL CHECK (setup_minutes BETWEEN 0 AND 180),
    start_time             TEXT NOT NULL
                           CHECK (start_time GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9] [0-9][0-9]:[0-9][0-9]:[0-9][0-9]'),
    processing_minutes     INTEGER NOT NULL CHECK (processing_minutes > 0),
    end_time               TEXT NOT NULL
                           CHECK (end_time GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9] [0-9][0-9]:[0-9][0-9]:[0-9][0-9]'),
    previous_schedule_id   TEXT REFERENCES operation_schedule(schedule_id),
    status                 TEXT NOT NULL CHECK (status IN ('PLANNED','LOCKED','SUPERSEDED')),
    created_at             TEXT NOT NULL,
    UNIQUE (run_id, operation_id),
    CHECK (end_time > start_time),
    CHECK (datetime(setup_start, '+' || setup_minutes || ' minutes') = start_time),
    CHECK (datetime(start_time, '+' || processing_minutes || ' minutes') = end_time)
);
CREATE INDEX ix_opsched_machine_time ON operation_schedule(run_id, machine_id, start_time);

CREATE TABLE inventory_transaction (
    txn_id        TEXT PRIMARY KEY CHECK (txn_id GLOB 'ITX-2026-[0-9][0-9][0-9][0-9][0-9][0-9]'),
    lot_id        TEXT NOT NULL REFERENCES inventory_lot(lot_id),
    txn_type      TEXT NOT NULL CHECK (txn_type IN ('RECEIPT','RESERVE','CONSUME','RELEASE','ADJUST')),
    quantity      REAL NOT NULL,
    operation_id  TEXT REFERENCES operation(operation_id),
    run_id        TEXT REFERENCES schedule_run(run_id),
    txn_time      TEXT NOT NULL
                  CHECK (txn_time GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9] [0-9][0-9]:[0-9][0-9]:[0-9][0-9]'),
    created_at    TEXT NOT NULL,
    CHECK ((txn_type = 'RECEIPT' AND quantity > 0) OR
           (txn_type IN ('RESERVE','CONSUME') AND quantity < 0) OR
           (txn_type = 'RELEASE' AND quantity > 0) OR
           (txn_type = 'ADJUST')),
    CHECK (txn_type NOT IN ('RESERVE','CONSUME','RELEASE') OR operation_id IS NOT NULL)
);
CREATE INDEX ix_txn_lot ON inventory_transaction(lot_id, txn_time);

CREATE TABLE placement_decision (
    decision_id        TEXT PRIMARY KEY CHECK (decision_id GLOB 'PD-2026-[0-9][0-9][0-9][0-9][0-9][0-9]'),
    run_id             TEXT NOT NULL REFERENCES schedule_run(run_id),
    operation_id       TEXT NOT NULL REFERENCES operation(operation_id),
    chosen_machine_id  TEXT REFERENCES machine(machine_id),
    candidates_json    TEXT NOT NULL CHECK (json_valid(candidates_json)),
    rule_applied       TEXT NOT NULL,
    explanation        TEXT NOT NULL,
    created_at         TEXT NOT NULL
);
CREATE INDEX ix_decision_op ON placement_decision(run_id, operation_id);

CREATE TABLE schedule_event (
    event_id        TEXT PRIMARY KEY CHECK (event_id GLOB 'EVT-2026-[0-9][0-9][0-9][0-9][0-9][0-9]'),
    run_id          TEXT NOT NULL REFERENCES schedule_run(run_id),
    event_type      TEXT NOT NULL CHECK (event_type IN ('PLACED','MOVED','LOCKED','UNSCHEDULED','REMOVED')),
    order_id        TEXT REFERENCES production_order(order_id),
    operation_id    TEXT REFERENCES operation(operation_id),
    old_value_json  TEXT CHECK (old_value_json IS NULL OR json_valid(old_value_json)),
    new_value_json  TEXT CHECK (new_value_json IS NULL OR json_valid(new_value_json)),
    event_time      TEXT NOT NULL
                    CHECK (event_time GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9] [0-9][0-9]:[0-9][0-9]:[0-9][0-9]')
);
CREATE INDEX ix_event_run ON schedule_event(run_id, event_type);

CREATE TABLE scheduling_exception (
    exception_id  TEXT PRIMARY KEY CHECK (exception_id GLOB 'EXC-2026-[0-9][0-9][0-9][0-9][0-9][0-9]'),
    run_id        TEXT NOT NULL REFERENCES schedule_run(run_id),
    category      TEXT NOT NULL CHECK (category IN ('INFEASIBLE_ORDER','CONSTRAINT_VIOLATION')),
    code          TEXT NOT NULL,
    severity      TEXT NOT NULL CHECK (severity IN ('HARD','WARNING')),
    order_id      TEXT REFERENCES production_order(order_id),
    operation_id  TEXT REFERENCES operation(operation_id),
    machine_id    TEXT REFERENCES machine(machine_id),
    message       TEXT NOT NULL,
    detected_by   TEXT NOT NULL CHECK (detected_by IN ('ENGINE','VALIDATOR')),
    created_at    TEXT NOT NULL,
    CHECK (
      (category = 'INFEASIBLE_ORDER' AND code IN
         ('NO_ELIGIBLE_MACHINE','MATERIAL_UNAVAILABLE','NO_CAPACITY_IN_HORIZON',
          'LABOR_UNAVAILABLE','TOOL_UNAVAILABLE'))
      OR
      (category = 'CONSTRAINT_VIOLATION' AND code IN
         ('MACHINE_OVERLAP','PRECEDENCE_VIOLATION','MATERIAL_BEFORE_AVAILABILITY',
          'OUTSIDE_SHIFT','MISSING_CHANGEOVER','INELIGIBLE_MACHINE',
          'LABOR_CAPACITY_EXCEEDED','TOOL_CAPACITY_EXCEEDED'))
    )
);
CREATE INDEX ix_exception_run ON scheduling_exception(run_id, category);

CREATE TABLE order_status_history (
    history_id   TEXT PRIMARY KEY CHECK (history_id GLOB 'OSH-2026-[0-9][0-9][0-9][0-9][0-9][0-9]'),
    order_id     TEXT NOT NULL REFERENCES production_order(order_id),
    from_status  TEXT CHECK (from_status IS NULL OR from_status IN
                   ('NEW','RELEASED','SCHEDULED','UNSCHEDULED','IN_PROGRESS','COMPLETED','CANCELLED')),
    to_status    TEXT NOT NULL CHECK (to_status IN
                   ('NEW','RELEASED','SCHEDULED','UNSCHEDULED','IN_PROGRESS','COMPLETED','CANCELLED')),
    changed_at   TEXT NOT NULL
                 CHECK (changed_at GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9] [0-9][0-9]:[0-9][0-9]:[0-9][0-9]'),
    run_id       TEXT REFERENCES schedule_run(run_id),
    reason       TEXT
);