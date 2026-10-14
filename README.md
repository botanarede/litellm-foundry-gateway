# LiteLLM Foundry Gateway

A local LiteLLM gateway that lets Codex CLI and the Codex/ChatGPT desktop app use Azure AI Foundry deployments through the OpenAI Responses API.

## Overview and architecture

The service is one authenticated LiteLLM 1.105.0 process on `127.0.0.1:4000`. Codex clients use one Responses API provider; LiteLLM sends each Foundry deployment through its native Azure Responses path. The only custom component is the `codex_tools_compat` callback configured in `gateway/config.yaml`.

```text
Codex CLI / desktop app ── Responses API ──> LiteLLM :4000 (loopback, master key)
                                                ├─ azure/<OpenAI deployment>
                                                └─ azure/<partner deployment> + compatibility callback
```

It addresses the failures reproduced against partner models:

- **F1:** DeepSeek and Kimi reject `reasoning.effort`; Profile B removes it before the upstream request.
- **F2:** Foundry rejects function tools with a composed or non-object root schema; the callback drops those unsafe tools rather than corrupting their meaning.
- **F3:** Foundry returns invalid content for Codex `namespace` tools; the callback flattens their child functions for the upstream request and restores the namespace in the response.
- **F4:** partner models can return upstream `429` responses (for example `x-ratelimit-type: InferenceContainer`) even with quota available; Codex request retries cover them.

The configured Foundry deployments are `gpt-6.1-sol`, `DeepSeek-V4-Pro`, `DeepSeek-V4-Flash`, `Kimi-K2.7-Code`, and `grok-4.7`. Adding another Foundry deployment changes configuration, not gateway code.

## Verified status

Verified with LiteLLM 1.105.0, Codex CLI 0.160 and the Codex/ChatGPT desktop app (bundled Codex app-server 0.162):

| Model | Profile | Codex CLI live matrix (shell, namespaced MCP, file edit) | Desktop app |
|---|---|---|---|
| `gpt-6.1-sol` | A | 3/3 PASS | Functional |
| `DeepSeek-V4-Pro` | B | 3/3 PASS | Functional |
| `DeepSeek-V4-Flash` | B | 3/3 PASS | Pending manual test |
| `Kimi-K2.7-Code` | B | 3/3 PASS | Functional |
| `grok-4.7` | B | 3/3 PASS (slow, see limitations) | Pending manual test |

The latest CLI matrix (2026-10-13) passed 15/15 on a candidate instance before the service restart; every model then answered through `127.0.0.1:4000`. The unit suite (`pytest tests/unit`) passes.

## Install and run the user service

Run these commands from the repository root. The virtual environment is deliberately outside the checkout.

```bash
V=~/.local/share/litellm-foundry-gateway/venv
uv venv --python 3.12 "$V"
uv pip install --python $V/bin/python --require-hashes -r requirements.lock -r requirements-dev.lock
```

Regenerate hashed locks only after reviewing a dependency change. The runtime lock command is:

```bash
uv pip compile --generate-hashes --python-version 3.12 --python-platform x86_64-manylinux_2_28 requirements.in -o requirements.lock
```

Regenerate the development lock with the same Python and platform target:

```bash
uv pip compile --generate-hashes --python-version 3.12 --python-platform x86_64-manylinux_2_28 requirements-dev.in -o requirements-dev.lock
```

Create the protected runtime environment file from `deploy/gateway.env.example`, then replace its local placeholders without printing values:

```bash
install -d -m 0700 ~/.config/litellm-foundry-gateway
install -m 0600 deploy/gateway.env.example \
  ~/.config/litellm-foundry-gateway/gateway.env
chmod 0600 ~/.config/litellm-foundry-gateway/gateway.env
```

The file uses these variable names; keep its values private:

