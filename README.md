# LiteLLM Foundry Gateway

A local LiteLLM gateway that lets Codex CLI and the Codex/ChatGPT desktop app use Azure AI Foundry deployments through the OpenAI Responses API.

## Overview and architecture

The service is one authenticated LiteLLM 1.105.0 process on `127.0.0.1:4000`. Codex clients use one Responses API provider; LiteLLM sends each Foundry deployment through its native Azure Responses path. The only custom component is the `codex_tools_compat` callback configured in `gateway/config.yaml`. It has two jobs: (a) Codex namespace-tool compatibility for models flagged in `model_info`, and (b) a router-level guard (`async_pre_call_check`) that refuses any `typesafe/` decision deployment whose resolved `api_base` is not an `https` Azure host (`*.openai.azure.com`, `*.services.ai.azure.com`) or that has no `api_key`.

```text
Codex CLI / desktop app ── Responses API ──> LiteLLM :4000 (loopback, master key)
                                                ├─ azure/<OpenAI deployment>
                                                └─ azure/<partner deployment> + compatibility callback
Decision clients ── /v1/systemone, /v1/decisions ──> same process
                                                └─ typesafe/Decision-1 ── callback guard ──> never leaves Azure Foundry hosts (`*.openai.azure.com`, `*.services.ai.azure.com`)
Optional: model `auto` ──> complexity router ── classifies with Decision-1 ──> one of the models above
```

It addresses the failures reproduced against partner models:

- **F1:** DeepSeek and Kimi reject `reasoning.effort`; Profile B removes it before the upstream request.
- **F2:** Foundry rejects function tools with a composed or non-object root schema; the callback drops those unsafe tools rather than corrupting their meaning.
- **F3:** Foundry returns invalid content for Codex `namespace` tools; the callback flattens their child functions for the upstream request and restores the namespace in the response.
- **F4:** partner models can return upstream `429` responses even with quota available; see "Retries and concurrency" under Limitations.
- **F5:** `gpt-6.1-sol` rejects `reasoning.effort: none`, which Codex sends when its config sets `model_reasoning_effort = "none"` or the selected catalog entry (for example `auto`) has no reasoning levels. `model_info.reasoning_effort_none_as: medium` on that deployment replaces only that value, after `auto` has picked the deployment; valid efforts and other models are untouched.

The configured Foundry deployments are `gpt-6.1-sol`, `DeepSeek-V4-Pro`, `DeepSeek-V4-Flash`, `Kimi-K2.7-Code`, and `grok-4.7`; the decision model is `Decision-1`, and the optional `auto` router is configured. Adding another Foundry deployment changes configuration, not gateway code.

## Verified status

Verified with LiteLLM 1.105.0, Codex CLI 0.160 and the Codex/ChatGPT desktop app:

Date-tagged entries below were confirmed by the maintainer in direct runs (CLI or desktop); no automated test covers these results. The five-model CLI matrix results come from the 2026-10-16 run, which used the current `tests/live/matrix.sh` (sandbox preflight, disposable key, managed gateway on `127.0.0.1:4010`). The `auto` row comes from an earlier run (2026-10-15) that used an older version of the script without a sandbox.

| Model | Profile | Codex CLI live matrix (shell, namespaced MCP, file edit) | Desktop app |
|---|---|---|---|
| `gpt-6.1-sol` | A | 3/3 PASS (CLI, 2026-10-16) | Functional (desktop, 2026-10-15) |
| `DeepSeek-V4-Pro` | B | 3/3 PASS (CLI, 2026-10-16) | Not verified in the desktop app |
| `DeepSeek-V4-Flash` | B | 3/3 PASS (CLI, 2026-10-16) | Pending manual test |
| `Kimi-K2.7-Code` | B | 3/3 PASS (CLI, 2026-10-16) | Not verified in the desktop app |
| `grok-4.7` | B | 3/3 PASS (CLI, 2026-10-16) | Pending manual test |
| `auto` | — | 3/3 PASS (CLI, 2026-10-15) | Functional (desktop, 2026-10-15) |

