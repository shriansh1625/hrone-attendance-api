# Validation

Verified locally on 10 October 2026 with Python 3.13.12 and a real, isolated MongoDB 7.0.14 server.

- **64 tests passed**: validation, timestamp units/types, grace boundaries, second truncation, half-up rounding, half days, overtime thresholds, overnight dates, duplicate writes, stale corrections, audit history, filters, pagination and generated response schemas. This count includes parameterized input cases; it is not 64 independent business scenarios.
- Analytics were compared with independent Python calculations on deterministic randomized data covering 30 employees, multiple departments, missing records, future and mid-month joiners, weekends, presence variants and legacy half-day defaults. API implementation performs its analytics in MongoDB, not in those Python reference functions.
- Explicit tie tests checked ranks 1, 2, 2, 4 and a cutoff returning all ties. Calendar tests checked no-log departments, zero headcounts, weekends and the 92-day range bound.
- Real executionStats explain checked every explain endpoint for IXSCAN and absence of COLLSCAN. A separate test checks BSON cluster metadata serialization used by replica sets/Atlas.
- Twelve concurrent duplicate creates and punch-ins gave one 201 and eleven 409s. Twelve concurrent punch-outs gave one 200 and eleven 409s. Eight correction requests were forced to read the same snapshot: one 200, seven 409s, one audit entry.

## 100,000-record benchmark

Synthetic data: 1,000 employees, 10 departments, 100 days of attendance, one log per employee/day. It was loaded before startup with only MongoDB's default indexes. The revised app created its nine indexes automatically in **1.710 seconds**. The measured month had 31,000 logs; the requested trend department had 100 employees. Filesystem and server caches may have helped.

| Main query | Local HTTP time | Explain contains IXSCAN | Explain contains COLLSCAN |
|---|---:|---|---|
| Attendance list, month range, page 4 | 0.0234 s | Yes | No |
| Employee monthly | 0.0169 s | Yes | No |
| Department summary, all departments | 0.6601 s | Yes | No |
| Late leaderboard | 0.1972 s | Yes | No |
| Department trend, 31 days | 0.0785 s | Yes | No |

## Audit of the first published version

The original 59 passing tests missed two behavior defects. Mixed Unicode digits in a shift clock were accepted and later caused a punch request to return 500. Punch-out also recalculated stored lateness, although the contract assigns that calculation to punch-in and corrections. ASCII clock/code validation and preservation of stored lateness on punch-out now have regression coverage. Explicit response models also replace the originally empty generated response schemas.

The original trend concatenated per-employee log arrays. A second benchmark concentrated all 100,000 logs in one department, with indexes already present:

| Department layout | Requested days | Original trend | Revised trend |
|---|---:|---|---|
| 1,000 employees, 100 logs each | 92 | 2.665 s | 1.360 s |
| 10,000 employees, 10 logs each | 10 | Exceeded 5-second server budget | 4.265 s |

The revised pipeline groups joined-date cohorts and daily metrics in separate `$facet` branches. It does not concatenate all department logs into one document. Both revised runs returned the expected row count, headcounts, present counts and weekday/weekend rates. The 4.265-second case still shows that employee count matters; daily summaries would be needed at much larger scale. These synthetic layouts do not establish worst-case performance.

## Interpretation and limits

`attendance_pct` is treated as a percentage reported to two decimals; `attendance_rate` and `moving_avg_7d` are fractional rates reported to four. For one present day out of 23 working days, these are 4.35 percent and 0.0435 respectively. R8 says "rates" without explicitly classifying the monthly percentage, so this is a documented interpretation rather than proof of the hidden grader's expectation.

These are local measurements, not latency guarantees. The employer's data and hidden checks are unavailable. The Python reference calculations are separately implemented algorithms written as part of this submission, not an external independent review. The local integration server was MongoDB 7.0.14. The GitHub Actions workflow runs the full suite on Python 3.11 with both MongoDB 6 and 7; current results are available in the repository's Actions tab.
