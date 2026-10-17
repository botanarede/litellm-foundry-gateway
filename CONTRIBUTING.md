# Contributing

Please read the relevant sections of [README.md](README.md) before contributing.

## Project rules

- Keep changes focused and avoid over-engineering.
- Use English for code, comments, documentation, and messages.
- Never log, print, or commit secrets.
- Do not duplicate canonical files in `deploy/`; update the source of truth instead.

## Testing

Run the offline unit suite:

```text
~/.local/share/litellm-foundry-gateway/venv/bin/pytest tests/unit -q
```

The full process is documented in README.md under **Testing**. The suite requires no network access or keys.

## Adding Foundry models

Add a model through configuration only; see **Adding Foundry models** in README.md. Regenerate the catalog as described in README.md under **Model catalog**.

## Security

Report security issues privately according to [SECURITY.md](SECURITY.md), never through public issues.

## Commits and license

Keep commits small and atomic, with a clear message. This project is licensed under MIT, and all contributions are provided under the same license.
