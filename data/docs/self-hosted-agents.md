---
{
  "doc_id": "self-hosted-agents",
  "title": "Self-hosted agents",
  "doc_type": "reference",
  "version": "3.2",
  "product_area": "concurrency",
  "last_updated": "2026-07-11",
  "canonical": true,
  "url": "https://docs.zephyrqueue.dev/reference/self-hosted-agents"
}
---

# Self-hosted agents

A self-hosted agent runs on your own infrastructure and polls Zephyr for jobs.
It is a different deployment model from managed workers, with different defaults.

## Agent concurrency defaults

**A self-hosted agent defaults its concurrency to the host's CPU count**
(`os.cpu_count()`), because unlike managed workers it has exclusive use of the
machine and can see real capacity.

```bash
zephyr-agent start --queue emails              # concurrency = CPU count
zephyr-agent start --queue emails --concurrency 4
```

This differs from managed workers, which default to a fixed 10 regardless of
host CPU count — see [Concurrency and workers](concurrency-and-workers.md).
The two numbers are not in conflict; they describe different components, and
which one applies depends on how you deploy.

| | Managed workers | Self-hosted agents |
|---|---|---|
| Default concurrency | 10 (fixed) | CPU count |
| Runs on | Zephyr infrastructure | Your infrastructure |
| Scaling | `--concurrency`, more workers | `--concurrency`, more agents |
| Started with | `zephyr worker start` | `zephyr-agent start` |

## Installation

```bash
pip install zephyr-agent
zephyr-agent configure --project prj_8831
```

The agent needs outbound HTTPS to `api.zephyrqueue.dev:443`. No inbound ports
are required; dispatch is poll-based.

## Health and supervision

```bash
zephyr-agent status
zephyr-agent start --queue emails --heartbeat-seconds 10
```

Run the agent under a supervisor (systemd, supervisord) and let it restart on
failure. In-flight jobs are redelivered after their visibility timeout expires,
so an abrupt restart loses no work.

## When to choose which

Use managed workers unless you have a specific reason not to. Self-hosted agents
make sense when handlers need local resources — GPUs, licensed software, data
that cannot leave your network.
