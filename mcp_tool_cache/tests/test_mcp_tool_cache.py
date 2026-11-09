import asyncio
import os
import sys
import threading
import types
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from mcp_tool_cache.functions import mcp_tool_cache as cache_module


class FakeClient:
    def __init__(self, name, specs):
        self.server_name = name
        self.instructions = f"{name} instructions"
        self.tool_definitions = {spec["name"]: {"meta": True} for spec in specs}
        self.calls = []
        self.disconnected = 0

    async def call_tool(self, function_name, function_args):
        self.calls.append((function_name, function_args))
        return [{"type": "text", "text": f"{function_name} ok"}]

    async def disconnect(self):
        self.disconnected += 1


class FakeWebUI:
    def __init__(self):
        self.connections = [self.connection("weather"), self.connection("news")]
        self.access = True
        self.error = None
        self.headers = {}
        self.connects = []
        self.clients = []
        self.specs = {"weather": [{"name": "search", "parameters": {"type": "object", "properties": {}}}]}

        async def get_config(key, default=None):
            assert key == "tool_server.connections"
            return self.connections

        async def has_connection_access(user, connection):
            return self.access

        async def build_tool_server_headers(connection, request, user, server_id="", metadata=None):
            return dict(self.headers), {}

        async def connect_mcp_server(request, server_id, user, extra_params):
            self.connects.append((user.id, server_id))
            if self.error:
                raise self.error
            if self.connections[0]["auth_type"] == "oauth_2.1" and not self.headers.get("Authorization"):
                raise PermissionError("Auth required")
            for connection in self.connections:
                if connection["info"]["id"] == server_id and connection["config"]["enable"] and self.access:
                    specs = self.specs.get(server_id, [{"name": "lookup", "parameters": {}}])
                    client = FakeClient(server_id, specs)
                    self.clients.append(client)
                    return client, [dict(spec) for spec in specs]
            return None

        self.original = connect_mcp_server
        self.tools = types.ModuleType("open_webui.utils.tools")
        self.tools.ENABLE_TOOL_SERVERS = True
        self.tools.ENABLE_FORWARD_USER_INFO_HEADERS = False
        self.tools.Config = SimpleNamespace(get=get_config)
        self.tools.has_connection_access = has_connection_access
        self.tools.build_tool_server_headers = build_tool_server_headers
        self.tools.connect_mcp_server = connect_mcp_server
        self.middleware = types.ModuleType("open_webui.utils.middleware")
        self.middleware.connect_mcp_server = connect_mcp_server
        self.request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace()))

    @staticmethod
    def connection(server_id):
        return {
            "type": "mcp",
            "url": f"https://{server_id}.example/mcp",
            "auth_type": "bearer",
            "key": "synthetic-secret",
            "info": {"id": server_id},
            "config": {"enable": True},
        }

    def per_user(self):
        for connection in self.connections:
            connection["auth_type"] = "session"

    def modules(self):
        return patch.dict(
            sys.modules,
            {
                "open_webui": types.ModuleType("open_webui"),
                "open_webui.utils": types.ModuleType("open_webui.utils"),
                "open_webui.utils.tools": self.tools,
                "open_webui.utils.middleware": self.middleware,
            },
        )


def user(user_id="u1"):
    return SimpleNamespace(id=user_id)


def run(coro):
    return asyncio.run(coro)