The latest full CLI matrix passed 15/15 on 2026-10-16 (five models, three cases each, `workspace-write` sandbox). The file-edit case checks the resulting file, not the tool used: only `gpt-6.1-sol` used the native `apply_patch`; the other four models created the file through a shell command or `python3`, so the native patch path is verified for the Sol deployment only. A transient Azure 500 on `DeepSeek-V4-Pro` during its MCP case was retried by Codex and the case passed. The previous full matrix, before the sandbox preflight, passed 13/15 on `127.0.0.1:4000` on 2026-10-14 (`grok-4.7` failed MCP on an Azure 429 rate limit and file edit on a timeout after reconnects). See the Testing section for sandbox and isolation context. The `Decision-1` smoke test (`tests/live/decisions.sh`) passed 4/4 on 2026-10-14. The optional `auto` router passed 3/3 on a temporary instance on 2026-10-14, with a simple, a medium and a hard prompt served by `DeepSeek-V4-Flash`, `DeepSeek-V4-Pro` and `gpt-6.1-sol`. The unit suite (`pytest tests/unit`) passes.

On 2026-10-15 a 15-case CLI run covered `gpt-6.1-sol`, both DeepSeek models, `Kimi-K2.7-Code` and `auto` (3/3 each, `auto` at effort `none` with prompts that routed to `DeepSeek-V4-Flash`); `grok-4.7` was not in that run. The F5 fix was then verified on a temporary instance with single requests only: `gpt-6.1-sol` with effort `none` (served at `medium`) and `high` (kept), `auto` with a hard prompt at `none` through `/v1/responses` and through `codex exec` (COMPLEX tier, served by `gpt-6.1-sol`), `auto` with a simple prompt (`DeepSeek-V4-Flash`), and `grok-4.7` at `none`. The 2026-10-16 matrix ran with the F5 fix in place.

## Prerequisites

Before installing, make sure the following prerequisites are available:

- **Host:** Linux x86_64 with a systemd user session where `systemctl --user` works, tested on Ubuntu 24.04; the hashed locks target x86_64 manylinux, so other platforms must regenerate them.
- **uv:** Install it first from <https://docs.astral.sh/uv/getting-started/installation/>; it creates the venv and installs the hashed locks.
- **Python 3.12:** `uv` uses an installed 3.12 or downloads one.
- **Tools:** `curl` and `openssl` for health checks and key generation.
- **Azure AI Foundry:** A resource with the deployments named in `gateway/config.yaml` (the name after `azure/` in each `litellm_params.model` must match a deployment name), its endpoint and API key, and enough tokens-per-minute quota for each deployment (see Limitations).
- **Codex:** Codex CLI 0.160.1 or the Codex/ChatGPT desktop app, only for the Codex setup and live tests; the gateway itself does not need it.

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
- `AZURE_FOUNDRY_DECISIONS_BASE` — only for Profile D decision models: the same resource URL plus `/providers/microsoft`. The callback refuses to call a decision model if this is empty or not an `https` Azure host.

The gateway never needs `TYPESAFE_API_KEY`, `TYPESAFE_API_BASE` or `GEMINI_API_KEY`. Keep them out of this file (see Security notes).

Open the file in a text editor and replace every placeholder; the Azure endpoint and API key are on the Keys and Endpoint page of the Foundry resource in the Azure portal.

```bash
# The key is generated inside the shell, so it never appears in the process list or on screen.
F=~/.config/litellm-foundry-gateway/gateway.env
(umask 077; { grep -v '^LITELLM_MASTER_KEY=' "$F"; printf 'LITELLM_MASTER_KEY=sk-%s\n' "$(openssl rand -hex 16)"; } > "$F.new") && mv "$F.new" "$F"
```

The same value goes into `~/.codex/.env` as `LITELLM_API_KEY` (see Codex setup).

```bash
grep -cE '<[^>]+>|=$' ~/.config/litellm-foundry-gateway/gateway.env
# Expected: 0 (1 if you leave AZURE_FOUNDRY_DECISIONS_BASE empty because you do not use Profile D)
```

Render the user unit template with this checkout's absolute path, install it and start it:

```bash
mkdir -p ~/.config/systemd/user
sed "s|@REPO_DIR@|$PWD|" deploy/litellm-foundry-gateway.service \
  > ~/.config/systemd/user/litellm-foundry-gateway.service
systemd-analyze --user verify ~/.config/systemd/user/litellm-foundry-gateway.service
systemctl --user daemon-reload
systemctl --user enable --now litellm-foundry-gateway.service
```

If the service does not start, check its status and the journal. For example, the unit refuses to start when `LITELLM_MASTER_KEY` is empty, and the journal shows why:

