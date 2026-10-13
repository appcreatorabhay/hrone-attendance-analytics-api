# DECISIONS.md

1. **Indexes.** I create a unique `employees.emp_code` index and a unique compound `attendance_logs(emp_code, date)` index to enforce the natural keys under concurrent requests. I also index employee department/join date, attendance date/employee for the sorted list, date/late minutes for monthly late queries, and employee/punch-in for punch-out lookup. I considered a separate status-first index but did not add it because the date-based list and monthly analytics are the primary access paths and extra indexes increase write cost.

2. **Punch-in race.** Both requests may pass the employee lookup, but only one can insert the same `(emp_code, date)`: MongoDB's unique compound index rejects the other insert. The API catches `DuplicateKeyError` and returns 409, so correctness does not depend on a check-then-insert sequence.

3. **Ties.** The leaderboard uses MongoDB's standard competition `$rank` over total late minutes. The limit is applied to rank, not row position, so all employees tied at the cutoff rank are returned; rows are then ordered by total minutes descending and `emp_code` ascending.

4. **Headcount.** Department summary starts from eligible employees and looks up each employee's logs. Therefore employees who joined by month end remain in the department total even if the lookup finds no attendance documents.

5. **100x data.** I would benchmark the explain plans and workload, then consider pre-aggregated daily/monthly summaries and a background rebuild strategy. I would only add them after defining consistency guarantees so summaries cannot silently diverge from source attendance records.
