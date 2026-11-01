"""Machine time availability (Phase 5a).

Free time per machine = calendar windows minus downtime. A MachineTimeline
answers "when is the earliest a block of N minutes fits?" and books time.
A block must fit inside one free interval (no pausing across gaps).
"""
from __future__ import annotations

import sqlite3
from bisect import bisect_right
from datetime import datetime, timedelta

TS = "%Y-%m-%d %H:%M:%S"
Interval = tuple[datetime, datetime]


def parse(text: str) -> datetime:
    return datetime.strptime(text, TS)


def subtract(windows: list[Interval], blocks: list[Interval],
             merge_adjacent: bool = False) -> list[Interval]:
    """Remove blocked intervals from windows. Both inputs may be unsorted."""
    blocks = sorted(blocks)
    pieces: list[Interval] = []
    for ws, we in sorted(windows):
        cur = ws
        for bs, be in blocks:
            if be <= cur:
                continue
            if bs >= we:
                break
            if bs > cur:
                pieces.append((cur, bs))
            cur = max(cur, be)
            if cur >= we:
                break
        if cur < we:
            pieces.append((cur, we))
    if not merge_adjacent:
        return pieces
    merged: list[Interval] = []
    for s, e in pieces:
        if merged and merged[-1][1] == s:
            merged[-1] = (merged[-1][0], e)
        else:
            merged.append((s, e))
    return merged


class MachineTimeline:
    """Sorted, disjoint free intervals for one machine."""

    def __init__(self, free: list[Interval]):
        self.free: list[Interval] = sorted(free)

    def earliest_start(self, not_before: datetime, minutes: int) -> datetime | None:
        """Earliest start >= not_before such that [start, start+minutes) is free."""
        need = timedelta(minutes=minutes)
        i = max(bisect_right(self.free, (not_before, datetime.max)) - 1, 0)
        for s, e in self.free[i:]:
            start = max(s, not_before)
            if start + need <= e:
                return start
        return None

    def reserve(self, start: datetime, end: datetime) -> None:
        """Book [start, end). It must lie inside one free interval."""
        i = bisect_right(self.free, (start, datetime.max)) - 1
        if i < 0 or not (self.free[i][0] <= start and end <= self.free[i][1]):
            raise ValueError(f"{start} to {end} is not inside free time")
        s, e = self.free[i]
        pieces = [p for p in ((s, start), (end, e)) if p[0] < p[1]]
        self.free[i:i + 1] = pieces

    def free_minutes(self) -> int:
        return int(sum((e - s).total_seconds() for s, e in self.free) // 60)


def load_timelines(con: sqlite3.Connection, merge_adjacent: bool = False) -> dict[str, MachineTimeline]:
    """One timeline per machine in the machine table."""
    windows: dict[str, list[Interval]] = {}
    for mid, ws, we in con.execute("SELECT machine_id, window_start, window_end FROM machine_calendar"):
        windows.setdefault(mid, []).append((parse(ws), parse(we)))
    downs: dict[str, list[Interval]] = {}
    for mid, ds, de in con.execute("SELECT machine_id, down_start, down_end FROM machine_downtime"):
        downs.setdefault(mid, []).append((parse(ds), parse(de)))
    return {mid: MachineTimeline(subtract(windows.get(mid, []), downs.get(mid, []), merge_adjacent))
            for (mid,) in con.execute("SELECT machine_id FROM machine ORDER BY machine_id")}