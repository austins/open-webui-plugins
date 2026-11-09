"""
title: MCP Tool Cache
description: Caches MCP tool lists in memory and on disk, per user unless every user gets the same list, so chats start without connecting to every enabled MCP server; connects only when a tool is called. Works with native Tool Search.
version: 1.2.0
required_open_webui_version: 0.13.0
"""

import asyncio
import contextlib
import copy
import hashlib
import hmac
import importlib
import json
import logging
import os
import tempfile
import time
from collections import OrderedDict
from pathlib import Path
from typing import Any, Optional

from pydantic import BaseModel, Field

log = logging.getLogger(__name__)

STATE_KEY = "_mcp_tool_cache_v1"
REQUEST_KEY = "_mcp_tool_cache_request_v1"
REFRESH_KEY = "_mcp_tool_cache_refresh_v1"
PATCH_MARKER = "__mcp_tool_cache_patch__"
STORAGE_FOLDER = "tool_lists"
MAXIMUM_FILE_BYTES = 8 * 1024 * 1024
ABANDONED_TEMP_SECONDS = 3_600

# Keeps fire-and-forget disk work alive until it finishes.
_pending_writes: set = set()
_pruned_directories: set = set()


def _in_background(function, *args) -> None:
    task = asyncio.get_running_loop().run_in_executor(None, function, *args)
    _pending_writes.add(task)
    task.add_done_callback(_finish_background)


def _finish_background(task: asyncio.Future) -> None:
    _pending_writes.discard(task)
    if not task.cancelled() and task.exception() is not None:
        log.warning("MCP Tool Cache: cache file maintenance failed.", exc_info=task.exception())


def _connection_fingerprint(connection: dict, secret: Optional[bytes]) -> str:
    # Any edit to the connection (URL, auth, headers, tool filter) yields a new fingerprint.
    # Keyed when it can be, because it is saved to disk and the connection includes the API key.
    encoded = json.dumps(connection, sort_keys=True, default=str, ensure_ascii=False).encode("utf-8")
    if secret is not None:
        return hmac.new(secret, encoded, hashlib.sha256).hexdigest()
    return hashlib.sha256(encoded).hexdigest()


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


def _remember(request: Any, key: tuple, entry: dict, settings: dict) -> None:
    cache = _cache(request)
    cache[key] = entry
    cache.move_to_end(key)
    while len(cache) > settings["maximum_entries"]:
        cache.popitem(last=False)


def _store(request: Any, key: tuple, fingerprint: str, client: Any, specs: list, settings: dict) -> dict:
    now = time.time()
    entry = {
        "fingerprint": fingerprint,
        "stored_at": now,
        "expires_at": now + settings["cache_seconds"],
        "specs": copy.deepcopy(specs),
        "instructions": getattr(client, "instructions", None),
        "tool_definitions": copy.deepcopy(getattr(client, "tool_definitions", {}) or {}),
    }
    _remember(request, key, entry, settings)
    return entry


def _shared(connection: dict, tools_module) -> bool:
    # True when Open WebUI sends every user the same MCP request (build_tool_server_headers
    # in open_webui.utils.tools); MCP drops forwarded cookies. Anything unknown stays per user.
    headers = connection.get("headers")
    return (
        connection.get("auth_type", "bearer") in ("bearer", "none")
        and not (isinstance(headers, dict) and any("{{" in str(value) for value in headers.values()))
        and getattr(tools_module, "ENABLE_FORWARD_USER_INFO_HEADERS", True) is False
    )


def _storage_dir(filter_id: Any) -> Optional[str]:
    # The filter id becomes a path component; identifiers, as Open WebUI requires, never hold / \ or ..
    if not isinstance(filter_id, str) or not filter_id.isidentifier():
        return None
    try:
        cache_dir = importlib.import_module("open_webui.config").CACHE_DIR
    except Exception:
        return None
    return str(Path(cache_dir) / "functions" / filter_id / STORAGE_FOLDER)