```bash
systemctl --user status litellm-foundry-gateway.service --no-pager
journalctl --user -u litellm-foundry-gateway.service -n 50 --no-pager
```

`WantedBy=default.target` starts the gateway with your user session, so it is back after every reboot as soon as you log in. To start it at boot without a login session, run `loginctl enable-linger "$USER"` once.

Verify both health endpoints and verify that an unauthenticated API request is denied:

```bash
curl -fsS http://127.0.0.1:4000/health/liveliness
curl -fsS http://127.0.0.1:4000/health/readiness
curl -sS -o /dev/null -w '%{http_code}\n' http://127.0.0.1:4000/v1/models
# Expected final status: 401

# Authenticated check — key never appears in the process list:
(KEY=$(grep '^LITELLM_MASTER_KEY=' ~/.config/litellm-foundry-gateway/gateway.env | cut -d= -f2-); printf 'header = "Authorization: Bearer %s"\n' "$KEY" | curl -sS -o /dev/null -w '%{http_code}\n' -K - http://127.0.0.1:4000/v1/models)
# Expected: 200
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
4. Start the changed configuration on a temporary port and run the full live matrix before exposing the model (see Limitations, "Compatibility evidence").

The following blocks are examples, not whole replacements. Copy only entries that are not already in `gateway/config.yaml` into its existing top-level `model_list` list; do not duplicate model names. Reuse the earlier `&foundry`, `&partner`, and `&compat` anchors where applicable. If copied entries instead reference an example-profile anchor, also copy that profile's first, anchor-defining entry as the anchor source (under a different `model_name` only if truly needed). Replace neither the callback nor global settings.

### Profile A — OpenAI model on Foundry

Profile A is native Azure Responses passthrough. Only `gpt-6.1-sol` is verified live from the examples below. Set `model_info.reasoning_effort_none_as` (see F5) only on a deployment verified to reject effort `none`; it is verified for `gpt-6.1-sol` alone.

```yaml
  - model_name: gpt-6.1-sol
    litellm_params: &profile_a_foundry
      model: azure/gpt-6.1-sol
      api_base: os.environ/AZURE_FOUNDRY_API_BASE
      api_version: v1
      api_key: os.environ/AZURE_FOUNDRY_API_KEY
    model_info:
      reasoning_effort_none_as: medium
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

Profile B adds `additional_drop_params` and `codex_tools_compat`. Only `DeepSeek-V4-Pro`, `DeepSeek-V4-Flash`, `Kimi-K2.7-Code`, and `grok-4.7` are verified live from the examples below. For `num_retries`, see Limitations, "Retries and concurrency".

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

Decision calls never leave Azure Foundry hosts (`*.openai.azure.com`, `*.services.ai.azure.com`). The callback guard enforces this (see Security notes). The resource name stays in your local `gateway.env`, never in the repository.

Clients call the gateway on `POST /v1/systemone` (System One body: `state` plus a `questions` object) or `POST /v1/decisions` (OpenAI decisions body: `input` plus a `questions` list). Decision models are not added to the Codex catalog, and the tool-compatibility transformations ignore these routes. Verify a deployment with:

```bash
bash tests/live/decisions.sh <port> [model_name]
```

### Profile E — optional `auto` router

`auto` is a LiteLLM complexity router (`auto_router/complexity_router`) that picks a model per request. It is optional: it runs only when a client selects the model `auto`, and every explicit model behaves exactly as before. There are no router-level fallbacks.

| Tier | Model |
|---|---|
| SIMPLE | `DeepSeek-V4-Flash` |
| MEDIUM | `DeepSeek-V4-Pro` |
| COMPLEX | `gpt-6.1-sol` |
| REASONING | `gpt-6.1-sol` |

The classifier is `Decision-1` (`classifier_type: jev`, `deployment_name: Decision-1`, `timeout_ms: 3000`). It reuses the Profile D deployment, so the callback guard keeps the call on Azure Foundry hosts (`*.openai.azure.com`, `*.services.ai.azure.com`). The classifier config holds no key or base URL. The `NON_REASONING` tier is off by default in LiteLLM and is not used, so no other model can be selected.

