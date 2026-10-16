"""Offline tests for tests/live/matrix.sh: sandbox preflight and isolation flags (fake codex, no model calls)."""

import json
import os
import socket
import subprocess
import time
from pathlib import Path

import pytest

MATRIX = Path(__file__).parents[1] / "live" / "matrix.sh"

FAKE_CODEX = """#!/usr/bin/env bash
printf '%s\\n' "$*" >> "$FAKE_CODEX_CALLS"
[ -z "${FAKE_CODEX_KEYS:-}" ] || printf '%s %s\\n' "$1" "${LITELLM_PF_KEY-}" >> "$FAKE_CODEX_KEYS"
case "$1" in
  sandbox) exit "${FAKE_SANDBOX_RC:-0}" ;;
  mcp)
    [ "${FAKE_MCP_RC:-0}" = "0" ] || exit "$FAKE_MCP_RC"
    echo '[{"name":"x","enabled":true},{"name":"other","enabled":true}]'
    exit 0 ;;
esac
echo "fake model output"
exit 1
"""

# Variables that change matrix.sh behaviour must not leak in from the developer's shell.
_SCRUBBED = (
    "MATRIX_DEFAULT_PROVIDER", "MATRIX_MCP_TOOL", "MATRIX_LOG_ROOT", "GATEWAY_ENV", "CODEX_HOME",
    "MATRIX_MANAGED_PORT", "MATRIX_LITELLM_BIN", "MATRIX_SERVER_TTL",
)


def _run_matrix(tmp_path: Path, sandbox_rc: int = 0, mcp_rc: int = 0) -> tuple[subprocess.CompletedProcess, list[str]]:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    codex = bin_dir / "codex"
    codex.write_text(FAKE_CODEX, encoding="utf-8")
    codex.chmod(0o755)
    gateway_env = tmp_path / "gateway.env"
    gateway_env.write_text("LITELLM_MASTER_KEY=unit-test-value\n", encoding="utf-8")
    calls = tmp_path / "calls.txt"
    calls.write_text("", encoding="utf-8")
    env = {key: value for key, value in os.environ.items() if key not in _SCRUBBED}
    env.update(
        PATH=f"{bin_dir}:{os.environ['PATH']}",
        FAKE_CODEX_CALLS=str(calls),
        FAKE_SANDBOX_RC=str(sandbox_rc),
        FAKE_MCP_RC=str(mcp_rc),
        GATEWAY_ENV=str(gateway_env),
        MATRIX_LOG_ROOT=str(tmp_path / "logs"),
        MATRIX_MCP_SERVER="x",
        MATRIX_MODELS="gpt-6.1-sol:medium",
    )
    result = subprocess.run(["bash", str(MATRIX), "4010"], env=env, capture_output=True, text=True, timeout=60)
    return result, calls.read_text(encoding="utf-8").splitlines()


def test_failed_preflight_stops_before_any_model_call(tmp_path: Path) -> None:
    result, calls = _run_matrix(tmp_path, sandbox_rc=1)
    assert result.returncode == 2
    assert "No model call was made" in result.stderr
    assert [call.split()[0] for call in calls] == ["sandbox"]
    assert not (tmp_path / "logs").exists()


def test_preflight_uses_the_policy_and_environment_of_the_cases(tmp_path: Path) -> None:
    _, calls = _run_matrix(tmp_path)
    preflight = next(call for call in calls if call.startswith("sandbox "))
    assert 'sandbox_mode="workspace-write"' in preflight
    assert "shell_environment_policy.inherit=none" in preflight
    assert 'PATH="/usr/bin:/bin"' in preflight


def test_passing_preflight_runs_every_case_isolated_without_bypass(tmp_path: Path) -> None:
    result, calls = _run_matrix(tmp_path)
    execs = [call for call in calls if call.startswith("exec ")]
    assert result.returncode == 1  # the fake model never produces the expected output
    assert len(execs) == 3  # shell, mcp, patch for the single selected model
    for call in execs:
        assert "--ephemeral" in call
        assert "-s workspace-write" in call
        assert "approval_policy=never" in call
        assert "shell_environment_policy.inherit=none" in call
        assert 'PATH="/usr/bin:/bin"' in call
        assert "dangerously" not in call
    assert "other MCP servers are enabled" in result.stderr


def test_failed_mcp_listing_warns_instead_of_staying_silent(tmp_path: Path) -> None:
    result, _ = _run_matrix(tmp_path, mcp_rc=1)
    assert "could not list MCP servers" in result.stderr
    assert "other MCP servers are enabled" not in result.stderr


@pytest.mark.parametrize("flag", ["--dangerously-bypass-approvals-and-sandbox", "danger-full-access"])
def test_script_has_no_bypass_path(flag: str) -> None:
    assert flag not in MATRIX.read_text(encoding="utf-8")


# --- Managed gateway mode (no PORT argument): fake litellm, fake codex, no network beyond loopback. ---

FAKE_LITELLM = """#!/usr/bin/env python3
import json, os, sys
from http.server import BaseHTTPRequestHandler, HTTPServer

args = sys.argv[1:]
port = int(args[args.index("--port") + 1])
names = ("LITELLM_MASTER_KEY", "LITELLM_MODE", "TYPESAFE_API_BASE", "TYPESAFE_API_KEY", "AZURE_FOUNDRY_API_KEY", "LITELLM_PF_KEY")
snap = {name: os.environ.get(name) for name in names}
snap["pid"] = os.getpid()
snap["parent"] = open("/proc/%d/cmdline" % os.getppid(), "rb").read().split(b"\\0")[0].decode()
snap["args"] = args
open(os.environ["FAKE_LITELLM_SNAPSHOT"], "w").write(json.dumps(snap))


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        ok = self.headers.get("Authorization") == "Bearer " + os.environ["LITELLM_MASTER_KEY"]
        self.send_response(200 if ok else 401)
        self.end_headers()

    def log_message(self, *args):
        pass


HTTPServer(("127.0.0.1", port), Handler).serve_forever()
"""