def _storage_secret() -> Optional[bytes]:
    try:
        secret = importlib.import_module("open_webui.env").WEBUI_SECRET_KEY
    except Exception:
        return None
    if not isinstance(secret, str) or not secret:
        return None
    return hmac.new(secret.encode("utf-8"), b"mcp_tool_cache/fingerprint/v1", hashlib.sha256).digest()


def _entry_name(key: tuple) -> str:
    # One fixed file per key, so a changed connection overwrites it instead of leaving an orphan.
    return hashlib.sha256(json.dumps(list(key)).encode("utf-8")).hexdigest()


def _valid_entry(entry: Any) -> bool:
    return (
        isinstance(entry, dict)
        and set(entry) == {"fingerprint", "stored_at", "expires_at", "specs", "instructions", "tool_definitions"}
        and isinstance(entry["fingerprint"], str)
        and all(
            isinstance(entry[field], (int, float)) and not isinstance(entry[field], bool)
            for field in ("stored_at", "expires_at")
        )
        and isinstance(entry["specs"], list)
        and all(isinstance(spec, dict) for spec in entry["specs"])
        and (entry["instructions"] is None or isinstance(entry["instructions"], str))
        and isinstance(entry["tool_definitions"], dict)
    )


def _unlink(path: str) -> None:
    try:
        os.unlink(path)
    except FileNotFoundError:
        pass


def _read_entry(directory: str, key: tuple) -> Optional[dict]:
    path = os.path.join(directory, f"{_entry_name(key)}.json")
    try:
        descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_BINARY", 0))
    except FileNotFoundError:
        return None
    except OSError:
        log.warning("MCP Tool Cache: discarding unreadable cache file.", exc_info=True)
        _unlink(path)
        return None
    try:
        with os.fdopen(descriptor, "rb") as file:
            raw = file.read(MAXIMUM_FILE_BYTES + 1)
        entry = json.loads(raw) if len(raw) <= MAXIMUM_FILE_BYTES else None
        if _valid_entry(entry):
            return entry
    except (OSError, ValueError):
        pass
    log.warning("MCP Tool Cache: discarding invalid cache file.")
    _unlink(path)
    return None


@contextlib.contextmanager
def _directory_lock(directory: str):
    try:
        import fcntl
    except ImportError:
        # shortcut: no cross-worker write lock without fcntl (Windows); a late older write can win until it expires.
        yield
        return
    descriptor = os.open(
        os.path.join(directory, ".lock"), os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0), 0o600
    )
    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX)
        yield
    finally:
        os.close(descriptor)


def _write_entry(directory: str, key: tuple, entry: dict, settings: dict) -> None:
    os.makedirs(directory, mode=0o700, exist_ok=True)
    with _directory_lock(directory):
        existing = _read_entry(directory, key)
        # Writes from other threads and workers can finish out of order; never replace a newer list.
        if existing is None or existing["stored_at"] < entry["stored_at"]:
            _replace_entry(directory, key, entry)
    _prune(directory, settings)


