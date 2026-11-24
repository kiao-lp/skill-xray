---
document:
  status: DRAFT
  role: PRIMARY_SOURCE
---

# Xray Cloud operations

## Tests and planning

`search`, `get` and `related` accept `test`, `precondition`, `set`, `plan`, `execution` and `requirement`. Restrict `search` with JQL, `--keys` or `--ids`.

| Issue | `related` values |
| --- | --- |
| `test` | `preconditions`, `sets`, `plans`, `executions`, `requirements`, `runs` |
| `plan` | `tests`, `executions` |
| `execution` | `tests`, `plans`, `runs` |
| `set`, `precondition`, `requirement` | `tests` |

```bash
python3 "$XRAY_CLI" project QA
python3 "$XRAY_CLI" create test QA 'Validate checkout' --type Manual --steps steps.json
python3 "$XRAY_CLI" create plan QA 'Release checks' --tests QA-17 QA-18
python3 "$XRAY_CLI" create execution QA 'Chrome qualification' --tests QA-17 --environment chrome
python3 "$XRAY_CLI" link add plan QA-22 executions QA-31
python3 "$XRAY_CLI" link add precondition QA-21 tests QA-17 QA-18
python3 "$XRAY_CLI" folder get QA /
```

Manual `steps.json` is an array such as `[{"action":"Open checkout","result":"The cart is shown"}]`. `step add TEST STEP_JSON` and `step update STEP_ID STEP_JSON` take one step object. Use `--gherkin FILE` for Cucumber, `--definition FILE` for Generic tests and preconditions, and `--type` for the configured type name. `create --fields FILE` supplies additional Jira field values as an object. Use the Jira skill for issue fields on existing issues. `folder tests PROJECT /path` lists the tests and `folder` also supports `create`, `rename` and `move`. [Xray GraphQL schema](https://xray.cloud.getxray.app/doc/graphql/)

## Runs and coverage

```bash
python3 "$XRAY_CLI" statuses QA
python3 "$XRAY_CLI" runs --plan QA-22 --status "$STATUS_ID" --limit 10
python3 "$XRAY_CLI" run "$RUN_ID" --details
python3 "$XRAY_CLI" record "$RUN_ID" --status "$STATUS"
python3 "$XRAY_CLI" record-step "$RUN_ID" "$STEP_ID" --status "$STEP_STATUS" --actual-result 'Cart was shown'
python3 "$XRAY_CLI" defects add "$RUN_ID" QA-34
python3 "$XRAY_CLI" evidence "$RUN_ID" screenshot.png
```

Use `statuses QA --steps` for step status names. A run status can also change step statuses. Record the actual step results when the request concerns individual steps. [Xray status calculation](https://docs.getxray.app/space/XRAYCLOUD/44565101)

`get requirement KEY` reads Xray coverage with final statuses taking precedence. `related requirement KEY tests` lists the contributing tests. To change coverage links, use the Jira skill and the configured Jira issue link type and direction. Find them under `testCoverageSettings` in `project` output. [Xray coverage links](https://docs.getxray.app/space/XRAYCLOUD/655360823)

## Custom GraphQL

Use `graphql QUERY.graphql [--variables VARIABLES.json]` for fields or mutations outside the named commands. Select only the fields needed. Supply `limit` to each paginated connection. `Test.status` and `CoverableIssue.status` accept `environment`, `version` and the numeric `testPlan` issue ID to scope coverage. Redirect large results to a file and read the needed fields. [Xray GraphQL API](https://getxraydocs.atlassian.net/wiki/spaces/XRAYCLOUD/pages/44565894/GraphQL+API)
