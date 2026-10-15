"""Unit tests for the Codex namespace-tool compatibility callback."""

from copy import deepcopy
import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
import yaml
from fastapi import HTTPException
from litellm.caching.dual_cache import DualCache
from litellm.proxy._types import UserAPIKeyAuth

import codex_tools_compat as compat_module


FIXTURES_DIR = Path(__file__).parents[1] / "fixtures"
REPOSITORY_ROOT = Path(__file__).parents[2]


def _load_json(filename: str) -> Any:
    return json.loads((FIXTURES_DIR / filename).read_text(encoding="utf-8"))


def _object_parameters() -> dict[str, Any]:
    return {"type": "object", "properties": {}}


def _namespace_tool(namespace: str = "mcp", name: str = "run") -> dict[str, Any]:
    return {
        "type": "namespace",
        "name": namespace,
        "tools": [{"type": "function", "name": name, "parameters": _object_parameters()}],
    }


class FakeRouter:
    """Minimal router fake for is_compat tests."""

    def __init__(self, deployments: list[dict[str, Any]]) -> None:
        self.deployments = deployments
        self.requested_models: list[str] = []

    def get_model_list(self, *, model_name: str) -> list[dict[str, Any]]:
        self.requested_models.append(model_name)
        return self.deployments


@pytest.fixture(autouse=True)
def clear_callback_maps() -> None:
    compat_module.codex_tools_compat._maps.clear()
    yield
    compat_module.codex_tools_compat._maps.clear()


@pytest.fixture
def user_api_key() -> UserAPIKeyAuth:
    return UserAPIKeyAuth()


@pytest.fixture
def cache() -> DualCache:
    return DualCache()


def test_r2_1_config_drops_reasoning_effort() -> None:
    """Compat models configure effort removal while OpenAI models remain plain."""
    config = yaml.safe_load((REPOSITORY_ROOT / "gateway" / "config.yaml").read_text(encoding="utf-8"))
    models = config["model_list"]
    # The `auto` router entry carries the flag but routes to the partner entries below, which drop effort.
    compat_models = [
        model
        for model in models
        if (model.get("model_info") or {}).get("codex_tools_compat")
        and model["litellm_params"]["model"].startswith("azure/")
    ]
    openai_models = [
        model
        for model in models
        if model["litellm_params"]["model"].startswith("azure/gpt-")
    ]

    assert compat_models
    assert openai_models
    assert all(
        "reasoning.effort" in model["litellm_params"].get("additional_drop_params", [])
        for model in compat_models
    )
    assert all(
        (model.get("model_info") or {}).get("codex_tools_compat") is True
        for model in compat_models
    )
    assert all(
        not (model.get("model_info") or {}).get("codex_tools_compat")
        for model in openai_models
    )
    assert all("additional_drop_params" not in model["litellm_params"] for model in openai_models)


async def test_r2_2_flattens_fixture_and_restores_non_stream_items(
    monkeypatch: pytest.MonkeyPatch, user_api_key: UserAPIKeyAuth, cache: DualCache
) -> None:
    """Namespace children from the Codex fixture flatten and restore after a normal response."""
    monkeypatch.setattr(compat_module, "is_compat", lambda _model: True)
    callback = compat_module.codex_tools_compat
    tools = _load_json("codex_tools_0160.json")
    expected_mapping = {
        f"{namespace_tool['name']}__{child['name']}": (namespace_tool["name"], child["name"])
        for namespace_tool in tools
        if namespace_tool.get("type") == "namespace"
        for child in namespace_tool["tools"]
    }
    top_level_function_names = {
        tool["name"] for tool in tools if tool.get("type") == "function"
    }
    data = {
        "model": "DeepSeek-V4-Pro",
        "litellm_call_id": "non-stream",
        "tools": deepcopy(tools),
    }

    returned = await callback.async_pre_call_hook(user_api_key, cache, data, "aresponses")

    function_names = {
        tool["name"] for tool in data["tools"] if tool.get("type") == "function"
    }
    assert returned is data
    assert expected_mapping.keys() <= function_names
    assert top_level_function_names <= function_names
    assert all(tool.get("type") != "namespace" for tool in data["tools"])
    assert {"type": "web_search"} in data["tools"]
    assert callback._maps["non-stream"] == expected_mapping

    dictionary_item = {"type": "function_call", "name": "multi_agent_v1__close_agent"}
    attribute_item = SimpleNamespace(type="function_call", name="mcp__example_repl__js")
    response = {"output": [dictionary_item, attribute_item]}

    assert await callback.async_post_call_success_hook(data, user_api_key, response) is response
    assert dictionary_item == {
        "type": "function_call",
        "name": "close_agent",
        "namespace": "multi_agent_v1",
    }
    assert attribute_item.name == "js"
    assert attribute_item.namespace == "mcp__example_repl"
    assert callback._maps == {}


