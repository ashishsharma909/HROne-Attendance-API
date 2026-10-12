# DECISIONS.md

*(About 150–200 words, plain language.)*

1. **Indexes.** I created seven indexes.
   `employees.emp_code` (unique) enforces uniqueness and backs 404 lookups.
   `employees.(department, joined_on)` and `employees.joined_on` back headcount filtering and department summary aggregations.
   `attendance_logs.(emp_code, date)` (unique) is the natural key: it prevents duplicate punch-ins atomically and backs single-record reads and monthly aggregations.
   `attendance_logs.(date, emp_code)` serves the list-attendance sort (`date` DESC, `emp_code` ASC) and date-range filtering.
   `attendance_logs.(emp_code, punch_in)` backs the punch-out lookup ("most-recent open record").
   `attendance_logs.(date, late_minutes)` backs the late leaderboard date range and late filter without collection scans.
   I considered an index on `status` alone but omitted it because status has low cardinality.

2. **Punch-in race.** Both requests reach `insert_one` concurrently. MongoDB enforces the unique `(emp_code, date)` index: exactly one insert wins and gets 201; the other raises `DuplicateKeyError`, which the handler catches and returns as 409. No `find_one` check is involved, so there is no window between check and insert.

3. **Ties at the leaderboard cutoff.** `$rank` assigns the same rank to all employees with the same `total_late_minutes` and skips the next rank (1, 2, 2, 4). After ranking, I keep every row whose `rank <= limit`. If two employees share rank 10 and `limit=10`, both are returned — the result set has more than 10 rows, which is correct per the spec.

4. **Headcount for employees with no logs.** The department summary starts its aggregation from `employees`, not from `attendance_logs`. A `$match` on `joined_on <= month_end` finds all qualifying employees; a `$group` counts them per department. The attendance stats are aggregated separately and joined by department name in Python. Employees who never logged a day contribute to `headcount` but not to any attendance sum.

5. **One thing I would change for 100× the data.** The trend endpoint queries all employee `joined_on` dates in Python and loops over rows to compute per-day headcount. At 100× scale I would push this into the aggregation pipeline using `$lookup` with a `$match` condition on `joined_on <= $$day`, eliminating the Python loop and letting MongoDB do the counting inside a single pass.