- `AZURE_FOUNDRY_API_BASE` — the bare resource URL, such as `https://<resource>.openai.azure.com`.
- `AZURE_FOUNDRY_API_KEY`
- `LITELLM_MASTER_KEY` — an `sk-`-prefixed gateway key.
- `AZURE_FOUNDRY_DECISIONS_BASE` — only for Profile D decision models: the same resource URL plus `/providers/microsoft`.
- `TYPESAFE_API_KEY` — optional; reserved for a future TypeSafe Jev deployment and unused by the current configuration.

Render the user unit template with this checkout's absolute path, install it and start it:

```bash
mkdir -p ~/.config/systemd/user
sed "s|@REPO_DIR@|$PWD|" deploy/litellm-foundry-gateway.service \
  > ~/.config/systemd/user/litellm-foundry-gateway.service
systemd-analyze --user verify ~/.config/systemd/user/litellm-foundry-gateway.service
systemctl --user daemon-reload
systemctl --user enable --now litellm-foundry-gateway.service
```

`WantedBy=default.target` starts the gateway with your user session, so it is back after every reboot as soon as you log in. To start it at boot without a login session, run `loginctl enable-linger "$USER"` once.

Verify both health endpoints and verify that an unauthenticated API request is denied:

```bash
curl -fsS http://127.0.0.1:4000/health/liveliness
curl -fsS http://127.0.0.1:4000/health/readiness
curl -sS -o /dev/null -w '%{http_code}\n' http://127.0.0.1:4000/v1/models
# Expected final status: 401
```

## Configuration contract

`gateway/config.yaml` owns the gateway model list. Every Azure Responses entry has a Codex-visible `model_name` and a `litellm_params.model` in the form `azure/<deployment>`. Its `api_base` must be the bare Azure resource URL; LiteLLM appends `/openai/v1/responses?api-version=v1`. Do **not** put `/openai/v1` in `api_base`.

Profile B entries include both of these settings:

```yaml
additional_drop_params: ["reasoning.effort"]
model_info:
  codex_tools_compat: true
```

The single callback is configured exactly as follows:

```yaml
litellm_settings:
  callbacks: ["codex_tools_compat.codex_tools_compat"]
```

For a compatible model and `call_type == "aresponses"`, `gateway/codex_tools_compat.py`:

- flattens each namespace child from `<namespace>`, `<tool>` to `<namespace>__<tool>`;
- rewrites replayed namespaced `function_call` inputs to the flat form before sending them upstream;
- restores original `name` and `namespace` on `function_call` output for normal and streaming Responses results;
- drops function tools whose root schema is non-object or has root `anyOf`, `oneOf`, `allOf`, or `not` (lossy flattening was rejected because it produced invented argument values), while treating omitted `parameters` as an empty object schema;
- rejects a flattened name longer than 64 characters or a collision with a clear HTTP 400; and
- leaves non-compatible models and all non-`aresponses` routes untouched.

No callback code is needed for a newly configured compatible deployment. The flag in `model_info` is the contract that enables this behavior.

## Adding Foundry models

Use this configuration-only procedure:

1. Choose Profile A for an OpenAI deployment or Profile B for a partner model that supports tool calling.
2. Add one entry to the existing `model_list` in `gateway/config.yaml`; set the slug sent by Codex equal to `model_name`, and set `model: azure/<that-deployment>`.
3. For any model exposed in Codex, ensure the same slug is present in the file referenced by `model_catalog_json`. Codex can select only catalog-listed models.
4. Start the changed configuration on a temporary port and run the full live matrix before exposing the model. Do not infer Responses API support from vendor documentation.

The following blocks are examples, not whole replacements. Copy only entries that are not already in `gateway/config.yaml` into its existing top-level `model_list` list; do not duplicate model names. Reuse the earlier `&foundry`, `&partner`, and `&compat` anchors where applicable. If copied entries instead reference an example-profile anchor, also copy that profile's first, anchor-defining entry as the anchor source (under a different `model_name` only if truly needed). Replace neither the callback nor global settings.

### Profile A — OpenAI model on Foundry

