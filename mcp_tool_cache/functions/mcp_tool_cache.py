"""
title: MCP Tool Cache
description: Caches MCP tool lists per user so chats start without connecting to every enabled MCP server; connects only when a tool is called. Works with native Tool Search.
version: 1.1.0
required_open_webui_version: 0.13.0
"""

import asyncio
import copy
import hashlib
import importlib
import json
import logging
import time
from collections import OrderedDict
from typing import Any, Optional

from pydantic import BaseModel, Field

log = logging.getLogger(__name__)

STATE_KEY = "_mcp_tool_cache_v1"
REQUEST_KEY = "_mcp_tool_cache_request_v1"
REFRESH_KEY = "_mcp_tool_cache_refresh_v1"
PATCH_MARKER = "__mcp_tool_cache_patch__"


def _connection_fingerprint(connection: dict) -> str:
    # Any edit to the connection (URL, auth, headers, tool filter) yields a new key.
    encoded = json.dumps(connection, sort_keys=True, default=str, ensure_ascii=False)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _cache(request: Any) -> OrderedDict:
    state = request.app.state
    cache = getattr(state, STATE_KEY, None)
    if not isinstance(cache, OrderedDict):
        cache = OrderedDict()
        setattr(state, STATE_KEY, cache)
    return cache


def _refreshes(request: Any) -> dict:
    state = request.app.state
    refreshes = getattr(state, REFRESH_KEY, None)
    if not isinstance(refreshes, dict):
        refreshes = {}
        setattr(state, REFRESH_KEY, refreshes)
    return refreshes


def _store(request: Any, key: tuple, client: Any, specs: list, settings: dict) -> None:
    cache = _cache(request)
    cache[key] = {
        "expires_at": time.time() + settings["cache_seconds"],
        "specs": copy.deepcopy(specs),
        "instructions": getattr(client, "instructions", None),
        "tool_definitions": copy.deepcopy(getattr(client, "tool_definitions", {}) or {}),
    }
    cache.move_to_end(key)
    while len(cache) > settings["maximum_entries"]:
        cache.popitem(last=False)


class CachedMCPClient:
    """Stands in for MCPClient on a cache hit and connects on the first tool call."""

    def __init__(self, server_id: str, entry: dict, connect):
        self.server_name = server_id
        self.instructions = entry["instructions"]
        self.tool_definitions = copy.deepcopy(entry["tool_definitions"])
        self.client = None
        self._connect = connect
        self._real = None
        self._lock = asyncio.Lock()

    async def _connected(self):
        async with self._lock:
            if self._real is None:
                result = await self._connect()
                if result is None:
                    raise RuntimeError(f"MCP server '{self.server_name}' is no longer available.")
                self._real = result[0]
                self.tool_definitions = getattr(self._real, "tool_definitions", self.tool_definitions)
                self.client = getattr(self._real, "client", None)
            return self._real

    async def call_tool(self, function_name: str, function_args: dict):
        return await (await self._connected()).call_tool(function_name, function_args=function_args)

    async def call_tool_raw(self, function_name: str, function_args: dict):
        return await (await self._connected()).call_tool_raw(function_name, function_args)

    async def list_tool_specs(self):
        return await (await self._connected()).list_tool_specs()

    async def read_ui_resource(self, uri: str):
        return await (await self._connected()).read_ui_resource(uri)

    async def list_resources(self, cursor: Optional[str] = None):
        return await (await self._connected()).list_resources(cursor=cursor)

    async def read_resource(self, uri: str):
        return await (await self._connected()).read_resource(uri)

    async def disconnect(self):
        real, self._real, self.client = self._real, None, None
        if real is not None:
            await real.disconnect()


