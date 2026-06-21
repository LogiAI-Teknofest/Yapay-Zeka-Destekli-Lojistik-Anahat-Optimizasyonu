# LogiAI Lojistik Optimizasyon Sistemi

Decision support system that optimizes logistics linehaul transportation capacity allocation and vehicle routing to minimize costs under volume, fleet, and site constraints.

## Overview

The LogiAI Lojistik Optimizasyon Sistemi addresses the linehaul planning problem, which involves shipping cargo from multiple origin transfer centers to their final destinations. The system aims to minimize overall daily freight expenditures, prevent vehicle overloading, and ensure deliveries meet time-bound service level agreements (SLAs).

To achieve this, the system implements a two-stage hybrid optimization engine:
1. **Stage 1 (Greedy Capacity Allocation):** Orders daily origin-destination cargo demands by size and greedily loads them onto pre-negotiated, fixed-rate rental vehicles allocated to specific routes. This stage maximizes the utilization of dedicated fleet resources.
2. **Stage 2 (OR-Tools Spot VRP):** Aggregates any remaining overflow (spill demand) by origin. Each origin's overflow is solved as an independent Open Vehicle Routing Problem (VRP) using Google OR-Tools. The solver routes and assigns spot vehicles from a heterogeneous fleet under strict constraints, including a 10% minimum load threshold for active spot trucks and truck-docking restrictions.

The system outputs detailed assignment logs, GIS routing trajectories, and cost breakdowns. These outputs are served via a FastAPI gateway, rendered on an interactive Streamlit dashboard, and compiled into multi-sheet Excel reports. Long-running optimization tasks are handled asynchronously using a Redis-backed job queue and polling pattern, preventing UI blocks and HTTP timeouts. While the current setup serves demand forecasting via a placeholder statistical baseline endpoint (`/api/predict`), the architecture is fully designed to integrate a future deep-learning LSTM (Long Short-Term Memory) time-series forecasting model.

## Architecture

The system is structured as a multi-layered application consisting of a Presentation Layer, API Gateway Layer, Optimization Engine Layer, Data/State Layer, and a Storage Layer.

```
                      +---------------------------------------+
                      |          Streamlit Dashboard          |
                      +-------------------+-------------------+
                                          |
                                    HTTP Requests
                                          v
                      +-------------------+-------------------+
                      |            FastAPI API                |
                      +-------------------+-------------------+
                                          |
                        +-----------------+-----------------+
                        |                                   |
                 Reads / Writes                       Writes Jobs
                        v                                   v
              +---------+----------+              +---------+----------+
              |  Redis State/ETA   |              |    Redis Queue     |
              +--------------------+              +--------------------+
                                                            ^
                                                      Polls / Updates
                                                            |
                                                  +---------+----------+
                                                  | Background Workers |
                                                  +---------+----------+
                                                            |
                                                    Runs Optimization
                                                            v
                                                  +---------+----------+
                                                  | Pipeline Result    |
                                                  +--------------------+
                                                            |
                                                 +----------+----------+
                                                 |                     |
                                                 v                     v
                                          [Greedy Stage]         [OR-Tools VRP]
```

- **Presentation Layer:** Contains the Streamlit dashboard (`src/app/dashboard.py`) and CLI entrypoint (`src/main.py`) which handle user input, render metrics, and display Folium maps and Plotly charts.
- **API Gateway Layer:** A FastAPI web server (`src/app/main.py`) that manages HTTP endpoints, handles CORS and GZip middleware, and runs optimization tasks in the background using FastAPI `BackgroundTasks`. It coordinates with `job_manager.py` to create and update asynchronous job states in Redis, allowing clients to query job status via periodic polling.
- **Optimization Engine Layer:** Implements the greedy allocation logic (`src/optimization/greedy.py`) and the OR-Tools solver (`src/optimization/vrp_solver.py`) which builds and executes VRP constraint models.
- **Data/State Layer:** Contains immutable domain data structures (`src/models/data_types.py`), the JSON schema sanitizer (`src/utils/data_loader.py`), the Redis job queue manager (`src/app/job_manager.py`), and the Redis state wrapper (`src/utils/state_manager.py`).
- **Storage Layer:** Manages file storage in `data/` and runtime states in Redis.

