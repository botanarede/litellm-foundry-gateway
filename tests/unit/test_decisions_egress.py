"""Checks that keep decision calls and their credentials on Azure Foundry hosts (README, Security notes)."""

import re
from pathlib import Path
from typing import Any

import pytest
import yaml

import codex_tools_compat as compat_module

REPOSITORY_ROOT = Path(__file__).parents[2]
KEY_REF = re.compile(r"^os\.environ/AZURE_FOUNDRY_API_KEY$")
BASE_REF = re.compile(r"^os\.environ/AZURE_FOUNDRY_DECISIONS_BASE$")
AZURE_BASE = "https://example.openai.azure.com/providers/microsoft"


def _config() -> dict[str, Any]:
    text = (REPOSITORY_ROOT / "gateway" / "config.yaml").read_text(encoding="utf-8")
    return yaml.safe_load(text)


def _decision_entries() -> list[dict[str, Any]]:
    return [
        entry
        for entry in _config()["model_list"]
        if str(entry["litellm_params"]["model"]).startswith("typesafe/")
    ]


def _unit_lines() -> set[str]:
    unit = (REPOSITORY_ROOT / "deploy" / "litellm-foundry-gateway.service").read_text(encoding="utf-8")
    return {line.strip() for line in unit.splitlines()}


def test_decision_entries_take_base_and_key_from_foundry_env_vars() -> None:
    """The repo holds no resource name; the callback guard enforces the Azure host at call time."""
    entries = _decision_entries()
    assert entries, "Profile D must keep a typesafe/ decision entry"
    for entry in entries:
        params = entry["litellm_params"]
        assert BASE_REF.match(str(params.get("api_base"))), entry["model_name"]
        assert KEY_REF.match(str(params.get("api_key"))), entry["model_name"]


def test_repository_config_holds_no_azure_hostname() -> None:
    """Tracked config must not embed a resource hostname (tenant identifier)."""
    text = (REPOSITORY_ROOT / "gateway" / "config.yaml").read_text(encoding="utf-8")
    assert not re.search(r"https?://(?!<)[^\s]*azure\.com", text)


def test_jev_classifier_uses_a_configured_deployment() -> None:
    """A Jev classifier must name a deployment; inline api_key/api_base would bypass the guard."""
    names = {entry["model_name"] for entry in _decision_entries()}
    for entry in _config()["model_list"]:
        router_config = entry["litellm_params"].get("complexity_router_config") or {}
        jev = router_config.get("jev_classifier_config")
        if jev is not None:
            assert jev.get("deployment_name") in names
            assert "api_key" not in jev and "api_base" not in jev


def test_unit_closes_the_typesafe_fallbacks() -> None:
    """The unit drops TYPESAFE_API_KEY, defaults the base to a local port and disables stray .env."""
    lines = _unit_lines()
    assert "UnsetEnvironment=TYPESAFE_API_KEY" in lines
    assert "Environment=TYPESAFE_API_BASE=http://127.0.0.1:9" in lines
    assert "Environment=LITELLM_MODE=PRODUCTION" in lines
    typesafe_pre = [
        line for line in lines
        if line.startswith("ExecStartPre=") and "TYPESAFE_API_BASE" in line
    ]
    assert typesafe_pre, "No ExecStartPre line checks TYPESAFE_API_BASE"
    assert "http://127.0.0.1:9" in typesafe_pre[0]
    assert "exit 1" in typesafe_pre[0]


def test_env_example_does_not_ship_third_party_decision_credentials() -> None:
    """The example env file must not offer a TypeSafe or Gemini key to the gateway."""
    example = (REPOSITORY_ROOT / "deploy" / "gateway.env.example").read_text(encoding="utf-8")
    keys = {line.split("=", 1)[0] for line in example.splitlines() if "=" in line and not line.startswith("#")}
    assert not keys & {"TYPESAFE_API_KEY", "TYPESAFE_API_BASE", "GEMINI_API_KEY"}


def _deployment(model: str, api_base: Any, api_key: Any = "key") -> dict[str, Any]:
    return {"litellm_params": {"model": model, "api_base": api_base, "api_key": api_key}}


async def test_guard_accepts_an_azure_decision_deployment() -> None:
    """A decision deployment on an Azure host with a key passes."""
    assert await compat_module.codex_tools_compat.async_pre_call_check(_deployment("typesafe/D", AZURE_BASE), None) is None
    services = "https://example.services.ai.azure.com/providers/microsoft"
    assert await compat_module.codex_tools_compat.async_pre_call_check(_deployment("typesafe/D", services), None) is None


@pytest.mark.parametrize(
    "api_base",
    [
        None,
        "",
        "https://api.typesafe.ai",
        "http://127.0.0.1:9",
        "http://example.openai.azure.com",
        "https://openai.azure.com.attacker.example",
        "https://attacker.example/example.openai.azure.com",
        "https://example.openai.azure.com@attacker.example",
    ],
)
async def test_guard_rejects_a_decision_deployment_outside_azure(api_base: Any) -> None:
    """Missing, plain-http and look-alike bases are refused before any request leaves the host."""
    with pytest.raises(ValueError):
        await compat_module.codex_tools_compat.async_pre_call_check(_deployment("typesafe/D", api_base), None)


@pytest.mark.parametrize("api_key", [None, ""])
async def test_guard_rejects_a_decision_deployment_without_a_key(api_key: Any) -> None:
    """Without a key the provider would substitute TYPESAFE_API_KEY, so the call is refused."""
    with pytest.raises(ValueError):
        await compat_module.codex_tools_compat.async_pre_call_check(_deployment("typesafe/D", AZURE_BASE, api_key), None)


async def test_guard_ignores_non_decision_deployments() -> None:
    """Chat deployments are not decision calls and are never blocked by the guard."""
    deployment = _deployment("azure/gpt-6.1-sol", None, None)
    assert await compat_module.codex_tools_compat.async_pre_call_check(deployment, None) is None


def test_unit_refuses_to_start_without_master_key() -> None:
    """Without LITELLM_MASTER_KEY LiteLLM would serve without auth, so the unit must fail first."""
    pre = [line for line in _unit_lines() if line.startswith("ExecStartPre=")]
    master_key_pre = [line for line in pre if "LITELLM_MASTER_KEY" in line]
    assert len(master_key_pre) == 1
    assert "exit 1" in master_key_pre[0]
