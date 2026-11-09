# Tool Broker

No longer needed: Open WebUI loads MCP tools lazily through native Tool Search.

Gives the model two tools, `search_tools` and `execute_tool`, for finding and running MCP tools instead of loading every tool schema. Requires Open WebUI 0.11.3 or later.

* Tool Broker (`tools/tool_broker.py`) is the tool.
* Tool Broker Policy (`functions/tool_broker_policy.py`) is a filter that tells the model how to use it.

Install both on the same models.
