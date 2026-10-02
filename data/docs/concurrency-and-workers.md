---
{
  "doc_id": "concurrency-and-workers",
  "title": "Concurrency and workers",
  "doc_type": "reference",
  "version": "3.2",
  "product_area": "concurrency",
  "last_updated": "2026-07-11",
  "canonical": true,
  "url": "https://docs.zephyrqueue.dev/reference/workers"
}
---

# Concurrency and workers

## Managed worker concurrency

Managed workers run **10 concurrent handlers** by default. Override per worker:

```bash
zephyr worker start --queue emails --concurrency 25
```

```python
worker = Worker(client, queue="emails", concurrency=25)
```

The ceiling is 200 per worker process. The default of 10 is a fixed number, not
derived from the host's CPU count — managed workers are scheduled on shared
infrastructure where the visible CPU count does not reflect available capacity.

Self-hosted agents, which are a separate deployment option, use a different
default; see [Self-hosted agents](self-hosted-agents.md).

## Visibility timeout

When a worker claims a job the job becomes invisible to other workers for the
**visibility timeout, 30 seconds by default**. If the worker has not acknowledged
the job by then, the job becomes visible again and another worker may claim it.

```python
client.create_queue("reports", visibility_timeout=300)
```

The default dropped from 60s to 30s in v3.0. Set it above the realistic
worst-case duration of your handler: a visibility timeout shorter than the
handler's runtime causes duplicate execution, which is the most common cause of
"my job ran twice" reports.

### Extending the lease

Long-running handlers should extend the lease rather than request a large
blanket timeout:

```python
@worker.handler
def transcode(job):
    for chunk in chunks:
        process(chunk)
        job.heartbeat()      # resets the visibility timeout
```

## Scaling guidance

Throughput is `concurrency × workers / mean_handler_seconds`. Raise concurrency
for I/O-bound handlers; add worker processes for CPU-bound ones, since a single
process shares one interpreter.

Watch `queue_lag` (seconds between enqueue and first dispatch). Rising lag at
flat concurrency means you are worker-bound; flat lag with rising latency means
handlers are slowing down.

## Graceful shutdown

```bash
zephyr worker stop --graceful
```

In-flight jobs finish, no new jobs are claimed, and the process exits when the
last handler returns. Without `--graceful`, in-flight jobs are abandoned and
become visible again after their visibility timeout expires.
