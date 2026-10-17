---
name: Bug report
about: Report a problem
labels: bug
---

## What happened

<!-- Describe the problem clearly. -->

## What you expected

<!-- Describe the expected behavior. -->

## Steps to reproduce

1.
2.
3.

## Environment

- OS:
- systemd version:
- Python:
- LiteLLM version:
- Codex version:

## Logs

Redact keys, endpoints, and resource names before sharing logs. Get recent service logs with:

```text
journalctl --user -u litellm-foundry-gateway.service -n 50 --no-pager
```

Do not report security vulnerabilities here; see SECURITY.md.