Profile A is native Azure Responses passthrough. Only `gpt-6.1-sol` is verified live from the examples below.

```yaml
  - model_name: gpt-6.1-sol
    litellm_params: &profile_a_foundry
      model: azure/gpt-6.1-sol
      api_base: os.environ/AZURE_FOUNDRY_API_BASE
      api_version: v1
      api_key: os.environ/AZURE_FOUNDRY_API_KEY
  - model_name: gpt-6-astra
    litellm_params:
      <<: *profile_a_foundry
      model: azure/gpt-6-astra
  - model_name: gpt-5.6-sol
    litellm_params:
      <<: *profile_a_foundry
      model: azure/gpt-5.6-sol
  - model_name: gpt-5.5
    litellm_params:
      <<: *profile_a_foundry
      model: azure/gpt-5.5
  - model_name: gpt-5.4-mini
    litellm_params:
      <<: *profile_a_foundry
      model: azure/gpt-5.4-mini
  - model_name: gpt-5.3-codex
    litellm_params:
      <<: *profile_a_foundry
      model: azure/gpt-5.3-codex
  - model_name: o4-mini
    litellm_params:
      <<: *profile_a_foundry
      model: azure/o4-mini
```

### Profile B — partner model with tool calling

Profile B adds `additional_drop_params` and `codex_tools_compat`. Only `DeepSeek-V4-Pro`, `DeepSeek-V4-Flash`, `Kimi-K2.7-Code`, and `grok-4.7` are verified live from the examples below. Add `num_retries` to an entry only if its upstream returns frequent `429` responses.

```yaml
  - model_name: DeepSeek-V4-Pro
    litellm_params: &profile_b_partner
      model: azure/DeepSeek-V4-Pro
      api_base: os.environ/AZURE_FOUNDRY_API_BASE
      api_version: v1
      api_key: os.environ/AZURE_FOUNDRY_API_KEY
      additional_drop_params: ["reasoning.effort"]
    model_info: &profile_b_compat
      codex_tools_compat: true
  - model_name: DeepSeek-V4-Flash
    litellm_params:
      <<: *profile_b_partner
      model: azure/DeepSeek-V4-Flash
    model_info: *profile_b_compat
  - model_name: DeepSeek-V3.2
    litellm_params:
      <<: *profile_b_partner
      model: azure/DeepSeek-V3.2
    model_info: *profile_b_compat
  - model_name: Kimi-K2.7-Code
    litellm_params:
      <<: *profile_b_partner
      model: azure/Kimi-K2.7-Code
    model_info: *profile_b_compat
  - model_name: Kimi-K2.6
    litellm_params:
      <<: *profile_b_partner
      model: azure/Kimi-K2.6
    model_info: *profile_b_compat
  - model_name: grok-4.7
    litellm_params:
      <<: *profile_b_partner
      model: azure/grok-4.7
    model_info: *profile_b_compat
  - model_name: gpt-oss-120b
    litellm_params:
      <<: *profile_b_partner
      model: azure/gpt-oss-120b
    model_info: *profile_b_compat
  - model_name: Mistral-Large-3
    litellm_params:
      <<: *profile_b_partner
      model: azure/Mistral-Large-3
    model_info: *profile_b_compat
```

### Profile D — decision models

Decision models such as `Microsoft-Decision-1` do not generate text; they return a calibrated probability for each option of typed questions (`noul`, `choice`, `score`). Foundry serves them on `<resource>/providers/microsoft/v1/systemone`, which uses the same System One wire format as TypeSafe's Jev. LiteLLM's built-in `typesafe/` decisions provider therefore handles them with configuration only; the provider prefix names the wire format, not the vendor.

```yaml
  - model_name: Decision-1
    litellm_params:
      model: typesafe/Decision-1                       # typesafe/<deployment>
      api_base: os.environ/AZURE_FOUNDRY_DECISIONS_BASE  # <resource URL>/providers/microsoft
      api_key: os.environ/AZURE_FOUNDRY_API_KEY
```

