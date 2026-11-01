"""Generate the dataset twice in separate processes and compare every checksum.

Usage:  python scripts\\verify_reproducibility.py [full|starter]
Exit code 0 if identical, 1 if not.
"""
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def run_export(profile: str, out_dir: Path, hash_seed: str) -> dict:
    code = ("import sys; from pathlib import Path; "
            "from src.generator.export import export_dataset; "
            "export_dataset(sys.argv[1], out_dir=Path(sys.argv[2]))")
    env = {**os.environ, "PYTHONHASHSEED": hash_seed}
    subprocess.run([sys.executable, "-c", code, profile, str(out_dir)],
                   cwd=ROOT, env=env, check=True, capture_output=True)
    return json.loads((out_dir / "manifest.json").read_text(encoding="utf-8"))


def main() -> int:
    profile = sys.argv[1] if len(sys.argv) > 1 else "full"
    with tempfile.TemporaryDirectory() as tmp:
        a = run_export(profile, Path(tmp) / "a", "0")
        b = run_export(profile, Path(tmp) / "b", "12345")
    same = {f["path"]: f["sha256"] for f in a["files"]} == {f["path"]: f["sha256"] for f in b["files"]}
    print(f"profile {profile}: {len(a['files'])} files, total rows {a['total_rows']}")
    print("IDENTICAL" if same and a == b else "DIFFERENT")
    if not (same and a == b):
        fa = {f["path"]: f["sha256"] for f in a["files"]}
        fb = {f["path"]: f["sha256"] for f in b["files"]}
        for path in sorted(fa):
            if fa[path] != fb.get(path):
                print("  differs:", path)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())