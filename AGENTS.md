# Repository instructions

Each Python file here is an Open WebUI function or tool, installed separately in Open WebUI. Local edits do not update the running installation.

## Projects

* A project folder holds the plugins installed together, in `functions/`, `tools/`, and `tests/`. A plugin that fits no project gets its own folder. `obsolete/` holds unmaintained projects; change them only when asked.
* Each project's `README.md` is the source of truth for its plugins, which must be installed together, and the Open WebUI version it requires. Read it before changing the project, and update it in the same change whenever the code stops matching it. READMEs do not list plugin versions.
* The root `README.md` lists every project in one line each. Update it when a project is added, removed, renamed, or moved to `obsolete/`, or when its one-line summary stops being true.
* When changing a plugin, check the plugins its README couples it to.

## Making changes

* Preserve Open WebUI entry-point signatures, injected arguments such as `__request__`, and tool schemas unless the task requires a compatibility change.
* Preserve authorization checks, per-user state isolation, call budgets, and explicit error reporting.
* Keep each file's metadata `description` and docstrings matching its behavior and its README; Open WebUI shows the metadata.
* This repository is public. Never copy text from the user's real chats, chat exports, or request captures into code, tests, comments, or docs. Use invented examples, reserved domains (`example.com`, `example.org`, `example.net`), and no local paths, hostnames, credentials, or names of other agents and private projects.

## Versions

* Bump the metadata `version` of every plugin whose runtime behavior changes, once per task: patch for fixes, minor for compatible features, major for incompatible interfaces or behavior. Tests and docs alone need no bump.
* Raise `required_open_webui_version` only when the code needs an API missing from the stated minimum, and update the README with it.

## Tests

Tests use pytest with plain `assert`s; `pytest.ini` holds the settings. They import plugins by project path, such as `from context_extraction.functions import context_extraction`. `async def` tests run through pytest-asyncio without a marker.

```sh
PYTHONDONTWRITEBYTECODE=1 uv run --no-project --with pydantic --with pytest --with pytest-asyncio pytest
```

This is the root README's command with bytecode writing off. Append a project folder or test file to run only that.

* Add or update tests for bug fixes and changes to branching, parsing, authorization, state handling, or returned data. For a regression, first confirm the test fails without the fix.
* Import the real implementation and mock Open WebUI, MCP, network, and process boundaries. Tests never need a running Open WebUI, credentials, or external services.
* Add a dependency to the command only after an import failure names it.
* Before finishing a code change, run the full suite and `git diff --check`. Documentation-only changes need neither.

## Prompt-prefix stability

* Whitespace, system-message order, and tool-schema order are cache-sensitive.
* Open WebUI merges system messages with `"\n".join(...)` and reruns request filters during tool loops, so removing and reinserting an unchanged injected block must reproduce the merged prompt exactly. Remove only separators the injected block added.
* For message-transforming changes, test repeated application after system-message merging, unchanged input objects, and untouched unrelated messages and tool definitions.
* Call out intentional prefix changes. Passing unit tests do not prove a server-side cache hit.

## Completion

Report which plugin versions and READMEs changed, the checks run, and whether deployment is still required. Keep local validation separate from claims about live Open WebUI behavior.
