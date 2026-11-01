"""Record the current seed.sql checksums as the golden reference.

Run this only when a generator change is intentional.
Usage:  python scripts\\update_golden.py
"""
import json
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.generator.export import export_dataset  # noqa: E402

GOLDEN = Path(__file__).resolve().parent.parent / "tests" / "unit" / "golden_checksums.json"


def main() -> None:
    golden = {}
    with tempfile.TemporaryDirectory() as tmp:
        for profile in ("starter", "full"):
            manifest = export_dataset(profile, out_dir=Path(tmp) / profile)
            golden[profile] = next(f["sha256"] for f in manifest["files"] if f["path"] == "seed.sql")
    GOLDEN.write_text(json.dumps(golden, indent=1) + "\n", encoding="utf-8")
    print(f"wrote {GOLDEN}")
    for profile, digest in golden.items():
        print(f"  {profile:8s} {digest}")


if __name__ == "__main__":
    main()