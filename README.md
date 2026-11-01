# Manufacturing Production Scheduling System

A constraint-aware manufacturing scheduler for planning, validating, and rescheduling production orders across machines while considering routing, machine capacity, shifts, downtime, changeovers, labor, and material availability.

The system provides a reproducible scheduling workflow, an independent constraint validator, anomaly detection, performance metrics, a REST API, and an interactive Streamlit dashboard.

---

## Table of Contents

- [Overview](#overview)
- [Business Problem](#business-problem)
- [Objectives](#objectives)
- [Key Features](#key-features)
- [Technology Stack](#technology-stack)
- [System Architecture](#system-architecture)
- [Scheduling Workflow](#scheduling-workflow)
- [Scheduling Rules](#scheduling-rules)
- [Data Model](#data-model)
- [Dashboard](#dashboard)
- [Validation and Anomaly Detection](#validation-and-anomaly-detection)
- [Performance Metrics](#performance-metrics)
- [Project Structure](#project-structure)
- [Installation](#installation)
- [Running the Project](#running-the-project)
- [API Documentation](#api-documentation)
- [Testing](#testing)
- [Expected Outputs](#expected-outputs)
- [Future Enhancements](#future-enhancements)
- [Author](#author)

---

## Overview

The **Manufacturing Production Scheduling System** helps manufacturing planners create feasible production schedules for multiple orders and machines.

Production scheduling is challenging because each order may require multiple operations, and every operation must be assigned to an eligible machine while satisfying operational constraints. The system evaluates available production capacity, operation routing, machine calendars, changeover requirements, labor availability, and material readiness before creating a schedule.

The application supports:

- Production schedule generation
- Machine-wise operation allocation
- Order prioritization
- Constraint validation
- Schedule comparison
- Rescheduling
- Anomaly injection and detection
- Production performance analysis
- Data browsing and export

The project uses seeded synthetic data to make experiments reproducible and suitable for testing.

---

## Business Problem

Manufacturing facilities need to schedule jobs across multiple machines while respecting several real-world constraints.

These include:

- **Routing:** Operations must follow the required sequence for each product.
- **Machine eligibility:** An operation can only run on a compatible machine.
- **Machine capacity:** A machine cannot process overlapping operations.
- **Machine calendars:** Operations must fit within available working periods.
- **Shifts and downtime:** Scheduling must account for working hours and unavailable periods.
- **Changeovers:** Switching between product types may require additional setup time.
- **Labor availability:** Operations must respect available workforce and labor shifts.
- **Material availability:** Required materials must be available before production starts.
- **Order priorities:** High-priority orders should receive appropriate scheduling preference.
- **Due dates:** The schedule should reduce late deliveries and total tardiness.

The goal is to generate a feasible schedule, explain unscheduled operations, and provide useful metrics for evaluating production performance.

---

## Objectives

1. Build a working end-to-end production scheduling application.
2. Generate reproducible synthetic manufacturing data.
3. Schedule production orders according to routing and operational constraints.
4. Support multiple scheduling rules for comparison.
5. Independently validate generated schedules.
6. Detect and report injected scheduling anomalies.
7. Provide dashboard visualizations and production metrics.
8. Support schedule comparison, rescheduling, and data export.
9. Provide automated tests and reproducible setup instructions.

---

## Key Features

### Production Scheduling

- Schedule operations across eligible machines.
- Respect operation precedence and routing.
- Consider machine calendars, shifts, and downtime.
- Account for sequence-dependent changeover time.
- Consider material and labor constraints.
- Report operations that cannot be scheduled.

### Scheduling Rule Comparison

The system supports scheduling strategies such as:

- **EDD:** Earliest Due Date
- **PRIORITY_EDD:** Order priority followed by due date
- **CRITICAL_RATIO:** Scheduling based on critical ratio

Different rules can be evaluated to compare their impact on production performance.

### Independent Schedule Validation

The validator checks schedules independently of the scheduling engine. It identifies constraint violations such as:

- Machine overlaps
- Routing or precedence violations
- Operations outside available working periods
- Material availability violations
- Missing or incorrect changeover requirements
- Other schedule consistency issues

### Anomaly Detection

The project supports labelled anomaly scenarios for testing the validator. Anomaly records include issue types and ground-truth labels so that detected issues can be compared with the injected scenarios.

### Rescheduling and Comparison

Users can create alternative scheduling runs, compare results, and explore rescheduling scenarios.

### Data Management and Export

The application supports browsing project data and exporting available data for further analysis.

---

## Technology Stack

| Component | Technology |
|---|---|
| Programming language | Python |
| Frontend dashboard | Streamlit |
| REST API | FastAPI |
| Database | SQLite |
| ORM | SQLAlchemy |
| Data processing | Pandas, NumPy |
| Scheduling / optimization | OR-Tools |
| Visualization | Plotly |
| Data validation | Pydantic |
| Synthetic data generation | Faker |
| Testing | Pytest, HTTPX |

---

## System Architecture

The project follows a modular architecture that separates data management, scheduling, validation, services, API endpoints, and user interface components.

```text
                 ┌─────────────────────────┐
                 │   Streamlit Dashboard  │
                 │  KPIs, Gantt, Reports  │
                 └────────────┬────────────┘
                              │
                 ┌────────────▼────────────┐
                 │       FastAPI           │
                 │     REST Endpoints      │
                 └────────────┬────────────┘
                              │
          ┌───────────────────┼───────────────────┐
          │                   │                   │
┌─────────▼─────────┐ ┌───────▼────────┐ ┌────────▼─────────┐
│ Scheduling Engine │ │ Validator      │ │ Metrics / Reports│
│ Rule-based logic  │ │ Constraint     │ │ KPI calculations│
└─────────┬─────────┘ │ checks         │ └────────┬─────────┘
          │           └────────────────┘          │
          └───────────────────┬───────────────────┘
                              │
                 ┌────────────▼────────────┐
                 │    SQLite Database     │
                 │  SQLAlchemy Models     │
                 └────────────┬────────────┘
                              │
                 ┌────────────▼────────────┐
                 │ Synthetic Data Generator│
                 │ Seeded Master / Order   │
                 │ and Production Data     │
                 └─────────────────────────┘
```

---

## Scheduling Workflow

1. **Initialize the database**  
   Create the database and required tables.

2. **Generate manufacturing data**  
   Create products, routings, operations, machines, calendars, materials, inventory, and production orders using a reproducible seed.

3. **Load production requirements**  
   Read order priorities, due dates, routing requirements, and operation details.

4. **Evaluate scheduling constraints**  
   Check machine eligibility, calendars, shifts, downtime, changeovers, labor, and material readiness.

5. **Generate a schedule**  
   Apply the selected scheduling rule and assign feasible start and end times.

6. **Validate the schedule**  
   Run independent checks to identify violations and inconsistencies.

7. **Calculate metrics**  
   Derive production KPIs from the generated schedule.

8. **Display results**  
   View schedules, exceptions, metrics, and Gantt charts in the dashboard.

9. **Compare or reschedule**  
   Create alternative runs and evaluate their results.

---

## Scheduling Rules

### Earliest Due Date (EDD)

Prioritizes orders with the earliest due dates. This rule provides a simple baseline for evaluating delivery performance.

### Priority followed by Earliest Due Date (PRIORITY_EDD)

Considers order priority first and uses due date as a secondary ordering criterion.

### Critical Ratio (CRITICAL_RATIO)

Uses the relationship between remaining time until the due date and remaining processing time to determine scheduling urgency.

The rules can be compared using metrics such as makespan, on-time delivery, tardiness, utilization, and unscheduled orders.

---

## Data Model

The manufacturing data model represents production requirements and available resources.

| Entity | Purpose |
|---|---|
| Product | Defines manufactured products or SKUs |
| Routing | Defines the production route for a product |
| Operation | Represents a production step |
| Machine | Defines available production equipment |
| Machine Calendar | Represents machine working periods |
| Machine Downtime | Represents unavailable machine periods |
| Material | Defines raw materials and components |
| Inventory Lot | Tracks material quantity and availability |
| Production Order | Defines quantity, priority, and due date |
| Operation Schedule | Stores scheduled operation timing and machine assignment |
| Changeover Matrix | Defines setup time between product or operation types |
| Scheduling Run | Stores a scheduling execution and its configuration |
| Scheduling Exception | Records unscheduled operations and reasons |
| Anomaly Record | Stores injected and detected scheduling issues |

The database uses primary keys, foreign keys, and validation rules to maintain relationships and data consistency.

---

## Dashboard

The Streamlit dashboard provides an interactive interface for exploring scheduling results.

### Home

- Scheduling run overview
- Key performance indicators
- Late order summary
- Makespan and violation information

### Gantt

- Machine-wise production timeline
- Scheduled operation start and end times
- Visual inspection of machine allocation

### Orders / Exceptions

- Production order status
- Scheduled and unscheduled operations
- Scheduling exception details

### Engine

- Select a scheduling rule
- Generate a new scheduling run
- Compare scheduling outcomes

### Compare / Reschedule

- Compare scheduling runs
- Explore alternative schedules
- Reschedule from a selected time

### Data / Export

- Browse available data
- Inspect database records
- Export data for analysis

### Anomalies

- Run anomaly scenarios
- View injected issues
- Review validator findings

---

## Validation and Anomaly Detection

The validator checks whether a generated schedule satisfies the defined manufacturing constraints.

Validation categories include:

| Validation | Description |
|---|---|
| Machine overlap | Detects operations assigned to the same machine at overlapping times |
| Precedence | Checks whether routing operations follow the required sequence |
| Machine eligibility | Checks whether operations use compatible machines |
| Calendar compliance | Checks whether operations fit within available machine working periods |
| Material availability | Checks whether required material is available before production |
| Changeover | Checks required setup time between applicable operations |
| Schedule consistency | Checks timing and relationship consistency |

The anomaly workflow supports controlled testing by injecting labelled issues and comparing them with validator findings.

---

## Performance Metrics

The system calculates production metrics from scheduling results.

| Metric | Description |
|---|---|
| Makespan | Total elapsed time from the first scheduled operation to the last |
| Production Time | Total processing time across scheduled operations |
| Changeover Time | Time reserved for required machine setup |
| Machine Utilization | Proportion of available machine time used for production |
| On-Time Delivery | Percentage of completed or scheduled orders meeting their due dates |
| Tardiness | Delay beyond an order's due date |
| Late Orders | Number of orders that miss their due dates |
| Unscheduled Orders | Orders or operations that could not be placed |
| Violation Count | Number of detected constraint violations |
| Material Violations | Number of material-related scheduling issues |
| Feasibility Rate | Proportion of evaluated schedules or operations satisfying defined feasibility checks |

Metrics should be interpreted alongside the validator results. A schedule with violations must not be presented as fully feasible.

---

## Project Structure

```text
mfg-scheduler/
│
├── app/
│   ├── Home.py
│   └── pages/
│       ├── 01_Gantt.py
│       ├── 02_Orders_Exceptions.py
│       ├── 03_Engine.py
│       ├── 04_Compare_Reschedule.py
│       ├── 05_Data_Export.py
│       └── 06_Anomalies.py
│
├── src/
│   ├── api/
│   ├── database/
│   ├── data/
│   ├── engine/
│   ├── scheduler/
│   ├── services/
│   ├── validation/
│   └── metrics/
│
├── tests/
├── data/
├── outputs/
├── config/
├── requirements.txt
├── requirements-lock.txt
├── .env.example
├── .gitignore
└── README.md
```

*The exact filenames may vary depending on the final repository implementation.*

---

## Installation

### 1. Clone the repository

```powershell
git clone https://github.com/navyadhanalakshmi-bandaru/mfg-scheduler.git
cd mfg-scheduler
```

### 2. Create a virtual environment

```powershell
py -3.11 -m venv .venv
```

### 3. Activate the virtual environment

```powershell
.\.venv\Scripts\Activate.ps1
```

If PowerShell blocks activation, use:

```powershell
Set-ExecutionPolicy -Scope Process -ExecutionPolicy Bypass
.\.venv\Scripts\Activate.ps1
```

### 4. Install dependencies

```powershell
python -m pip install --upgrade pip
python -m pip install -r requirements-lock.txt
```

If the lock file is not available, install from:

```powershell
python -m pip install -r requirements.txt
```

---

## Running the Project

Run commands from the project root with the virtual environment activated.

### 1. Initialize the database and data

```powershell
python -m src.services.bootstrap
```

This initializes the database and prepares the data required by the application.

### 2. Start the Streamlit dashboard

```powershell
streamlit run app/Home.py
```

Open the application in your browser:

```text
http://localhost:8501
```

Use the sidebar to navigate between dashboard pages.

### 3. Run the scheduling engine

```powershell
python -m src.engine.dispatcher full
```

This runs the scheduling engine in command-line mode and displays scheduling results.

### 4. Run anomaly detection

```powershell
python -m src.validation.anomalies full
```

This executes the anomaly workflow and displays validation findings.

### 5. Stop the dashboard

Press:

```text
Ctrl + C
```

in the terminal where Streamlit is running.

---

## API Documentation

Start the FastAPI server:

```powershell
uvicorn src.api.app:app --reload
```

Open the interactive API documentation:

```text
http://localhost:8000/docs
```

The Swagger interface allows users to inspect available endpoints and test supported API requests.

---

## Testing

Run the automated test suite:

```powershell
pytest -q
```

The tests cover the implemented data, scheduling, validation, API, and application behavior.

For a reproducible final verification, run the tests after installing dependencies in a clean environment.

---

## Expected Outputs

When the project is running, users can explore:

- A generated production schedule
- Machine-wise Gantt chart
- Scheduled and unscheduled production orders
- Scheduling exceptions and explanations
- Comparison of different scheduling rules
- Rescheduling results
- Production KPI dashboard
- Independent validation report
- Injected and detected anomaly report
- Data tables and exportable records
- Automated test results

---

## Future Enhancements

Potential future improvements include:

- Advanced CP-SAT optimization objectives
- Multi-objective optimization for cost, throughput, and delivery performance
- More detailed labor and shared-tool constraints
- Real-time machine status integration
- Live production data integration
- User authentication and role-based access
- Cloud deployment
- Additional scheduling strategies and scenario analysis

---

## Author

**Bandaru Navya Dhana Lakshmi**

B.Tech — Computer Science and Design  
Specialization: Artificial Intelligence and Machine Learning

GitHub: [navyadhanalakshmi-bandaru](https://github.com/navyadhanalakshmi-bandaru)

Repository: [mfg-scheduler](https://github.com/navyadhanalakshmi-bandaru/mfg-scheduler)

---

## License

Add the license applicable to this repository before distributing or reusing the project.
