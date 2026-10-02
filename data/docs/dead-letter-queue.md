---
{
  "doc_id": "dead-letter-queue",
  "title": "Dead-letter queue",
  "doc_type": "reference",
  "version": "3.2",
  "product_area": "dlq",
  "last_updated": "2026-08-01",
  "canonical": true,
  "url": "https://docs.zephyrqueue.dev/reference/dlq"
}
---

# Dead-letter queue

## What lands in the DLQ

A job moves to the dead-letter queue when it has exhausted every retry attempt
permitted by its retry policy. With the v3 default of 5 retries that means 6
failed attempts in total.

Three other conditions also route a job to the DLQ:

- the payload fails validation at dispatch time (bad schema, oversized),
- the handler raises `PermanentFailure`, which skips remaining retries by design,
- the job's 7-day TTL expires while it is still pending.

## Replay is manual

**Jobs in the dead-letter queue are never replayed automatically.** There is no
timer, no background sweep and no automatic re-queue after any interval. A job
stays in the DLQ until you replay it or delete it. This is deliberate: a job that
failed six times usually fails a seventh, and automatic replay turns a broken
handler into an unbounded retry loop.

Replay explicitly:

```python
client.dlq.replay("job_8f2a91c4")              # one job
client.dlq.replay_all(queue="emails", limit=500)  # bulk
```

Or from the CLI:

```bash
zephyr dlq replay job_8f2a91c4
zephyr dlq replay-all --queue emails --limit 500
```

Replayed jobs re-enter the queue with a fresh retry budget and the same job id.
Their `attempt_count` continues from where it stopped, so a replayed job that
fails again returns to the DLQ rather than retrying indefinitely.

## Inspecting the DLQ

```bash
zephyr dlq list --queue emails
```

Each row shows the job id, the queue, `attempt_count`, the final error class and
the timestamp of the last attempt.

```python
for job in client.dlq.list(queue="emails", limit=100):
    print(job.id, job.attempt_count, job.last_error)
```

## Retention

DLQ entries are retained for **30 days**, after which they are deleted
permanently. Export anything you need to keep before the window closes; there is
no recovery path afterwards.

## Operational guidance

Alert on DLQ depth rather than polling it. A DLQ that grows steadily indicates a
systematic failure — a bad deploy, an expired credential, a downstream outage —
and replaying before fixing the cause simply refills it.