class TestMCPToolCache:
    @pytest.fixture(autouse=True)
    def open_webui(self):
        self.webui = FakeWebUI()
        with self.webui.modules():
            self.filter = cache_module.Filter()
            self.connect = self.webui.middleware.connect_mcp_server
            yield

    def extra(self, **valves):
        for name, value in valves.items():
            setattr(self.filter.valves, name, value)
        metadata = {"session_id": "s1"}
        run(self.filter.inlet({"messages": []}, __metadata__=metadata))
        return {"__metadata__": metadata, "__event_call__": None}

    def call(self, server_id="weather", account=None, extra=None):
        return run(self.connect(self.webui.request, server_id, account or user(), extra or self.extra()))

    def test_install_patches_middleware_once(self):
        assert self.connect is not self.webui.original
        assert cache_module.install()
        cache_module.Filter()
        assert self.webui.middleware.connect_mcp_server is self.connect
        assert self.webui.tools.connect_mcp_server is self.webui.original

    def test_reloaded_module_replaces_stale_wrapper(self):
        stale = cache_module._build_cached_connect(self.webui.original, self.webui.tools)
        stale.__mcp_tool_cache_original__ = self.webui.original
        with patch.object(cache_module, "_build_cached_connect", wraps=cache_module._build_cached_connect) as build:
            self.webui.middleware.connect_mcp_server = types.FunctionType(
                stale.__code__, {**cache_module.__dict__}, closure=stale.__closure__
            )
            setattr(self.webui.middleware.connect_mcp_server, cache_module.PATCH_MARKER, True)
            self.webui.middleware.connect_mcp_server.__mcp_tool_cache_original__ = self.webui.original
            assert cache_module.install()
            build.assert_called_once()
        assert self.webui.middleware.connect_mcp_server.__globals__ is cache_module.__dict__

    def test_install_refuses_unexpected_internals(self):
        webui = FakeWebUI()
        del webui.tools.has_connection_access
        with webui.modules():
            assert not cache_module.install()
            assert webui.middleware.connect_mcp_server is webui.original

    def test_install_refuses_foreign_patch(self):
        webui = FakeWebUI()

        async def foreign(*args):
            return None

        webui.middleware.connect_mcp_server = foreign
        with webui.modules():
            assert not cache_module.install()
            assert webui.middleware.connect_mcp_server is foreign

    def test_requests_without_filter_flag_pass_through(self):
        result = run(self.connect(self.webui.request, "weather", user(), {"__metadata__": {}}))
        run(self.connect(self.webui.request, "weather", user(), {"__metadata__": {}}))
        assert isinstance(result[0], FakeClient)
        assert len(self.webui.connects) == 2

    def test_zero_seconds_disables_cache(self):
        self.call(extra=self.extra(cache_seconds=0))
        self.call(extra=self.extra(cache_seconds=0))
        assert len(self.webui.connects) == 2

    def test_hit_skips_connection_and_preserves_tools(self):
        first_client, first_specs = self.call()
        client, specs = self.call()
        assert isinstance(first_client, FakeClient)
        assert isinstance(client, cache_module.CachedMCPClient)
        assert specs == first_specs
        assert client.instructions == "weather instructions"
        assert client.tool_definitions == {"search": {"meta": True}}
        assert self.webui.connects == [("u1", "weather")]

    def test_cache_returns_independent_copies(self):
        self.call()
        _, specs = self.call()
        specs[0]["parameters"]["properties"]["injected"] = {}
        _, fresh = self.call()
        assert fresh[0]["parameters"]["properties"] == {}

    def test_cache_is_isolated_per_user_and_server(self):
        self.webui.per_user()
        self.call(account=user("u1"))
        self.call(account=user("u2"))
        self.call(server_id="news")
        self.call(account=user("u2"))
        assert self.webui.connects == [("u1", "weather"), ("u2", "weather"), ("u1", "news")]

    @pytest.mark.parametrize(
        "change, shared",
        [
            ({}, True),
            ({"auth_type": "none"}, True),
            ({"auth_type": None}, False),
            ({"headers": {"X-Team": "static"}}, True),
            ({"headers": {"X-User": "{{USER_EMAIL}}"}}, False),
            ({"auth_type": "session"}, False),
            ({"auth_type": "system_oauth"}, False),
            ({"auth_type": "oauth_2.1"}, False),
            ({"auth_type": "oauth_2.1_static"}, False),
            ({"forwarding": True}, False),
            ({"forwarding": "missing"}, False),
        ],
    )
    def test_only_user_independent_servers_share_an_entry(self, change, shared):
        self.webui.headers = {"Authorization": "Bearer synthetic-token"}
        change = dict(change)
        forwarding = change.pop("forwarding", False)
        if forwarding == "missing":
            del self.webui.tools.ENABLE_FORWARD_USER_INFO_HEADERS
        else:
            self.webui.tools.ENABLE_FORWARD_USER_INFO_HEADERS = forwarding
        self.webui.connections[0].update(change)
        self.call(account=user("u1"))
        self.call(account=user("u2"))
        expected = [("u1", "weather")] if shared else [("u1", "weather"), ("u2", "weather")]
        assert self.webui.connects == expected

    def test_shared_entry_still_checks_each_users_access(self):
        self.call(account=user("u1"))
        self.webui.access = False
        self.call(account=user("u2"))
        assert self.webui.connects == [("u1", "weather"), ("u2", "weather")]

    def test_entry_expires(self):
        extra = self.extra(stale_seconds=0)
        with patch.object(cache_module.time, "time", return_value=1000):
            self.call(extra=extra)
        with patch.object(cache_module.time, "time", return_value=1000 + 14_399):
            self.call(extra=extra)
        with patch.object(cache_module.time, "time", return_value=1000 + 14_400):
            result = self.call(extra=extra)
        assert isinstance(result[0], FakeClient)
        assert len(self.webui.connects) == 2

    def test_stale_entry_is_served_while_refreshing_in_background(self):
        extra = self.extra()
        stale_at = 1000 + 14_400 + 5

        async def scenario():
            with patch.object(cache_module.time, "time", return_value=1000):
                await self.connect(self.webui.request, "weather", user(), extra)
            self.webui.specs["weather"] = [{"name": "forecast", "parameters": {}}]
            with patch.object(cache_module.time, "time", return_value=stale_at):
                stale_client, stale_specs = await self.connect(self.webui.request, "weather", user(), extra)
                await self.connect(self.webui.request, "weather", user(), extra)
                assert isinstance(stale_client, cache_module.CachedMCPClient)
                assert [spec["name"] for spec in stale_specs] == ["search"]
                await asyncio.gather(*cache_module._refreshes(self.webui.request).values())
                return await self.connect(self.webui.request, "weather", user(), extra)

        client, specs = run(scenario())
        assert isinstance(client, cache_module.CachedMCPClient)
        assert [spec["name"] for spec in specs] == ["forecast"]
        assert len(self.webui.connects) == 2
        assert self.webui.clients[-1].disconnected == 1
        assert cache_module._refreshes(self.webui.request) == {}

    def test_failed_refresh_keeps_stale_entry_and_retries(self):
        extra = self.extra()
        stale_at = 1000 + 14_400 + 5

        async def scenario():
            with patch.object(cache_module.time, "time", return_value=1000):
                await self.connect(self.webui.request, "weather", user(), extra)
            self.webui.error = RuntimeError("down")
            with patch.object(cache_module.time, "time", return_value=stale_at):
                client, _ = await self.connect(self.webui.request, "weather", user(), extra)
                await asyncio.gather(*cache_module._refreshes(self.webui.request).values())
                assert cache_module._refreshes(self.webui.request) == {}
                self.webui.error = None
                await self.connect(self.webui.request, "weather", user(), extra)
                await asyncio.gather(*cache_module._refreshes(self.webui.request).values())
            return client

        client = run(scenario())
        assert isinstance(client, cache_module.CachedMCPClient)
        assert len(self.webui.connects) == 3

    def test_entry_past_stale_window_blocks_on_live_connection(self):
        extra = self.extra(stale_seconds=100)
        with patch.object(cache_module.time, "time", return_value=1000):
            self.call(extra=extra)
        with patch.object(cache_module.time, "time", return_value=1000 + 14_400 + 100):
            result = self.call(extra=extra)
        assert isinstance(result[0], FakeClient)
        assert cache_module._refreshes(self.webui.request) == {}

    def test_connection_edit_invalidates_entry(self):
        self.call()
        self.webui.connections[0]["config"]["function_name_filter_list"] = "search"
        result = self.call()
        assert isinstance(result[0], FakeClient)
        assert len(self.webui.connects) == 2

    def test_cache_does_not_store_raw_connection(self):
        self.call()
        stored = repr(getattr(self.webui.request.app.state, cache_module.STATE_KEY))
        assert "synthetic-secret" not in stored

    def test_revoked_access_and_disabled_connection_are_not_served(self):
        self.call()
        self.webui.access = False
        assert self.call() is None
        self.webui.access = True
        self.webui.connections[0]["config"]["enable"] = False
        assert self.call() is None

    def test_tool_servers_disabled_delegates(self):
        self.call()
        self.webui.tools.ENABLE_TOOL_SERVERS = False
        self.webui.access = False
        assert self.call() is None

    def test_oauth_without_authorization_delegates_to_original(self):
        self.webui.connections[0]["auth_type"] = "oauth_2.1"
        self.webui.headers = {"Authorization": "Bearer synthetic"}
        self.call()
        self.call()
        self.webui.headers = {}
        with pytest.raises(PermissionError, match="Auth required"):
            self.call()
        assert len(self.webui.connects) == 2

    def test_cached_client_connects_once_on_concurrent_calls(self):
        self.call()
        client, _ = self.call()

        async def calls():
            return await asyncio.gather(
                client.call_tool("search", function_args={"q": "paris"}),
                client.call_tool("search", function_args={"q": "oslo"}),
            )

        results = run(calls())
        assert [r[0]["text"] for r in results] == ["search ok", "search ok"]
        assert len(self.webui.connects) == 2
        real = self.webui.clients[-1]
        assert sorted(real.calls, key=repr) == [("search", {"q": "oslo"}), ("search", {"q": "paris"})]
        run(client.disconnect())
        run(client.disconnect())
        assert real.disconnected == 1

    def test_unused_cached_client_disconnects_without_connecting(self):
        self.call()
        client, _ = self.call()
        run(client.disconnect())
        assert len(self.webui.connects) == 1

    def test_cached_client_reports_unavailable_server(self):
        self.call()
        client, _ = self.call()
        self.webui.connections[0]["config"]["enable"] = False
        with pytest.raises(RuntimeError, match="no longer available"):
            run(client.call_tool("search", function_args={}))

    def test_entries_are_bounded(self):
        extra = self.extra(maximum_entries=1)
        self.call(extra=extra)
        self.call(server_id="news", extra=extra)
        self.call(extra=extra)
        assert len(self.webui.connects) == 3