## Project Structure
repo/
├── data/
│   ├── processed/          # Directory containing generated Excel reports and optimized output JSON files
│   └── raw/                # Directory containing raw input Excel files, coordinates, and consolidated parameters JSON
├── src/
│   ├── app/
│   │   ├── dashboard.py    # Streamlit web dashboard serving KPI, GIS, fleet, and Excel download views
│   │   ├── job_manager.py  # Redis manager handling job lifecycles and concurrency locking
│   │   └── main.py         # FastAPI gateway serving endpoints, reports, and scheduling background jobs
│   ├── models/
│   │   └── data_types.py   # Immutable dataclasses defining domain contracts and validation logic
│   ├── optimization/
│   │   ├── greedy.py       # First-stage optimizer allocating cargo to contracted rental vehicles
│   │   └── vrp_solver.py   # Second-stage OR-Tools open VRP solver for spot vehicles with fallback heuristics
│   ├── preprocessing/
│   │   └── logiai_mvp_preprocessing.py # Script converting raw Excel spreadsheets to the unified JSON schema
│   ├── utils/
│   │   ├── config.py       # Configuration parser managing connection details and variables
│   │   ├── data_loader.py  # Sanitizer module verifying JSON structure and ignoring corrupt nodes
│   │   └── state_manager.py# Redis interface tracking vehicle states, package loads, and route ETAs
│   └── main.py             # CLI runner orchestrating the two-stage pipeline for single or multiple dates
├── tests/
│   ├── mock_generator.py   # Mock demand generator for standalone pipeline verification
│   ├── test_edge_cases.py  # Resilience test suite evaluating data sanitization with bad inputs
│   └── test_suite.py       # Main test suite verifying Redis state transitions and schema loaders
├── Dockerfile              # Multi-stage configuration building FastAPI and Streamlit images
├── docker-compose.yml      # Orchestration stack mapping API, dashboard, and Redis containers
└── start.sh                # Docker quick-start shell script

## Requirements

The project can be run locally or within Docker containers.

- Docker Engine 20.10+
- Docker Compose v2.0+
- Python 3.11 (if running locally)
- Redis Server 7.0+ (if running locally)

## Getting Started

### Docker

Build and run the entire stack using the Docker Compose configuration:

```bash
docker compose up -d --build
```

Alternatively, use the provided startup script which checks for Docker and starts the containers:

```bash
chmod +x start.sh
./start.sh
```

Once running, the services are exposed at:
- API Gateway: `http://localhost:8000`
- Interactive API Docs: `http://localhost:8000/docs`
- Streamlit Dashboard: `http://localhost:8501`

### Local

To run the application locally without Docker:

1. Start a local Redis server on port 6379:
   ```bash
   redis-server --port 6379
   ```

2. Install backend dependencies and start the FastAPI gateway:
   ```bash
   pip install -r src/app/requirements.txt
   python -m uvicorn src.app.main:app --host 0.0.0.0 --port 8000
   ```

3. Install dashboard dependencies and launch Streamlit:
   ```bash
   pip install -r src/app/dashboard_requirements.txt
   streamlit run src/app/dashboard.py --server.port 8501 --server.address 0.0.0.0
   ```

## Configuration

The system is configured via environment variables.

| Variable | Description | Example |
|----------|-------------|---------|
| REDIS_HOST | Hostname of the Redis instance | localhost |
| REDIS_PORT | Port number of the Redis instance | 6379 |
| REDIS_DB | Database index to use within Redis | 0 |
| DATA_DIR | Directory containing raw input parameters | data/raw |
| OUTPUT_DIR | Directory where processed files are output | data/processed |
| INPUT_JSON | Path to the consolidated input JSON | data/processed/logiai_mvp_input.json |
| API_BASE | Gateway endpoint queried by the dashboard | http://localhost:8000 |
| ALLOWED_ORIGINS | Comma-separated list of permitted CORS origins | http://localhost:8501 |