async def test_r2_2_restores_streaming_item_and_response_output(
    monkeypatch: pytest.MonkeyPatch, user_api_key: UserAPIKeyAuth, cache: DualCache
) -> None:
    """Streaming restoration handles both chunk.item and chunk.response.output."""
    monkeypatch.setattr(compat_module, "is_compat", lambda _model: True)
    callback = compat_module.codex_tools_compat
    namespace_tool = next(
        tool
        for tool in _load_json("codex_tools_0160.json")
        if tool.get("type") == "namespace" and tool["name"] == "mcp__example_repl"
    )
    data = {
        "model": "Kimi-K2.7-Code",
        "litellm_call_id": "stream-end",
        "tools": [deepcopy(namespace_tool)],
    }
    await callback.async_pre_call_hook(user_api_key, cache, data, "aresponses")

    async def fake_stream() -> Any:
        yield SimpleNamespace(
            item=SimpleNamespace(type="function_call", name="mcp__example_repl__js"),
            response=SimpleNamespace(
                output=[{"type": "function_call", "name": "mcp__example_repl__js"}]
            ),
        )

    chunks = [
        chunk
        async for chunk in callback.async_post_call_streaming_iterator_hook(
            user_api_key, fake_stream(), data
        )
    ]

    assert len(chunks) == 1
    assert chunks[0].item.name == "js"
    assert chunks[0].item.namespace == "mcp__example_repl"
    assert chunks[0].response.output[0] == {
        "type": "function_call",
        "name": "js",
        "namespace": "mcp__example_repl",
    }
    assert callback._maps == {}


async def test_r2_2_cleans_mapping_after_stream_error(
    monkeypatch: pytest.MonkeyPatch, user_api_key: UserAPIKeyAuth, cache: DualCache
) -> None:
    """The streaming wrapper discards state even when the upstream iterator raises."""
    monkeypatch.setattr(compat_module, "is_compat", lambda _model: True)
    callback = compat_module.codex_tools_compat
    data = {
        "model": "grok-4.7",
        "litellm_call_id": "stream-error",
        "tools": [_namespace_tool()],
    }
    await callback.async_pre_call_hook(user_api_key, cache, data, "aresponses")

    async def failing_stream() -> Any:
        yield SimpleNamespace(item=None, response=SimpleNamespace(output=[]))
        raise RuntimeError("simulated stream failure")

    received = []
    with pytest.raises(RuntimeError, match="simulated stream failure"):
        async for chunk in callback.async_post_call_streaming_iterator_hook(
            user_api_key, failing_stream(), data
        ):
            received.append(chunk)

    assert len(received) == 1
    assert callback._maps == {}


async def test_r2_2_cleans_mapping_after_failure_hook(
    monkeypatch: pytest.MonkeyPatch, user_api_key: UserAPIKeyAuth, cache: DualCache
) -> None:
    """The failure hook removes the pending restoration mapping."""
    monkeypatch.setattr(compat_module, "is_compat", lambda _model: True)
    callback = compat_module.codex_tools_compat
    data = {
        "model": "grok-4.7",
        "litellm_call_id": "failure",
        "tools": [_namespace_tool()],
    }
    await callback.async_pre_call_hook(user_api_key, cache, data, "aresponses")
    assert callback._maps

    assert await callback.async_post_call_failure_hook(
        data, RuntimeError("simulated upstream failure"), user_api_key
    ) is None
    assert callback._maps == {}