def _build_cached_connect(original, tools_module):
    async def cached_connect_mcp_server(request, server_id: str, user, extra_params: dict):
        metadata = (extra_params or {}).get("__metadata__") or {}
        settings = metadata.get(REQUEST_KEY) if isinstance(metadata, dict) else None
        if not settings or settings.get("cache_seconds", 0) <= 0 or request is None or user is None:
            return await original(request, server_id, user, extra_params)

        # Mirror the original's cheap gates on every hit; delegate anything unusual so it
        # keeps its own logging, errors, and None results.
        if not getattr(tools_module, "ENABLE_TOOL_SERVERS", False):
            return await original(request, server_id, user, extra_params)
        connection = None
        for candidate in await tools_module.Config.get("tool_server.connections", []):
            if candidate.get("type", "") == "mcp" and (candidate.get("info") or {}).get("id") == server_id:
                connection = candidate
                break
        if (
            not connection
            or not (connection.get("config") or {}).get("enable")
            or not await tools_module.has_connection_access(user, connection)
        ):
            return await original(request, server_id, user, extra_params)
        if connection.get("auth_type") in ("oauth_2.1", "oauth_2.1_static"):
            headers, _ = await tools_module.build_tool_server_headers(
                connection, request, user, server_id=server_id, metadata=metadata
            )
            if not headers.get("Authorization"):
                return await original(request, server_id, user, extra_params)

        key = (str(user.id), server_id, _connection_fingerprint(connection))

        async def connect_and_store():
            result = await original(request, server_id, user, extra_params)
            if result is not None:
                _store(request, key, result[0], result[1], settings)
            return result

        async def refresh():
            # Runs after the chat has its stale tool list; the connection is only used to read specs.
            try:
                result = await original(request, server_id, user, extra_params)
                if result is not None:
                    _store(request, key, result[0], result[1], settings)
                    await result[0].disconnect()
            except Exception:
                log.warning("MCP Tool Cache: background refresh of '%s' failed.", server_id, exc_info=True)
            finally:
                _refreshes(request).pop(key, None)

        cache = _cache(request)
        entry = cache.get(key)
        now = time.time()
        if entry is not None and entry["expires_at"] + settings.get("stale_seconds", 0) > now:
            cache.move_to_end(key)
            refreshes = _refreshes(request)
            if entry["expires_at"] <= now and key not in refreshes:
                refreshes[key] = asyncio.ensure_future(refresh())
            return CachedMCPClient(server_id, entry, connect_and_store), copy.deepcopy(entry["specs"])
        cache.pop(key, None)
        return await connect_and_store()

    setattr(cached_connect_mcp_server, PATCH_MARKER, True)
    return cached_connect_mcp_server


def install() -> bool:
    """Route chat-time MCP connections through the cache. Safe to call repeatedly."""
    try:
        middleware = importlib.import_module("open_webui.utils.middleware")
        tools_module = importlib.import_module("open_webui.utils.tools")
    except Exception:
        log.warning("MCP Tool Cache: Open WebUI modules unavailable; cache not installed.", exc_info=True)
        return False

    current = getattr(middleware, "connect_mcp_server", None)
    original = getattr(tools_module, "connect_mcp_server", None)
    required = ("Config", "has_connection_access", "build_tool_server_headers")
    ours = getattr(current, PATCH_MARKER, False) and getattr(current, "__mcp_tool_cache_original__", None) is original
    if (
        current is None
        or original is None
        or any(not hasattr(tools_module, name) for name in required)
        or (current is not original and not ours)
    ):
        log.warning("MCP Tool Cache: unexpected Open WebUI internals; cache not installed.")
        return False
    # Editing the function reloads this module; replace a wrapper left by older code.
    if ours and current.__globals__ is globals():
        return True

    patched = _build_cached_connect(original, tools_module)
    patched.__mcp_tool_cache_original__ = original
    middleware.connect_mcp_server = patched
    return True


class Filter:
    class Valves(BaseModel):
        priority: int = Field(
            default=0,
            description="Filter execution priority. Lower values run first.",
        )
        cache_seconds: int = Field(
            default=14_400,
            ge=0,
            description="Seconds a user's MCP tool list per server is fresh. 0 disables the cache.",
        )
        stale_seconds: int = Field(
            default=604_800,
            ge=0,
            description=(
                "Seconds after a tool list stops being fresh during which it is still served instantly "
                "while a background refresh updates it. After that, the next chat waits for a live connection."
            ),
        )
        maximum_entries: int = Field(
            default=512,
            ge=1,
            description="Maximum cached server tool lists across all users in this worker.",
        )

    def __init__(self):
        self.valves = self.Valves()
        install()

    async def inlet(self, body: dict, __metadata__: Optional[dict] = None) -> dict:
        if isinstance(__metadata__, dict) and install():
            __metadata__[REQUEST_KEY] = {
                "cache_seconds": self.valves.cache_seconds,
                "stale_seconds": self.valves.stale_seconds,
                "maximum_entries": self.valves.maximum_entries,
            }
        return body
