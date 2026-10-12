# Employee Attendance & Analytics API

FastAPI + PyMongo backend for the HROne engineering assignment.

## Requirements

- Python 3.11+
- MongoDB 6.0+ (local Docker or Atlas free tier)

## Local setup

```bash
python -m venv .venv
# Windows:
.venv\Scripts\activate
# macOS/Linux:
source .venv/bin/activate

# Optional: set environment variables (defaults to localhost:27017 and attendance_db)
# Windows:  $env:MONGO_URI="mongodb://localhost:27017"; $env:MONGO_DB="attendance_db"
# Linux:    export MONGO_URI="mongodb://localhost:27017" MONGO_DB="attendance_db"

# Local MongoDB via Docker:
# docker run -d -p 27017:27017 mongo:7

python sample_seed.py         # loads the 9 sample documents
uvicorn app.main:app --port 8000 --reload
```

## Running (production-like)

```bash
uvicorn app.main:app --port 8000
```

The app is ready when `GET /health` returns `{"status":"ok"}` (≤ 20 s after start).

## Environment variables

| Variable   | Description                          | Default                        |
|------------|--------------------------------------|--------------------------------|
| `MONGO_URI` | MongoDB connection URI              | `mongodb://localhost:27017`    |
| `MONGO_DB`  | Database name                       | `attendance_db`                |

Real environment variables override the `.env` file. **Do not commit `.env`.**

## What's implemented

All 12 endpoints from `openapi.yaml`:

- `GET /health`
- `POST /employees`, `GET /employees`
- `POST /attendance/punch-in`, `POST /attendance/punch-out`
- `GET /attendance`, `PATCH /attendance/{emp_code}/{date}`
- `GET /analytics/employees/{emp_code}/monthly`
- `GET /analytics/departments/summary`
- `GET /analytics/leaderboard/late`
- `GET /analytics/departments/{department}/trend`
- `GET /admin/explain/{endpoint}`

All indexes are created at startup (idempotent). See `DECISIONS.md` for details.