- **Failure behaviour:** if the classifier errors, times out, returns an unknown tier or rejects the input as too large, or encounters Decision-1 missing or refused by the callback guard, `classifier_fallback: default_model` routes the request to `DeepSeek-V4-Flash`. The request still succeeds, but `auto` degrades to the cheapest model for that turn.
- **Session affinity:** `session_affinity: true` pins the model chosen on a session's first turn for the rest of the session. LiteLLM reads the `session-id` or `thread-id` header only when the User-Agent starts with `codex` and the value has at least 8 characters from [A-Za-z0-9_-]; Codex sends both headers ([headers.rs](https://github.com/openai/codex/blob/main/codex-rs/codex-api/src/requests/headers.rs)). The pin lasts 24 h (`session_affinity_ttl_seconds: 86400`) and lives in memory only (a restart clears it). Without a qualifying session id, the router classifies every turn.
- **Seeing which model answered:** the gateway logs one line per `auto` answer (`journalctl --user -u litellm-foundry-gateway | grep 'auto routed'`), e.g. `auto routed: tier=SIMPLE cause=jev_classifier model=DeepSeek-V4-Flash`. It never includes prompt content, and it is not written for explicit model requests. Each response also carries the `x-litellm-complexity-router-tier`, `x-litellm-complexity-router-cause` and `x-litellm-model-name` headers (the `model` field in the body stays `auto`).
- **Mixed-model sessions:** if a session switches model (no session id, first-turn fallback, expired pin or restart), replayed encrypted reasoning from one model can be rejected by another; start a new session or select an explicit model if that error appears.
- **What Decision-1 receives:** the current user ask, the system or `instructions` text in full (for Codex, tens of KB on every turn), and up to 3 prior user turns capped at 8,000 characters in total (LiteLLM defaults for `classifier_context_window_size` and `classifier_context_budget_chars`; no per-turn cap). Tool output and Codex harness blocks are excluded. Decision-1 accepts about 64,000 tokens of state (roughly 250,000 characters); probes up to 128,000 characters returned `200` in about 1.5 s, and larger input returns `422`, which takes the fallback path. LiteLLM 1.105.0 has no option to cap or exclude system text for Codex, so the defaults stay; the settings are deliberately not lowered because they are far below the limit.
- **Cost of misrouting:** a request sent too low gets a weaker answer from `DeepSeek-V4-Flash` or `DeepSeek-V4-Pro`; one sent too high spends `gpt-6.1-sol`. Each classification adds one Decision-1 call (about 0.5–1.5 s). Pin an explicit model when the choice matters.
- **Tool compatibility:** `auto` carries `codex_tools_compat: true` because the callback keys on the requested model name. Tools are therefore flattened and restored for every target, including `gpt-6.1-sol`.
- **Reasoning effort:** the `auto` catalog entry has no reasoning levels, so Codex sends effort `none`. DeepSeek tiers drop it, and `gpt-6.1-sol` receives `medium` (F5).

Test it on a temporary port, never on the live service:

```bash
bash tests/live/decisions.sh <port>
MATRIX_MODELS="auto:none" MATRIX_MCP_SERVER=<server> bash tests/live/matrix.sh <port>
```

Also send a simple, a medium and a hard prompt to model `auto` through `/v1/responses` and check which deployment served each one.

### Profile C — models without tool calling

Profile C is for models without tool calling, such as `Llama-4-Maverick` or `mistral-medium-3-5`. It is unusable with Codex because Codex requires tool calls. Do not add compatibility flags as a workaround.

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

### Model catalog

Codex lists only models in the file named by `model_catalog_json`. Generate it from the installed Codex instead of copying one: each entry embeds the Codex system prompt in the `model_messages` field, so a copied catalog goes stale; the generated file must not be committed or published. `deploy/catalog-overrides.example.json` holds only the per-model fields (no prompt, no secrets); its slugs must match `model_name` in `gateway/config.yaml`. Fields not covered by `partner_defaults` (such as `context_window`) are inherited verbatim from the base entry and may be wrong for non-OpenAI models; verify and adjust them per model in `deploy/catalog-overrides.example.json`. The `Kimi-K2.7-Code` value (256,000 tokens) comes from the vendor's documentation and should be confirmed against the model card in your Foundry subscription.

```bash
python3 deploy/build-catalog.py deploy/catalog-overrides.example.json ~/.codex/azure-foundry-catalog.json
# Desktop app: build with its bundled binary instead
python3 deploy/build-catalog.py deploy/catalog-overrides.example.json <new-path> \
  --codex ~/.codex/packages/app-server-daemon/releases/<version>/bin/codex
```

