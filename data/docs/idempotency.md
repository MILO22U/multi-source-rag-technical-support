---
{
  "doc_id": "idempotency",
  "title": "Idempotency",
  "doc_type": "reference",
  "version": "3.2",
  "product_area": "idempotency",
  "last_updated": "2026-06-20",
  "canonical": true,
  "url": "https://docs.zephyrqueue.dev/reference/idempotency"
}
---

# Idempotency

## Idempotency keys

Pass `idempotency_key` to deduplicate enqueues. Two enqueues with the same key
on the same queue produce one job; the second returns the original job and does
not create a duplicate.

```python
client.enqueue("invoices", payload, idempotency_key=f"invoice-{invoice_id}")
```

Keys are scoped per queue and may be up to 255 characters. Use a deterministic
business identifier, never a random UUID generated at call time — a fresh UUID
per attempt defeats the entire mechanism.

## Key retention

**Idempotency keys are retained for 24 hours** from first use. After that window
the key is forgotten and the same key will create a new job.

That TTL bounds the deduplication guarantee: it protects against retry storms,
duplicate webhook deliveries and double-submits, but it is not a permanent
uniqueness constraint. If you need deduplication over a longer horizon, enforce
it in your own database.

## At-least-once delivery

Zephyr delivers **at least once**, not exactly once. A job can be delivered more
than once even with an idempotency key, because the key deduplicates *enqueues*,
not *deliveries*. Redelivery happens when:

- a worker claims a job and dies before acknowledging it,
- the handler runs longer than the visibility timeout,
- a network partition hides the acknowledgement from the dispatcher.

Handlers must therefore be idempotent themselves.

## Making handlers idempotent

```python
@worker.handler
def charge(job):
    key = job.payload["invoice_id"]
    if db.already_charged(key):
        return                      # safe to run twice
    charge_card(job.payload)
    db.mark_charged(key)
```

Writing a completion marker in the same transaction as the side effect is the
reliable pattern. Checking a marker written in a *separate* transaction leaves a
window where both the check and the side effect can run twice.

## Inspecting deduplication

```python
job = client.enqueue("invoices", payload, idempotency_key="invoice-991")
print(job.deduplicated)   # True when an existing job was returned
```