class TestPersistence:
    @pytest.fixture(autouse=True)
    def open_webui(self, tmp_path):
        cache_module._pruned_directories.clear()
        self.webui = FakeWebUI()
        self.webui.connections[0]["headers"] = {"X-Api-Key": "synthetic-header-secret"}
        self.config = types.ModuleType("open_webui.config")
        self.config.CACHE_DIR = tmp_path / "cache"
        self.env = types.ModuleType("open_webui.env")
        self.env.WEBUI_SECRET_KEY = "synthetic-webui-secret"
        self.directory = self.config.CACHE_DIR / "functions" / "mcp_tool_cache" / cache_module.STORAGE_FOLDER
        with self.webui.modules(), patch.dict(
            sys.modules, {"open_webui.config": self.config, "open_webui.env": self.env}
        ):
            self.filter = cache_module.Filter()
            self.connect = self.webui.middleware.connect_mcp_server
            yield

    def extra(self, filter_id="mcp_tool_cache", **valves):
        for name, value in valves.items():
            setattr(self.filter.valves, name, value)
        metadata = {"session_id": "s1"}
        run(self.filter.inlet({"messages": []}, __metadata__=metadata, __id__=filter_id))
        return {"__metadata__": metadata, "__event_call__": None}

    def call(self, server_id="weather", account=None, extra=None):
        return run(self.connect(self.webui.request, server_id, account or user(), extra or self.extra()))

    def restart(self):
        self.webui.request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace()))

    def files(self):
        return sorted(self.directory.glob("*.json")) if self.directory.exists() else []

    def test_files_hold_no_secrets_or_ids_and_are_private(self):
        self.webui.specs["weather"][0]["description"] = "Looks up weather."
        self.call(account=user("user-123"))
        [path] = self.files()
        text = path.read_text()
        for secret in ("synthetic-secret", "synthetic-header-secret", "synthetic-webui-secret", "weather.example"):
            assert secret not in text
        for identifier in ("user-123", "weather"):
            assert identifier not in path.name
        assert '"Looks up weather."' in text
        assert path.stat().st_mode & 0o777 == 0o600
        assert self.directory.stat().st_mode & 0o777 == 0o700
        assert list(self.directory.glob("*.tmp")) == []

    def test_restart_serves_from_disk_without_connecting(self):
        _, first_specs = self.call()
        self.restart()
        client, specs = self.call()
        assert isinstance(client, cache_module.CachedMCPClient)
        assert specs == first_specs
        assert client.instructions == "weather instructions"
        assert len(self.webui.connects) == 1

    def test_restart_keeps_users_and_connection_edits_apart(self):
        self.webui.per_user()
        self.call(account=user("u1"))
        self.restart()
        self.call(account=user("u2"))
        self.webui.connections[0]["key"] = "synthetic-rotated"
        self.call(account=user("u1"))
        assert self.webui.connects == [("u1", "weather"), ("u2", "weather"), ("u1", "weather")]

    def test_shared_server_keeps_one_file_for_all_users(self):
        self.call(account=user("u1"))
        self.restart()
        self.call(account=user("u2"))
        assert len(self.files()) == 1
        assert self.webui.connects == [("u1", "weather")]

    def test_rotated_secret_key_replaces_file(self):
        self.call()
        self.env.WEBUI_SECRET_KEY = "synthetic-other-secret"
        self.restart()
        assert isinstance(self.call()[0], FakeClient)
        self.restart()
        assert isinstance(self.call()[0], cache_module.CachedMCPClient)
        assert len(self.webui.connects) == 2
        assert len(self.files()) == 1

    def test_connection_edit_overwrites_the_same_file(self):
        self.call()
        [path] = self.files()
        self.webui.connections[0]["key"] = "synthetic-rotated"
        self.restart()
        assert isinstance(self.call()[0], FakeClient)
        assert self.files() == [path]
        self.restart()
        assert isinstance(self.call()[0], cache_module.CachedMCPClient)
        assert len(self.webui.connects) == 2

    def test_first_request_in_each_worker_prunes_old_files(self):
        self.call()
        [path] = self.files()
        os.utime(path, (0, 0))
        cache_module._pruned_directories.clear()
        self.extra()
        assert not path.exists()

    @pytest.mark.parametrize("content", ["not json", "[]", '{"specs": []}'])
    def test_corrupt_file_is_discarded(self, content):
        self.call()
        [path] = self.files()
        path.write_text(content)
        self.restart()
        assert isinstance(self.call()[0], FakeClient)

    def test_symlinked_file_is_not_followed(self, tmp_path):
        self.call()
        [path] = self.files()
        target = tmp_path / "outside.json"
        target.write_text(path.read_text())
        path.unlink()
        path.symlink_to(target)
        self.restart()
        assert isinstance(self.call()[0], FakeClient)
        assert target.exists()
        assert not self.files()[0].is_symlink()

    def test_no_secret_key_keeps_cache_in_memory(self):
        self.env.WEBUI_SECRET_KEY = ""
        self.call()
        assert self.files() == []

    @pytest.mark.parametrize("filter_id", ["../escape", "a/b", "a.b", "", None])
    def test_unsafe_filter_id_keeps_cache_in_memory(self, filter_id):
        self.call(extra=self.extra(filter_id=filter_id))
        assert not self.config.CACHE_DIR.exists()

    def test_non_ascii_filter_id_persists(self):
        self.call(extra=self.extra(filter_id="café_cache"))
        assert list((self.config.CACHE_DIR / "functions" / "café_cache" / cache_module.STORAGE_FOLDER).glob("*.json"))

    def test_files_are_bounded_and_expired_files_pruned(self):
        self.webui.per_user()
        extra = self.extra(maximum_entries=1)
        self.call(extra=extra)
        self.call(server_id="news", extra=extra)
        assert len(self.files()) == 1
        extra = self.extra(maximum_entries=10, cache_seconds=10, stale_seconds=0)
        self.call(account=user("u2"), extra=extra)
        for path in self.files():
            os.utime(path, (0, cache_module.time.time() - 60))
        (self.directory / ".abandoned.tmp").write_text("")
        os.utime(self.directory / ".abandoned.tmp", (0, 0))
        self.call(account=user("u3"), extra=extra)
        assert len(self.files()) == 1
        assert list(self.directory.glob("*.tmp")) == []

    def test_stale_memory_uses_fresher_list_from_another_worker(self):
        extra = self.extra()
        with patch.object(cache_module.time, "time", return_value=1000):
            self.call(extra=extra)
        other_worker = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace()))
        self.webui.specs["weather"] = [{"name": "forecast", "parameters": {}}]
        with patch.object(cache_module.time, "time", return_value=20_000):
            run(self.connect(other_worker, "weather", user(), self.extra(stale_seconds=0)))
        with patch.object(cache_module.time, "time", return_value=20_001):
            client, specs = self.call(extra=extra)
        assert isinstance(client, cache_module.CachedMCPClient)
        assert [spec["name"] for spec in specs] == ["forecast"]
        assert cache_module._refreshes(self.webui.request) == {}
        assert len(self.webui.connects) == 2

    def test_write_failure_keeps_chat_working(self):
        self.config.CACHE_DIR.mkdir(parents=True)
        (self.config.CACHE_DIR / "functions").write_text("not a folder")
        assert isinstance(self.call()[0], FakeClient)
        assert isinstance(self.call()[0], cache_module.CachedMCPClient)

    def test_key_evicted_during_disk_read_is_still_served(self):
        extra = self.extra()
        with patch.object(cache_module.time, "time", return_value=1000):
            self.call(extra=extra)
        read = cache_module._read_entry

        def evicting_read(*args):
            cache_module._cache(self.webui.request).clear()
            return read(*args)

        with patch.object(cache_module.time, "time", return_value=20_000), patch.object(
            cache_module, "_read_entry", evicting_read
        ):
            client, _ = self.call(extra=extra)
        assert isinstance(client, cache_module.CachedMCPClient)

    def test_connection_is_returned_without_waiting_for_disk_write(self):
        started, release = threading.Event(), threading.Event()
        write = cache_module._write_entry

        def slow_write(*args):
            started.set()
            release.wait(5)
            return write(*args)

        extra = self.extra()

        async def scenario():
            with patch.object(cache_module, "_write_entry", slow_write):
                result = await self.connect(self.webui.request, "weather", user(), extra)
                assert self.files() == []
                release.set()
                await asyncio.gather(*cache_module._pending_writes)
            return result

        assert isinstance(run(scenario())[0], FakeClient)
        assert len(self.files()) == 1

    def test_older_write_does_not_replace_newer_file(self):
        self.call()
        key = next(iter(cache_module._cache(self.webui.request)))
        newer = cache_module._cache(self.webui.request)[key]
        older = {**newer, "stored_at": newer["stored_at"] - 1, "specs": [{"name": "removed"}]}
        settings = self.extra()["__metadata__"][cache_module.REQUEST_KEY]
        cache_module._write_entry(str(self.directory), key, older, settings)
        assert cache_module._read_entry(str(self.directory), key) == newer

    def test_disk_read_does_not_replace_newer_memory_entry(self):
        extra = self.extra()
        with patch.object(cache_module.time, "time", return_value=1000):
            self.call(extra=extra)
        read = cache_module._read_entry
        cache = cache_module._cache(self.webui.request)
        [key] = cache
        newer = {**cache[key], "stored_at": 19_000, "expires_at": 40_000, "specs": [{"name": "forecast"}]}

        def racing_read(*args):
            stored = read(*args)
            cache[key] = newer
            return stored

        with patch.object(cache_module.time, "time", return_value=20_000), patch.object(
            cache_module, "_read_entry", racing_read
        ):
            _, specs = self.call(extra=extra)
        assert [spec["name"] for spec in specs] == ["forecast"]
        assert cache[key] is newer