The script uses `codex debug models --bundled` as the schema and base entry, stops if the `base` model is missing from that Codex or an override key is not in its schema, refuses to write over an existing file, and checks the result by loading it back through `codex debug models`. To replace a catalog, move the old file aside first. It is verified with Codex CLI 0.160.1 and app-server 0.162.1; it was not tested as the live desktop catalog. Fields that exist only in newer Codex versions (for example `auto_compact_token_limit`) are rejected by older ones; add them to the overrides only when the reading Codex has them.

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

The unit suite needs no network or keys. Per-file coverage:

- `test_codex_tools_compat.py` — namespace-tool flatten/restore (including streaming), dropping tools with non-object or composed root schemas, name-length and collision rejection, the `auto routed` log line (tier, cause, model; no prompt content), and the F5 `reasoning.effort: none` replacement (flagged deployments only; valid efforts and unflagged models pass through untouched).
- `test_decisions_egress.py` — decision egress guard across the gateway config, the `async_pre_call_check` callback, and systemd unit text assertions including the `ExecStartPre` line content.
- `test_auto_router.py` — the optional `auto` complexity-router config: allowed tier targets, classifier wiring, fallback model, and session affinity.
- `test_build_catalog.py` — `deploy/build-catalog.py` against a fake bundled Codex catalog and the real `catalog-overrides.example.json`.
- `test_matrix_preflight.py` — `tests/live/matrix.sh` sandbox preflight, isolation flags and the managed gateway (disposable key, unit egress hardening, busy-port and port-4000 refusal, teardown) using a fake Codex and a fake LiteLLM on loopback; no model calls.

Gaps — not covered by the offline suite:
- No unit test calls Azure or Codex; those paths are exercised only by the live scripts below.
- The 2026-10-16 live run passed the sandbox probe and ran every case under `workspace-write`, but the isolation of model commands was not otherwise verified (see "Matrix isolation and limits").
- The unit's `ExecStartPre` lines are tested as text assertions (string match on the service file), not by executing the commands through systemd.

Run the 5-model, 3-case live Codex matrix (defaults: `gpt-6.1-sol` at `medium`, partners at `none`; `auto` runs only when named in `MATRIX_MODELS`). The namespaced-MCP case needs an MCP server configured in Codex whose tool evaluates JavaScript; name it with `MATRIX_MCP_SERVER` (and `MATRIX_MCP_TOOL` if the tool is not called `js`). Without a port argument the script starts its own short-lived gateway on `127.0.0.1:4010` (`MATRIX_MANAGED_PORT` to change it), authenticated with a disposable key generated for that run, and stops it when the script ends. It refuses a port that is already in use and never uses port 4000:

```bash
MATRIX_MCP_SERVER=<server> bash tests/live/matrix.sh
```

To target a gateway that is already running instead, pass its port; the script then uses that gateway's master key from the protected environment file:

```bash
MATRIX_MCP_SERVER=<server> bash tests/live/matrix.sh <port>
```

Without `MATRIX_DEFAULT_PROVIDER=1`, the script creates a temporary LiteLLM provider override. To validate the installed Codex configuration instead, use:

```bash
MATRIX_DEFAULT_PROVIDER=1 MATRIX_MCP_SERVER=<server> bash tests/live/matrix.sh 4000
```

Each run writes protected logs below `~/.local/state/litellm-foundry-gateway/matrix/` and stores the latest path for that port there. The matrix verifies the shell, namespaced MCP, and file-edit outcomes for each model.

**Matrix isolation and limits**