Clients call the gateway on `POST /v1/systemone` (System One body: `state` plus a `questions` object) or `POST /v1/decisions` (OpenAI decisions body: `input` plus a `questions` list). Decision models are not added to the Codex catalog, and the compatibility callback ignores these routes. Verify a deployment with:

```bash
bash tests/live/decisions.sh <port> [model_name]
```

### Profile C — models without tool calling

Profile C is for models without tool calling, such as `Llama-4-Maverick` or `mistral-medium-3-5`. It is unusable with Codex because Codex requires tool calls. Do not add compatibility flags as a workaround.

Across all profiles, the only models verified live are `gpt-6.1-sol`, `DeepSeek-V4-Pro`, `DeepSeek-V4-Flash`, `Kimi-K2.7-Code`, and `grok-4.7`. Every other deployment must pass the live matrix before it is exposed.

## Codex setup

Merge the LiteLLM configuration from `deploy/codex-config.example.toml` into `~/.codex/config.toml`; do not replace unrelated settings. Codex CLI and the Codex/ChatGPT desktop app (which runs its own bundled Codex app-server) both read this file and `~/.codex/.env`.

```toml
model_provider = "litellm"
model = "DeepSeek-V4-Pro"
model_catalog_json = "/home/<user>/.codex/azure-foundry-catalog.json"

[model_providers.litellm]
name = "LiteLLM gateway (local)"
base_url = "http://127.0.0.1:4000/v1"
env_key = "LITELLM_API_KEY"
wire_api = "responses"
supports_websockets = false
request_max_retries = 4
stream_max_retries = 5
stream_idle_timeout_ms = 600000
```

Put an entry named `LITELLM_API_KEY` in `~/.codex/.env`, with the same private value as `LITELLM_MASTER_KEY`, and keep the file mode `0600`. Keep the existing Azure key available for the direct fallback.

The desktop app reads the configuration when it starts. After the live matrix passes, quit the app completely (make sure no `codex app-server` process from the old session remains) and reopen it.

The direct Azure provider remains the manual CLI fallback:

```bash
codex --profile azure-direct
```

For Codex 0.160 and later, do not place `[profiles.azure-direct]` in `~/.codex/config.toml` when using `--profile`: Codex rejects that combination. Instead create the separate `~/.codex/azure-direct.config.toml` file containing:

```toml
model_provider = "azure-foundry"
model = "gpt-6.1-sol"
```

For the desktop fallback, set `model_provider = "azure-foundry"` in `~/.codex/config.toml` and restart the desktop app.

## Testing

Run unit tests from the project root:

```bash
V=~/.local/share/litellm-foundry-gateway/venv
$V/bin/pytest tests/unit -q
```

Run the 5-model, 3-case live Codex matrix against any gateway port:

```bash
bash tests/live/matrix.sh <port>
```

Without `MATRIX_DEFAULT_PROVIDER=1`, the script creates a temporary LiteLLM provider override and obtains only the gateway key from the protected environment file. To validate the installed Codex configuration instead, use:

```bash
MATRIX_DEFAULT_PROVIDER=1 bash tests/live/matrix.sh 4000
```

Each run writes protected logs below `~/.local/state/litellm-foundry-gateway/matrix/` and stores the latest path for that port there. The matrix verifies the shell, namespaced MCP, and file-edit outcomes for each model.

For a desktop smoke test, run the desktop app's bundled Codex binary with `codex exec` and the same temporary provider override used by the non-default matrix mode. Smoke at least one Profile A and two Profile B models (for example `gpt-6-astra`, `DeepSeek-V4-Pro`, and `Kimi-K2.7-Code`) before restarting the desktop app.

## Upgrade procedure

