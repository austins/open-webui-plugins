# MCP Tool Cache

A filter (`functions/mcp_tool_cache.py`) that caches each user's MCP tool lists, so chats start without connecting to every MCP server. A server is connected only when the model calls one of its tools. Once a list is older than `cache_seconds`, it is still served instantly for up to `stale_seconds` more while a background refresh updates it, so no chat waits on reconnecting. Editing a server's connection in Open WebUI, or calling one of its tools, also refreshes the list. Works with Open WebUI's native Tool Search. Requires Open WebUI 0.13.0 or later.

Install it on its own. It only affects models it runs on, so make it global.
