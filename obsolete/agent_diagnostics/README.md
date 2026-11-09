# Agent Diagnostics

No longer needed: it reports on Agent Efficiency and Tool Broker, which Open WebUI's built-in compaction and lazy tool loading replace.

An action (`functions/agent_diagnostics.py`) that adds message buttons reporting context use, efficiency, tool schemas, `view_skill` use, Tool Broker routing, and MCP latency. Requires Open WebUI 0.11.3 or later.

Most reports need Agent Efficiency installed. The Tool Broker routing report needs Tool Broker, and the MCP latency report works on its own.
