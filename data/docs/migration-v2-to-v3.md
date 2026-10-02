---
{
  "doc_id": "migration-v2-to-v3",
  "title": "Migrating from v2 to v3",
  "doc_type": "guide",
  "version": "3.2",
  "product_area": "migration",
  "last_updated": "2026-09-05",
  "canonical": true,
  "url": "https://docs.zephyrqueue.dev/guides/migrate-v2-v3"
}
---

# Migrating from v2 to v3

Support for 2.x ended on 2026-06-30. This guide covers every breaking change
between 2.4 and 3.2 in the order you should address them.

## 1. Change the authentication header

The `Authorization: Zephyr <key>` form was removed in v3.0. Use `X-Zephyr-Key`.

```diff
- Authorization: Zephyr zq_live_8f2a91c4
+ X-Zephyr-Key: zq_live_8f2a91c4
```

Symptom if missed: every request returns `401 unauthorized_header_form`. This is
the single most common upgrade failure, and it looks like a revoked key rather
than a header problem.

## 2. Review retry defaults

Default `max_retries` rose from **3 to 5** in v3.0. Queues created under 2.x keep
their stored value of 3; newly created queues get 5. Mixed fleets therefore
behave inconsistently until you set the value explicitly.

```python
client.update_queue("emails", max_retries=5)
```

Pin `max_retries` per queue during migration rather than relying on defaults.

## 3. Account for exponential backoff

v2.4 retried on a fixed 2-second delay. v3 uses exponential backoff with full
jitter (base 1s, factor 2). Total time to exhaust the retry budget grows from
roughly 10 seconds to roughly 30 seconds, and individual delays are no longer
predictable. Alerting that assumed a bounded fixed retry window needs retuning.

## 4. Raise visibility timeouts if needed

The default visibility timeout dropped from **60s to 30s**. Handlers that
reliably took 40 seconds under v2 now exceed the default and get redelivered,
which presents as sudden duplicate execution after an otherwise clean upgrade.

```python
client.update_queue("reports", visibility_timeout=300)
```

## 5. Replace deprecated SDK calls

```diff
- client.queue.push("emails", payload)
+ client.enqueue("emails", payload)
```

`push()` still works in 3.2 but warns, and is removed in 4.0.

## 6. Remove the --legacy-ack flag

`--legacy-ack` was deprecated in v3.0 and **removed in v3.2**. Worker startup
fails with `unknown_flag` if it is still present. Remove it from systemd units,
Dockerfiles and process managers before upgrading.

## 7. Upgrade to 3.1+ for correct rate-limit handling

v3.0 ignored the `Retry-After` header on `429` responses. If your workload is
rate-limit sensitive, do not stop at 3.0 — go to 3.1 or later.

## Migration checklist

- [ ] Auth header changed to `X-Zephyr-Key`
- [ ] `max_retries` set explicitly per queue
- [ ] Retry-window alerting retuned for jitter
- [ ] Visibility timeouts raised where handlers run long
- [ ] `queue.push()` replaced with `enqueue()`
- [ ] `--legacy-ack` removed from all worker invocations
- [ ] Running 3.1 or later
