---
name: litellm-foundry-gateway-xpert
description: Guides installing, verifying, configuring Codex for, diagnosing, and uninstalling the LiteLLM Foundry Gateway. Triggers when the user asks to install, set up, fix, or remove the gateway.
---

# LiteLLM Foundry Gateway: guided setup
Guide a user through a safe, verified gateway installation.
Keep secrets private and stop at every required approval gate.
Run every command from the repository root. The README is authoritative: open the named section and follow its commands; do not retype them from memory.

## Ground rules
- Never open, print, cat, or echo `~/.config/litellm-foundry-gateway/gateway.env` or `~/.codex/.env`. Check them only with counting commands (`grep -c`, `test -s`, `stat -c %a`) whose output holds no secret.
- The user types every secret (Azure endpoint and key) into the file with their own editor. Never ask for secrets in chat or write them.
- Ask the user and wait before installing any package or tool, including `uv`; enabling or starting the systemd unit; editing anything under `~/.codex/`; or deleting anything.
- Never restart or stop a service that this guide did not create.
- After each phase, report one line: done, or the exact blocker.

## Phase 1: pre-flight
In one read-only pass, check Linux x86_64, that `systemctl --user is-system-running` answers, and that `uv`, `curl`, `openssl`, and `python3` are present. Check that the unit and environment file do not already exist. If either exists, ask whether to reuse, repair, or stop.
Ask which Azure AI Foundry deployments the user owns. Compare them with every `model_name` in `gateway/config.yaml`; remove models the user lacks (tell the user which entries you are editing in `gateway/config.yaml`). Decision-1 and `auto` matter only when the user wants optional auto routing: see README sections "Profile D" and "Profile E". Point to README section "Prerequisites".

## Phase 2: Python environment
Follow README section "Install and run the user service" for the venv. Use only `requirements.lock` and `requirements-dev.lock`, exactly as the README says; the development lock lets the user run the tests that demonstrate the setup works. Confirm with the venv's Python that `litellm` imports.

## Phase 3: gateway.env
Follow the README steps that create the environment file with mode 0600 and generate `LITELLM_MASTER_KEY`. Stop and tell the user to open the file in their editor and fill `AZURE_FOUNDRY_API_BASE` and `AZURE_FOUNDRY_API_KEY`; fill `AZURE_FOUNDRY_DECISIONS_BASE` only for Profile D. For other profiles, clear its value so the line reads `AZURE_FOUNDRY_DECISIONS_BASE=` (the placeholder check below would otherwise fail). Do not continue until the user says it is done.
Require all secret-free checks to pass: `stat -c %a` reports 600; `grep -cE` reports no line containing an angle-bracket placeholder; `AZURE_FOUNDRY_API_KEY=` has a non-empty value; `LITELLM_MASTER_KEY` matches `^sk-[0-9a-f]{32}$`; and `TYPESAFE_API_KEY`, `TYPESAFE_API_BASE`, and `GEMINI_API_KEY` are absent. Use `grep -cE` without printing matching lines. A unit start can pass with an unedited placeholder, so never skip this gate.

## Phase 4: service
Follow README section "Install and run the user service" for unit installation. Ask for confirmation before `enable --now`. On failure, read `systemctl --user status` and `journalctl --user -u litellm-foundry-gateway.service -n 50 --no-pager`, explain the cause, fix it with the user, and retry at most twice. Then stop and report the blocker.

## Phase 5: verify
Follow the README health checks. Expect liveliness and readiness OK, 401 without a key, and 200 with the key read from the file inside a subshell. Run the offline tests with the venv's Python as described in README section "Testing"; expect all to pass. This proves the installation end to end. With the user's consent, optionally send one authenticated request to one configured model.

## Phase 6: Codex client (optional)
Only if the user uses Codex, follow README sections "Codex setup" and "Model catalog". Show the exact diff for `~/.codex/config.toml` and wait for approval before writing; merge it and never overwrite unrelated settings. Fill the `<user>` placeholder. Add `LITELLM_API_KEY` to `~/.codex/.env` with mode 0600 without printing it. The generated catalog contains the Codex system prompt: never commit or share it. Remind the user to fully quit and reopen the desktop app.

## Troubleshooting
| Symptom | Cause | Fix |
|---|---|---|
| 401 on every call | Wrong or missing key in the client `.env` | Compare key presence and permissions without printing values; correct it in the user's editor. |
| 400 `No connected db` | Wrong key; the request is still denied | Correct the client key and retry. |
| 429 from a partner model | Azure quota or transient upstream limit | Rerun as appropriate and see README section "Limitations and operational facts". |
| Service does not start | Journal error, often an empty master key | Inspect the unit status and journal; repair the environment file after the user edits it. |
| Model missing in Codex | Catalog slug is not in `gateway/config.yaml` | Align the catalog override slug with `model_name` and regenerate as README directs. |

## Uninstall
Follow README section "Uninstall". Confirm each deletion with the user, apply Codex cleanup only when configured, and delete the secrets file last.
