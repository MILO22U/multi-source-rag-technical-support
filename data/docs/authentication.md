---
{
  "doc_id": "authentication",
  "title": "Authentication",
  "doc_type": "reference",
  "version": "3.2",
  "product_area": "auth",
  "last_updated": "2026-06-02",
  "canonical": true,
  "url": "https://docs.zephyrqueue.dev/reference/auth"
}
---

# Authentication

## API keys

Every request must carry a project API key in the **`X-Zephyr-Key`** header:

```http
POST /v3/queues/emails/jobs HTTP/1.1
Host: api.zephyrqueue.dev
X-Zephyr-Key: zq_live_8f2a91c4e7b3
Content-Type: application/json
```

The SDK reads the key from the `ZEPHYR_API_KEY` environment variable when no
key is passed explicitly:

```python
from zephyr import Client

client = Client()                       # reads ZEPHYR_API_KEY
client = Client(api_key="zq_live_...")  # explicit
```

## Header change in v3.0

Before v3.0 the key was sent as `Authorization: Zephyr <key>`. That form was
removed in v3.0 and now returns `401 unauthorized_header_form`. If you are
upgrading from 2.x, changing the header is a required migration step — see
[Migrating from v2 to v3](migration-v2-to-v3.md).

## Key types and scopes

| Prefix | Type | Scope |
|---|---|---|
| `zq_live_` | Live | Full read/write on production queues |
| `zq_test_` | Test | Read/write on test queues only; jobs are never dispatched |
| `zq_ro_` | Read-only | Status and DLQ inspection; enqueue returns `403` |

Read-only keys are the right choice for dashboards and alerting.

## Rotation

Create the replacement key first, deploy it, then revoke the old one. Both keys
remain valid during the overlap, so rotation needs no downtime. Revocation takes
effect within 30 seconds.

## Common failures

| Status | Code | Cause |
|---|---|---|
| 401 | `missing_api_key` | No `X-Zephyr-Key` header present |
| 401 | `unauthorized_header_form` | Using the pre-v3.0 `Authorization: Zephyr` form |
| 401 | `revoked_api_key` | Key was revoked |
| 403 | `insufficient_scope` | Read-only key used for enqueue |
