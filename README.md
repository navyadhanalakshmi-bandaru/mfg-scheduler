# Manufacturing Scheduler

A rule-based production scheduling engine with a database, REST API and Streamlit dashboard.
It generates a reproducible synthetic factory (seed 20261022), schedules orders onto machines
under hard constraints, validates the result, and supports rescheduling and run comparison.

## Setup (Windows PowerShell)

```powershell
python -m venv .venv
.venv\Scripts\Activate.ps1
pip install -r requirements-lock.txt
```

## Build the database

```powershell
python -m src.services.bootstrap
```

This creates `data/app.db` from the generated dataset and runs a baseline schedule.

## Run

```powershell
streamlit run app\Home.py
```

The Streamlit app has nine pages: Dashboard, Gantt, Orders, Exceptions, Engine controls,
Compare, Reschedule, Data and Export. The REST API lives under `src/` (see the API tests
for the routes).

## Test

```powershell
pytest -q
```

## Data generation

`python -m src.generator.export full` (or `starter`) writes CSV, JSON, `seed.sql` and a
checksummed `manifest.json` to `data/generated/<profile>/`. Output is byte-for-byte
reproducible; golden checksums are in `tests/unit/golden_checksums.json`. After any
intentional generator change run `python scripts\update_golden.py`.

| Profile | Orders | Operations | Machines |
|---|---|---|---|
| full | 500 | 2,000 | 40 |
| starter | 40 | 160 | 12 |

## Scheduling model and assumptions

- Dispatch rules: `EDD`, `PRIORITY_EDD`, `CRITICAL_RATIO` (standard definitions).
- Placement is append-only per machine; a job goes after the last job on its machine.
- Processing minutes are `ceil(base_minutes / rate_factor)`; setup comes from the
  changeover matrix by product family.
- Each setup plus processing block must fit inside one calendar shift window and avoid downtime.
- Labor headcount and tool units are hard constraints, held during setup and processing.
- Orders are served by priority rank, then due time, for material allocation; an order that
  cannot be fully covered is marked `MATERIAL_UNAVAILABLE`.
- Release window is 2 weeks (`RELEASE_WEEKS = 2`) to create real scheduling pressure
  (about a third of orders late under the baseline rules).
- These assumptions were chosen without the original challenge text and each lives in one place
  in the code, so they are easy to change.