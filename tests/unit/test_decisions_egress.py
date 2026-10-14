"""Static checks that keep decision-model credentials on the Foundry resource.

LiteLLM's `typesafe/` decisions provider falls back to TYPESAFE_API_BASE, then
https://api.typesafe.ai, and to TYPESAFE_API_KEY when a config value is empty. These tests make
sure the shipped config and unit never leave that fallback reachable.
"""

import re
from pathlib import Path
from typing import Any

import pytest
import yaml

REPOSITORY_ROOT = Path(__file__).parents[2]
ENV_REF = re.compile(r"^os\.environ/AZURE_FOUNDRY_[A-Z_]+$")


def _config() -> dict[str, Any]:
    text = (REPOSITORY_ROOT / "gateway" / "config.yaml").read_text(encoding="utf-8")
    return yaml.safe_load(text)


def _decision_entries() -> list[dict[str, Any]]:
    return [
        entry
        for entry in _config()["model_list"]
        if str(entry["litellm_params"]["model"]).startswith("typesafe/")
    ]


def test_decision_entries_take_base_and_key_from_foundry_env_vars() -> None:
    """A typesafe/ entry without both Foundry env references would use the TypeSafe fallback."""
    for entry in _decision_entries():
        params = entry["litellm_params"]
        assert ENV_REF.match(str(params.get("api_base"))), entry["model_name"]
        assert ENV_REF.match(str(params.get("api_key"))), entry["model_name"]


def test_jev_classifier_uses_a_configured_deployment() -> None:
    """A Jev classifier must name a deployment; inline api_key/api_base would bypass the gateway."""
    for entry in _config()["model_list"]:
        router_config = entry["litellm_params"].get("complexity_router_config") or {}
        jev = router_config.get("jev_classifier_config")
        if jev is not None:
            assert jev.get("deployment_name") in {e["model_name"] for e in _decision_entries()}
            assert "api_key" not in jev and "api_base" not in jev


def test_unit_closes_the_typesafe_fallback_when_decision_models_exist() -> None:
    """The systemd unit must drop TYPESAFE_API_KEY and point TYPESAFE_API_BASE at a local port."""
    if not _decision_entries():
        pytest.skip("no decision models configured")
    unit = (REPOSITORY_ROOT / "deploy" / "litellm-foundry-gateway.service").read_text(encoding="utf-8")
    lines = {line.strip() for line in unit.splitlines()}
    assert "UnsetEnvironment=TYPESAFE_API_KEY" in lines
    assert "Environment=TYPESAFE_API_BASE=http://127.0.0.1:9" in lines


def test_env_example_does_not_ship_third_party_decision_credentials() -> None:
    """The example env file must not offer a TypeSafe or Gemini key to the gateway."""
    example = (REPOSITORY_ROOT / "deploy" / "gateway.env.example").read_text(encoding="utf-8")
    keys = {line.split("=", 1)[0] for line in example.splitlines() if "=" in line and not line.startswith("#")}
    assert "TYPESAFE_API_KEY" not in keys
    assert "GEMINI_API_KEY" not in keys
