"""Central settings. Every other module imports constants from here."""
from pathlib import Path

SEED = 20261022  # fixed by the challenge

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DOCS_DIR = PROJECT_ROOT / "docs"
DATA_DIR = PROJECT_ROOT / "data"
GENERATED_DIR = DATA_DIR / "generated"
GROUND_TRUTH_DIR = DATA_DIR / "ground_truth"

SCHEMA_SQL = DOCS_DIR / "schema.sql"
GROUND_TRUTH_SCHEMA_SQL = DOCS_DIR / "schema_ground_truth.sql"
DB_PATH = DATA_DIR / "app.db"
GROUND_TRUTH_DB_PATH = GROUND_TRUTH_DIR / "ground_truth.db"

HORIZON_START = "2026-11-02 00:00:00"
HORIZON_WEEKS = 8

VOLUMES = {
    "full": {"skus": 100, "machines": 40, "orders": 500, "operations": 2000, "materials": 200},
    "starter": {"skus": 20, "machines": 12, "orders": 40, "operations": 160, "materials": 40},
}