- A working Codex sandbox is required. Before any model call the script runs a probe through `codex sandbox` with the same `sandbox_mode="workspace-write"` and environment overrides the cases use, and exits 2 if the sandbox fails or the probe sees the gateway key. There is no bypass option.
- Every case runs with `--ephemeral`, `-s workspace-write` and `approval_policy=never`.
- The managed gateway receives the Azure credentials from the protected environment file but a disposable master key, so Codex and its model commands never get the real master key as an environment variable. A hard time limit (`MATRIX_SERVER_TTL`, default 5400 s) stops the gateway even if the script is killed with SIGKILL, which skips its cleanup. Its log is written to the run's protected log directory.
- **Limit:** the Azure credentials are real inside the managed gateway and in the environment file, which model commands can still read; use a dedicated Azure key or deployment for the matrix if that matters.
- Shell commands run by the model are intended to get an empty environment with only `PATH=/usr/bin:/bin` (Codex may add its own variables), so the gateway key is not inherited by them. Verified live on 2026-10-16 with `codex exec` on Codex 0.160.1 and these same flags: a model-run `env` listed only `PATH`, shell and locale basics and variables added by Codex or its host integration (`CODEX_*`, pager and colour settings); neither the key variable nor an extra canary variable was present. That check ran one extra case against a running gateway, not the matrix cases themselves.
- **Limit:** `workspace-write` restricts writes, not reads, so model commands can still read files the user can read (for example the gateway env file); use a dedicated test key and a throwaway instance.
- **Limit:** MCP servers run outside the sandbox and are not restricted, and they may inherit the exported gateway key; the script only warns about other enabled MCP servers (or that it could not list them). Features such as `js_repl` may also run outside the sandbox; this was not verified.
- **Limit:** the probe uses `codex sandbox` while the cases use `codex exec`. On 2026-10-16, with Codex 0.160.1, both hid the key from model commands (see above), but a different Codex version could diverge, and a mismatch where the probe isolates and `exec` does not would go undetected.
- On hosts that restrict unprivileged user namespaces (for example Ubuntu 24.04+ with AppArmor), the probe fails until the host administrator allows the helper that creates the namespace (the `bwrap` binary Codex invokes, not the Codex binary itself) to do so. That is a system-wide security change: a profile for `bwrap` applies to every process on the host that runs it, so weigh it before granting. This repository neither ships nor applies such a profile. The 2026-10-16 run passed the probe on a host where the administrator had allowed `bwrap`; what that run did not verify is stated in the limits above.

For a desktop smoke test, run the desktop app's bundled Codex binary with `codex exec` and the same temporary provider override used by the non-default matrix mode. Smoke at least one Profile A and two Profile B models (for example `gpt-6.1-sol`, `DeepSeek-V4-Pro`, and `Kimi-K2.7-Code`) before restarting the desktop app.

## Upgrade procedure

1. Review LiteLLM's GitHub security advisories first. If a newer stable version is appropriate, update the exact pin in `requirements.in` (and any justified companion pin).
2. Regenerate both hashed locks with the commands in the install section, then recreate or update the dedicated venv using the `--require-hashes` installation command.
3. Run the unit suite.
4. From the repository root, run the matrix against a candidate gateway that the script starts and stops itself (port 4010, disposable key, same egress hardening as the unit):

   ```bash
   MATRIX_MCP_SERVER=<server> bash tests/live/matrix.sh
   ```

5. If every validation passes, restart the installed service:

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

