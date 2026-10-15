"""Offline tests for tests/live/matrix.sh: sandbox preflight and isolation flags (fake codex, no model calls)."""

import os
import subprocess
from pathlib import Path

import pytest

MATRIX = Path(__file__).parents[1] / "live" / "matrix.sh"

FAKE_CODEX = """#!/usr/bin/env bash
printf '%s\\n' "$*" >> "$FAKE_CODEX_CALLS"
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
_SCRUBBED = ("MATRIX_DEFAULT_PROVIDER", "MATRIX_MCP_TOOL", "MATRIX_LOG_ROOT", "GATEWAY_ENV", "CODEX_HOME")


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
