"""Dataset export (Phase 4, step 6).

Builds the whole dataset and writes, for one profile, under
GENERATED_DIR/<profile>/:
    csv/<table>.csv     one file per table
    json/<table>.json   one file per table
    seed.sql            INSERT statements in parent-first load order
    manifest.json       row counts and SHA-256 of every file

Output is byte-for-byte deterministic: fixed column order (the dict key order),
"\\n" line endings, UTF-8, NULL as an empty CSV field / JSON null / SQL NULL,
no timestamps anywhere. Step 7 relies on this.
"""
from __future__ import annotations

import csv
import hashlib
import io
import json
from pathlib import Path

from config.settings import GENERATED_DIR, SEED, VOLUMES
from src.generator.calendars import CALENDAR_LOAD_ORDER, generate_calendar_data
from src.generator.master_data import MASTER_LOAD_ORDER, generate_master_data
from src.generator.materials import INVENTORY_LOAD_ORDER, generate_inventory_data
from src.generator.orders import ORDER_LOAD_ORDER, generate_order_data

ALL_TABLES = MASTER_LOAD_ORDER + CALENDAR_LOAD_ORDER + ORDER_LOAD_ORDER + INVENTORY_LOAD_ORDER


def build_dataset(profile: str = "full") -> dict[str, list[dict]]:
    """Run every generator in order and return all tables, parent-first."""
    if profile not in VOLUMES:
        raise ValueError(f"unknown profile {profile!r}; choose from {sorted(VOLUMES)}")
    master = generate_master_data(profile)
    calendar = generate_calendar_data(master)
    orders = generate_order_data(master, profile)
    inventory = generate_inventory_data(master, orders)
    everything = {**master, **calendar, **orders, **inventory}
    return {name: everything[name] for name in ALL_TABLES}


def _columns(name: str, rows: list[dict]) -> list[str]:
    if not rows:
        return []
    cols = list(rows[0].keys())
    for r in rows:
        if list(r.keys()) != cols:
            raise ValueError(f"table {name}: rows do not all have the same columns")
    return cols


def _csv_bytes(cols: list[str], rows: list[dict]) -> bytes:
    if not cols:
        return b""
    buf = io.StringIO(newline="")
    writer = csv.writer(buf, lineterminator="\n")
    writer.writerow(cols)
    for r in rows:
        writer.writerow(["" if r[c] is None else r[c] for c in cols])
    return buf.getvalue().encode("utf-8")


def _json_bytes(rows: list[dict]) -> bytes:
    return (json.dumps(rows, indent=1, ensure_ascii=False) + "\n").encode("utf-8")


def _sql_literal(value) -> str:
    if value is None:
        return "NULL"
    if isinstance(value, bool):
        return "1" if value else "0"
    if isinstance(value, (int, float)):
        return repr(value)
    return "'" + str(value).replace("'", "''") + "'"


def _seed_bytes(tables: dict[str, list[dict]]) -> bytes:
    lines = ["-- Generated seed data. Load after docs/schema.sql.",
             "PRAGMA foreign_keys = ON;", "BEGIN TRANSACTION;"]
    for name in ALL_TABLES:
        rows = tables[name]
        cols = _columns(name, rows)
        for r in rows:
            values = ", ".join(_sql_literal(r[c]) for c in cols)
            lines.append(f"INSERT INTO {name} ({', '.join(cols)}) VALUES ({values});")
    lines.append("COMMIT;")
    return ("\n".join(lines) + "\n").encode("utf-8")


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def export_dataset(profile: str = "full", out_dir: Path | None = None) -> dict:
    """Write all files for one profile and return the manifest."""
    tables = build_dataset(profile)
    out = Path(out_dir) if out_dir is not None else GENERATED_DIR / profile
    (out / "csv").mkdir(parents=True, exist_ok=True)
    (out / "json").mkdir(parents=True, exist_ok=True)

    files: dict[str, bytes] = {}
    for name in ALL_TABLES:
        rows = tables[name]
        files[f"csv/{name}.csv"] = _csv_bytes(_columns(name, rows), rows)
        files[f"json/{name}.json"] = _json_bytes(rows)
    files["seed.sql"] = _seed_bytes(tables)

    for rel, data in files.items():
        (out / rel).write_bytes(data)  # bytes, so Windows never rewrites line endings

    manifest = {
        "profile": profile,
        "seed": SEED,
        "total_rows": sum(len(rows) for rows in tables.values()),
        "tables": {name: len(tables[name]) for name in ALL_TABLES},
        "files": [{"path": rel, "bytes": len(files[rel]), "sha256": _sha256(files[rel])}
                  for rel in sorted(files)],
    }
    (out / "manifest.json").write_bytes(
        (json.dumps(manifest, indent=1) + "\n").encode("utf-8"))
    return manifest


if __name__ == "__main__":
    import sys

    chosen = sys.argv[1] if len(sys.argv) > 1 else "full"
    result = export_dataset(chosen)
    for table_name, count in result["tables"].items():
        print(f"{table_name:26s} {count:6d}")
    print()
    print(f"profile      {result['profile']}")
    print(f"total rows   {result['total_rows']}")
    print(f"files        {len(result['files'])} (plus manifest.json)")
    print(f"output       {GENERATED_DIR / chosen}")
    seed_info = next(f for f in result["files"] if f["path"] == "seed.sql")
    print(f"seed.sql     {seed_info['bytes']} bytes, sha256 {seed_info['sha256'][:16]}...")