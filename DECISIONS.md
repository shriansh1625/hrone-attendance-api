# Design decisions

1. **Indexes.** Unique `emp_code` and `(emp_code, date)` enforce identity and one record per day. `(emp_code, punch_in)` selects the latest eligible punch. Date/code and status/date/code indexes serve attendance ordering and monthly ranges; employee/status/date covers combined filters. Employee department/code, department/joined_on and joined_on/department indexes serve employee listing and eligible headcounts. I rejected an index on `history`: audit entries are returned, not searched, so it would add write cost without helping these queries.

2. **Punch-in race.** Both requests validate and resolve the employee, then attempt an insert. MongoDB's unique natural-key index accepts one. The other insert raises DuplicateKeyError, which becomes 409. No prior attendance existence check is needed.

3. **Ties.** `$rank` ranks totals before the cutoff filter. Every row with rank <= limit survives, including all cutoff ties; the next rank is skipped. Equal totals are ordered by employee code.

4. **Headcount.** The summary starts from eligible employees. Empty attendance lookups still contribute one employee to headcount and zero attendance metrics. Work-hour sums and record counts are combined before dividing.

5. **100x data.** I would maintain daily per-employee aggregates, invalidate them after corrections, and derive reports from those smaller records, while keeping the original audit history authoritative.
