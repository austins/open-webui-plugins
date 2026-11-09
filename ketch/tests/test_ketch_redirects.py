import sys
import types
from contextlib import contextmanager
from unittest.mock import AsyncMock, Mock, patch

import pytest

if "fastapi" not in sys.modules:
    fastapi = types.ModuleType("fastapi")
    fastapi.Request = object
    sys.modules["fastapi"] = fastapi

from ketch.tools import ketch


class FakeResponse:
    def __init__(self, status_code, headers=None, body=b""):
        self.status_code = status_code
        self.headers = headers or {}
        self.body = body

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def iter_content(self, chunk_size):
        yield self.body


class FakeSession:
    def __init__(self, responses):
        self.responses = responses
        self.requested = []
        self.accepts = []

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def get(self, url, **kwargs):
        assert kwargs["allow_redirects"] is False
        self.requested.append(url)
        self.accepts.append(kwargs["headers"]["Accept"])
        response = self.responses[url]
        return response.pop(0) if isinstance(response, list) else response


def redirect(location, status=302):
    return FakeResponse(status, {"Location": location})


def page(body=b"<html>ok</html>"):
    return FakeResponse(200, {"Content-Type": "text/html"}, body)


@contextmanager
def open_webui_modules(*, allow_redirects=False, validate_url=None, session=None):
    env = types.ModuleType("open_webui.env")
    env.AIOHTTP_CLIENT_ALLOW_REDIRECTS = allow_redirects
    env.AIOHTTP_CLIENT_SESSION_SSL = True
    env.AIOHTTP_CLIENT_SSL_CERT_FILE = None
    env.USER_AGENT = "test-agent"
    utils = types.ModuleType("open_webui.retrieval.web.utils")
    utils.validate_url = validate_url or Mock(return_value=True)
    utils.get_ssrf_safe_requests_session = Mock(return_value=session)
    utils.get_ssrf_safe_session = Mock(return_value=session)
    utils._forwardable_request_headers = lambda headers: headers
    utils._fulfillable_response_headers = lambda pairs: dict(pairs)
    modules = {
        "open_webui": types.ModuleType("open_webui"),
        "open_webui.env": env,
        "open_webui.retrieval": types.ModuleType("open_webui.retrieval"),
        "open_webui.retrieval.web": types.ModuleType("open_webui.retrieval.web"),
        "open_webui.retrieval.web.utils": utils,
    }
    with patch.dict(sys.modules, modules):
        yield utils


class TestSafeFetchRedirect:
    def setup_method(self):
        self.tool = ketch.Tools()

    async def fetch(self, url, responses, **modules):
        session = FakeSession(responses)
        with open_webui_modules(session=session, **modules) as utils:
            result = await self.tool._safe_fetch(url)
        return result, session, utils

    async def test_follows_relative_same_host_redirect_with_global_redirects_off(self):
        result, session, utils = await self.fetch(
            "https://example.com/a",
            {
                "https://example.com/a": redirect("/b?x=1"),
                "https://example.com/b?x=1": page(),
            },
        )
        assert result["url"] == "https://example.com/b?x=1"
        assert result["body"] == b"<html>ok</html>"
        assert session.requested == ["https://example.com/a", "https://example.com/b?x=1"]
        validated = [call.args[0] for call in utils.validate_url.call_args_list]
        assert "https://example.com/b?x=1" in validated

    async def test_same_host_redirect_may_change_scheme_and_port(self):
        result, _, _ = await self.fetch(
            "http://Example.com/a",
            {
                "http://Example.com/a": redirect("https://example.com:8443/b", 301),
                "https://example.com:8443/b": page(),
            },
        )
        assert result["url"] == "https://example.com:8443/b"

    async def test_cross_host_redirect_returns_validated_destination(self, subtests):
        for target in ("https://www.example.com/a", "https://example.net/a"):
            with subtests.test(target=target):
                with pytest.raises(ketch.KetchError) as caught:
                    await self.fetch(
                        "https://example.com/a",
                        {"https://example.com/a": redirect(target)},
                    )
                message = str(caught.value)
                assert "different host" in message
                assert "disabled by Open WebUI" not in message
                match = ketch.REDIRECT_DESTINATION_RE.search(message)
                assert match.group(1) == target

    async def test_valve_off_refuses_same_host_redirect(self):
        self.tool.valves.follow_same_host_redirects = False
        with pytest.raises(ketch.KetchError) as caught:
            await self.fetch(
                "https://example.com/a",
                {"https://example.com/a": redirect("/b")},
            )
        message = str(caught.value)
        assert "Redirects are disabled by Open WebUI configuration" in message
        assert ketch.REDIRECT_DESTINATION_RE.search(message).group(1) == "https://example.com/b"

    async def test_global_setting_still_follows_cross_host_redirects(self):
        result, _, _ = await self.fetch(
            "https://example.com/a",
            {
                "https://example.com/a": redirect("https://example.net/b"),
                "https://example.net/b": page(),
            },
            allow_redirects=True,
        )
        assert result["url"] == "https://example.net/b"

    async def test_same_host_redirect_to_blocked_address_is_not_requested(self):
        session = FakeSession(
            {"https://example.com/a": redirect("http://example.com:8080/admin")}
        )

        def validate_url(url):
            if ":8080" in url:
                raise ValueError("blocked")
            return True

        with open_webui_modules(session=session, validate_url=validate_url):
            with pytest.raises(ketch.KetchError):
                await self.tool._safe_fetch("https://example.com/a")
        assert session.requested == ["https://example.com/a"]

    async def test_hop_limit_is_five(self):
        def chain(hops):
            responses = {
                f"https://example.com/{index}": redirect(f"/{index + 1}")
                for index in range(hops)
            }
            responses[f"https://example.com/{hops}"] = page()
            return responses

        result, _, _ = await self.fetch("https://example.com/0", chain(5))
        assert result["url"] == "https://example.com/5"
        with pytest.raises(ketch.KetchError, match="Too many redirects"):
            await self.fetch("https://example.com/0", chain(6))

    async def test_missing_location_is_an_error(self):
        with pytest.raises(ketch.KetchError, match="without a location"):
            await self.fetch(
                "https://example.com/a",
                {"https://example.com/a": FakeResponse(302)},
            )

    async def test_success_does_not_retry(self):
        _, session, _ = await self.fetch(
            "https://example.com/a", {"https://example.com/a": page()}
        )
        assert len(session.requested) == 1
        assert session.accepts[0].startswith("text/html")

    async def test_refused_redirect_retries_once_with_wildcard_accept(self):
        login = "https://login.example.org/auth?prompt=none"
        result, session, _ = await self.fetch(
            "https://example.com/a",
            {"https://example.com/a": [redirect(login), page()]},
        )
        assert result["url"] == "https://example.com/a"
        assert result["body"] == b"<html>ok</html>"
        assert session.requested == ["https://example.com/a"] * 2
        assert login not in session.requested
        assert session.accepts[1] == "*/*"

    async def test_failed_retry_reports_original_redirect(self):
        with pytest.raises(ketch.KetchError) as caught:
            await self.fetch(
                "https://example.com/a",
                {
                    "https://example.com/a": [
                        redirect("https://login.example.org/first"),
                        redirect("https://login.example.org/second"),
                    ]
                },
            )
        assert ketch.REDIRECT_DESTINATION_RE.search(str(caught.value)).group(1) == "https://login.example.org/first"

    async def test_redirect_loop_retries_with_wildcard_accept(self):
        loop = [redirect("/a")] * (ketch.MAX_REDIRECTS + 1) + [page()]
        result, session, _ = await self.fetch(
            "https://example.com/a",
            {"https://example.com/a": loop},
            allow_redirects=True,
        )
        assert result["body"] == b"<html>ok</html>"
        assert session.accepts[-1] == "*/*"

    async def test_http_error_does_not_retry(self):
        with pytest.raises(ketch.KetchError, match="HTTP 404"):
            await self.fetch(
                "https://example.com/a",
                {"https://example.com/a": [FakeResponse(404), page()]},
            )


