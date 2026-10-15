# DECISIONS.md

1. **Indexes.**  I created a unique index on employees.emp_code and a unique compound index on attendance_logs(emp_code, date) to avoid duplicate employees and attendance records. I also added indexes for department, joining date, attendance date, late minutes, and punch-in time to make queries faster. I considered a status-first index but skipped it because date-based queries are more important.

2. **Punch-in race.** If two identical requests arrive together, both may find the employee, but the unique index allows only one attendance record. The second request gets a DuplicateKeyError, which my API handles by returning HTTP 409 Conflict.

3. **Ties.** The leaderboard uses $rank based on total late minutes. If employees are tied at the cutoff, all of them are included. Results are sorted by late minutes and employee code.

4. **Headcount.** The department summary starts with eligible employees and looks up their attendance records. This ensures employees with no attendance logs are also counted.

5. **100x data.** I would first test query performance with more data. If needed, I would store pre-calculated summaries and update them in the background to make reports faster.