def _replace_entry(directory: str, key: tuple, entry: dict) -> None:
    data = json.dumps(
        entry,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode("utf-8")
    if len(data) > MAXIMUM_FILE_BYTES:
        return
    # mkstemp creates the file with mode 0600; os.replace swaps it in atomically for other workers.
    descriptor, temporary = tempfile.mkstemp(dir=directory, prefix=".", suffix=".tmp")
    try:
        with os.fdopen(descriptor, "wb") as file:
            file.write(data)
        os.replace(temporary, os.path.join(directory, f"{_entry_name(key)}.json"))
    except BaseException:
        _unlink(temporary)
        raise


def _prune(directory: str, settings: dict) -> None:
    now = time.time()
    keep_seconds = settings["cache_seconds"] + settings.get("stale_seconds", 0)
    files = []
    if not os.path.isdir(directory):
        return
    with os.scandir(directory) as entries:
        for item in entries:
            if not item.is_file(follow_symlinks=False):
                continue
            modified = item.stat(follow_symlinks=False).st_mtime
            if item.name.endswith(".tmp"):
                if modified + ABANDONED_TEMP_SECONDS < now:
                    _unlink(item.path)
            elif item.name.endswith(".json"):
                if modified + keep_seconds < now:
                    _unlink(item.path)
                else:
                    files.append((modified, item.path))
    files.sort()
    for _, path in files[: max(0, len(files) - settings["maximum_entries"])]:
        _unlink(path)


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

        secret = _storage_secret()
        # Disk needs the keyed fingerprint; without WEBUI_SECRET_KEY the cache stays in memory.
        directory = settings.get("storage_dir") if secret is not None else None
        # Open WebUI user ids are UUIDs, so "*" can't collide with one.
        key = ("*" if _shared(connection, tools_module) else str(user.id), server_id)
        fingerprint = _connection_fingerprint(connection, secret)

        def save(client, specs):
            entry = _store(request, key, fingerprint, client, specs, settings)
            if directory:
                # Not awaited, so a cancelled chat can't strand the connection that was just opened.
                _in_background(_write_entry, directory, key, entry, settings)

        async def connect_and_store():
            result = await original(request, server_id, user, extra_params)
            if result is not None:
                save(result[0], result[1])
            return result

        async def refresh():
            # Runs after the chat has its stale tool list; the connection is only used to read specs.
            try:
                result = await original(request, server_id, user, extra_params)
                if result is not None:
                    try:
                        save(result[0], result[1])
                    finally:
                        await result[0].disconnect()
            except Exception:
                log.warning("MCP Tool Cache: background refresh of '%s' failed.", server_id, exc_info=True)
            finally:
                _refreshes(request).pop(key, None)

        def current(item):
            return item if item is not None and item["fingerprint"] == fingerprint else None

        cache = _cache(request)
        entry = current(cache.get(key))
        now = time.time()
        if directory and (entry is None or entry["expires_at"] <= now):
            # Another worker or an earlier process may hold a fresher list.
            try:
                stored = await asyncio.to_thread(_read_entry, directory, key)
            except Exception:
                log.warning("MCP Tool Cache: could not read the cache file for '%s'.", server_id, exc_info=True)
                stored = None
            # Another request may have stored a newer list in memory while the read was in flight.
            candidates = [item for item in (entry, current(cache.get(key)), current(stored)) if item is not None]
            entry = max(candidates, key=lambda item: item["stored_at"], default=None)
        if entry is not None and entry["expires_at"] + settings.get("stale_seconds", 0) > now:
            # The disk read above may have let another request evict this key.
            _remember(request, key, entry, settings)
            refreshes = _refreshes(request)
            if entry["expires_at"] <= now and key not in refreshes:
                refreshes[key] = asyncio.ensure_future(refresh())
            return CachedMCPClient(server_id, entry, connect_and_store), copy.deepcopy(entry["specs"])
        cache.pop(key, None)
        return await connect_and_store()

    setattr(cached_connect_mcp_server, PATCH_MARKER, True)
    return cached_connect_mcp_server


def install() -> bool:
    """Replace open_webui.utils.middleware.connect_mcp_server with a caching wrapper.

    The original in open_webui.utils.tools is left untouched and is called on every miss,
    refresh, and tool call. Refuses to patch unexpected internals or another plugin's
    replacement. The patch lasts until Open WebUI restarts. Safe to call repeatedly.
    """
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
            description="Maximum cached server tool lists across all users, both in each worker's memory and on disk.",
        )

    def __init__(self):
        self.valves = self.Valves()
        install()

    async def inlet(
        self, body: dict, __metadata__: Optional[dict] = None, __id__: Optional[str] = None
    ) -> dict:
        if isinstance(__metadata__, dict) and install():
            __metadata__[REQUEST_KEY] = {
                "cache_seconds": self.valves.cache_seconds,
                "stale_seconds": self.valves.stale_seconds,
                "maximum_entries": self.valves.maximum_entries,
                "storage_dir": _storage_dir(__id__),
            }
            directory = __metadata__[REQUEST_KEY]["storage_dir"]
            if directory and directory not in _pruned_directories:
                # Clears files for removed users and servers even when nothing new is written.
                _pruned_directories.add(directory)
                _in_background(_prune, directory, __metadata__[REQUEST_KEY])
        return body
