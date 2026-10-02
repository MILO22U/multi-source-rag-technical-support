---
{
  "doc_id": "quickstart",
  "title": "Quickstart",
  "doc_type": "tutorial",
  "version": "3.2",
  "product_area": "getting_started",
  "last_updated": "2026-08-28",
  "canonical": true,
  "url": "https://docs.zephyrqueue.dev/quickstart"
}
---

# Quickstart

Enqueue and process your first job in about five minutes.

## 1. Install

```bash
pip install zephyr-sdk zephyr-cli
```

## 2. Authenticate

Create a key in the console, then export it:

```bash
export ZEPHYR_API_KEY=zq_live_8f2a91c4e7b3
```

The SDK and CLI both read `ZEPHYR_API_KEY` and send it as the `X-Zephyr-Key`
header.

## 3. Create a queue

```bash
zephyr queues create emails --max-retries 5
```

Defaults for a new queue: 5 retries, exponential backoff from 1 second, 30-second
visibility timeout.

## 4. Enqueue a job

```python
from zephyr import Client

client = Client()
job = client.enqueue("emails", {"to": "a@example.com", "template": "welcome"})
print(job.id)        # job_8f2a91c4
print(job.status)    # queued
```

## 5. Run a worker

```python
from zephyr import Client, Worker

worker = Worker(Client(), queue="emails", concurrency=10)

@worker.handler
def send(job):
    print("sending to", job.payload["to"])

worker.run()
```

```bash
zephyr worker start --queue emails
```

## 6. Watch what happens

```bash
zephyr worker status
zephyr dlq list --queue emails
```

A healthy queue has an empty DLQ. If jobs appear there, read `last_error` first —
it names the exception class from the final attempt.

## Next steps

- [Retries and backoff](retries-and-backoff.md) — tune the retry policy
- [Idempotency](idempotency.md) — make handlers safe under redelivery
- [Dead-letter queue](dead-letter-queue.md) — inspect and replay failures
- [Migrating from v2 to v3](migration-v2-to-v3.md) — if you are upgrading