class FakeAiohttpResponse:
    def __init__(self, status, headers):
        self.status = status
        self.headers = headers
        self.content = object()

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


class FakeAiohttpSession:
    def __init__(self, responses):
        self.responses = responses

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    def request(self, method, url, **kwargs):
        assert kwargs["allow_redirects"] is False
        return self.responses[url]


class TestBrowserRedirect:
    async def render(self, location):
        tool = ketch.Tools()
        start = "https://example.com/a"
        session = FakeAiohttpSession(
            {start: FakeAiohttpResponse(302, {"Location": location})}
        )
        handler = {}
        main_frame = object()
        page = Mock()
        page.main_frame = main_frame
        request = Mock()
        request.url = start
        request.post_data_buffer = None
        request.all_headers = AsyncMock(return_value={})
        request.is_navigation_request = Mock(return_value=True)
        request.frame = main_frame
        route = Mock()
        route.request = request
        route.abort = AsyncMock()
        route.fulfill = AsyncMock()

        async def goto(url, **kwargs):
            await handler["route"](route)
            if route.abort.await_count:
                raise RuntimeError("net::ERR_FAILED")
            raise RuntimeError("stop after first hop")

        page.goto = goto
        context = Mock()
        context.route = AsyncMock(
            side_effect=lambda pattern, callback: handler.setdefault("route", callback)
        )
        context.route_web_socket = AsyncMock()
        context.new_page = AsyncMock(return_value=page)
        context.close = AsyncMock()
        browser = Mock()
        browser.new_context = AsyncMock(return_value=context)

        playwright = types.ModuleType("playwright")
        async_api = types.ModuleType("playwright.async_api")
        async_api.TimeoutError = TimeoutError
        aiohttp = types.ModuleType("aiohttp")
        aiohttp.ClientTimeout = Mock()

        async def read_stream(self, content, limit, label):
            return b""

        with open_webui_modules(session=session), patch.dict(
            sys.modules,
            {
                "playwright": playwright,
                "playwright.async_api": async_api,
                "aiohttp": aiohttp,
            },
        ), patch.object(ketch._SharedByteBudget, "read_stream", read_stream):
            with pytest.raises(ketch.KetchError) as caught:
                await tool._safe_render_html(start, browser)
        return route, str(caught.value)

    async def test_same_host_navigation_redirect_is_passed_to_browser(self):
        route, _ = await self.render("/b")
        route.fulfill.assert_awaited_once()
        assert route.fulfill.await_args.kwargs["status"] == 302
        route.abort.assert_not_awaited()

    async def test_cross_host_navigation_redirect_returns_destination(self):
        route, message = await self.render("https://www.example.com/b")
        route.abort.assert_awaited_once()
        route.fulfill.assert_not_awaited()
        assert "different host" in message
        assert ketch.REDIRECT_DESTINATION_RE.search(message).group(1) == "https://www.example.com/b"


class TestMayFollowRedirect:
    def test_host_comparison_is_exact_and_case_insensitive(self):
        follow = ketch._may_follow_redirect
        options = {"allow_all": False, "follow_same_host": True}
        assert follow("https://EXAMPLE.com/", "http://example.com:81/", **options)
        assert not follow("https://example.com/", "https://www.example.com/", **options)
        assert not follow("https://example.com/", "https://example.org/", **options)