1. Review LiteLLM's GitHub security advisories first. If a newer stable version is appropriate, update the exact pin in `requirements.in` (and any justified companion pin).
2. Regenerate both hashed locks with the commands in the install section, then recreate or update the dedicated venv using the `--require-hashes` installation command.
3. Run the unit suite.
4. From the repository root, start the candidate manually on a temporary port such as 4010 with the protected environment file. Do not echo its values:

   ```bash
   V=~/.local/share/litellm-foundry-gateway/venv
   set -a
   . ~/.config/litellm-foundry-gateway/gateway.env
   set +a
   "$V/bin/litellm" --config "$PWD/gateway/config.yaml" --host 127.0.0.1 --port 4010
   ```

5. In another terminal, run `bash tests/live/matrix.sh 4010`; stop the temporary process only after the matrix result is available.
6. If every validation passes, restart the installed service:

   ```bash
   systemctl --user restart litellm-foundry-gateway.service
   ```

## Rollback

**Codex only.** Set `model_provider = "azure-foundry"` in `~/.codex/config.toml` (the direct provider block stays in place) and restart the desktop app. Partner models lose the compatibility fixes on the direct path; OpenAI models keep working.

**Gateway.** Stop and disable the service, then point Codex at the direct provider as above:

```bash
systemctl --user disable --now litellm-foundry-gateway.service
```

To roll back a LiteLLM upgrade, check out the previous `requirements.in` and locks, reinstall the venv with `--require-hashes`, and restart the service.

## Limitations and operational facts

- **Composed root schemas:** partner models cannot use tools whose root schema is composed (for example the desktop app's `automation_update`); those tools are intentionally removed from partner-model requests.
- **grok-4.7:** expect roughly 70–140 seconds per matrix task.
- **Context window versus TPM:** a single request cannot exceed the deployment's tokens-per-minute limit. When a catalog `context_window` is larger than that limit (for example grok-4.7 with 80K TPM and a 500K window), set `auto_compact_token_limit` in the Codex catalog entry below the TPM limit so Codex compacts the conversation before Azure returns `429`.
- **Partner file edits:** DeepSeek, Kimi, and grok never receive `apply_patch` because their Codex catalog entries set `apply_patch_tool_type` to `null`. They write files through the shell instead. The matrix patch case checks the resulting file, not which tool performed the edit.
- **Kimi-K2.7-Code:** it can occasionally return an empty final answer or skip a tool call. This is observed model behavior; retry the case before treating it as a gateway failure.
- **Authentication without a database:** a missing key is denied with 401. A wrong key can be denied with 400 and `No connected db`; it is still denied. `prisma==0.15.0` remains pinned in `requirements.in` because LiteLLM's auth error handler (1.104.2 and 1.105.0) imports it even when no database is configured.
- **Clients must authenticate:** the gateway binds to `127.0.0.1` and requires `LITELLM_MASTER_KEY`; every client (Codex or any other tool) must send it as a Bearer token.
- **Logs:** neither request bodies nor secrets are logged by this gateway.
- **Compatibility evidence:** Microsoft documentation is inconsistent about which partner deployments support `/openai/v1/responses`. The live matrix, not a catalog claim, is the source of truth.
- **User-unit sandboxing:** on hosts that restrict unprivileged user namespaces (for example Ubuntu 24.04+ AppArmor defaults), systemd user units ignore `ProtectSystem` and `ProtectHome`. The unit intentionally does not rely on those directives.
- **Retries and concurrency:** `num_retries: 2` on Kimi-K2.7-Code, DeepSeek-V4-Flash, and grok-4.7 retries short-window `429` and Azure peak-load rejections. LiteLLM 1.105.0 `max_parallel_requests` rejects with `429` instead of queueing, so it is intentionally not used; parallel subagent bursts and grok-4.7 desktop use still need an Azure quota increase.

## Security notes

Keep secrets only in the mode-0600 files `~/.config/litellm-foundry-gateway/gateway.env` and `~/.codex/.env`. Never commit the gateway environment file, never print secret values, and do not run LiteLLM with detailed debugging outside a throwaway test.
