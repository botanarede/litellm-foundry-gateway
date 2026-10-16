#!/usr/bin/env bash
# Run the live Codex compatibility matrix (5 models x shell, namespaced MCP, file edit) against a gateway port.
# Set MATRIX_MODELS="<model>:<effort> ..." to run only selected models (default: the five below).
set -u

if [ "$#" -gt 1 ]; then
  printf 'Usage: %s [PORT]\n' "$0" >&2
  exit 2
fi

# Without a PORT (and without MATRIX_DEFAULT_PROVIDER=1) the script starts its own short-lived gateway on
# MATRIX_MANAGED_PORT (default 4010) with a disposable master key and stops it on exit. With a PORT it targets that
# running gateway as before. The installed service on 4000 is never started or stopped by this script.
if [ "$#" -eq 0 ] && [ "${MATRIX_DEFAULT_PROVIDER:-0}" != "1" ]; then
  MANAGED=1
  PORT="${MATRIX_MANAGED_PORT:-4010}"
else
  MANAGED=0
  PORT=${1:-4000}
fi
# The namespaced-MCP case needs an MCP server configured in Codex whose tool evaluates JavaScript.
MCP_SERVER="${MATRIX_MCP_SERVER:-}"
MCP_TOOL="${MATRIX_MCP_TOOL:-js}"
if [ -z "$MCP_SERVER" ]; then
  printf 'Error: set MATRIX_MCP_SERVER to a Codex MCP server with a JavaScript evaluation tool.\n' >&2
  exit 2
fi

# Model-run shell commands get an empty environment plus a fixed PATH (Codex may still add its own variables), so the
# gateway key is not inherited by them. The preflight below uses the same sandbox policy and environment arguments.
ISOLATION_ARGS=(-c shell_environment_policy.inherit=none -c 'shell_environment_policy.set={PATH="/usr/bin:/bin"}')

# Every case runs model-generated commands, so a working Codex sandbox that hides the key is mandatory. Stop before
# any paid call when it is unavailable (for example, Linux hosts that restrict unprivileged user namespaces) or when
# the key stays visible to a sandboxed command. There is no bypass.
if ! LITELLM_PF_KEY=preflight-probe timeout 30 codex sandbox -c 'sandbox_mode="workspace-write"' "${ISOLATION_ARGS[@]}" \
  -- sh -c 'test -z "${LITELLM_PF_KEY-}"' > /dev/null 2>&1; then
  printf 'Error: the Codex sandbox is unavailable or leaks the key to sandboxed commands (check: codex sandbox -- true). No model call was made.\n' >&2
  exit 2
fi

# This does not stop file reads (workspace-write only limits writes) and does not restrict MCP servers, which run
# outside the sandbox: warn about every enabled one other than the server under test.
if OTHER_MCP=$(set -o pipefail; codex mcp list --json 2>/dev/null | MCP_SERVER="$MCP_SERVER" python3 -c \
  'import json,os,sys; print(" ".join(s["name"] for s in json.load(sys.stdin) if s.get("enabled") and s["name"] != os.environ["MCP_SERVER"]))' 2>/dev/null); then
  [ -z "$OTHER_MCP" ] || printf 'Warning: other MCP servers are enabled and reachable by the model: %s\n' "$OTHER_MCP" >&2
else
  printf 'Warning: could not list MCP servers; other servers may be reachable by the model.\n' >&2
fi

if [ "$MANAGED" = "1" ]; then
  GATEWAY_ENV="${GATEWAY_ENV:-$HOME/.config/litellm-foundry-gateway/gateway.env}"
  LITELLM_BIN="${MATRIX_LITELLM_BIN:-$HOME/.local/share/litellm-foundry-gateway/venv/bin/litellm}"
  GATEWAY_CONFIG="$(cd "$(dirname "$0")/../.." && pwd)/gateway/config.yaml"
  case "$PORT" in
    ''|*[!0-9]*|4000)
      printf 'Error: MATRIX_MANAGED_PORT must be a number other than 4000 (the installed service port).\n' >&2
      exit 2 ;;
  esac
  if [ ! -r "$GATEWAY_ENV" ] || [ ! -x "$LITELLM_BIN" ] || [ ! -r "$GATEWAY_CONFIG" ]; then
    printf 'Error: the managed gateway needs a readable gateway env file, an executable litellm (MATRIX_LITELLM_BIN) and gateway/config.yaml.\n' >&2
    exit 2
  fi
  # Never reuse or kill a process this script did not start.
  if (exec 3<>"/dev/tcp/127.0.0.1/${PORT}") 2>/dev/null; then
    printf 'Error: port %s is already in use; stop that process or set MATRIX_MANAGED_PORT.\n' "$PORT" >&2
    exit 2
  fi
