"""Unit tests for deploy/build-catalog.py (no Codex binary needed)."""

import importlib.util
import json
from pathlib import Path

import pytest
import yaml

REPOSITORY_ROOT = Path(__file__).parents[2]
_SPEC = importlib.util.spec_from_file_location("build_catalog", REPOSITORY_ROOT / "deploy" / "build-catalog.py")
build_catalog = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(build_catalog)

EXAMPLE = json.loads((REPOSITORY_ROOT / "deploy" / "catalog-overrides.example.json").read_text(encoding="utf-8"))


def _bundled() -> list[dict]:
    keys = {key: None for model in EXAMPLE["models"].values() for key in model if key != "partner"}
    keys.update(EXAMPLE["partner_defaults"])
    base = {**keys, "slug": "gpt-6.1-sol", "model_messages": {"instructions_template": "x"}}
    return [base, {**base, "slug": "other"}]


def test_example_builds_every_gateway_model_with_sol_medium() -> None:
    """The example covers every Codex-facing gateway model; Sol defaults to medium and partners get no effort levels."""
    catalog = build_catalog.build(_bundled(), EXAMPLE)
    by_slug = {entry["slug"]: entry for entry in catalog["models"]}
    config = yaml.safe_load((REPOSITORY_ROOT / "gateway" / "config.yaml").read_text(encoding="utf-8"))
    assert set(by_slug) == {entry["model_name"] for entry in config["model_list"]} - {"Decision-1"}
    assert by_slug["gpt-6.1-sol"]["default_reasoning_level"] == "medium"
    assert by_slug["auto"]["supported_reasoning_levels"] == [] and by_slug["auto"]["apply_patch_tool_type"] is None
    assert by_slug["auto"]["model_messages"] == {"instructions_template": "x"}


def test_example_has_no_prompt_or_host_data() -> None:
    text = json.dumps(EXAMPLE)
    assert "model_messages" not in text and "instructions" not in text
    assert not any(marker in text for marker in ("azure.com", "/home/", "api_key"))


def test_rejects_missing_base_and_unknown_keys() -> None:
    with pytest.raises(SystemExit, match="base model 'missing'"):
        build_catalog.build(_bundled(), {**EXAMPLE, "base": "missing"})
    with pytest.raises(SystemExit, match="not in this Codex's catalog schema: bogus"):
        build_catalog.build(_bundled(), {**EXAMPLE, "models": {"x": {"bogus": 1}}})


def test_refuses_to_overwrite_existing_output(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    existing = tmp_path / "catalog.json"
    existing.write_text("personal", encoding="utf-8")
    monkeypatch.setattr("sys.argv", ["build-catalog.py", "overrides.json", str(existing)])
    with pytest.raises(SystemExit, match="never overwritten"):
        build_catalog.main()
    assert existing.read_text(encoding="utf-8") == "personal"