def test_r2_2_leaves_unmapped_function_call_untouched() -> None:
    """Unmapped function calls must not gain a namespace or a rewritten name."""
    item = {"type": "function_call", "name": "ordinary", "namespace": "existing"}
    original = deepcopy(item)

    compat_module.restore_item(item, {"mcp__known": ("mcp", "known")})

    assert item == original


async def test_r2_3_rewrites_function_call_history(
    monkeypatch: pytest.MonkeyPatch, user_api_key: UserAPIKeyAuth, cache: DualCache
) -> None:
    """Replayed namespaced calls use the upstream flattened name and omit namespace."""
    monkeypatch.setattr(compat_module, "is_compat", lambda _model: True)
    callback = compat_module.codex_tools_compat
    namespace_tool = next(
        tool
        for tool in _load_json("codex_tools_0160.json")
        if tool.get("type") == "namespace" and tool["name"] == "mcp__example_repl"
    )
    replayed_call = {
        "type": "function_call",
        "name": "js",
        "namespace": "mcp__example_repl",
        "call_id": "call-1",
    }
    data = {
        "model": "DeepSeek-V4-Pro",
        "litellm_call_id": "history",
        "tools": [deepcopy(namespace_tool)],
        "input": [replayed_call],
    }

    await callback.async_pre_call_hook(user_api_key, cache, data, "aresponses")

    assert replayed_call == {
        "type": "function_call",
        "name": "mcp__example_repl__js",
        "call_id": "call-1",
    }
    await callback.async_post_call_failure_hook(data, RuntimeError("cleanup"), user_api_key)
    assert callback._maps == {}


@pytest.mark.parametrize("composition", ("anyOf", "oneOf", "allOf", "not"))
def test_r2_4_drops_composed_roots(composition: str) -> None:
    """Composed schemas at a function root cannot be sent to compatibility models."""
    rewritten, mapping = compat_module.rewrite_tools(
        [
            {
                "type": "function",
                "name": f"composed_{composition}",
                "parameters": {"type": "object", composition: {}},
            }
        ]
    )

    assert rewritten == []
    assert mapping == {}


def test_r2_4_drops_non_object_root() -> None:
    """A scalar root schema is dropped rather than being flattened lossily."""
    rewritten, mapping = compat_module.rewrite_tools(
        [{"type": "function", "name": "scalar", "parameters": {"type": "string"}}]
    )

    assert rewritten == []
    assert mapping == {}


def test_r2_4_defaults_missing_parameters() -> None:
    """Functions without parameters receive the required empty object schema."""
    rewritten, mapping = compat_module.rewrite_tools([{"type": "function", "name": "no_schema"}])

    assert rewritten == [
        {
            "type": "function",
            "name": "no_schema",
            "parameters": {"type": "object", "properties": {}},
        }
    ]
    assert mapping == {}


def test_r2_4_drops_automation_update_anyof_and_keeps_other_tools() -> None:
    """The automation fixture drops while valid functions and web search survive."""
    automation_update = _load_json("automation_update_anyof.json")
    valid_function = {
        "type": "function",
        "name": "surviving_tool",
        "parameters": _object_parameters(),
    }
    web_search = {"type": "web_search"}

    rewritten, mapping = compat_module.rewrite_tools([automation_update, valid_function, web_search])

    assert automation_update not in rewritten
    assert valid_function in rewritten
    assert web_search in rewritten
    assert mapping == {}


async def test_r2_4_preserves_missing_tools_key(
    monkeypatch: pytest.MonkeyPatch, user_api_key: UserAPIKeyAuth, cache: DualCache
) -> None:
    """A compatible request without tools must not acquire an empty tools list."""
    monkeypatch.setattr(compat_module, "is_compat", lambda _model: True)
    data = {"model": "DeepSeek-V4-Pro", "litellm_call_id": "no-tools"}

    returned = await compat_module.codex_tools_compat.async_pre_call_hook(
        user_api_key, cache, data, "aresponses"
    )

    assert returned is data
    assert "tools" not in data
    assert compat_module.codex_tools_compat._maps == {}