- **Composed root schemas:** partner models cannot use tools whose root schema is composed (for example the desktop app's `automation_update`); those tools are intentionally removed from partner-model requests. Under `auto`, those tools are also removed when the router picks `gpt-6.1-sol`.
- **grok-4.7:** expect roughly 70–140 seconds per matrix task. Under the full matrix it can hit Azure `429` and slow reconnects; rerun the case before treating it as a gateway failure.
- **Context window versus TPM:** a single request cannot exceed the deployment's tokens-per-minute limit. When a catalog `context_window` is larger than that limit (for example grok-4.7 with 80K TPM and a 500K window), set `auto_compact_token_limit` in the Codex catalog entry below the TPM limit so Codex compacts the conversation before Azure returns `429`.
- **Partner file edits:** DeepSeek, Kimi, and grok never receive `apply_patch` because their Codex catalog entries set `apply_patch_tool_type` to `null`. They write files through the shell instead. The matrix patch case checks the resulting file, not which tool performed the edit.
- **Kimi-K2.7-Code:** it can occasionally return an empty final answer or skip a tool call. This is observed model behavior; retry the case before treating it as a gateway failure.
- **Authentication without a database:** a missing key is denied with 401. A wrong key can be denied with 400 and `No connected db`; it is still denied. `prisma==0.15.0` remains pinned in `requirements.in` because LiteLLM's auth error handler (1.104.2 and 1.105.0) imports it even when no database is configured.
- **Compatibility evidence:** Microsoft documentation is inconsistent about which partner deployments support `/openai/v1/responses`. The live matrix, not a catalog claim, is the source of truth.
- **User-unit sandboxing:** on hosts that restrict unprivileged user namespaces (for example Ubuntu 24.04+ AppArmor defaults), systemd user units ignore `ProtectSystem` and `ProtectHome`. The unit intentionally does not rely on those directives.
- **Retries and concurrency:** partner models can return upstream `429` responses (for example `x-ratelimit-type: InferenceContainer`) even with quota available. `num_retries: 2` on Kimi-K2.7-Code, DeepSeek-V4-Flash, and grok-4.7 retries short-window `429` and Azure peak-load rejections (add it to another entry only if that upstream returns frequent `429`s), and the Codex request and stream retries cover the rest. LiteLLM 1.105.0 `max_parallel_requests` rejects with `429` instead of queueing, so it is intentionally not used; parallel subagent bursts and grok-4.7 desktop use still need an Azure quota increase.

## Security notes

- **Secrets:** keep them only in the mode-0600 files `~/.config/litellm-foundry-gateway/gateway.env` and `~/.codex/.env`. Never commit them and never print their values.
- **Clients must authenticate:** the gateway binds to `127.0.0.1` and requires `LITELLM_MASTER_KEY`; every client (Codex or any other tool) must send it as a Bearer token.
- **Empty master key:** without a master key LiteLLM serves without authentication, so the unit's `ExecStartPre` refuses to start when `LITELLM_MASTER_KEY` is empty.
- **Decision-call hardening:** the `typesafe/` provider falls back to `TYPESAFE_API_BASE`, then `https://api.typesafe.ai`, and to `TYPESAFE_API_KEY` whenever a config value is empty. Layers, strongest first:
  - The callback guard (`async_pre_call_check`) is the enforcing layer. It works on the resolved deployment, so it holds whatever the environment contains, and the router runs it for every decision call, including internal ones. `tests/unit/test_decisions_egress.py` guards the config, the unit and the callback.
  - The unit drops `TYPESAFE_API_KEY` with `UnsetEnvironment`, which wins over `EnvironmentFile`.
  - The unit defaults `TYPESAFE_API_BASE` to a closed local port; an `EnvironmentFile` line can override that default, so an `ExecStartPre` refuses to start if the final value is anything other than `http://127.0.0.1:9`.
  - The unit sets `LITELLM_MODE=PRODUCTION` so LiteLLM does not load a stray `.env`.
- **Running without the unit:** unset `TYPESAFE_API_KEY`, export `TYPESAFE_API_BASE=http://127.0.0.1:9` (do **not** unset it, because unset falls back to the public `api.typesafe.ai`), and set `LITELLM_MODE=PRODUCTION`. Always set `LITELLM_MASTER_KEY`.
- **TypeSafe pass-through:** LiteLLM's `/typesafe/*` pass-through route skips the callback guard and forwards the client body to `TYPESAFE_API_BASE`; the Azure key is not attached, and the closed-port default keeps it local.
- **Response header (x-litellm-model-api-base):** this header exposes the Azure resource hostname; it is visible only to an authenticated client already connected to 127.0.0.1, but do not paste raw headers into issues or logs you share.
- **Logs and debugging:** the gateway logs neither request bodies nor secrets. Do not run LiteLLM with detailed debugging outside a throwaway test.
- **Dependency licensing:** the MIT license of this repository covers only the repository's own code, not its dependencies. `litellm[proxy]` installs `litellm-enterprise` as a hard transitive dependency; that package carries a proprietary BerriAI Enterprise License that conditions production use on BerriAI subscription terms. This repository does not import or use enterprise features in its own code, but the LiteLLM proxy loads the package at startup when it is present. Read the `litellm-enterprise` package license and the BerriAI terms before production use.

## Contributing

Run the unit suite before any change — no network or keys required (see [Testing](#testing)).

To add a Foundry model, follow the configuration-only procedure in [Adding Foundry models](#adding-foundry-models). No gateway code change is needed. To regenerate the Codex model catalog after adding entries, see [Model catalog](#model-catalog).

Project rules:
- **No over-engineering.** The callback stays small; any growth needs a security or compatibility reason.
- **English only** for code, comments, documentation, and commit messages.
- **Secrets are never logged, printed, or committed.** Reference env var names; never write their values.
- Do not duplicate files that already have a canonical copy in `deploy/`.

License: MIT — see [LICENSE](LICENSE).
