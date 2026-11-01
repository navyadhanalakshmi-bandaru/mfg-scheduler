"""The engine and services must never touch ground-truth labels."""
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
FORBIDDEN = ("ground_truth", "GROUND_TRUTH", "anomaly_ground_truth", "open_ground_truth")
PROTECTED = ["src/engine", "src/services", "src/metrics", "src/api", "src/db"]


def test_engine_and_services_do_not_reference_ground_truth():
    hits = []
    for folder in PROTECTED:
        for path in sorted((ROOT / folder).rglob("*.py")):
            text = path.read_text(encoding="utf-8")
            for word in FORBIDDEN:
                if word in text:
                    hits.append(f"{path.relative_to(ROOT)}: {word}")
    assert not hits, hits