fi

LOG_ROOT="${MATRIX_LOG_ROOT:-$HOME/.local/state/litellm-foundry-gateway/matrix}"
TIMESTAMP=$(date -u +%Y%m%dT%H%M%SZ)
LOG_DIR="${LOG_ROOT}/${PORT}-${TIMESTAMP}"
SUMMARY_FILE="${LOG_DIR}/summary.txt"
LATEST_PATH="${LOG_ROOT}/latest-${PORT}.path"

if ! mkdir -p "$LOG_DIR" || ! chmod 0700 "$LOG_DIR"; then
  printf 'Error: cannot create matrix log directory.\n' >&2
  exit 2
fi

if ! : > "$SUMMARY_FILE"; then
  printf 'Error: cannot create matrix summary.\n' >&2
  exit 2
fi

if ! printf '%s\n' "$LOG_DIR" > "$LATEST_PATH" || ! chmod 0600 "$LATEST_PATH"; then
  printf 'Error: cannot write latest matrix log path.\n' >&2
  exit 2
fi

if ! WORK_DIR=$(mktemp -d); then
  printf 'Error: cannot create temporary matrix work directory.\n' >&2
  exit 2
fi
# The trap prevents file-edit cases and the managed gateway from outliving a failure or interruption.
GW_PID=""
cleanup() {
  if [ -n "$GW_PID" ]; then
    kill "$GW_PID" 2>/dev/null
    wait "$GW_PID" 2>/dev/null
  fi
  rm -rf -- "$WORK_DIR"
}
trap cleanup EXIT
trap 'exit 130' INT
trap 'exit 143' TERM

if ! printf '%s\n' 'PREFLIGHT-OK-4217' > "$WORK_DIR/hello.txt"; then
  printf 'Error: cannot prepare matrix work file.\n' >&2
  exit 2
fi

PROVIDER_ARGS=()
if [ "${MATRIX_DEFAULT_PROVIDER:-0}" = "1" ]; then
  # This mode verifies the installed Codex configuration, which loads its own key from ~/.codex/.env.
  printf 'Mode: default Codex provider (no provider override)\n'
else
  if [ "$MANAGED" = "1" ]; then
    # Disposable key: generated per run, kept only in this script's environment and the gateway's. The real master key
    # is overridden before the gateway starts, so Codex never receives it. The unit's egress hardening is repeated.
    LITELLM_PF_KEY="sk-matrix-$(python3 -c 'import secrets; print(secrets.token_hex(24))')"
    export LITELLM_PF_KEY
    (
      set -a
      . "$GATEWAY_ENV"
      set +a
      export LITELLM_MASTER_KEY="$LITELLM_PF_KEY" LITELLM_MODE=PRODUCTION TYPESAFE_API_BASE=http://127.0.0.1:9
      unset TYPESAFE_API_KEY LITELLM_PF_KEY
      # The timeout is a backstop for a SIGKILL of this script, which skips the trap.
      exec timeout -k 5 "${MATRIX_SERVER_TTL:-5400}" "$LITELLM_BIN" --config "$GATEWAY_CONFIG" --host 127.0.0.1 --port "$PORT"
    ) > "$LOG_DIR/gateway.log" 2>&1 &
    GW_PID=$!
    READY=0
    for _ in $(seq 1 120); do
      kill -0 "$GW_PID" 2>/dev/null || break
      # Authenticating with the disposable key proves the process on the port is the one this script started.
      CODE=$(printf 'header = "Authorization: Bearer %s"\n' "$LITELLM_PF_KEY" \
        | curl -sS -m 3 -K - -o /dev/null -w '%{http_code}' "http://127.0.0.1:${PORT}/v1/models" 2>/dev/null)
      if [ "$CODE" = "200" ]; then READY=1; break; fi
      sleep 0.5
    done
    if [ "$READY" != "1" ]; then
      printf 'Error: the managed gateway did not become ready (see %s). No model call was made.\n' "$LOG_DIR/gateway.log" >&2
      exit 2
    fi
    printf 'Managed gateway ready on 127.0.0.1:%s (disposable key, stops on exit)\n' "$PORT"
  else
    # Read only the master-key value needed by the temporary provider; never source or print the env file.
    GATEWAY_ENV="${GATEWAY_ENV:-$HOME/.config/litellm-foundry-gateway/gateway.env}"
    LITELLM_PF_KEY="$(grep -m 1 '^LITELLM_MASTER_KEY=' "$GATEWAY_ENV" | cut -d= -f2-)"
    if [ -z "$LITELLM_PF_KEY" ]; then
      printf 'Error: LITELLM_MASTER_KEY is unavailable in the gateway environment file.\n' >&2
      exit 2
    fi
    export LITELLM_PF_KEY
  fi

  # Temporary provider override with the same retry limits as the recommended Codex config.
  PROVIDER="model_providers.litellm_pf={name=\"pf\",base_url=\"http://127.0.0.1:${PORT}/v1\",env_key=\"LITELLM_PF_KEY\",wire_api=\"responses\",supports_websockets=false,request_max_retries=4,stream_max_retries=5,stream_idle_timeout_ms=600000}"
  PROVIDER_ARGS=(-c model_provider=litellm_pf -c "$PROVIDER")
  printf 'Mode: temporary LiteLLM provider override\n'
