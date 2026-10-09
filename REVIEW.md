# Starter review

Locations below refer to the supplied starter, before my changes. All application fixes are in `app/main.py`.

| # | Where (starter function / line) | What is wrong | How to notice it | Fix |
|---|---|---|---|---|
| 1 | MongoClient / 22 | No startup indexes; identity, attendance uniqueness and indexed reads are not guaranteed. | Two identical creates/punch-ins can both succeed; explain can show a collection scan. | Create named unique and query indexes idempotently in lifespan. |
| 2 | MongoClient / 22 | Reads return naive datetimes, leaving UTC versus local time ambiguous. | Read a BSON punch and compare it with an aware shift boundary. | Enable `tz_aware=True`; use aware UTC instants throughout. |
| 3 | MongoClient / 22 | Default selection timeout can exceed readiness budget when MongoDB is unreachable. | Start against an unavailable server. | Bound selection and connection timeouts; close the client on shutdown. |
| 4 | compute_late_minutes / 35 | Uses the punch's timezone/calendar day for an IST shift boundary. | Run on a UTC host or punch after midnight for a 22:00–06:00 shift. | Build aware IST boundaries from the attendance day. |
| 5 | compute_late_minutes / 36–37 | Floors before testing grace, so 09:40:01 is treated as on time. | A 09:30 shift with 09:40:01 must return 10, not 0. | Compare seconds strictly against 600, then floor elapsed minutes. |
| 6 | compute_work_hours / 41 | Binary float `round` does not guarantee decimal half-up. | 18 minutes 54 seconds is 0.315 hours and must become 0.32. | Divide integer seconds using Decimal and quantize with `ROUND_HALF_UP`. |
| 7 | compute_overtime / 46 | Overnight shift end is built on the shift's start date. | 22:00–06:00 shift, out at 06:40 next day. | Advance the end boundary one day for an overnight shift. |
| 8 | compute_overtime / 47 | Counts overtime below the 30-minute minimum. | Out 18:35 for an 18:30 shift should give 0. | Floor elapsed minutes and count only totals at least 30. |
| 9 | EmployeeIn / 53–60 | Employee code pattern, name/department lengths and email format are unvalidated. | `EMP123`, empty name, or `a@b` is accepted. | Add strict string constraints matching the contract. |
| 10 | EmployeeIn / 58–60 | Invalid shift clocks, impossible calendar dates and equal shift endpoints are accepted. | `24:00`, `2026-02-30`, or 09:30–09:30. | Validate clock syntax, real ISO dates and distinct endpoints. |
| 11 | PunchInIn / 65 | Integer coercion accepts strings, floats and booleans; timestamp range is unchecked. | String milliseconds, seconds-looking `1783312500`, or oversized integer. | Strict bounded integer milliseconds; reject explicitly supplied null. |
| 12 | PunchInIn / 66 | Arbitrary statuses are accepted at punch-in. | Punch in with `LEAVE` or `UNKNOWN`. | Restrict this endpoint to PRESENT, WFH and ON_DUTY. |
| 13 | health / 74 | Reports ready without checking MongoDB. | Stop the database and `/health` still returns 200. | Ping MongoDB; database failures return 503. |
| 14 | create_employee / 79–83 | Check-then-insert races; no database uniqueness enforcement. | Submit 12 identical concurrent creates. | Insert against the unique index; catch DuplicateKeyError as 409. |
| 15 | create_employee / 82 | Naive local creation time is stored as if UTC. | Run on an IST host and inspect BSON creation time. | Use the server clock in aware UTC, truncated to seconds. |
| 16 | create_employee / 85 | Returns an ISO datetime instead of integer epoch milliseconds. | Inspect `created_at` in the HTTP response. | Explicit employee boundary serializer returns integer milliseconds. |
| 17 | list_employees / 93 | First page skips an entire page. | Create two employees, request page 1. | Offset is `(page - 1) * page_size`. |
| 18 | list_employees / 94 | Total ignores the department filter. | Two departments, query only one. | Count the same query used for the page. |
| 19 | list_employees / 95 | No required emp_code ordering. | Insert EMP0003 before EMP0001. | Sort ascending in MongoDB before pagination. |
| 20 | both list endpoints / 89, 130–131 | Page and page_size bounds are missing. | Page 0 or page_size 101 succeeds. | Shared query constraints enforce page >= 1 and size 1–100. |
| 21 | punch_in / 101, 113 | Missing employee is dereferenced, causing a 500. | Punch in for EMP9999. | Resolve the employee explicitly and return 404. |
| 22 | punch_in / 102 | Host-local naive instant is stored as UTC. | Provide an epoch timestamp on a host outside UTC. | Convert milliseconds using the UTC epoch; derive calendar labels in IST. |
| 23 | punch_in / 102 | Truthiness treats a supplied zero like omitted time. | `punched_at=0` silently uses now. | Validate bounds first; omission is distinct from a supplied value. |
| 24 | punch_in / 102 | Fractional seconds are not truncated before calculation/storage. | 09:40:00.999 must store and return 09:40:00. | Integer millisecond division produces whole-second instants. |
| 25 | punch_in / 103 | Attendance day ignores IST and the overnight exception. | 01:00 on July 7 for a 22:00–06:00 shift belongs to July 6. | Dedicated attendance-day helper implements R1. |
| 26 | punch_in / 104–118 | Duplicate check races. | Parallel punch-ins for one employee/day. | Unique natural-key index plus DuplicateKeyError handling gives one 201. |
| 27 | punch_in / 119, list_attendance / 149 | Exposes an internal attendance id forbidden by the contract. | Response contains `id`. | Whitelist response fields; no ObjectId is exposed in resource responses. |
| 28 | punch_in / 121, list_attendance / 150 | Punch/history dates are not epoch milliseconds. | Inspect nested `history[].changes.punch_in.to`. | Recursive datetime conversion includes nested audit values. |
| 29 | list_attendance / 127–129 | Calendar dates, status values and range ordering are unchecked. | Invalid date or reversed bounds returns 200. | Shared validated types plus inclusive range validation return 422. |
| 30 | list_attendance / 144–147 | Loads, sorts and slices all matching records in Python. | A first-page read grows with 100,000 matching records. | Indexed MongoDB sort/skip/limit; filtered count runs in MongoDB. |
| 31 | list_attendance / 145 | Equal-date records lack the emp_code ascending tie-breaker. | Insert the day's employees in reverse code order. | Use compound sort `(date DESC, emp_code ASC)`. |
| 32 | list_attendance / 148–150 | Legacy missing history/half_day fields are not filled. | Seed a record without those fields. | Supply empty history, false half_day and other documented defaults at the boundary. |
| 33 | filter checks / 91, 134, 142 | Truthiness drops an explicitly supplied empty filter. | Query `department=` unexpectedly lists all departments. | Distinguish `None` from an exact string filter. |

Examined and retained: `load_dotenv()` normally preserves real environment variables; I make `override=False` explicit. `emp_code` as the business identifier is correct. The fixed UTC+05:30 IST offset is appropriate here. Synchronous PyMongo in ordinary `def` FastAPI handlers runs in FastAPI's thread pool. The starter's open-record defaults are correct. The attendance date predicates are inclusive and its filtered total is correct in principle; only their validation/execution needed repair. Ignoring extra client fields is appropriate for server-owned derived fields.

The starter's TODO endpoints were incomplete features, rather than defects in existing code. I implemented punch-out, corrections, the four analytics endpoints and executionStats explain. Corrections use snapshot comparison plus an atomic `$push`; punch-out also compares the snapshot, preventing lost writes between the two operations. Analytics read stored derived values and use database pipelines throughout.
