# Validation

Verified locally on 10 October 2026 with Python 3.13.12 and a real, isolated MongoDB 7.0.14 server.

- **59 tests passed**: validation, timestamp units/types, grace boundaries, second truncation, half-up rounding, half days, overtime thresholds, overnight dates, duplicate writes, stale corrections, audit history, filters and pagination.
- Analytics were compared with independent Python calculations on deterministic randomized data covering 30 employees, multiple departments, missing records, future and mid-month joiners, weekends, presence variants and legacy half-day defaults. API implementation performs its analytics in MongoDB, not in those Python reference functions.
- Explicit tie tests checked ranks 1, 2, 2, 4 and a cutoff returning all ties. Calendar tests checked no-log departments, zero headcounts, weekends and the 92-day range bound.
- Real executionStats explain checked every explain endpoint for IXSCAN and absence of COLLSCAN. A separate test checks BSON cluster metadata serialization used by replica sets/Atlas.
- Twelve concurrent duplicate creates and punch-ins gave one 201 and eleven 409s. Twelve concurrent punch-outs gave one 200 and eleven 409s. Eight correction requests were forced to read the same snapshot: one 200, seven 409s, one audit entry.

## 100,000-record benchmark

Synthetic data: 1,000 employees, 10 departments, 100 days of attendance, one log per employee/day. It was loaded before startup with only MongoDB's default indexes. The app created its nine indexes automatically in **1.637 seconds**. The measured month had 31,000 logs; the requested trend department had 100 employees. The final run followed an earlier run, so filesystem and server caches may have helped.

| Main query | Local HTTP time | Explain contains IXSCAN | Explain contains COLLSCAN |
|---|---:|---|---|
| Attendance list, month range, page 4 | 0.0313 s | Yes | No |
| Employee monthly | 0.0134 s | Yes | No |
| Department summary, all departments | 0.6810 s | Yes | No |
| Late leaderboard | 0.1497 s | Yes | No |
| Department trend, 31 days | 0.0710 s | Yes | No |

These are local measurements, not latency guarantees. The employer's dataset and hidden checks are unavailable. MongoDB 6 compatibility follows the operators used, but the local integration run used MongoDB 7.0.14. The GitHub Actions workflow is prepared to run the tests on Python 3.11 and MongoDB 7 after publication.
