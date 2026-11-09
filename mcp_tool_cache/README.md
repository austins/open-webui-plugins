# MCP Tool Cache

A filter (`functions/mcp_tool_cache.py`) that caches each user's MCP tool lists, so chats start without connecting to every MCP server. A server is connected only when the model calls one of its tools. Works with Open WebUI's native Tool Search. Requires Open WebUI 0.13.0 or later.

Install it on its own. It only affects models it runs on, so make it global.
