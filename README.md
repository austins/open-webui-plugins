# Open WebUI plugins

Functions and tools for [Open WebUI](https://github.com/open-webui/open-webui). Each Python file is installed separately: functions under Admin Settings > Functions, tools under Workspace > Tools. Each project's README says what its plugins do, which ones go together, and the Open WebUI version they need.

* [Context Extraction](context_extraction/) keeps long chats inside the model's context window, with Chat Recall to read back what it removed.
* [MCP Tool Cache](mcp_tool_cache/) caches MCP tool lists so chats start without connecting to every MCP server.
* [Ketch](ketch/) searches the web and extracts page content.

The projects in [obsolete](obsolete/) are no longer needed and aren't maintained: [Tool Broker](obsolete/tool_broker/), [Agent Efficiency](obsolete/agent_efficiency/), and [Agent Diagnostics](obsolete/agent_diagnostics/).

## Tests

Run from the repository root with [uv](https://docs.astral.sh/uv/):

```sh
uv run --no-project --with pydantic --with pytest --with pytest-asyncio pytest
```

Add a project folder or test file to run only that.
