"""Tests for anomaly injection and evaluation (Phase 5h)."""
import json
import sqlite3
from pathlib import Path

import pytest

from config.settings import SCHEMA_SQL
from src.db.loader import load_tables
from src.engine.baseline import write_baseline
from src.engine.dispatcher import dispatch, prepare
from src.generator.export import ALL_TABLES, build_dataset
from src.validation.anomalies import (
    CODE_OF, ISSUE_TYPES, evaluate, inject_anomalies, open_ground_truth,
)
from src.validation.constraints import validate_and_record, validate_run


def baseline(profile):
    con = sqlite3.connect(":memory:")
    con.executescript(SCHEMA_SQL.read_text(encoding="utf-8"))
    load_tables(con, build_dataset(profile), ALL_TABLES)
    con.execute("PRAGMA foreign_keys = ON")
    model, feas, tls, wins = prepare(con)
    run_id = write_baseline(con, model, feas, dispatch(model, feas, tls, wins, "CRITICAL_RATIO"))
    return con, run_id


@pytest.fixture(scope="module")
def injected():
    con, base = baseline("full")
    gt = open_ground_truth()
    summary = inject_anomalies(con, gt, base)
    yield con, gt, base, summary
    con.close()
    gt.close()


def labels(gt, run_id):
    cur = gt.execute("SELECT record_id, related_record_id, issue_type, ground_truth"
                     " FROM anomaly_ground_truth WHERE run_id = ? ORDER BY gt_id", (run_id,))
    return [tuple(r) for r in cur.fetchall()]


def test_every_issue_type_is_injected(injected):
    _, _, _, summary = injected
    for t in ISSUE_TYPES:
        assert summary["injected"][t] >= 1, f"no clean candidate found for {t}: {summary}"
    assert summary["controls"] >= 1


def test_base_run_is_untouched(injected):
    con, _, base, _ = injected
    assert validate_run(con, base) == []


def test_injected_run_metadata_and_no_leakage_into_main_db(injected):
    con, gt, base, summary = injected
    run_type, parent, status, config = con.execute(
        "SELECT run_type, parent_run_id, status, config_json FROM schedule_run WHERE run_id = ?",
        (summary["run_id"],)).fetchone()
    assert (run_type, parent, status) == ("INJECTED", base, "COMPLETED")
    assert all(t not in config for t in ISSUE_TYPES)
    assert json.loads(config)["mode"] == "anomaly_injection"
    main_tables = {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    gt_tables = {r[0] for r in gt.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert "anomaly_ground_truth" not in main_tables
    assert gt_tables == {"anomaly_ground_truth"}


def test_labels_are_consistent(injected):
    con, gt, _, summary = injected
    run = summary["run_id"]
    rows = labels(gt, run)
    schedule_ids = {r[0] for r in con.execute(
        "SELECT schedule_id FROM operation_schedule WHERE run_id = ?", (run,))}
    assert len({r[0] for r in rows}) == len(rows)
    for record_id, related, issue_type, truth in rows:
        assert record_id in schedule_ids
        assert related is None or related in schedule_ids
        assert (truth == 1) == (issue_type != "NONE")
    for t in ISSUE_TYPES:
        assert sum(1 for r in rows if r[2] == t) == summary["injected"][t]
    assert sum(1 for r in rows if r[3] == 0) == summary["controls"]


def test_only_labelled_rows_were_changed(injected):
    con, gt, base, summary = injected
    run = summary["run_id"]
    cols = ("operation_id, machine_id, setup_start, setup_minutes, start_time,"
            " processing_minutes, end_time")

    def snapshot(run_id):
        cur = con.execute(f"SELECT {cols} FROM operation_schedule WHERE run_id = ?", (run_id,))
        return {r[0]: tuple(r) for r in cur.fetchall()}

    before, after = snapshot(base), snapshot(run)
    assert set(before) == set(after)
    changed = {op for op in before if before[op] != after[op]}
    positives = [r[0] for r in labels(gt, run) if r[3] == 1]
    marks = ",".join("?" for _ in positives)
    labelled_ops = {r[0] for r in con.execute(
        f"SELECT operation_id FROM operation_schedule WHERE run_id = ? AND schedule_id IN ({marks})",
        (run, *positives))}
    assert changed == labelled_ops
    assert len(changed) == len(positives)


def test_validator_detects_every_injected_anomaly(injected):
    con, gt, _, summary = injected
    result = evaluate(con, gt, summary["run_id"])
    for t in ISSUE_TYPES:
        row = result["by_type"][t]
        assert row["code"] == CODE_OF[t]
        assert row["injected"] == summary["injected"][t]
        assert row["detected"] == row["injected"], f"{t}: {row}"
        assert row["false_positives"] == 0, f"{t}: {row}"
    assert result["total"]["recall"] == 1.0
    assert result["total"]["precision"] == 1.0
    assert result["controls"]["flagged"] == 0


def test_corrupted_run_is_labelled_infeasible(injected):
    src, _, _, summary = injected
    con = sqlite3.connect(":memory:")
    src.backup(con)
    found = validate_and_record(con, summary["run_id"])
    label = con.execute("SELECT feasibility_label FROM schedule_run WHERE run_id = ?",
                        (summary["run_id"],)).fetchone()[0]
    assert found and label == "INFEASIBLE_VIOLATIONS"
    con.close()


def test_injection_is_deterministic():
    first_con, first_base = baseline("starter")
    second_con, second_base = baseline("starter")
    gt1, gt2 = open_ground_truth(), open_ground_truth()
    s1 = inject_anomalies(first_con, gt1, first_base)
    s2 = inject_anomalies(second_con, gt2, second_base)
    assert s1 == s2
    assert labels(gt1, s1["run_id"]) == labels(gt2, s2["run_id"])


def test_bad_inputs_are_rejected(injected):
    con, gt, base, summary = injected
    with pytest.raises(ValueError):
        inject_anomalies(con, gt, base, counts={"NOT_A_TYPE": 1})
    with pytest.raises(ValueError):
        inject_anomalies(con, gt, summary["run_id"])  # an INJECTED run cannot be a base
    with pytest.raises(ValueError):
        inject_anomalies(con, gt, "RUN-2026-999999")
    with pytest.raises(ValueError):
        evaluate(con, gt, base)  # the base run has no labels


def test_engine_validator_and_metrics_never_reference_the_ground_truth_db():
    src = Path(__file__).resolve().parents[2] / "src"
    files = (list((src / "engine").glob("*.py")) + list((src / "metrics").glob("*.py"))
             + [src / "validation" / "constraints.py", src / "validation" / "inventory.py"])
    for path in files:
        text = path.read_text(encoding="utf-8")
        assert "anomaly_ground_truth" not in text, path
        assert "GROUND_TRUTH" not in text, path