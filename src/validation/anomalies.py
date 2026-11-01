"""Anomaly injection and evaluation (Phase 5h).

inject_anomalies() copies a VALID run into an INJECTED run and corrupts a few rows.
Labels go to the SEPARATE ground-truth database only. The scheduling engine never
reads that database, and neither does the validator (constraints.py).

Isolation: each injection is tried inside a SAVEPOINT. It is kept only when the
validator does not report any OTHER kind of violation because of it (collateral
damage). It is NOT required to report the intended one: if the validator misses an
injected anomaly, that shows up as recall < 1 in evaluate().

Each anomaly gets its own machine and its own order, so they cannot interact.
Only the labelled row of each anomaly is changed; every other row is a copy.

Issue types (names from schema_ground_truth.sql) and the code the validator uses:
    OVERLAP            MACHINE_OVERLAP
    PRECEDENCE         PRECEDENCE_VIOLATION
    MATERIAL_EARLY     MATERIAL_BEFORE_AVAILABILITY
    OUTSIDE_SHIFT      OUTSIDE_SHIFT
    MISSING_CHANGEOVER MISSING_CHANGEOVER
"""
from __future__ import annotations

import csv
import json
import sqlite3
from datetime import datetime, timedelta
from pathlib import Path

from config.settings import GROUND_TRUTH_SCHEMA_SQL, SEED
from src.generator import ids
from src.generator.rng import make_rng
from src.validation.constraints import validate_run

TS = "%Y-%m-%d %H:%M:%S"
INJECTION_TIME = "2026-10-22 11:00:00"  # fixed so output is reproducible

ISSUE_TYPES = ("OVERLAP", "PRECEDENCE", "MATERIAL_EARLY", "OUTSIDE_SHIFT", "MISSING_CHANGEOVER")
CODE_OF = {
    "OVERLAP": "MACHINE_OVERLAP",
    "PRECEDENCE": "PRECEDENCE_VIOLATION",
    "MATERIAL_EARLY": "MATERIAL_BEFORE_AVAILABILITY",
    "OUTSIDE_SHIFT": "OUTSIDE_SHIFT",
    "MISSING_CHANGEOVER": "MISSING_CHANGEOVER",
}
DEFAULT_COUNTS = {t: 3 for t in ISSUE_TYPES}
DEFAULT_CONTROLS = 10
MAX_TRIES = 60  # candidates tried per issue type


# ------------------------------------------------------------------ helpers
def _rows(con: sqlite3.Connection, sql: str, params: tuple = ()) -> list[dict]:
    cur = con.execute(sql, params)
    names = [d[0] for d in cur.description]
    return [dict(zip(names, r)) for r in cur.fetchall()]


def _dt(text: str) -> datetime:
    return datetime.strptime(text, TS)


def _fmt(dt: datetime) -> str:
    return dt.strftime(TS)