## How It Works

1. **Data Preprocessing:** Running `logiai_mvp_preprocessing.py` parses raw coordinates, vehicle specs, and demands. It maps and normalizes city names, computes pairwise distances using the Haversine formula, and compiles cost matrices into a single JSON schema.
2. **Schema Sanitization:** `data_loader.py` reads the consolidated JSON file. It performs schema checking and validates date formats. Bad values (e.g., negative distances or non-numeric demands) are flagged with warnings and omitted, preventing pipeline crashes.
3. **Stage 1 (Greedy Capacity Allocation):** `greedy.py` aggregates and sorts daily cargo demands from largest to smallest. It assigns cargo to dedicated rental vehicles running matching routes up to their capacity. Overflow cargo is written to a "spill demand" registry.
4. **Stage 2 (OR-Tools Spot VRP):** `vrp_solver.py` processes spill demand grouped by origin. For each origin group, it constructs a VRP model. It applies integer scaling to float demands (with a scale factor of 10) and assigns spot vehicles to routes. It enforces a 10% minimum load limit on active spot trucks and restricts large trucks from cities without docking clearance.
5. **Stage 2 Fallback:** Any cargo nodes that remain unassigned due to OR-Tools constraints are processed by a direct-assignment fallback loop. This loop calculates the cheapest combination of spot vehicles to ship the remaining cargo directly, guaranteeing a feasible solution.
6. **State Tracking:** `state_manager.py` records vehicle utilization rates, routes, and arrival times (ETAs) in Redis, which are read by the API endpoints.
7. **Report Compilation:** When requested, the backend processes optimization outputs and generates an Excel report with distinct sheets mapping vehicle assignments, overall demand, and cost distributions.
8. **Demand Forecasting Placeholder:** The `GET /api/predict` endpoint provides a statistical baseline demand estimation (historical average and standard deviation). This serves as an API interface placeholder for a future deep-learning LSTM (Long Short-Term Memory) time-series forecasting model using quantile regression (Pinball Loss).
9. **Asynchronous Job Scheduling:** When an optimization run is initiated, `POST /api/optimize/async` creates a job in Redis with status `PENDING`. A background task runs the optimization pipeline asynchronously, transitioning the job's state through `RUNNING` to `COMPLETED` or `FAILED` while limiting concurrency to 4 parallel runs via a semaphore.
10. **State Polling:** The Streamlit dashboard queries the `GET /api/jobs/{job_id}` endpoint every 3 seconds to retrieve real-time job status and execution progress, caching the final results in the Streamlit session state once the job is completed.

## Output

The system produces:
1. **JSON Outputs:** Comprehensive runtime logs containing status keys, vehicle paths, and costs.
2. **Excel Workbooks:** Consolidated reports with three specific spreadsheets:
   - **Cozum:** Chronological transport log with column fields: `Tarih`, `Arac Tipi`, `Cikis TM`, `Varis TM`, `Atanan Desi`, `Maliyet`.
   - **Talep Ozeti:** Input demand log mapping daily cargo volumes.
   - **Maliyet Analizi:** Analytical breakdown detailing fixed rental expenditures, spot vehicle bills, and total project costs.

## Tech Stack

| Technology | Purpose |
|------------|---------|
| Python 3.11 | Core programming language runtime |
| FastAPI 0.104.1 | High-performance ASGI web framework for backend APIs |
| Uvicorn 0.24.0 | ASGI web server running the FastAPI application |
| Streamlit 1.29.0 | Frontend framework for building interactive KDS views |
| OR-Tools 9.15.6755 | Google constraint programming engine solving VRP routes |
| Redis 7.0 (Alpine) | In-memory key-value database managing job queues and states |
| Pandas | High-performance library for loading and managing datasets |
| Numpy | Vectorized mathematical operations and array calculations |
| Plotly | Interactive graphical charts embedded in Streamlit |
| openpyxl | Excel parsing engine generating sheet outputs |
| Folium | Leaflet-based map rendering displaying routing paths |

## License

MIT