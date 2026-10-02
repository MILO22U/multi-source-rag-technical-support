---
{
  "doc_id": "job-payloads",
  "title": "Job payloads",
  "doc_type": "reference",
  "version": "3.2",
  "product_area": "payloads",
  "last_updated": "2026-05-19",
  "canonical": true,
  "url": "https://docs.zephyrqueue.dev/reference/payloads"
}
---

# Job payloads

## Size limit

A job payload may be at most **256 KiB (262,144 bytes)** after JSON
serialisation and before transport compression. The limit has been 256 KiB since
v1.0 and is the same on every plan.

Exceeding it returns:

```http
HTTP/1.1 413 Payload Too Large
{"error": "payload_too_large", "max_bytes": 262144, "received_bytes": 491020}
```

The limit is measured on the serialised bytes, not on the size of your in-memory
object. UTF-8 multi-byte characters and base64 padding both count, so a payload
that looks comfortably small in Python can still be rejected.

## Checking size before enqueue

```python
import json

encoded = json.dumps(payload).encode("utf-8")
if len(encoded) > 262_144:
    raise ValueError(f"payload is {len(encoded)} bytes, limit is 262144")
```

## The claim-check pattern

For anything approaching the limit, do not put the data in the payload. Write it
to object storage and enqueue a reference:

```python
client.enqueue("transcode", {
    "bucket": "uploads",
    "key": "raw/2026/09/clip-8831.mov",
    "sha256": "9f2c...",
})
```

This keeps payloads small, makes retries cheap, and avoids re-sending large
blobs on every attempt. Treat it as the default for any binary or
user-uploaded content rather than an optimisation.

## Serialisation rules

- Payloads must be JSON objects at the top level — arrays and bare scalars are
  rejected with `invalid_payload_type`.
- Keys must be strings; nesting is limited to 32 levels.
- `NaN`, `Infinity` and `-Infinity` are rejected.
- Binary data must be base64-encoded, which inflates it by roughly 33% — account
  for that against the 256 KiB ceiling.

## Compression

The SDK gzips request bodies over 1 KiB automatically. Compression affects
transport only; the 256 KiB limit applies to the uncompressed serialised
payload, so compressing does not let you exceed it.
