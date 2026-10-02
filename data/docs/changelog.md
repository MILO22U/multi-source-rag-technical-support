---
{
  "doc_id": "changelog",
  "title": "Changelog",
  "doc_type": "changelog",
  "version": "3.2",
  "product_area": "release_notes",
  "last_updated": "2026-09-12",
  "canonical": true,
  "url": "https://docs.zephyrqueue.dev/changelog"
}
---

# Changelog

## v3.2 — 2026-09-12

**Removed**

- The `--legacy-ack` flag has been **removed** from `zephyr worker start`.
  Passing it now exits with code 1 and `unknown_flag: --legacy-ack`. The
  two-phase acknowledgement protocol it enabled was deprecated in v3.0 and is no
  longer supported by the dispatcher. Remove it from your worker startup scripts
  before upgrading.
- `client.queue.push()` remains available but now emits a `DeprecationWarning`
  on every call. It is scheduled for removal in v4.0; use `client.enqueue()`.

**Added**

- `zephyr dlq replay-all --queue <q>` for bulk replay.
- `X-Zephyr-Quota-Remaining` on all responses.

## v3.1 — 2026-04-22

**Fixed**

- The SDK now honours the `Retry-After` header on `429` responses. In v3.0 the
  header was parsed but discarded, and retries used plain exponential backoff
  instead. Under sustained rate limiting this exhausted `max_retries` much
  faster than intended and sent jobs to the DLQ prematurely. Any workload seeing
  jobs dead-letter during rate-limit episodes should upgrade.
- Windows CLI support restored. A symlink-resolution bug in the credential store
  caused `zephyr` to fail on startup with `credential_store_unreadable` on all
  Windows hosts running v3.0. Windows is a supported platform from 3.1 onward.

## v3.0 — 2026-01-15

**Breaking**

- Authentication header changed from `Authorization: Zephyr <key>` to
  `X-Zephyr-Key: <key>`. The old form returns `401 unauthorized_header_form`.
- Default `max_retries` raised from **3 to 5** for newly created queues.
  Existing queues keep their stored value.
- Default visibility timeout lowered from **60s to 30s**.
- Fixed 2-second retry delays replaced with exponential backoff (base 1s,
  factor 2) and mandatory full jitter.

**Deprecated**

- `client.queue.push()` in favour of `client.enqueue()`.
- The `--legacy-ack` worker flag.

## v2.4 — 2025-06-03

- Final 2.x release. Security fixes only from this point; end of support was
  2026-06-30.
- Default `max_retries` is 3; retry delay is a fixed 2 seconds; default
  visibility timeout is 60 seconds.
