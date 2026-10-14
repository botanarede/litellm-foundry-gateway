"""Checks for the optional `auto` complexity router (Profile E).

`auto` is a LiteLLM complexity router whose classifier is the Decision-1 deployment. It is used only
when a client asks for model `auto`; every other model entry must stay exactly as it was. The shape
is validated with LiteLLM's own pydantic class so field names are checked by the library.
"""

from pathlib import Path
from typing import Any

import yaml
from litellm.router_strategy.complexity_router.config import ComplexityRouterConfig

REPOSITORY_ROOT = Path(__file__).parents[2]
ALLOWED_TARGETS = {"DeepSeek-V4-Flash", "DeepSeek-V4-Pro", "gpt-6.1-sol"}
TIERS = {"SIMPLE", "MEDIUM", "COMPLEX", "REASONING"}


def _config() -> dict[str, Any]:
    text = (REPOSITORY_ROOT / "gateway" / "config.yaml").read_text(encoding="utf-8")
    return yaml.safe_load(text)


def _entries() -> dict[str, dict[str, Any]]:
    return {entry["model_name"]: entry for entry in _config()["model_list"]}


def _router_config() -> dict[str, Any]:
    return _entries()["auto"]["litellm_params"]["complexity_router_config"]


def test_auto_is_a_complexity_router_with_a_decision_classifier() -> None:
    """`auto` must exist and classify with a typesafe/ deployment, never with inline credentials."""
    entries = _entries()
    assert entries["auto"]["litellm_params"]["model"] == "auto_router/complexity_router"
    router_config = _router_config()
    assert router_config["classifier_type"] == "jev"
    jev = router_config["jev_classifier_config"]
    deployment = entries[jev["deployment_name"]]
    assert str(deployment["litellm_params"]["model"]).startswith("typesafe/")
    assert "api_key" not in jev and "api_base" not in jev


def test_every_target_is_an_existing_non_decision_model() -> None:
    """Tier targets and the default are real, routable model names limited to the owner's three."""
    entries = _entries()
    router_config = _router_config()
    assert set(router_config["tiers"]) == TIERS
    targets = set(router_config["tiers"].values()) | {router_config["default_model"]}
    for target in targets:
        assert target in entries and target != "auto"
        assert not str(entries[target]["litellm_params"]["model"]).startswith("typesafe/")
    assert targets == ALLOWED_TARGETS
    assert router_config["tiers"] == {
        "SIMPLE": "DeepSeek-V4-Flash",
        "MEDIUM": "DeepSeek-V4-Pro",
        "COMPLEX": "gpt-6.1-sol",
        "REASONING": "gpt-6.1-sol",
    }


def test_classifier_failure_routes_to_the_cheapest_model() -> None:
    """A failed or timed-out classifier call must not guess a tier."""
    router_config = _router_config()
    assert router_config["classifier_fallback"] == "default_model"
    assert router_config["default_model"] == "DeepSeek-V4-Flash"
    assert router_config["session_affinity"] is True


def test_auto_enables_codex_tool_compatibility() -> None:
    """The callback keys on the requested model name, so the flag must be on `auto` itself."""
    assert _entries()["auto"]["model_info"]["codex_tools_compat"] is True


def test_no_fallbacks_are_configured_anywhere() -> None:
    """Explicit models must behave as before: no entry-level or router-level fallbacks."""
    config = _config()
    for entry in config["model_list"]:
        assert "fallbacks" not in entry
        assert "fallbacks" not in entry["litellm_params"]
    assert "fallbacks" not in (config.get("router_settings") or {})
    assert "fallbacks" not in (config.get("litellm_settings") or {})


def test_router_config_is_valid_for_litellm() -> None:
    """LiteLLM's own pydantic class validates every field name and cross-field rule."""
    validated = ComplexityRouterConfig.model_validate(_router_config())
    assert validated.classifier_type == "jev"
    assert validated.jev_classifier_config is not None
    assert validated.jev_classifier_config.deployment_name == "Decision-1"
    assert validated.jev_classifier_config.timeout_ms == 3000
    assert validated.classifier_fallback == "default_model"
    assert validated.session_affinity is True


def test_explicit_models_do_not_reference_the_router() -> None:
    """Only `auto` uses the router; every other entry is a plain Azure or decision deployment."""
    for name, entry in _entries().items():
        if name != "auto":
            assert not str(entry["litellm_params"]["model"]).startswith("auto_router/")
