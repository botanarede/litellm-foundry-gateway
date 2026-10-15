"""LiteLLM compatibility callback for Codex namespace tools."""

import logging
from collections.abc import AsyncGenerator
from typing import Any
from urllib.parse import urlparse

from fastapi import HTTPException
from litellm.integrations.custom_logger import CustomLogger

AZURE_HOSTS = (".openai.azure.com", ".services.ai.azure.com")
SEP = "__"
MAX_NAME = 64
_COMPOSITIONS = ("anyOf", "oneOf", "allOf", "not")
ToolMapping = dict[str, tuple[Any, Any]]
# Own handler: the proxy leaves module loggers at WARNING, which would hide the per-request routing line.
ROUTE_LOG = logging.getLogger("auto_route")
ROUTE_LOG.setLevel(logging.INFO)
if not ROUTE_LOG.handlers:
    ROUTE_LOG.addHandler(logging.StreamHandler())


def is_compat(model_group: str | None) -> bool:
    """Return whether a model group enables Codex tool compatibility."""
    if not model_group:
        return False
    # Resolve this at request time because LiteLLM initializes its router later.
    from litellm.proxy.proxy_server import llm_router

    if llm_router is None:
        return False
    return any(
        (deployment.get("model_info") or {}).get("codex_tools_compat")
        for deployment in llm_router.get_model_list(model_name=model_group) or []
    )


def _value(item: Any, name: str) -> Any:
    return item.get(name) if isinstance(item, dict) else getattr(item, name, None)


def _schema_ok(parameters: Any) -> bool:
    return isinstance(parameters, dict) and parameters.get("type") == "object" and not any(
        key in parameters for key in _COMPOSITIONS
    )


def _function(tool: dict[str, Any], name: Any) -> dict[str, Any] | None:
    parameters = tool.get("parameters")
    if parameters is None:
        parameters = {"type": "object", "properties": {}}
    if not _schema_ok(parameters):
        logging.getLogger(__name__).info("dropped tool with unsupported root schema: %s", tool.get("name"))
        return None
    return dict(tool, type="function", name=name, parameters=parameters)


def rewrite_tools(tools: list[Any] | None) -> tuple[list[Any], ToolMapping]:
    """Flatten safe namespace functions and return their restoration mapping."""
    output: list[Any] = []
    mapping: ToolMapping = {}
    reserved_names = {
        tool.get("name")
        for tool in tools or []
        if isinstance(tool, dict) and tool.get("type") != "namespace" and isinstance(tool.get("name"), str)
    }
    for tool in tools or []:
        if not isinstance(tool, dict):
            continue
        if tool.get("type") == "namespace":
            namespace = tool.get("name")
            for child in tool.get("tools") or []:
                if not isinstance(child, dict) or child.get("type", "function") != "function":
                    continue
                flat_name = f"{namespace}{SEP}{child.get('name')}"
                if len(flat_name) > MAX_NAME:
                    raise ValueError(f"flattened tool name exceeds {MAX_NAME} characters: {flat_name}")
                if flat_name in reserved_names:
                    raise ValueError(f"flattened tool name collides with another tool: {flat_name}")
                function = _function(child, flat_name)
                if function:
                    reserved_names.add(flat_name)
                    mapping[flat_name] = (namespace, child.get("name"))
                    output.append(function)
        elif tool.get("type") == "function":
            function = _function(tool, tool.get("name"))
            if function:
                output.append(function)
        else:
            output.append(tool)
    return output, mapping


def _rewrite_replayed_calls(items: Any, mapping: ToolMapping) -> None:
    original_names = {original: flat_name for flat_name, original in mapping.items()}
    for item in items or []:
        if not isinstance(item, dict) or item.get("type") != "function_call":
            continue
        namespace = item.get("namespace")
        if namespace is None:
            continue
        original = (namespace, item.get("name"))
        item["name"] = original_names.get(original, f"{namespace}{SEP}{item.get('name')}")
        item.pop("namespace", None)


