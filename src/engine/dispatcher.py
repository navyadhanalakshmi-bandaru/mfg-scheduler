"""Dispatcher (Phase 5c): places operations on machines, append-only per machine.

Rules (RULES): EDD, PRIORITY_EDD, CRITICAL_RATIO.
Assumptions, each in one place:
  * processing = ceil(base_minutes / rate_factor); setup from changeover_matrix
  * a setup+processing block must fit inside one free interval (one shift window)
  * labor headcount and tool units are held during setup and processing
  * a job goes after the last job on its machine (no gap filling)
"""
from __future__ import annotations

import math
import sqlite3
from bisect import bisect_right
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from src.engine.model import FeasibilityResult, Issue, Model, find_issues, load_model
from src.engine.timeline import TS, MachineTimeline, load_timelines, parse

RULES = ("EDD", "PRIORITY_EDD", "CRITICAL_RATIO")
DISPATCH_WINDOW = timedelta(minutes=480)
MAX_RETRIES = 1000
Window = tuple[datetime, datetime, str, str]  # start, end, work_date, shift_code


def _ts(dt: datetime) -> str:
    return dt.strftime(TS)


@dataclass
class Placement:
    operation_id: str
    order_id: str
    machine_id: str
    setup_start: datetime
    setup_minutes: int
    start: datetime
    processing_minutes: int
    end: datetime
    previous_operation_id: str | None
    rule: str
    candidates: list[dict]
    explanation: str


@dataclass
class DispatchResult:
    rule: str
    placements: list[Placement] = field(default_factory=list)
    issues: list[Issue] = field(default_factory=list)
    skipped_orders: set[str] = field(default_factory=set)


@dataclass
class _Option:
    machine_id: str
    rank: int
    rate_factor: float
    setup_start: datetime
    setup: int
    start: datetime
    proc: int
    end: datetime


class Usage:
    """Booked (start, end, amount) blocks of one limited resource."""

    def __init__(self) -> None:
        self.items: list[tuple[datetime, datetime, int]] = []

    def conflict_end(self, s: datetime, e: datetime, need: int, cap: int,
                     fallback: datetime) -> datetime | None:
        """None if [s, e) fits; otherwise a later time worth retrying at."""
        if need > cap:
            return fallback
        overlapping = [(a, b, n) for a, b, n in self.items if a < e and b > s]
        for p in [s] + [a for a, _, _ in overlapping if a > s]:
            active = [(b, n) for a, b, n in overlapping if a <= p < b]
            if sum(n for _, n in active) + need > cap:
                return min(b for b, _ in active)
        return None

    def add(self, s: datetime, e: datetime, n: int) -> None:
        self.items.append((s, e, n))


def load_windows(con: sqlite3.Connection) -> dict[str, list[Window]]:
    """Calendar windows per machine with their work date and shift code."""
    out: dict[str, list[Window]] = {}
    for cid, mid, ws, we, shift in con.execute(
            "SELECT calendar_id, machine_id, window_start, window_end, shift_code"
            " FROM machine_calendar ORDER BY machine_id, window_start"):
        d = cid.split("-")[3]  # CAL-MACH-001-YYYYMMDD-S1
        out.setdefault(mid, []).append((parse(ws), parse(we), f"{d[:4]}-{d[4:6]}-{d[6:]}", shift))
    return out


def prepare(con: sqlite3.Connection):
    model = load_model(con)
    return model, find_issues(model), load_timelines(con), load_windows(con)