async def test_r2_5_rejects_colliding_namespace_flattened_names_as_bad_request(
    monkeypatch: pytest.MonkeyPatch, user_api_key: UserAPIKeyAuth, cache: DualCache
) -> None:
    """Distinct namespace paths that flatten identically must fail rather than guess."""
    tools = [_namespace_tool("mcp", "node__run"), _namespace_tool("mcp__node", "run")]

    with pytest.raises(ValueError, match="collides"):
        compat_module.rewrite_tools(deepcopy(tools))

    monkeypatch.setattr(compat_module, "is_compat", lambda _model: True)
    data = {"model": "DeepSeek-V4-Pro", "litellm_call_id": "namespace-collision", "tools": tools}
    with pytest.raises(HTTPException) as error:
        await compat_module.codex_tools_compat.async_pre_call_hook(
            user_api_key, cache, data, "aresponses"
        )

    assert error.value.status_code == 400
    assert "collides" in error.value.detail


@pytest.mark.parametrize(
    ("tools", "error_text"),
    (
        (
            [
                {
                    "type": "namespace",
                    "name": "n" * 60,
                    "tools": [
                        {"type": "function", "name": "abc", "parameters": _object_parameters()}
                    ],
                }
            ],
            "exceeds",
        ),
        (
            [
                {
                    "type": "function",
                    "name": "mcp__js",
                    "parameters": _object_parameters(),
                },
                _namespace_tool("mcp", "js"),
            ],
            "collides",
        ),
    ),
    ids=("name-over-64-characters", "namespace-child-collides-with-top-level"),
)
async def test_r2_5_rejects_invalid_flattened_names_as_bad_requests(
    monkeypatch: pytest.MonkeyPatch,
    user_api_key: UserAPIKeyAuth,
    cache: DualCache,
    tools: list[dict[str, Any]],
    error_text: str,
) -> None:
    """Helper errors become clear HTTP 400 responses at the pre-call boundary."""
    with pytest.raises(ValueError, match=error_text):
        compat_module.rewrite_tools(deepcopy(tools))

    monkeypatch.setattr(compat_module, "is_compat", lambda _model: True)
    data = {"model": "DeepSeek-V4-Pro", "litellm_call_id": error_text, "tools": deepcopy(tools)}
    with pytest.raises(HTTPException) as error:
        await compat_module.codex_tools_compat.async_pre_call_hook(
            user_api_key, cache, data, "aresponses"
        )

    assert error.value.status_code == 400
    assert error_text in error.value.detail


@pytest.mark.parametrize(
    ("deployments", "expected"),
    (
        ([{"model_info": {"codex_tools_compat": True}}], True),
        ([{"model_info": {"codex_tools_compat": False}}, {"model_info": {}}], False),
        (None, False),
    ),
    ids=("compat-deployment", "non-compat-deployments", "no-router"),
)
def test_r2_6_is_compat_checks_router_deployments(
    monkeypatch: pytest.MonkeyPatch, deployments: list[dict[str, Any]] | None, expected: bool
) -> None:
    """is_compat handles true, false, and unavailable router states."""
    from litellm.proxy import proxy_server

    router = None if deployments is None else FakeRouter(deployments)
    monkeypatch.setattr(proxy_server, "llm_router", router)

    assert compat_module.is_compat("fixture-model") is expected
    if router is not None:
        assert router.requested_models == ["fixture-model"]