GATEWAY_ENV_TEXT = (
    "LITELLM_MASTER_KEY=real-master-key\n"
    "AZURE_FOUNDRY_API_KEY=azure-test-value\n"
    "TYPESAFE_API_KEY=typesafe-test-value\n"
    "TYPESAFE_API_BASE=https://elsewhere.example.invalid\n"
)


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def _run_managed(tmp_path: Path, port: int, litellm_script: str = FAKE_LITELLM, **extra_env: str):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    for name, text in (("codex", FAKE_CODEX), ("litellm", litellm_script)):
        path = bin_dir / name
        path.write_text(text, encoding="utf-8")
        path.chmod(0o755)
    gateway_env = tmp_path / "gateway.env"
    gateway_env.write_text(GATEWAY_ENV_TEXT, encoding="utf-8")
    calls, keys, snapshot = tmp_path / "calls.txt", tmp_path / "keys.txt", tmp_path / "snapshot.json"
    calls.write_text("", encoding="utf-8")
    keys.write_text("", encoding="utf-8")
    env = {key: value for key, value in os.environ.items() if key not in _SCRUBBED}
    env.update(
        PATH=f"{bin_dir}:{os.environ['PATH']}",
        FAKE_CODEX_CALLS=str(calls),
        FAKE_CODEX_KEYS=str(keys),
        FAKE_LITELLM_SNAPSHOT=str(snapshot),
        GATEWAY_ENV=str(gateway_env),
        MATRIX_LOG_ROOT=str(tmp_path / "logs"),
        MATRIX_LITELLM_BIN=str(bin_dir / "litellm"),
        MATRIX_MANAGED_PORT=str(port),
        MATRIX_MCP_SERVER="x",
        MATRIX_MODELS="gpt-6.1-sol:medium",
        **extra_env,
    )
    result = subprocess.run(["bash", str(MATRIX)], env=env, capture_output=True, text=True, timeout=90)
    call_lines = calls.read_text(encoding="utf-8").splitlines()
    key_lines = keys.read_text(encoding="utf-8").splitlines()
    snap = json.loads(snapshot.read_text(encoding="utf-8")) if snapshot.exists() else None
    return result, call_lines, key_lines, snap


def _port_is_closed(port: int) -> bool:
    with socket.socket() as sock:
        return sock.connect_ex(("127.0.0.1", port)) != 0


def test_managed_gateway_uses_a_disposable_key_and_the_unit_hardening(tmp_path: Path) -> None:
    port = _free_port()
    result, calls, keys, snap = _run_managed(tmp_path, port)
    assert snap is not None, result.stderr
    assert snap["args"][snap["args"].index("--port") + 1] == str(port)
    assert snap["args"][snap["args"].index("--host") + 1] == "127.0.0.1"
    assert snap["LITELLM_MASTER_KEY"].startswith("sk-matrix-")
    assert snap["LITELLM_MASTER_KEY"] != "real-master-key"
    assert snap["LITELLM_MODE"] == "PRODUCTION"
    assert snap["TYPESAFE_API_BASE"] == "http://127.0.0.1:9"
    assert snap["TYPESAFE_API_KEY"] is None
    assert snap["AZURE_FOUNDRY_API_KEY"] == "azure-test-value"
    assert snap["LITELLM_PF_KEY"] is None
    assert snap["parent"].endswith("timeout")
    exec_keys = [line.split(" ", 1)[1] for line in keys if line.startswith("exec ")]
    assert len(exec_keys) == 3
    assert set(exec_keys) == {snap["LITELLM_MASTER_KEY"]}
    assert f'base_url="http://127.0.0.1:{port}/v1"' in " ".join(c for c in calls if c.startswith("exec "))
    assert "real-master-key" not in result.stdout + result.stderr


def test_managed_gateway_is_stopped_when_the_matrix_ends(tmp_path: Path) -> None:
    port = _free_port()
    _, _, _, snap = _run_managed(tmp_path, port)
    assert snap is not None
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        try:
            os.kill(snap["pid"], 0)
        except ProcessLookupError:
            break
        time.sleep(0.1)
    else:
        os.kill(snap["pid"], 9)
        pytest.fail("managed gateway outlived the matrix")
    assert _port_is_closed(port)


def test_managed_gateway_refuses_a_busy_port_without_touching_it(tmp_path: Path) -> None:
    with socket.socket() as busy:
        busy.bind(("127.0.0.1", 0))
        busy.listen(1)
        port = busy.getsockname()[1]
        result, calls, _, snap = _run_managed(tmp_path, port)
    assert result.returncode == 2
    assert "already in use" in result.stderr
    assert snap is None
    assert not [call for call in calls if call.startswith("exec ")]


def test_managed_gateway_never_targets_the_installed_service_port(tmp_path: Path) -> None:
    result, calls, _, snap = _run_managed(tmp_path, 4000)
    assert result.returncode == 2
    assert "other than 4000" in result.stderr
    assert snap is None
    assert not [call for call in calls if call.startswith("exec ")]


def test_managed_gateway_that_never_becomes_ready_stops_before_any_model_call(tmp_path: Path) -> None:
    result, calls, _, _ = _run_managed(tmp_path, _free_port(), litellm_script="#!/bin/sh\nexit 1\n")
    assert result.returncode == 2
    assert "did not become ready" in result.stderr
    assert not [call for call in calls if call.startswith("exec ")]
