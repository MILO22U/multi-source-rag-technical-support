---
{
  "doc_id": "rate-limits",
  "title": "Rate limits",
  "doc_type": "reference",
  "version": "3.2",
  "product_area": "rate_limits",
  "last_updated": "2026-07-30",
  "canonical": true,
  "url": "https://docs.zephyrqueue.dev/reference/rate-limits"
}
---

# Rate limits

## Quotas

The API accepts **600 requests per minute** per project, measured as a sliding
window. Enqueue, status and DLQ operations all draw on the same quota. Batch
enqueue counts as a single request regardless of how many jobs it carries, which
makes batching the most effective way to stay inside the quota.

## Rate-limit responses

Exceeding the quota returns HTTP `429` with a `Retry-After` header expressed in
seconds:

```http
HTTP/1.1 429 Too Many Requests
Retry-After: 12
X-Zephyr-Quota-Remaining: 0
```

## How the SDK handles 429

**The SDK honours `Retry-After`.** When a `429` carries the header, the next
attempt is scheduled at or after that interval and the computed exponential
backoff delay is ignored for that attempt. When the header is absent the SDK
falls back to normal exponential backoff.

This behaviour requires **v3.1 or later**. In v3.0 the SDK computed backoff
without consulting `Retry-After`, which under sustained rate limiting produced
retry storms that exhausted `max_retries` far sooner than expected. Upgrade to
3.1+ if you are seeing that pattern.

## Staying inside the quota

- Batch with `client.enqueue_many()` — up to 100 jobs per request.
- Keep worker concurrency proportional to your quota; 10 workers polling
  aggressively can saturate 600 rpm on their own.
- Treat `X-Zephyr-Quota-Remaining` as a backpressure signal rather than waiting
  for a `429`.

## Per-queue throughput limits

Separate from the API quota, each queue has a dispatch ceiling of 1,000 jobs per
second. Exceeding it does not return an error; jobs simply queue and `queue_lag`
rises. Monitor `queue_lag` rather than inferring throughput from enqueue latency.
