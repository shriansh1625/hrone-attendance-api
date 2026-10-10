# Employee Attendance & Analytics API

Python 3.11+ and MongoDB 6.0+ are required. All application code is in `app/main.py`.

```sh
python -m venv .venv
# Activate: Windows PowerShell: .\.venv\Scripts\Activate.ps1
# Activate: macOS/Linux: source .venv/bin/activate
pip install -r requirements.txt
uvicorn app.main:app --port 8000
```

Set `MONGO_URI` and `MONGO_DB` in the environment before starting. Defaults are `mongodb://localhost:27017` and `attendance_db`; real environment values take priority over a local `.env`. For example, in PowerShell:

```powershell
$env:MONGO_URI = "mongodb://localhost:27017"
$env:MONGO_DB = "attendance_db"
```

Use an existing MongoDB server, Atlas, or `docker run --rm -p 127.0.0.1:27017:27017 mongo:7`. Indexes are created automatically; startup does not seed, clear or modify attendance data. `/health` checks MongoDB readiness; `/docs` provides interactive API documentation.

All endpoints are implemented, with response schemas in `/docs`. Stored derived fields are used for analytics; punch-out preserves stored lateness, manual changes append an audit entry, and concurrent stale writes return 409. Calendar expansion and moving averages run in MongoDB. Monthly percentages use two decimal places; fractional trend rates use four (see the interpretation in `VALIDATION.md`). No authentication is added to this assignment; keep a running instance local. Application code makes no network calls except to MongoDB.

```sh
pip install -r requirements-dev.txt
# Optional TEST_MONGO_URI selects a test server; default localhost:27017.
python -m pytest -q
```

Tests create and remove their own randomly named database. They cover boundary rules, concurrent requests, seeded legacy shapes, ties, no-log employees, joiners, randomized analytics against separately implemented reference calculations, and explain plans. GitHub Actions runs them on Python 3.11 with MongoDB 6 and 7. See `VALIDATION.md` for the 100,000-record benchmarks, audit findings and limitations, and `REVIEW.md` / `DECISIONS.md` for the review and design rationale.
