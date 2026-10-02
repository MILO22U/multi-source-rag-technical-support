---
{
  "doc_id": "cli-reference",
  "title": "CLI reference",
  "doc_type": "reference",
  "version": "3.2",
  "product_area": "cli",
  "last_updated": "2025-11-08",
  "canonical": true,
  "url": "https://docs.zephyrqueue.dev/reference/cli",
  "maintenance_note": "This page lags the changelog; see changelog.md for authoritative flag availability."
}
---

# CLI reference

Install with `pip install zephyr-cli`. The CLI reads `ZEPHYR_API_KEY` from the
environment, or from `~/.config/zephyr/credentials`.

## Global options

| Flag | Description |
|---|---|
| `--project <id>` | Override the configured project |
| `--json` | Emit machine-readable JSON instead of a table |
| `--quiet` | Suppress progress output |
| `--timeout <s>` | HTTP timeout for CLI requests (default 30) |

## zephyr enqueue

```bash
zephyr enqueue <queue> --payload '{"id": 42}' [--max-retries N] [--timeout S]
zephyr enqueue <queue> --payload-file job.json
```

## zephyr worker

```bash
zephyr worker start --queue emails [--concurrency N] [--legacy-ack]
zephyr worker stop --graceful
zephyr worker status
```

| Flag | Description |
|---|---|
| `--concurrency N` | Number of concurrent handlers. Defaults to 10. |
| `--legacy-ack` | Use the pre-v3 two-phase acknowledgement protocol. |
| `--graceful` | Finish in-flight jobs before exiting. |

## zephyr dlq

```bash
zephyr dlq list --queue <queue> [--limit N]
zephyr dlq replay <job-id>
zephyr dlq replay-all --queue <queue> [--limit N]
zephyr dlq purge --queue <queue> --confirm
```

| Flag | Description |
|---|---|
| `--queue <queue>` | Restrict to one queue |
| `--limit N` | Maximum rows or jobs to act on (default 50) |
| `--confirm` | Required for `purge`; there is no undo |

## zephyr queues

```bash
zephyr queues list
zephyr queues create <name> --max-retries 5 --backoff-base 1
zephyr queues describe <name>
```

## Exit codes

| Code | Meaning |
|---|---|
| 0 | Success |
| 1 | Generic error |
| 2 | Authentication failure |
| 3 | Resource not found |
| 4 | Rate limited; retry later |
