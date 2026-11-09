# Context Extraction

Keeps long chats inside the model's context window without calling a model to summarize them. Requires Open WebUI 0.13.0 or later.

* Context Extraction (`functions/context_extraction.py`) is a filter. Near the context limit, it replaces large older tool results with stubs, then older turns with notes. Each stub and note names the ref of its tool call.
* Chat Recall (`tools/chat_recall.py`) is a tool. Its `recall_search` and `recall_read` let the model search the current chat and read a tool call in full by its ref.

Install both on the same models; without Chat Recall the model can't get back what was removed. Turn off Open WebUI's built-in Context Compaction in Admin Settings, since the two conflict. Also turn off the built-in Chat History tool in each model's settings, so the model uses Chat Recall instead.