class _Engine:
    def __init__(self, model: Model, timelines: dict[str, MachineTimeline],
                 windows: dict[str, list[Window]]):
        self.m = model
        self.tl = {mid: MachineTimeline(list(t.free)) for mid, t in timelines.items()}
        self.windows = windows
        self.starts = {mid: [w[0] for w in ws] for mid, ws in windows.items()}
        self.last_end: dict[str, datetime] = {}
        self.last_family: dict[str, str] = {}
        self.last_op: dict[str, str] = {}
        self.labor: dict[tuple, Usage] = {}
        self.tools: dict[str, Usage] = {}

    def _window(self, mid: str, t: datetime) -> Window | None:
        i = bisect_right(self.starts[mid], t) - 1
        if i < 0:
            return None
        w = self.windows[mid][i]
        return w if w[0] <= t < w[1] else None

    def _try(self, cand, op, family: str, ready: datetime) -> _Option | None:
        mid = cand.machine_id
        if mid not in self.tl or mid not in self.windows:
            return None
        prev = self.last_family.get(mid)
        setup = self.m.changeover.get((op.machine_group, prev, family), 0) if prev else 0
        proc = max(1, math.ceil(round(op.base_minutes / cand.rate_factor, 6)))
        total = timedelta(minutes=setup + proc)
        not_before = max(ready, self.last_end.get(mid, ready))
        for _ in range(MAX_RETRIES):
            s = self.tl[mid].earliest_start(not_before, int(total.total_seconds() // 60))
            if s is None:
                return None
            e = s + total
            w = self._window(mid, s)
            if w is None:
                return None
            retry = []
            lkey = (op.labor_skill, w[2], w[3])
            r = self.labor.setdefault(lkey, Usage()).conflict_end(
                s, e, op.labor_headcount, self.m.labor_capacity.get(lkey, 0), w[1])
            if r:
                retry.append(r)
            for tool_id, need in self.m.step_tools.get(op.step_id, []):
                cap = self.m.tool_units.get(tool_id, (0, "RETIRED"))[0]
                r = self.tools.setdefault(tool_id, Usage()).conflict_end(s, e, need, cap, w[1])
                if r:
                    retry.append(r)
            if not retry:
                return _Option(mid, cand.preference_rank, cand.rate_factor, s, setup,
                               s + timedelta(minutes=setup), proc, e)
            not_before = min(retry)
        return None

    def place(self, op, family: str, ready: datetime, rule: str) -> Placement | None:
        options, record = [], []
        for cand in self.m.candidates.get(op.step_id, []):
            opt = self._try(cand, op, family, ready)
            if opt is None:
                record.append({"machine_id": cand.machine_id, "feasible": False})
                continue
            options.append(opt)
            record.append({"machine_id": opt.machine_id, "feasible": True,
                           "preference_rank": opt.rank, "rate_factor": opt.rate_factor,
                           "setup_minutes": opt.setup, "start": _ts(opt.start),
                           "end": _ts(opt.end)})
        if not options:
            return None
        best = min(options, key=lambda o: (o.end, o.rank, o.machine_id))
        mid = best.machine_id
        self.tl[mid].reserve(best.setup_start, best.end)
        w = self._window(mid, best.setup_start)
        self.labor.setdefault((op.labor_skill, w[2], w[3]), Usage()).add(
            best.setup_start, best.end, op.labor_headcount)
        for tool_id, need in self.m.step_tools.get(op.step_id, []):
            self.tools.setdefault(tool_id, Usage()).add(best.setup_start, best.end, need)
        previous = self.last_op.get(mid)
        self.last_end[mid], self.last_family[mid], self.last_op[mid] = best.end, family, op.operation_id
        why = (f"{rule}: {mid} gives the earliest finish {_ts(best.end)} among "
               f"{len(options)} feasible of {len(record)} eligible machine(s)")
        return Placement(op.operation_id, op.order_id, mid, best.setup_start, best.setup,
                         best.start, best.proc, best.end, previous, rule, record, why)


def dispatch(model: Model, feas: FeasibilityResult, timelines: dict[str, MachineTimeline],
             windows: dict[str, list[Window]], rule: str = "PRIORITY_EDD") -> DispatchResult:
    """Place every feasible order. The passed timelines are not modified."""
    if rule not in RULES:
        raise ValueError(f"unknown rule {rule!r}; choose from {RULES}")
    engine = _Engine(model, timelines, windows)
    release = {o: parse(x.release_time) for o, x in model.orders.items()}
    due = {o: parse(x.due_time) for o, x in model.orders.items()}
    material = {k: parse(v) for k, v in feas.material_ready.items()}
    suffix = {}
    for oid, ops in model.ops_by_order.items():
        acc = [0] * (len(ops) + 1)
        for i in range(len(ops) - 1, -1, -1):
            acc[i] = acc[i + 1] + ops[i].base_minutes
        suffix[oid] = acc

    active = [o for o in sorted(model.orders)
              if o not in feas.infeasible_orders and model.ops_by_order.get(o)]
    idx = dict.fromkeys(active, 0)
    free_at = {o: release[o] for o in active}
    result = DispatchResult(rule=rule, skipped_orders=set(feas.infeasible_orders))

    def ready(o: str) -> datetime:
        op = model.ops_by_order[o][idx[o]]
        return max(free_at[o], material.get(op.operation_id, free_at[o]))

    def rank_key(o: str, t0: datetime):
        order = model.orders[o]
        if rule == "EDD":
            return (due[o], o)
        if rule == "PRIORITY_EDD":
            return (order.priority_rank, due[o], o)
        remaining = max(1, suffix[o][idx[o]])
        return ((due[o] - t0).total_seconds() / 60 / remaining, order.priority_rank, due[o], o)

    while active:
        ready_at = {o: ready(o) for o in active}
        t0 = min(ready_at.values())
        pool = [o for o in active if ready_at[o] <= t0 + DISPATCH_WINDOW]
        oid = min(pool, key=lambda o: rank_key(o, t0))
        op = model.ops_by_order[oid][idx[oid]]
        placed = engine.place(op, model.orders[oid].family, ready_at[oid], rule)
        if placed is None:
            result.issues.append(Issue(oid, op.operation_id, "NO_CAPACITY_IN_HORIZON",
                                       f"no machine slot for {op.operation_id} inside the horizon"))
            active.remove(oid)
            continue
        result.placements.append(placed)
        free_at[oid] = placed.end
        idx[oid] += 1
        if idx[oid] >= len(model.ops_by_order[oid]):
            active.remove(oid)
    return result


if __name__ == "__main__":
    import sys

    from config.settings import SCHEMA_SQL
    from src.db.loader import load_tables
    from src.generator.export import ALL_TABLES, build_dataset

    chosen = sys.argv[1] if len(sys.argv) > 1 else "full"
    con = sqlite3.connect(":memory:")
    con.executescript(SCHEMA_SQL.read_text(encoding="utf-8"))
    load_tables(con, build_dataset(chosen), ALL_TABLES)
    mdl, feasibility, tls, wins = prepare(con)
    print(f"{'rule':15s} {'placed':>7s} {'no-cap':>7s} {'late':>5s} {'tardy hours':>12s}")
    for name in RULES:
        res = dispatch(mdl, feasibility, tls, wins, name)
        last_end: dict[str, datetime] = {}
        for p in res.placements:
            last_end[p.order_id] = max(last_end.get(p.order_id, p.end), p.end)
        incomplete = {i.order_id for i in res.issues}
        late = [(e - parse(mdl.orders[o].due_time)).total_seconds() / 3600
                for o, e in last_end.items() if o not in incomplete
                and e > parse(mdl.orders[o].due_time)]
        print(f"{name:15s} {len(res.placements):7d} {len(res.issues):7d} "
              f"{len(late):5d} {sum(late):12.1f}")