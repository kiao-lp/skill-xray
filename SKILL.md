---
name: xray
description: Manage Xray Cloud tests, preconditions, sets, plans, executions, run results and requirement coverage in Jira. Use for test management, Cucumber features and importing test results through the Kiao worker.
---

# Xray Cloud

Set `XRAY_CLI` to the absolute path of [scripts/xray.py](scripts/xray.py) in this skill directory. Run it with Python 3.11 or later. It reads `xray` credentials from the worker's `atlassian.json`.

```bash
python3 "$XRAY_CLI" search test --jql 'project = QA ORDER BY key' --limit 10
python3 "$XRAY_CLI" get test QA-17 --details
python3 "$XRAY_CLI" related plan QA-22 executions --limit 10
python3 "$XRAY_CLI" related execution QA-31 runs --limit 10
python3 "$XRAY_CLI" import-results junit results.xml --execution QA-31
```

Use known Jira keys directly. Reuse returned `issueId` values to skip key lookups and use the run `id` to record results. Commands return compact JSON. Fetch one page and pass `nextStart` with `--start` when needed. Request `--details` for definitions or run steps. After an uncertain write, read back the target before retrying.

Use `COMMAND --help` for arguments. Read only the reference for the operation at hand.

- [Tests, planning, execution and coverage](references/operations.md) for definitions, links, run updates and custom GraphQL.
- [Imports and exports](references/reports.md) for local validation, CI reports, bulk imports and Cucumber features.
- [Worker setup](references/setup.md) for authentication or configuration errors.