fi

printf 'Matrix log directory: %s\n' "$LOG_DIR"

PASSED=0
TOTAL=0

check_shell_command() {
  grep -q "^/bin/bash -lc 'cat hello.txt' in " "$1"
}

check_mcp_tool() {
  grep -q "^mcp: ${MCP_SERVER}/${MCP_TOOL} " "$1"
}

check_patch_file() {
  [ -f "$WORK_DIR/note.txt" ] && grep -qx 'patched-ok' "$WORK_DIR/note.txt"
}

run() { # model effort tag prompt expectation [extra_check]
  local model=$1
  local effort=$2
  local tag=$3
  local prompt=$4
  local expectation=$5
  local extra_check=${6:-}
  local started rc last result summary_line log_file

  log_file="${LOG_DIR}/${model}-${tag}.log"
  started=$(date +%s)
  timeout 300 codex exec --skip-git-repo-check --ephemeral -s workspace-write -c approval_policy=never -C "$WORK_DIR" \
    "${ISOLATION_ARGS[@]}" "${PROVIDER_ARGS[@]}" -m "$model" -c model_reasoning_effort="$effort" "$prompt" > "$log_file" 2>&1
  rc=$?
  last=$(tail -1 "$log_file" | cut -c1-80)

  if [ "$rc" -eq 0 ] && grep -q -- "$expectation" "$log_file" && \
    { [ -z "$extra_check" ] || "$extra_check" "$log_file"; }; then
    result=PASS
    PASSED=$((PASSED + 1))
  else
    result=FAIL
  fi
  TOTAL=$((TOTAL + 1))

  printf -v summary_line '%-5s %-16s %-6s rc=%s %3ss last=%s' \
    "$result" "$model" "$tag" "$rc" "$(( $(date +%s) - started ))" "$last"
  printf '%s\n' "$summary_line" | tee -a "$SUMMARY_FILE"
}

SHELL_P="Run the shell command 'cat hello.txt' and then reply with only the exact file contents. Do not guess."
MCP_P="Use the ${MCP_SERVER} ${MCP_TOOL} tool (MCP namespace mcp__${MCP_SERVER}, tool ${MCP_TOOL}) to evaluate the JavaScript expression 6*7+0.5 and reply with only the numeric result it returned."
PATCH_P="Create a file named note.txt containing exactly the line 'patched-ok' using your file editing tool (apply_patch if available), then run 'cat note.txt' and reply with its contents."

for spec in ${MATRIX_MODELS:-gpt-6.1-sol:medium DeepSeek-V4-Pro:none DeepSeek-V4-Flash:none Kimi-K2.7-Code:none grok-4.7:none}; do
  model=${spec%%:*}
  effort=${spec##*:}
  run "$model" "$effort" shell "$SHELL_P" "PREFLIGHT-OK-4217" check_shell_command
  run "$model" "$effort" mcp "$MCP_P" "42.5" check_mcp_tool
  rm -f "$WORK_DIR/note.txt"
  run "$model" "$effort" patch "$PATCH_P" "patched-ok" check_patch_file
done
rm -f "$WORK_DIR/note.txt"

RESULT="RESULT: ${PASSED}/${TOTAL} PASS"
printf '%s\n' "$RESULT" | tee -a "$SUMMARY_FILE"

if [ "$PASSED" -eq "$TOTAL" ]; then
  exit 0
fi
exit 1