async def test_r2_6_leaves_non_compat_request_unchanged(
    monkeypatch: pytest.MonkeyPatch, user_api_key: UserAPIKeyAuth, cache: DualCache
) -> None:
    """OpenAI/non-compat requests pass through with byte-for-byte equivalent data."""
    monkeypatch.setattr(compat_module, "is_compat", lambda _model: False)
    data = {
        "model": "gpt-6.1-sol",
        "litellm_call_id": "plain-model",
        "tools": [_namespace_tool()],
        "input": [{"type": "function_call", "name": "run", "namespace": "mcp"}],
    }
    original = deepcopy(data)

    returned = await compat_module.codex_tools_compat.async_pre_call_hook(
        user_api_key, cache, data, "aresponses"
    )

    assert returned == original
    assert compat_module.codex_tools_compat._maps == {}


@pytest.mark.parametrize("call_type", ("acompletion", "aembedding", "adecisions"))
async def test_r2_7_leaves_other_call_types_untouched(
    monkeypatch: pytest.MonkeyPatch,
    user_api_key: UserAPIKeyAuth,
    cache: DualCache,
    call_type: str,
) -> None:
    """Chat, embedding, and decisions-like routes bypass compatibility rewriting."""
    monkeypatch.setattr(compat_module, "is_compat", lambda _model: True)
    data = {
        "model": "DeepSeek-V4-Pro",
        "litellm_call_id": call_type,
        "tools": [_namespace_tool()],
        "input": [{"type": "function_call", "name": "run", "namespace": "mcp"}],
    }
    original = deepcopy(data)

    returned = await compat_module.codex_tools_compat.async_pre_call_hook(
        user_api_key, cache, data, call_type
    )

    assert returned == original
    assert compat_module.codex_tools_compat._maps == {}


async def test_routing_log_reports_tier_and_model_without_prompt(caplog: pytest.LogCaptureFixture) -> None:
    """Only complexity-routed answers are logged, with tier, cause and model but no request content."""
    callback = compat_module.codex_tools_compat
    decision = {"router_type": "complexity", "tier": "SIMPLE", "cause": "jev_classifier"}
    routed = {"model": "DeepSeek-V4-Flash", "messages": "secret prompt", "litellm_params": {"metadata": {"routing_decision": decision}}}
    classifier = {"model": "Decision-1", "litellm_params": {"metadata": {}}}
    with caplog.at_level("INFO", logger="auto_route"):
        await callback.async_log_success_event(classifier, None, None, None)
        await callback.async_log_success_event(routed, None, None, None)
    assert [record.getMessage() for record in caplog.records] == [
        "auto routed: tier=SIMPLE cause=jev_classifier model=DeepSeek-V4-Flash"
    ]


@pytest.mark.parametrize(
    ("model_info", "reasoning", "expected"),
    [
        ({"reasoning_effort_none_as": "medium"}, {"effort": "none", "summary": "auto"}, {"effort": "medium", "summary": "auto"}),
        ({"reasoning_effort_none_as": "medium"}, {"effort": "high"}, {"effort": "high"}),
        ({"reasoning_effort_none_as": "medium"}, None, None),
        ({"codex_tools_compat": True}, {"effort": "none"}, {"effort": "none"}),
        (None, {"effort": "none"}, {"effort": "none"}),
    ],
)
async def test_effort_none_is_replaced_only_for_flagged_deployments(
    model_info: dict[str, Any] | None, reasoning: dict[str, Any] | None, expected: dict[str, Any] | None
) -> None:
    """Only a deployment flagged in model_info gets "none" replaced; valid efforts and other models pass through."""
    kwargs = {"model": "azure/x", "model_info": model_info, "reasoning": deepcopy(reasoning)}
    result = await compat_module.codex_tools_compat.async_pre_call_deployment_hook(kwargs, None)
    assert (result or kwargs).get("reasoning") == expected


def test_only_sol_maps_effort_none_to_medium() -> None:
    """The config flags Sol alone, with medium (never low), so partner tiers keep dropping effort."""
    config = yaml.safe_load((REPOSITORY_ROOT / "gateway" / "config.yaml").read_text(encoding="utf-8"))
    flagged = {
        entry["model_name"]: entry["model_info"]["reasoning_effort_none_as"]
        for entry in config["model_list"]
        if (entry.get("model_info") or {}).get("reasoning_effort_none_as")
    }
    assert flagged == {"gpt-6.1-sol": "medium"}
