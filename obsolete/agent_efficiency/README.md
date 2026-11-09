# Agent Efficiency

No longer needed: Open WebUI's built-in compaction and Context Extraction replace it.

A filter (`functions/agent_efficiency.py`) that records request size metrics and compacts older tool results. Requires Open WebUI 0.11.3 or later.

Install it on its own, but not alongside Context Extraction, since both compact tool results.