def _mins(a: datetime, b: datetime) -> int:
    """Whole minutes from a to b."""
    return int((b - a).total_seconds() // 60)


def _gt_id(n: int) -> str:
    return f"GT-2026-{n:06d}"


def open_ground_truth(path=None) -> sqlite3.Connection:
    """Open the separate ground-truth database (in memory when path is None)."""
    if path is not None:
        Path(path).parent.mkdir(parents=True, exist_ok=True)
    gt = sqlite3.connect(":memory:" if path is None else str(path))
    exists = gt.execute("SELECT 1 FROM sqlite_master WHERE type = 'table'"
                        " AND name = 'anomaly_ground_truth'").fetchone()
    if exists is None:
        gt.executescript(GROUND_TRUTH_SCHEMA_SQL.read_text(encoding="utf-8"))
    return gt


def _found(con: sqlite3.Connection, run_id: str) -> set[tuple[str, str]]:
    """What the independent validator reports: {(code, schedule_id)}."""
    return {(v.code, v.schedule_id) for v in validate_run(con, run_id)}


# --------------------------------------------------------------- the copy
def _copy_run(con: sqlite3.Connection, base_run_id: str) -> str:
    base = con.execute("SELECT run_type, scheduling_rule, status FROM schedule_run"
                       " WHERE run_id = ?", (base_run_id,)).fetchone()
    if base is None:
        raise ValueError(f"unknown run {base_run_id}")
    if base[0] == "INJECTED":
        raise ValueError("cannot inject into an INJECTED run; use a BASELINE or RESCHEDULE run")
    if base[2] != "COMPLETED":
        raise ValueError(f"run {base_run_id} is {base[2]}; only COMPLETED runs can be copied")
    existing = validate_run(con, base_run_id)
    if existing:
        raise ValueError(f"run {base_run_id} already has {len(existing)} violations; "
                         "inject only into a valid run")

    run_id = ids.run_id(con.execute("SELECT COUNT(*) FROM schedule_run").fetchone()[0] + 1)
    config = {"mode": "anomaly_injection", "base_run_id": base_run_id}
    con.execute(
        "INSERT INTO schedule_run (run_id, run_type, parent_run_id, scheduling_rule, config_json,"
        " trigger_time, trigger_reason, started_at, finished_at, status, feasibility_label,"
        " metrics_json) VALUES (?, 'INJECTED', ?, ?, ?, NULL, NULL, ?, NULL, 'RUNNING',"
        " 'NOT_VALIDATED', NULL)",
        (run_id, base_run_id, base[1], json.dumps(config, sort_keys=True), INJECTION_TIME))

    rows = _rows(con, "SELECT * FROM operation_schedule WHERE run_id = ? ORDER BY schedule_id",
                 (base_run_id,))
    first = con.execute("SELECT COUNT(*) FROM operation_schedule").fetchone()[0] + 1
    new_id = {r["schedule_id"]: ids.schedule_id(first + i) for i, r in enumerate(rows)}
    con.executemany(
        "INSERT INTO operation_schedule (schedule_id, run_id, operation_id, machine_id,"
        " setup_start, setup_minutes, start_time, processing_minutes, end_time,"
        " previous_schedule_id, status, created_at) VALUES (?,?,?,?,?,?,?,?,?,NULL,?,?)",
        [(new_id[r["schedule_id"]], run_id, r["operation_id"], r["machine_id"], r["setup_start"],
          r["setup_minutes"], r["start_time"], r["processing_minutes"], r["end_time"],
          r["status"], INJECTION_TIME) for r in rows])
    con.executemany(
        "UPDATE operation_schedule SET previous_schedule_id = ? WHERE schedule_id = ?",
        [(new_id[r["previous_schedule_id"]], new_id[r["schedule_id"]])
         for r in rows if r["previous_schedule_id"] in new_id])
    return run_id


# ------------------------------------------------------------- candidates
def _overlap_candidates(con, run_id):
    rows = _rows(con, """
        SELECT s.schedule_id, s.machine_id, s.setup_start, o.order_id,
               LAG(s.schedule_id) OVER (PARTITION BY s.machine_id
                   ORDER BY s.setup_start, s.schedule_id) AS prev_id,
               LAG(s.setup_start) OVER (PARTITION BY s.machine_id
                   ORDER BY s.setup_start, s.schedule_id) AS prev_setup,
               LAG(s.end_time) OVER (PARTITION BY s.machine_id
                   ORDER BY s.setup_start, s.schedule_id) AS prev_end
        FROM operation_schedule s JOIN operation o ON o.operation_id = s.operation_id
        WHERE s.run_id = ? ORDER BY s.schedule_id""", (run_id,))
    out = []
    for r in rows:
        if r["prev_id"] is None:
            continue
        a_len = _mins(_dt(r["prev_setup"]), _dt(r["prev_end"]))
        if a_len < 2:
            continue
        new_setup = _dt(r["prev_setup"]) + timedelta(minutes=max(1, a_len // 2))
        delta = _mins(new_setup, _dt(r["setup_start"]))
        if delta < 1:
            continue
        out.append({
            "action": "shift", "minutes": -delta, "sid": r["schedule_id"],
            "related": r["prev_id"], "machine": r["machine_id"], "order": r["order_id"],
            "rank": 0 if delta <= 240 else 1,
            "description": (f"{r['schedule_id']} moved {delta} min earlier so it starts at "
                            f"{_fmt(new_setup)}, inside {r['prev_id']} on {r['machine_id']} "
                            f"(which runs until {r['prev_end']})")})
    return out


def _precedence_candidates(con, run_id):
    rows = _rows(con, """
        SELECT s.schedule_id, s.machine_id, s.start_time, o.order_id, o.sequence_no,
               LAG(s.schedule_id) OVER (PARTITION BY o.order_id ORDER BY o.sequence_no) AS prev_id,
               LAG(s.end_time) OVER (PARTITION BY o.order_id ORDER BY o.sequence_no) AS prev_end
        FROM operation_schedule s JOIN operation o ON o.operation_id = s.operation_id
        WHERE s.run_id = ? ORDER BY s.schedule_id""", (run_id,))
    out = []
    for r in rows:
        if r["prev_id"] is None:
            continue
        new_start = _dt(r["prev_end"]) - timedelta(minutes=1)
        delta = _mins(new_start, _dt(r["start_time"]))
        if delta < 1:
            continue
        out.append({
            "action": "shift", "minutes": -delta, "sid": r["schedule_id"],
            "related": r["prev_id"], "machine": r["machine_id"], "order": r["order_id"],
            "rank": 0 if delta <= 240 else 1,
            "description": (f"{r['schedule_id']} (operation {r['sequence_no']}) moved {delta} min "
                            f"earlier so it starts at {_fmt(new_start)}, before {r['prev_id']} "
                            f"ends at {r['prev_end']}")})
    return out


def _material_candidates(con, run_id):
    rows = _rows(con, """
        SELECT s.schedule_id, s.machine_id, s.start_time, o.order_id, q.material_id,
               (SELECT MIN(l.available_from) FROM inventory_lot l
                 WHERE l.material_id = q.material_id AND l.status <> 'QUARANTINED') AS first_avail
        FROM operation_schedule s
        JOIN operation o ON o.operation_id = s.operation_id
        JOIN operation_material_req q ON q.operation_id = s.operation_id
        WHERE s.run_id = ? ORDER BY s.schedule_id, q.material_id""", (run_id,))
    best: dict[str, dict] = {}
    for r in rows:
        if r["first_avail"] is None or r["start_time"] < r["first_avail"]:
            continue
        gap = _mins(_dt(r["first_avail"]), _dt(r["start_time"]))
        if gap > 1440:
            continue
        delta = gap + 1
        if r["schedule_id"] in best and best[r["schedule_id"]]["delta"] <= delta:
            continue
        new_start = _dt(r["first_avail"]) - timedelta(minutes=1)
        best[r["schedule_id"]] = {
            "delta": delta, "action": "shift", "minutes": -delta, "sid": r["schedule_id"],
            "related": None, "machine": r["machine_id"], "order": r["order_id"],
            "rank": 0 if delta <= 240 else 1,
            "description": (f"{r['schedule_id']} moved {delta} min earlier so it starts at "
                            f"{_fmt(new_start)}, before the first usable lot of "
                            f"{r['material_id']} arrives at {r['first_avail']}")}
    return [best[k] for k in sorted(best)]


def _outside_shift_candidates(con, run_id):
    rows = _rows(con, """
        SELECT s.schedule_id, s.machine_id, s.setup_start, s.end_time, o.order_id,
               LEAD(s.setup_start) OVER (PARTITION BY s.machine_id
                   ORDER BY s.setup_start, s.schedule_id) AS machine_next_setup,
               LEAD(s.start_time) OVER (PARTITION BY o.order_id
                   ORDER BY o.sequence_no) AS order_next_start,
               (SELECT c.window_end FROM machine_calendar c
                 WHERE c.machine_id = s.machine_id AND c.window_start <= s.setup_start
                 ORDER BY c.window_start DESC LIMIT 1) AS window_end
        FROM operation_schedule s JOIN operation o ON o.operation_id = s.operation_id
        WHERE s.run_id = ? ORDER BY s.schedule_id""", (run_id,))
    out = []
    for r in rows:
        if r["window_end"] is None or r["window_end"] < r["end_time"]:
            continue
        new_setup = _dt(r["window_end"]) + timedelta(minutes=30)
        delta = _mins(_dt(r["setup_start"]), new_setup)
        new_end = _dt(r["end_time"]) + timedelta(minutes=delta)
        if r["machine_next_setup"] is not None and r["machine_next_setup"] < _fmt(new_end):
            continue
        if r["order_next_start"] is not None and r["order_next_start"] < _fmt(new_end):
            continue
        # the new block must touch no working window of this machine at all
        clash = con.execute(
            "SELECT 1 FROM machine_calendar WHERE machine_id = ? AND window_start < ?"
            " AND window_end > ? LIMIT 1",
            (r["machine_id"], _fmt(new_end), _fmt(new_setup))).fetchone()
        if clash is not None:
            continue
        out.append({
            "action": "shift", "minutes": delta, "sid": r["schedule_id"], "related": None,
            "machine": r["machine_id"], "order": r["order_id"], "rank": 0,
            "description": (f"{r['schedule_id']} moved {delta} min later to "
                            f"{_fmt(new_setup)} to {_fmt(new_end)}, where {r['machine_id']} "
                            f"has no working shift")})
    return out


def _changeover_candidates(con, run_id):
    rows = _rows(con, """
        WITH r AS (
          SELECT s.schedule_id, s.machine_id, s.setup_minutes, o.order_id,
                 p.product_family AS fam, m.machine_group AS grp,
                 LAG(p.product_family) OVER (PARTITION BY s.machine_id
                     ORDER BY s.setup_start, s.schedule_id) AS prev_fam,
                 LAG(s.schedule_id) OVER (PARTITION BY s.machine_id
                     ORDER BY s.setup_start, s.schedule_id) AS prev_id
          FROM operation_schedule s
          JOIN operation o ON o.operation_id = s.operation_id
          JOIN production_order po ON po.order_id = o.order_id
          JOIN product p ON p.sku_id = po.sku_id
          JOIN machine m ON m.machine_id = s.machine_id
          WHERE s.run_id = ?)
        SELECT r.schedule_id, r.machine_id, r.order_id, r.prev_id, r.prev_fam, r.fam,
               r.setup_minutes, c.setup_minutes AS need
        FROM r JOIN changeover_matrix c ON c.machine_group = r.grp
             AND c.from_family = r.prev_fam AND c.to_family = r.fam
        WHERE r.prev_id IS NOT NULL AND c.setup_minutes > 0 AND r.setup_minutes >= c.setup_minutes
        ORDER BY r.schedule_id""", (run_id,))
    return [{
        "action": "drop_setup", "minutes": 0, "sid": r["schedule_id"], "related": r["prev_id"],
        "machine": r["machine_id"], "order": r["order_id"], "rank": 0,
        "description": (f"{r['schedule_id']}: setup cut from {r['setup_minutes']} to 0 min, but "
                        f"changeover {r['prev_fam']} to {r['fam']} needs {r['need']} min")}
        for r in rows]


_BUILDERS = {
    "OVERLAP": _overlap_candidates,
    "PRECEDENCE": _precedence_candidates,
    "MATERIAL_EARLY": _material_candidates,
    "OUTSIDE_SHIFT": _outside_shift_candidates,
    "MISSING_CHANGEOVER": _changeover_candidates,
}


def _apply(con: sqlite3.Connection, cand: dict) -> None:
    """Change exactly one operation_schedule row (the labelled one)."""
    if cand["action"] == "shift":
        mod = f"{cand['minutes']:+d} minutes"
        con.execute(
            "UPDATE operation_schedule SET setup_start = datetime(setup_start, ?),"
            " start_time = datetime(start_time, ?), end_time = datetime(end_time, ?)"
            " WHERE schedule_id = ?", (mod, mod, mod, cand["sid"]))
    else:  # drop_setup: keep start and end, remove the setup time
        con.execute("UPDATE operation_schedule SET setup_start = start_time, setup_minutes = 0"
                    " WHERE schedule_id = ?", (cand["sid"],))


# ----------------------------------------------------------------- inject
def inject_anomalies(con: sqlite3.Connection, gt: sqlite3.Connection, base_run_id: str,
                     counts: dict | None = None, control_rows: int = DEFAULT_CONTROLS,
                     seed: int = SEED) -> dict:
    """Create an INJECTED run from a valid run and write the labels to gt.

    Returns {"run_id", "base_run_id", "requested", "injected", "controls"}.
    "injected" is what was actually achieved per issue type (it can be lower than
    requested when no clean candidate exists).
    """
    counts = dict(DEFAULT_COUNTS if counts is None else counts)
    unknown = set(counts) - set(ISSUE_TYPES)
    if unknown:
        raise ValueError(f"unknown issue types {sorted(unknown)}; choose from {ISSUE_TYPES}")
    rng = make_rng("anomalies", seed)
    try:
        run_id = _copy_run(con, base_run_id)
        current = _found(con, run_id)
        used_machines: set[str] = set()
        used_orders: set[str] = set()
        labels: list[dict] = []
        achieved = dict.fromkeys(ISSUE_TYPES, 0)

        for issue_type in ISSUE_TYPES:
            want = counts.get(issue_type, 0)
            if want <= 0:
                continue
            cands = _BUILDERS[issue_type](con, run_id)
            rng.shuffle(cands)
            cands.sort(key=lambda c: c["rank"])  # stable: small changes first
            tries = 0
            for cand in cands:
                if achieved[issue_type] >= want or tries >= MAX_TRIES:
                    break
                if cand["machine"] in used_machines or cand["order"] in used_orders:
                    continue
                tries += 1
                con.execute("SAVEPOINT trial")
                _apply(con, cand)
                after = _found(con, run_id)
                side_effects = {code for code, _ in after - current} - {CODE_OF[issue_type]}
                if side_effects:
                    con.execute("ROLLBACK TO trial")
                    con.execute("RELEASE trial")
                    continue
                con.execute("RELEASE trial")
                current = after
                used_machines.add(cand["machine"])
                used_orders.add(cand["order"])
                achieved[issue_type] += 1
                labels.append({"record_id": cand["sid"], "related": cand["related"],
                               "issue_type": issue_type, "truth": 1,
                               "description": cand["description"]})

        untouched = [r["schedule_id"] for r in _rows(
            con, "SELECT s.schedule_id, s.machine_id, o.order_id FROM operation_schedule s"
                 " JOIN operation o ON o.operation_id = s.operation_id"
                 " WHERE s.run_id = ? ORDER BY s.schedule_id", (run_id,))
            if r["machine_id"] not in used_machines and r["order_id"] not in used_orders]
        picked = sorted(rng.sample(untouched, min(control_rows, len(untouched))))
        for sid in picked:
            labels.append({"record_id": sid, "related": None, "issue_type": "NONE", "truth": 0,
                           "description": "Control row: untouched, valid in the base run"})

        n = gt.execute("SELECT COUNT(*) FROM anomaly_ground_truth").fetchone()[0] + 1
        gt.executemany(
            "INSERT INTO anomaly_ground_truth (gt_id, run_id, record_entity, record_id,"
            " related_record_id, issue_type, ground_truth, description, created_at)"
            " VALUES (?,?,?,?,?,?,?,?,?)",
            [(_gt_id(n + i), run_id, "operation_schedule", lab["record_id"], lab["related"],
              lab["issue_type"], lab["truth"], lab["description"], INJECTION_TIME)
             for i, lab in enumerate(labels)])
        con.execute("UPDATE schedule_run SET status = 'COMPLETED', finished_at = ?"
                    " WHERE run_id = ?", (INJECTION_TIME, run_id))
        con.commit()
        gt.commit()
    except Exception:
        con.rollback()
        gt.rollback()
        raise
    return {"run_id": run_id, "base_run_id": base_run_id, "requested": counts,
            "injected": achieved, "controls": len(picked)}


# --------------------------------------------------------------- evaluate
def evaluate(con: sqlite3.Connection, gt: sqlite3.Connection, run_id: str) -> dict:
    """Compare the validator's findings with the ground-truth labels of one run."""
    labels = _rows(gt, "SELECT record_id, issue_type, ground_truth FROM anomaly_ground_truth"
                       " WHERE run_id = ?", (run_id,))
    if not labels:
        raise ValueError(f"no ground-truth labels for run {run_id}")
    found = _found(con, run_id)
    by_type = {}
    for t in ISSUE_TYPES:
        code = CODE_OF[t]
        positives = {l["record_id"] for l in labels if l["issue_type"] == t and l["ground_truth"] == 1}
        flagged = {sid for c, sid in found if c == code}
        tp, fp, fn = len(positives & flagged), len(flagged - positives), len(positives - flagged)
        by_type[t] = {
            "code": code, "injected": len(positives), "detected": tp, "missed": fn,
            "false_positives": fp,
            "precision": tp / (tp + fp) if tp + fp else None,
            "recall": tp / (tp + fn) if tp + fn else None}
    controls = {l["record_id"] for l in labels if l["ground_truth"] == 0}
    flagged_any = {sid for _, sid in found}
    total_tp = sum(v["detected"] for v in by_type.values())
    total_fp = sum(v["false_positives"] for v in by_type.values())
    total_fn = sum(v["missed"] for v in by_type.values())
    return {
        "by_type": by_type,
        "controls": {"rows": len(controls), "flagged": len(controls & flagged_any)},
        "total": {"injected": total_tp + total_fn, "detected": total_tp, "missed": total_fn,
                  "false_positives": total_fp,
                  "precision": total_tp / (total_tp + total_fp) if total_tp + total_fp else None,
                  "recall": total_tp / (total_tp + total_fn) if total_tp + total_fn else None}}


def format_report(result: dict) -> str:
    def f(x):
        return "n/a" if x is None else f"{x:.2f}"

    lines = [f"{'issue type':20s}{'injected':>9s}{'detected':>9s}{'missed':>8s}"
             f"{'false pos':>10s}{'precision':>10s}{'recall':>8s}"]
    rows = list(result["by_type"].items()) + [("TOTAL", result["total"])]
    for name, v in rows:
        lines.append(f"{name:20s}{v['injected']:9d}{v['detected']:9d}{v['missed']:8d}"
                     f"{v['false_positives']:10d}{f(v['precision']):>10s}{f(v['recall']):>8s}")
    c = result["controls"]
    lines.append(f"control rows {c['rows']}, flagged by the validator {c['flagged']}")
    return "\n".join(lines)


def export_labels_csv(gt: sqlite3.Connection, path) -> Path:
    """Write the labels to ground_truth.csv (evaluation use only)."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    cur = gt.execute("SELECT gt_id, run_id, record_entity, record_id, related_record_id,"
                     " issue_type, ground_truth, description, created_at"
                     " FROM anomaly_ground_truth ORDER BY gt_id")
    with path.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.writer(fh)
        writer.writerow([d[0] for d in cur.description])
        writer.writerows(cur.fetchall())
    return path


if __name__ == "__main__":
    import sys

    from config.settings import GROUND_TRUTH_DIR, SCHEMA_SQL
    from src.db.loader import load_tables
    from src.engine.baseline import write_baseline
    from src.engine.dispatcher import dispatch, prepare
    from src.generator.export import ALL_TABLES, build_dataset
    from src.validation.constraints import validate_and_record

    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    chosen = args[0] if args else "full"
    main_con = sqlite3.connect(":memory:")
    main_con.executescript(SCHEMA_SQL.read_text(encoding="utf-8"))
    load_tables(main_con, build_dataset(chosen), ALL_TABLES)
    mdl, fe, tls, wins = prepare(main_con)
    base_run = write_baseline(main_con, mdl, fe, dispatch(mdl, fe, tls, wins, "CRITICAL_RATIO"))
    gt_con = open_ground_truth()
    summary = inject_anomalies(main_con, gt_con, base_run)
    print(f"base run {summary['base_run_id']}  injected run {summary['run_id']}")
    print(f"requested {summary['requested']}")
    print(f"achieved  {summary['injected']}  control rows {summary['controls']}")
    found_now = validate_and_record(main_con, summary["run_id"])
    label_now = main_con.execute("SELECT feasibility_label FROM schedule_run WHERE run_id = ?",
                                 (summary["run_id"],)).fetchone()[0]
    print(f"validator found {len(found_now)} violations, run label {label_now}")
    print()
    print(format_report(evaluate(main_con, gt_con, summary["run_id"])))
    if "--save" in sys.argv:
        print("labels written to", export_labels_csv(gt_con, GROUND_TRUTH_DIR / "ground_truth.csv"))