# Project rules
- Manufacturing Production Scheduling System (coding challenge). Stack: Python 3.13 (use the `py` launcher), SQLite + SQLAlchemy, FastAPI, Streamlit, pandas, Plotly, pytest.
- Follow docs/PROJECT_PLAN.md. Work one phase at a time; do not build ahead.
- Seed is always 20261022. All randomness goes through one seeded generator. Iterate in sorted order. Use stable tie-breaks.
- The scheduling engine must NEVER read ground-truth/anomaly label tables.
- The validator is written independently of the engine.
- No placeholders or TODO stubs for required features. Every step ships with tests.
- Windows PowerShell commands only. Explain changes in simple language.
- Do not rename files, modules, or IDs without asking.