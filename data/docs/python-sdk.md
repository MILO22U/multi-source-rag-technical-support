---
{
  "doc_id": "python-sdk",
  "title": "Python SDK",
  "doc_type": "api",
  "version": "3.2",
  "product_area": "sdk",
  "last_updated": "2026-09-01",
  "canonical": true,
  "url": "https://docs.zephyrqueue.dev/api/python"
}
---

# Python SDK

```bash
pip install zephyr-sdk
```

## Client

```python
from zephyr import Client

client = Client(api_key="zq_live_...", timeout=30.0, max_connections=20)
```

`api_key` defaults to `ZEPHYR_API_KEY`. The client is thread-safe and holds a
connection pool, so construct one per process and reuse it.

## enqueue

```python
job = client.enqueue(
    queue="emails",
    payload={"to": "a@example.com", "template": "welcome"},
    max_retries=5,
    timeout=30,
    idempotency_key="welcome-user-8831",
    delay_seconds=0,
)
print(job.id, job.status)   # job_8f2a91c4 queued
```

`client.enqueue()` is the supported way to submit a job.

### Deprecated: queue.push

`client.queue.push(queue, payload)` is the pre-v3 form. It still works in v3.2
and emits a `DeprecationWarning`, and it is scheduled for removal in v4.0.
It accepts no `idempotency_key` and no `timeout`, so code using it cannot take
advantage of either feature. Older forum threads and blog posts frequently show
`push()`; translate it to `enqueue()` when you adapt them.

```python
client.queue.push("emails", payload)   # deprecated
client.enqueue("emails", payload)      # use this
```

## enqueue_many

```python
jobs = client.enqueue_many("emails", [p1, p2, p3])   # max 100 per call
```

Counts as one request against the rate-limit quota. Partial failures are
reported per item; the call does not raise if some items succeed.

## Handlers

```python
from zephyr import Worker, PermanentFailure

worker = Worker(client, queue="emails", concurrency=10)

@worker.handler
def send(job):
    if not job.payload.get("to"):
        raise PermanentFailure("no recipient")   # skip remaining retries
    deliver(job.payload)

worker.run()
```

Raising `PermanentFailure` sends the job straight to the DLQ. Any other
exception consumes a retry attempt.

## Status and DLQ

```python
job = client.get_job("job_8f2a91c4")
print(job.status, job.attempt_count, job.last_error)

for job in client.dlq.list(queue="emails", limit=100):
    client.dlq.replay(job.id)
```

## Exceptions

| Exception | Raised when |
|---|---|
| `AuthError` | 401/403 — bad, revoked or under-scoped key |
| `PayloadTooLarge` | 413 — serialised payload exceeds 256 KiB |
| `RateLimited` | 429 — carries `.retry_after` in seconds |
| `PermanentFailure` | Raised *by you* to skip remaining retries |
| `ZephyrTimeout` | Client-side HTTP timeout |
