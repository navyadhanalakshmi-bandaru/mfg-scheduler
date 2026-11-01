"""Reproducibility tests: same seed, same bytes, in any process and any call order."""
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from config.settings import PROJECT_ROOT
from src.generator.calendars import generate_calendar_data
from src.generator.export import export_dataset
from src.generator.master_data import generate_master_data
from src.generator.materials import generate_inventory_data
from src.generator.orders import generate_order_data

GOLDEN = Path(__file__).parent / "golden_checksums.json"

_CODE = ("import sys; from pathlib import Path; "
         "from src.generator.export import export_dataset; "
         "export_dataset(sys.argv[1], out_dir=Path(sys.argv[2]))")


def _run_in_fresh_process(profile, out_dir, hash_seed):
    env = {**os.environ, "PYTHONHASHSEED": hash_seed}
    subprocess.run([sys.executable, "-c", _CODE, profile, str(out_dir)],
                   cwd=PROJECT_ROOT, env=env, check=True, capture_output=True)
    return json.loads((out_dir / "manifest.json").read_text(encoding="utf-8"))


@pytest.mark.parametrize("profile", ["starter", "full"])
def test_fresh_processes_with_different_hash_seeds_agree(profile, tmp_path):
    a = _run_in_fresh_process(profile, tmp_path / "a", "0")
    b = _run_in_fresh_process(profile, tmp_path / "b", "12345")
    assert a == b


def test_in_process_matches_fresh_process(tmp_path):
    here = export_dataset("starter", out_dir=tmp_path / "here")
    fresh = _run_in_fresh_process("starter", tmp_path / "fresh", "7")
    assert here == fresh


def test_generators_do_not_depend_on_call_order():
    master = generate_master_data("starter")
    orders_first = generate_order_data(master, "starter")
    calendar = generate_calendar_data(master)
    inventory = generate_inventory_data(master, orders_first)
    # run everything again in a different order
    master2 = generate_master_data("starter")
    inventory_stub_orders = generate_order_data(master2, "starter")
    inventory2 = generate_inventory_data(master2, inventory_stub_orders)
    calendar2 = generate_calendar_data(master2)
    assert master == master2
    assert orders_first == inventory_stub_orders
    assert calendar == calendar2
    assert inventory == inventory2


@pytest.mark.parametrize("profile", ["starter", "full"])
def test_seed_sql_matches_golden_checksum(profile, tmp_path):
    manifest = export_dataset(profile, out_dir=tmp_path)
    got = next(f["sha256"] for f in manifest["files"] if f["path"] == "seed.sql")
    golden = json.loads(GOLDEN.read_text(encoding="utf-8")) if GOLDEN.exists() else {}
    if profile not in golden:
        pytest.fail(f"no golden checksum for {profile!r}; run "
                    "python scripts\\update_golden.py and commit tests\\unit\\golden_checksums.json")
    assert got == golden[profile], (
        f"{profile} dataset changed. If the change is intended, run "
        "python scripts\\update_golden.py and commit the new file.")