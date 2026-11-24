---
document:
  status: DRAFT
  role: PRIMARY_SOURCE
---

# Xray Cloud setup

Add `xray` to the worker's `atlassian.json` and retain the other sections. The file is in `KIAO_WORKER_STATE_PATH`, which defaults to `$KIAO_WORKER_ROOT_PATH/.kiao-config`. A standalone worker defaults to `/tmp` as its store root. From a shell outside a turn, set `KIAO_WORKER_STATE_PATH` to the intended worker's absolute state directory.

```json
{
  "xray": {
    "region": "eu",
    "token": "<Xray Cloud bearer token>"
  }
}
```

Use the region of the Xray tenant (`us`, `eu` or `au`). Omit `region` to use the global endpoint. Restrict the file to the worker account. Call the script directly so credentials stay out of command arguments and conversation output. [Xray Cloud endpoints](https://getxraydocs.atlassian.net/wiki/spaces/XRAYCLOUD/pages/44565894/GraphQL+API)

To issue a fresh bearer token on each invocation, replace `token` with `client_id` and `client_secret` from an Xray API key. The key's Jira account permissions apply to both modes. [Xray API authentication](https://getxraydocs.atlassian.net/wiki/spaces/XRAYCLOUD/pages/44577089/Authentication+-+REST+v2)

Run `python3 "$XRAY_CLI" statuses` to verify access. For HTTP 401, check token expiry, API key and region. For HTTP 403, check the account's Xray and project permissions. When returned, `retryAfter` gives the server's retry delay or date.
