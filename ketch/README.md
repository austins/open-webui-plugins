# Ketch

A tool (`tools/ketch.py`) for web search and page extraction using the [Ketch](https://github.com/1broseidon/ketch) command-line tool. It gives the model `ketch_search`, `ketch_scrape`, and `ketch_scrape_continue`. Requires Open WebUI 0.11.3 or later on Linux.

Install it on its own. On first use it downloads Ketch into `cache/tools/<tool id>/` in Open WebUI's data directory, the folder Open WebUI creates for the tool, and, unless the browser is turned off, Chromium for pages that need JavaScript. Open WebUI installs its `playwright` requirement.
