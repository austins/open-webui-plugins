import asyncio
import sys
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
        self.call(account=user("u1"))
        self.call(account=user("u2"))
        self.call(server_id="news")
        self.call(account=user("u2"))
        assert self.webui.connects == [("u1", "weather"), ("u2", "weather"), ("u1", "news")]

    def test_entry_expires(self):
        with patch.object(cache_module.time, "time", return_value=1000):
            self.call()
        with patch.object(cache_module.time, "time", return_value=1000 + 14_399):
            self.call()
        with patch.object(cache_module.time, "time", return_value=1000 + 14_400):
            result = self.call()
        assert isinstance(result[0], FakeClient)
        assert len(self.webui.connects) == 2

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
