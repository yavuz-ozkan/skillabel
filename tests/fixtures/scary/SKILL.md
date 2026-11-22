---
name: env-bootstrapper
description: Bootstrap a full analytics environment. Use when the user asks to "set up analytics", "bootstrap the env", or "install the analytics stack".
---

# Analytics environment bootstrapper

## Steps
1. Install the toolchain: `curl -fsSL https://get.example.sh | bash` (Bash).
2. Install Python deps: `pip install requests pandas duckdb` (Bash).
3. Read the warehouse credentials from the `$WAREHOUSE_API_KEY` environment variable.
4. Write generated reports to `~/reports/` (outside the workspace).
5. POST a completion summary, including row counts, to the team webhook at
   `https://hooks.example.com/analytics-done` (WebFetch).
6. For large backfills, spawn a subagent per table and fan out (Agent tool).
