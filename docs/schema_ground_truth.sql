-- =====================================================================
-- Ground-truth database schema (SEPARATE FILE: data/ground_truth/ground_truth.db)
-- The scheduling engine must never open this database.
-- No foreign keys on purpose: it refers to the main database by ID text only.
-- =====================================================================

CREATE TABLE anomaly_ground_truth (
    gt_id              TEXT PRIMARY KEY CHECK (gt_id GLOB 'GT-2026-[0-9][0-9][0-9][0-9][0-9][0-9]'),
    run_id             TEXT NOT NULL,
    record_entity      TEXT NOT NULL CHECK (record_entity = 'operation_schedule'),
    record_id          TEXT NOT NULL,
    related_record_id  TEXT,
    issue_type         TEXT NOT NULL
                       CHECK (issue_type IN ('OVERLAP','PRECEDENCE','MATERIAL_EARLY',
                                             'OUTSIDE_SHIFT','MISSING_CHANGEOVER','NONE')),
    ground_truth       INTEGER NOT NULL CHECK (ground_truth IN (0, 1)),
    description        TEXT NOT NULL,
    created_at         TEXT NOT NULL,
    UNIQUE (run_id, record_id),
    CHECK ((ground_truth = 1 AND issue_type <> 'NONE') OR
           (ground_truth = 0 AND issue_type = 'NONE'))
);