def restore_item(item: Any, mapping: ToolMapping) -> None:
    """Restore a flattened function-call item to its namespace and original name."""
    flat_name = _value(item, "name")
    if _value(item, "type") != "function_call" or flat_name not in mapping:
        return
    namespace, name = mapping[flat_name]
    if isinstance(item, dict):
        item.update(name=name, namespace=namespace)
    else:
        setattr(item, "name", name)
        setattr(item, "namespace", namespace)


class CodexToolsCompat(CustomLogger):
    """Apply compatibility transformations only to configured Responses requests."""

    def __init__(self) -> None:
        super().__init__()
        self._maps: dict[str, ToolMapping] = {}

    async def async_pre_call_check(self, deployment: dict, parent_otel_span: Any) -> None:
        """Refuse typesafe/ calls whose base is not an https Azure host (blocks TYPESAFE_* fallback)."""
        params = deployment.get("litellm_params") or {}
        if not str(params.get("model", "")).startswith("typesafe/"):
            return None
        base = urlparse(str(params.get("api_base") or ""))
        if base.scheme != "https" or not (base.hostname or "").endswith(AZURE_HOSTS) or not params.get("api_key"):
            raise ValueError("decision deployment needs an https Azure Foundry api_base and an api_key")
        return None

    async def async_pre_call_hook(
        self, user_api_key_dict: Any, cache: Any, data: dict[str, Any], call_type: str
    ) -> dict[str, Any]:
        """Flatten tools and replayed calls for a compatible Responses request."""
        if call_type != "aresponses" or not is_compat(data.get("model")):
            return data
        mapping: ToolMapping = {}
        try:
            if data.get("tools"):
                data["tools"], mapping = rewrite_tools(data["tools"])
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=f"Invalid Codex tool configuration: {exc}") from exc
        _rewrite_replayed_calls(data.get("input"), mapping)
        call_id = data.get("litellm_call_id")
        if mapping and call_id is not None:
            self._maps[call_id] = mapping
        return data

    async def async_post_call_success_hook(
        self, data: dict[str, Any], user_api_key_dict: Any, response: Any
    ) -> Any:
        """Restore function-call output after a non-streaming upstream response."""
        mapping = self._maps.pop(data.get("litellm_call_id"), None)
        if mapping:
            for item in _value(response, "output") or []:
                restore_item(item, mapping)
        return response

    async def async_post_call_streaming_iterator_hook(
        self, user_api_key_dict: Any, response: Any, request_data: dict[str, Any]
    ) -> AsyncGenerator[Any, None]:
        """Restore streamed function-call items until iteration completes or fails."""
        mapping = self._maps.get(request_data.get("litellm_call_id"))
        try:
            async for chunk in response:
                if mapping:
                    restore_item(_value(chunk, "item"), mapping)
                    for item in _value(_value(chunk, "response"), "output") or []:
                        restore_item(item, mapping)
                yield chunk
        finally:
            self._maps.pop(request_data.get("litellm_call_id"), None)

    async def async_log_success_event(self, kwargs: dict[str, Any], response_obj: Any, start_time: Any, end_time: Any) -> None:
        """Log the tier and deployment that answered a complexity-routed request (no prompt content)."""
        metadata = (kwargs.get("litellm_params") or {}).get("metadata") or {}
        decision = metadata.get("routing_decision") or {}
        if decision.get("router_type") == "complexity":
            ROUTE_LOG.info("auto routed: tier=%s cause=%s model=%s", decision.get("tier"), decision.get("cause"), kwargs.get("model"))

    async def async_post_call_failure_hook(
        self,
        request_data: dict[str, Any],
        original_exception: Exception,
        user_api_key_dict: Any,
        traceback_str: str | None = None,
    ) -> None:
        """Discard per-call mappings when an upstream request fails."""
        self._maps.pop(request_data.get("litellm_call_id"), None)
        return None


codex_tools_compat = CodexToolsCompat()
