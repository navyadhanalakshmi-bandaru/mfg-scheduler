"""Tests for the dataset export."""
import csv
import hashlib
import json
import sqlite3

import pytest

from config.settings import SCHEMA_SQL
from src.generator.export import ALL_TABLES, build_dataset, export_dataset


@pytest.fixture(scope="module", params=["starter", "full"])
def exported(request, tmp_path_factory):
    out = tmp_path_factory.mktemp(f"export_{request.param}")
    manifest = export_dataset(request.param, out_dir=out)
    return request.param, out, manifest, build_dataset(request.param)


def test_manifest_counts_match_tables(exported):
    profile, _, manifest, tables = exported
    assert manifest["profile"] == profile
    assert list(manifest["tables"]) == ALL_TABLES
    assert manifest["tables"] == {name: len(rows) for name, rows in tables.items()}
    assert manifest["total_rows"] == sum(len(rows) for rows in tables.values())


def test_every_file_exists_and_checksum_matches(exported):
    _, out, manifest, _ = exported
    assert len(manifest["files"]) == 2 * len(ALL_TABLES) + 1
    for f in manifest["files"]:
        data = (out / f["path"]).read_bytes()
        assert len(data) == f["bytes"]
        assert hashlib.sha256(data).hexdigest() == f["sha256"]
        assert b"\r" not in data


def test_manifest_file_on_disk_matches_return_value(exported):
    _, out, manifest, _ = exported
    assert json.loads((out / "manifest.json").read_text(encoding="utf-8")) == manifest


def test_csv_round_trip(exported):
    _, out, _, tables = exported
    for name, rows in tables.items():
        if not rows:
            continue
        with open(out / "csv" / f"{name}.csv", newline="", encoding="utf-8") as fh:
            got = list(csv.reader(fh))
        assert got[0] == list(rows[0].keys())
        assert len(got) - 1 == len(rows)


def test_json_round_trip(exported):
    _, out, _, tables = exported
    for name, rows in tables.items():
        assert json.loads((out / "json" / f"{name}.json").read_text(encoding="utf-8")) == rows


def test_seed_sql_loads_into_real_schema(exported):
    _, out, _, tables = exported
    con = sqlite3.connect(":memory:")
    con.executescript(SCHEMA_SQL.read_text(encoding="utf-8"))
    con.executescript((out / "seed.sql").read_text(encoding="utf-8"))
    for name, rows in tables.items():
        assert con.execute(f"SELECT COUNT(*) FROM {name}").fetchone()[0] == len(rows)
    assert con.execute("PRAGMA foreign_key_check").fetchall() == []
    con.close()


def test_export_is_deterministic(tmp_path):
    first = export_dataset("starter", out_dir=tmp_path / "a")
    second = export_dataset("starter", out_dir=tmp_path / "b")
    assert first == second


def test_unknown_profile_rejected(tmp_path):
    with pytest.raises(ValueError):
        export_dataset("huge", out_dir=tmp_path)