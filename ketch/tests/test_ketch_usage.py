import sys
import types
from types import SimpleNamespace

if "fastapi" not in sys.modules:
    fastapi = types.ModuleType("fastapi")
    fastapi.Request = object
    sys.modules["fastapi"] = fastapi

from ketch.tools import ketch

URL = "https://example.com/app"
USER = {"id": "user-1"}
METADATA = {"chat_id": "chat-1", "message_id": "message-1"}


class TestForceBrowserRepeat:
    def setup_method(self):
        self.tool = ketch.Tools()
        self.request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace()))

    async def reserve(self, force_browser):
        return await self.tool._reserve_scrape_usage(
            self.request, USER, METADATA, [URL], force_browser, 8_000
        )

    async def scrape_once(self, retrieval_mode):
        usage_key, error, eligible, _ = await self.reserve(False)
        assert error is None
        assert eligible == [URL]
        await self.tool._record_scrape_usage(
            self.request,
            usage_key,
            [
                {
                    "requested_url": URL,
                    "resolved_url": URL,
                    "retrieval_mode": retrieval_mode,
                    "_ketch_request_index": 0,
                }
            ],
            [],
            [
                {
                    "url": URL,
                    "force_browser": False,
                    "max_chars": 8_000,
                    "start_char": 0,
                    "document_mode": False,
                }
            ],
        )

    async def test_force_browser_refused_after_automatic_browser_render(self):
        await self.scrape_once("protected_browser")
        _, error, eligible, _ = await self.reserve(True)
        assert "already retrieved" in error
        assert eligible == []

    async def test_force_browser_allowed_after_static_result(self):
        await self.scrape_once("protected_http")
        _, error, eligible, _ = await self.reserve(True)
        assert error is None
        assert eligible == [URL]
