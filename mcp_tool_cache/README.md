# MCP Tool Cache

A filter (`functions/mcp_tool_cache.py`) that caches MCP tool lists, so chats start without connecting to every MCP server. A server is connected only when the model calls one of its tools. Works with Open WebUI's native Tool Search. Requires Open WebUI 0.13.0 or later.

A server's list is shared by all users when Open WebUI sends every user the same request to it: `bearer` or no auth, no `{{...}}` templates in its custom headers, and `ENABLE_FORWARD_USER_INFO_HEADERS` off. Otherwise each user gets their own list. Access to the server is still checked for each user on every request.

A list older than `cache_seconds` is still served for up to `stale_seconds` more while a background task refreshes it. Calling one of a server's tools also refreshes its list, and editing the server's connection in Open WebUI discards it.

Lists are also saved to `cache/functions/<filter id>/tool_lists/` in Open WebUI's data directory, so they survive restarts and are shared between workers. Each list has one file, readable only by Open WebUI's user, holding the tool specs, the server's instructions, expiry times, and a fingerprint of the connection settings keyed with `WEBUI_SECRET_KEY`. Connection keys, headers, and URLs are never written. Editing a connection overwrites its file. Files older than `cache_seconds` plus `stale_seconds`, or beyond `maximum_entries`, are deleted after writes and on each worker's first request. Without `WEBUI_SECRET_KEY` the cache stays in memory.

The filter replaces `connect_mcp_server` in `open_webui.utils.middleware`, which the chat middleware calls for each enabled MCP server, with a caching wrapper. The original in `open_webui.utils.tools` is unchanged, and the wrapper calls it whenever it needs a live connection. If Open WebUI's internals are not what the filter expects, or another plugin has already replaced the function, it logs a warning and patches nothing. With the filter disabled the wrapper passes requests straight through; restarting Open WebUI removes it.

Install it on its own. It only affects models it runs on, so make it global.
