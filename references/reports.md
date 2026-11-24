---
document:
  status: DRAFT
  role: PRIMARY_SOURCE
---

# Xray Cloud imports and exports

```bash
python3 "$XRAY_CLI" import-results xray results.json --validate-only
python3 "$XRAY_CLI" import-results junit junit.xml --execution QA-31
python3 "$XRAY_CLI" import-tests tests.json
python3 "$XRAY_CLI" import-status "$JOB_ID"
python3 "$XRAY_CLI" export-cucumber features.zip --keys QA-17 QA-18
```

`import-results` accepts `xray`, `cucumber`, `behave`, `junit`, `nunit`, `xunit`, `testng` and `robot`. For XML, use `--execution` to update an existing Test Execution or `--project` to create one in a project. Use `--plan` and `--environment` when applicable. For Xray JSON, put the destination in `testExecutionKey` or `info`. For Cucumber and Behave reports, retain the Xray issue tags emitted from the feature files. [Execution imports](https://getxraydocs.atlassian.net/wiki/spaces/XRAYCLOUD/pages/44577176)

```json
{"testExecutionKey":"QA-31","tests":[{"testKey":"QA-17","status":"PASSED"}]}
```

Use status names configured for the project. `--validate-only` checks report structure locally. Xray checks project fields, result semantics and permissions when importing. `testInfo` can create or change Test definitions during an Xray JSON import. [Xray JSON format](https://getxraydocs.atlassian.net/wiki/spaces/XRAYCLOUD/pages/44565311)

`import-tests` accepts an array of up to 1000 Xray test definitions. Each new test can specify `testtype`, `steps` and `fields` with `summary` and `project.key`. `--validate-only` checks the JSON and batch size. Keep the returned `jobId` and check `import-status` for the outcome. Use `--details` to retrieve per-issue errors or the complete list of imported keys. [Bulk test imports](https://getxraydocs.atlassian.net/wiki/spaces/XRAYCLOUD/pages/44577092/Importing+Tests+-+REST+v2)

`export-cucumber` saves a ZIP at a new path. Select Jira keys or a saved Jira filter with `--filter`. [Cucumber exports](https://getxraydocs.atlassian.net/wiki/spaces/XRAYCLOUD/pages/44577687)
