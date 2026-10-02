---
{
  "doc_id": "retries-and-backoff",
  "title": "Retries and backoff",
  "doc_type": "reference",
  "version": "3.2",
  "product_area": "retries",
  "last_updated": "2026-08-14",
  "canonical": true,
  "url": "https://docs.zephyrqueue.dev/reference/retries"
}
---

# Retries and backoff

Zephyr retries a job whenever the handler raises an uncaught exception or the
worker fails to acknowledge the job before its visibility timeout expires.

## Default retry policy

Every queue inherits a default of **5 retry attempts** beyond the initial
delivery, giving a maximum of 6 total attempts per job. Set `max_retries` on the
queue to override it, or pass `max_retries` per job to `client.enqueue()` — the
per-job value always wins.

```python
client.enqueue("emails", payload, max_retries=2)
```

The default was raised from 3 to 5 in v3.0 after telemetry showed a large
fraction of transient failures resolving on the fourth or fifth attempt. Queues
created before v3.0 keep whatever value was stored at creation time; the new
default applies only to newly created queues.

## Configuring backoff

Delay between attempts follows **exponential backoff with full jitter**. The
base delay is 1 second and the factor is 2, so nominal delays are 1s, 2s, 4s,
8s, 16s. Full jitter then samples the actual delay uniformly from `[0, nominal]`.

```python
client.create_queue(
    "emails",
    max_retries=5,
    backoff_base_seconds=1,
    backoff_factor=2,
    backoff_max_seconds=300,
)
```

Jitter is not optional and cannot be disabled. Synchronised retries from many
workers are the dominant cause of self-inflicted rate limiting, and jitter is
what breaks up the thundering herd.

`backoff_max_seconds` caps the nominal delay. Once the cap is reached every
subsequent attempt waits a jittered interval drawn from `[0, backoff_max_seconds]`.

## Timeouts

`timeout` bounds how long a single attempt may run before the worker is
considered to have failed.

**`timeout=0` means fail-fast: the job fails immediately on its first attempt
and is retried according to the retry policy.** It does *not* disable the
timeout. To run without a per-attempt time limit, omit `timeout` entirely or set
it to `None`.

```python
client.enqueue("reports", payload, timeout=None)   # no limit
client.enqueue("reports", payload, timeout=0)      # fails instantly — rarely what you want
```

## Rate-limit responses

When the API returns `429`, the SDK reads the `Retry-After` response header and
schedules the next attempt at or after the indicated time, overriding the
computed backoff delay for that attempt. A `429` consumes a retry attempt like
any other failure.

## After the final attempt

Once `max_retries` attempts are exhausted the job moves to the dead-letter
queue. It is not retried again automatically. See
[Dead-letter queue](dead-letter-queue.md) for inspection and replay.
