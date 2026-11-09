"""
title: Tool Broker Policy
description: Injects MCP Tool Broker discovery, authorization, evidence, and execution policy while preserving unchanged prompt prefixes.
version: 2.5.3
required_open_webui_version: 0.11.3
"""

import json
import logging
import re
from typing import Any, Optional

from pydantic import BaseModel, Field

log = logging.getLogger(__name__)
AUTOMATIC_DISCOVERY_MARKER = "<tool_broker_automatic_discovery>"
AUTOMATIC_DISCOVERY_END = "</tool_broker_automatic_discovery>"
REQUIRED_EXECUTION_MARKER = "<tool_broker_required_execution>"
REQUIRED_EXECUTION_END = "</tool_broker_required_execution>"
REQUIRED_EXECUTION_BLOCK_RE = re.compile(
    rf"\n?{re.escape(REQUIRED_EXECUTION_MARKER)}.*?{re.escape(REQUIRED_EXECUTION_END)}",
    re.DOTALL,
)
AUTOMATIC_DISCOVERY_BLOCK_RE = re.compile(
    rf"\n?{re.escape(AUTOMATIC_DISCOVERY_MARKER)}.*?{re.escape(AUTOMATIC_DISCOVERY_END)}",
    re.DOTALL,
)
AUTOMATIC_HINT_MARKER = "<tool_broker_automatic_hint>"
AUTOMATIC_HINT_END = "</tool_broker_automatic_hint>"
AUTOMATIC_HINT_BLOCK_RE = re.compile(
    rf"\n?{re.escape(AUTOMATIC_HINT_MARKER)}.*?{re.escape(AUTOMATIC_HINT_END)}",
    re.DOTALL,
)
ACTIVE_MCP_MARKER = "<tool_broker_active_mcp>"
ACTIVE_MCP_END = "</tool_broker_active_mcp>"
ACTIVE_MCP_BLOCK_RE = re.compile(
    rf"\n?{re.escape(ACTIVE_MCP_MARKER)}.*?{re.escape(ACTIVE_MCP_END)}",
    re.DOTALL,
)
AUTOMATIC_DISCOVERY_GUIDANCE = (
    "Authenticated Tool Broker discovery is complete for this user message. Do not "
    "repeat covered searches. When a returned MCP tool directly covers the request, "
    "prefer `execute_tool` over generic search, browsing, or scraping. Never guess an "
    "identifier: execute only one returned here or by a later successful search; no "
    "MCP operation was executed."
)
ACTIVE_MCP_GUIDANCE = (
    "Authenticated active MCP tools were revalidated for this user message. "
    "Their complete current schemas are provided below and their exact identifiers "
    "may be passed directly to execute_tool without another search. Search "
    "only for a distinct requirement the active set does not cover. Visibility is "
    "not permission to invent aliases, arguments, or other tool identifiers."
)
AUTOMATIC_CATALOG_REQUEST_RE = re.compile(
    r"^\s*(?:please\s+)?(?:list|show|describe|enumerate|what|which)\s+"
    r"(?:(?!(?:with|using|from|into|for)\b)\w+[\s-]+){0,5}"
    r"(?:tools?|tools|catalog|servers?)\b",
    re.IGNORECASE,
)
AUTOMATIC_NEGATION_RE = re.compile(
    r"\b(?:do\s+not|don['\u2018\u2019]?t|never|without)\b"
    r"(?:[\s,;:.-]+\w+){0,2}[\s,;:.-]+"
    r"(?:use|using|call|tools?|search(?:ing)?|"
    r"look(?:ing)?(?:\s+\w+){0,2}\s+up|lookup|research(?:ing)?|"
    r"browse|browsing|go(?:ing)?\s+online|online|internet|web|"
    r"mcp|discovery)\b|"
    r"\bfrom\s+(?:memory|your\s+own\s+knowledge)\b|"
    r"\b(?:explain|define|count|spell)\s+(?:only\s+)?(?:the\s+)?"
    r"(?:word|term|phrase|letters|characters)\b",
    re.IGNORECASE,
)
AUTOMATIC_FOLLOWUP_RE = re.compile(
    r"^\s*(?:and\s+)?(?:what\s+about|how\s+about|make\s+it|do\s+the\s+same)\b",
    re.IGNORECASE,
)
AUTOMATIC_VERBATIM_RE = re.compile(
    r'```.*?```|`[^`\n]*`|"(?:\\.|[^"\\])*"|'
    r"\u201c[^\u201d]*\u201d|\u2018[^\u2019]*\u2019|"
    r"\u201e[^\u201c]*\u201c|\u00ab[^\u00bb]*\u00bb|"
    r"\u2039[^\u203a]*\u203a|"
    r"\u300c[^\u300d]*\u300d|\u300e[^\u300f]*\u300f|"
    r"\uff02[^\uff02]*\uff02",
    re.DOTALL,
)
POLICY_MARKER = "<tool_broker_policy>"
POLICY_END_MARKER = "</tool_broker_policy>"
POLICY = f"""{POLICY_MARKER}
DISCOVERY: Before work needing external or changing information or a domain-specific operation, call `search_tools`; users need not mention tools. Skip only reliably self-contained greetings, conversation, rewriting, formatting, or arithmetic. A directly available general-purpose search, browsing, or scraping tool is fallback evidence gathering, not a reason to skip Tool Broker discovery when a domain-specific MCP tool may fit. Discovery precedes other research or execution. When general discovery first reveals a concrete source or domain that may have dedicated retrieval, run one focused `search_tools` query before generic scraping. For a Tool Broker inventory, search once with an empty query and catalog `mcp` or `all`, then present entries without executing them or issuing thematic searches.

AUTOMATIC PREFLIGHT: A current-message `tool_broker_automatic_discovery` packet satisfies its returned requirements; do not repeat covered searches. A `tool_broker_active_mcp` packet provides current schemas and authorizes its exact identifiers for direct reuse; search only for uncovered requirements. Preflight executes no MCP operation.

TOOL NAMESPACES: Direct Open WebUI tools and Tool Broker MCP identifiers are separate. Call a directly listed tool by its own function name. `execute_tool` accepts only an exact MCP identifier returned by `search_tools` or a current authenticated active-MCP packet; never pass another top-level tool name to it. When a direct tool result names a follow-up direct tool, invoke that function directly.

QUERY IDENTITY: Use discriminating entities, domains, operations, requested fields or distinctions, and provenance. Preserve every supplied identifier, URL, reference, proper name, quotation, and source-language term verbatim and in context. When a known schema argument names a supplied value, pair that exact name and value in the query; never replace it with "specific record," "record ID," or similar abstraction. For non-English requests, use a concise English discovery query while preserving those immutable terms, then answer in the user's language. Do not add generic authority or category terms that overpower the subject.

AUTHORIZATION AND ARGUMENTS: Each user message has a fresh authorization scope; conversation history does not authorize execution. Successful searches in the same message union their exact returned identifiers. Execute only those identifiers and only with that tool's shown argument names and schema. Never guess a prefix, identifier, URL, required argument, or transfer arguments between tools. Domain-specific property, taxonomy, concept, field, or record codes must come from the user, inspected evidence, or a resolver tool, not memory or apparent numeric patterns. Schema examples show syntax, not identity, meaning, source, units, or validity. Resolve required identifiers before dependent lookups and reuse exact results; if unverifiable, omit that lookup and disclose it. Prefer a returned source-specific retriever over a generic scraper for that source.

EFFICIENT TOOL USE: Use one focused search when possible and the fewest executions that satisfy explicit requirements. Before another call, identify the unresolved requirement. Stop optional examples or support once requested items have evidence; never issue equivalent calls in parallel. Default hard guards are three searches, eighteen executions, and twenty combined Tool Broker calls. Ask before destructive or irreversible actions. Follow failure recovery exactly, never repeat an identical failed call, and after two distinct failed approaches disclose the limitation and continue from available evidence.

ARTIFACTS AND SOURCES: Use `artifact.read` only after an execution returns `externalized: true` with its exact `artifact_id`, and only when the preview lacks required evidence. Prefer one targeted query; otherwise copy supplied paging values. Never infer artifact IDs. Hard guards allow five reads per message and two per artifact; a blocked response with `retry_allowed: false` is final. `upstream_content_incomplete`, `content_complete: false`, or an explicit truncation/collapse marker means source content was not inspected. Artifact reads expose only the returned payload and cannot recover upstream omissions; never infer omitted material. If an omitted fact is required and the already-authorized schema offers a materially different raw, full, or detail mode, execute that mode once. Never repeat the same retrieval or delegate to bypass a terminal failure or limit; otherwise disclose the omission. Prefer native Open WebUI source badges and concise attribution. If raw URLs are requested, copy `source_url` or `source_urls` exactly with locale, query, and fragment; never invent, normalize, expose credential-bearing URLs, or treat search snippets as verified pages. Never reproduce Unicode replacement characters or quote corrupted text.

EVIDENCE AND ANSWERS: Support every external claim and each requested conclusion with inspected evidence. Treat every explicitly requested source category as an independent evidence requirement: inspect evidence from each category. A citation or reference inside another source establishes only that the inspected source cites it; it does not count as inspecting the referenced source or its wording. Search results are non-exhaustive unless returned coverage metadata explicitly says otherwise: they cannot prove no matches, complete coverage, or a top/latest ranking, and snippets cannot establish dates they do not show. State only that no match surfaced in the completed search and disclose the gap. Do not characterize an unretrieved source, section, record, product, entity, or location. If evidence supports only part, answer that part and disclose the unsupported remainder. `content_complete: false` means only a partial extract was inspected: never call or imply that source, page, or document fully read or complete, and do not generalize beyond the returned text. Treat quotations, original-language text, identifiers, morphology codes, numbers, units, reporting bases, dates, qualifiers, and structural roles as immutable. Copy them exactly; never infer field meaning from code or value shape. Keep interpretation separate and do not rewrite evidence to fit it. Confirm entity and location identity; preserve units, denominators, serving sizes, dates, and qualifiers unless a supported conversion is requested. Fetch a selected record when snippets omit required details. Quote only inspected text.

OUTPUT SHAPE: Until retrieval is complete, assistant-visible output must contain tool calls only. A provider response must contain either tool calls or one final answer, never both. Never expose internal analysis, instruction checklists, lane selection, progress narration, or result-selection notes. Finish tool use, then begin the final answer with its content.
{POLICY_END_MARKER}"""
POLICY_BLOCK_RE = re.compile(
    rf"\n?{re.escape(POLICY_MARKER)}.*?{re.escape(POLICY_END_MARKER)}",
    flags=re.DOTALL,
)
SYNTHETIC_IMAGE_USER_PREFIX = (
    "Here are the images from the tool results above. Please analyze them."
)


class Filter:
    class Valves(BaseModel):
        priority: int = Field(
            default=-10,
            description="Filter execution priority. Lower values run first.",
        )
        automatic_discovery_mode: str = Field(
            default="on",
            description=(
                "on: inject confident authenticated discovery matches only "
                "(default; below-threshold hints remain in broker diagnostics); "
                "off: retain policy only."
            ),
        )
        automatic_discovery_preparation_seconds: float = Field(
            default=4.0,
            ge=0.1,
            le=10.0,
            description=(
                "Cold automatic metadata preparation budget. MCP cancellation cleanup "
                "can take up to ten additional seconds; explicit search timeouts are unchanged."
            ),
        )

    def __init__(self):
        self.valves = self.Valves()

    def _has_broker(self, tools: Any) -> bool:
        if not isinstance(tools, list):
            return False
        names = {
            function.get("name")
            for tool in tools
            if isinstance(tool, dict)
            and isinstance((function := tool.get("function")), dict)
            and isinstance(function.get("name"), str)
        }
        return {"search_tools", "execute_tool"}.issubset(names)

    def _tool_name(self, tool: Any) -> Optional[str]:
        if not isinstance(tool, dict):
            return None
        function = tool.get("function")
        if not isinstance(function, dict):
            return None
        name = function.get("name")
        return name if isinstance(name, str) else None

    def _order_tools(self, tools: list[Any]) -> list[Any]:
        ordered = tools.copy()
        search_index = next(
            (
                index
                for index, tool in enumerate(ordered)
                if self._tool_name(tool) == "search_tools"
            ),
            None,
        )
        execute_index = next(
            (
                index
                for index, tool in enumerate(ordered)
                if self._tool_name(tool) == "execute_tool"
            ),
            None,
        )
        if (
            search_index is not None
            and execute_index is not None
            and search_index > execute_index
        ):
            ordered[search_index], ordered[execute_index] = (
                ordered[execute_index],
                ordered[search_index],
            )
        return ordered

    def _content_text(self, content: Any) -> str:
        if isinstance(content, str):
            return content
        if not isinstance(content, list):
            return ""
        return "\n".join(
            item.get("text", "")
            for item in content
            if isinstance(item, dict) and isinstance(item.get("text"), str)
        )

    @staticmethod
    def _match_tool_spec(match: Any) -> Optional[tuple]:
        """Extract ``(server, spec)`` from a discovery match dict, in the
        shape ``broker._score``/``broker._missing_reference_arguments``/
        ``broker._is_discovery_spec`` expect. Shared by every Filter helper
        that needs to re-derive a spec from a compact discovery match instead
        of duplicating this extraction. Returns ``None`` when a match is
        missing a usable tool identifier.
        """
        if not isinstance(match, dict):
            return None
        tool = match.get("tool")
        if not isinstance(tool, str) or not tool:
            return None
        server = match.get("server")
        if not isinstance(server, str) or not server:
            server = tool.split(".", 1)[0]
        spec = {
            "name": tool.rsplit(".", 1)[-1],
            "description": match.get("description", ""),
            "parameters": match.get("arguments", {}),
        }
        return server, spec

    def _tool_execution_outcomes(self, messages: list, start_index: int) -> dict:
        """Map each ``execute_tool`` identifier called after
        ``start_index`` to its most recent outcome ("success" or "error"),
        pairing assistant tool calls with the matching tool-result message
        by ``tool_call_id`` when available and positionally otherwise.
        Non-``execute_tool`` calls are queued as placeholders so parallel
        tool results cannot be misattributed to an MCP execution.
        """
        outcomes: dict = {}
        pending: list = []
        for message in messages[start_index + 1 :]:
            if not isinstance(message, dict):
                continue
            if message.get("role") == "assistant":
                tool_calls = message.get("tool_calls")
                if isinstance(tool_calls, list):
                    for tool_call in tool_calls:
                        if not isinstance(tool_call, dict):
                            continue
                        function = tool_call.get("function")
                        if not isinstance(function, dict):
                            continue
                        tool = None
                        if function.get("name") == "execute_tool":
                            arguments = function.get("arguments")
                            parsed = None
                            if isinstance(arguments, str):
                                try:
                                    parsed = json.loads(arguments)
                                except (TypeError, ValueError, json.JSONDecodeError):
                                    parsed = None
                            elif isinstance(arguments, dict):
                                parsed = arguments
                            value = (
                                parsed.get("tool") if isinstance(parsed, dict) else None
                            )
                            if isinstance(value, str) and value:
                                tool = value
                        call_id = tool_call.get("id")
                        pending.append(
                            (
                                (
                                    call_id
                                    if isinstance(call_id, str) and call_id
                                    else None
                                ),
                                tool,
                            )
                        )
                continue
            text = None
            tool_call_id = None
            if message.get("role") == "tool":
                text = self._content_text(message.get("content"))
                candidate_id = message.get("tool_call_id")
                if isinstance(candidate_id, str) and candidate_id:
                    tool_call_id = candidate_id
            else:
                candidate = self._content_text(message.get("content"))
                if message.get("role") == "user" and self._is_tool_result_user(
                    candidate
                ):
                    text = candidate
            if text is not None and pending:
                pending_index = 0
                if tool_call_id is not None:
                    pending_index = next(
                        (
                            index
                            for index, (call_id, _) in enumerate(pending)
                            if call_id == tool_call_id
                        ),
                        0,
                    )
                _, tool = pending.pop(pending_index)
                if tool:
                    outcome_text = text.strip()
                    if outcome_text.startswith(
                        "<tool_response>"
                    ) and outcome_text.endswith("</tool_response>"):
                        outcome_text = outcome_text[
                            len("<tool_response>") : -len("</tool_response>")
                        ].lstrip()
                    outcomes[tool] = (
                        "error"
                        if outcome_text.lstrip().startswith("Error:")
                        else "success"
                    )
        return outcomes

    def _companion_execution_target(
        self, broker: Any, response: dict, messages: list, user_index: int
    ) -> Optional[dict]:
        """Find the highest-ranked non-discovery-style companion that
        shares a server with a discovery-style tool already executed
        successfully this message (e.g. a search/lookup style tool followed
        by a same-server retrieval tool). At most one companion is selected
        from a result set; once attempted, later matches do not become a new
        companion. Purely generic: relies only on
        ``broker._is_discovery_spec`` and shared ``server`` grouping, never
        a hardcoded server or tool name.
        """
        matches = response.get("matches")
        if not isinstance(matches, list):
            return None
        by_server: dict = {}
        for match in matches:
            extracted = self._match_tool_spec(match)
            if extracted is None:
                continue
            server, spec = extracted
            tool = match["tool"]
            try:
                discovery_style = bool(broker._is_discovery_spec(spec))
            except (AttributeError, TypeError, ValueError):
                continue
            by_server.setdefault(server, []).append((tool, discovery_style, match))
        if not by_server:
            return None
        outcomes = self._tool_execution_outcomes(messages, user_index)
        for entries in by_server.values():
            if not any(
                is_discovery and outcomes.get(tool) == "success"
                for tool, is_discovery, _ in entries
            ):
                continue
            companion = next(
                (
                    (tool, match)
                    for tool, is_discovery, match in entries
                    if not is_discovery
                ),
                None,
            )
            if companion is None:
                continue
            tool, match = companion
            return None if tool in outcomes else match
        return None

    @staticmethod
    def _automatic_status_description(response: dict) -> str:
        identifiers = [
            match["tool"]
            for match in response.get("matches", [])
            if isinstance(match, dict)
            and isinstance(match.get("tool"), str)
            and match["tool"]
        ]
        shown = [
            identifier if len(identifier) <= 64 else identifier[:61] + "..."
            for identifier in identifiers[:2]
        ]
        noun = "tool" if len(identifiers) == 1 else "tools"
        description = f"Found {len(identifiers)} candidate {noun}"
        if shown:
            description += ": " + ", ".join(shown)
        remaining = len(identifiers) - len(shown)
        if remaining:
            description += f", +{remaining} more"
        return description + "."

    def _is_tool_result_user(self, content: str) -> bool:
        stripped = content.strip()
        return (
            stripped.startswith("<tool_response>")
            and stripped.endswith("</tool_response>")
        ) or stripped.startswith(SYNTHETIC_IMAGE_USER_PREFIX)

    def _latest_user_index(self, messages: list[Any]) -> Optional[int]:
        for index in range(len(messages) - 1, -1, -1):
            message = messages[index]
            if not isinstance(message, dict) or message.get("role") != "user":
                continue
            content = self._content_text(message.get("content")).strip()
            if self._is_tool_result_user(content):
                continue
            return index
        return None

    def _current_turn_insert_at(self, messages: list[Any]) -> int:
        user_index = self._latest_user_index(messages)
        if user_index is not None:
            return user_index
        return next(
            (
                index
                for index, message in enumerate(messages)
                if not isinstance(message, dict) or message.get("role") != "system"
            ),
            len(messages),
        )

    def _has_tool_call_after(
        self,
        messages: list[Any],
        start_index: int,
        tool_name: str,
    ) -> bool:
        for message in messages[start_index + 1 :]:
            if not isinstance(message, dict) or message.get("role") != "assistant":
                continue
            tool_calls = message.get("tool_calls")
            if not isinstance(tool_calls, list):
                continue
            for tool_call in tool_calls:
                if not isinstance(tool_call, dict):
                    continue
                function = tool_call.get("function")
                if isinstance(function, dict) and function.get("name") == tool_name:
                    return True
        return False

    def _is_search_choice(self, tool_choice: Any) -> bool:
        return (
            isinstance(tool_choice, dict)
            and isinstance(tool_choice.get("function"), dict)
            and tool_choice["function"].get("name") == "search_tools"
        )

    def _is_execute_choice(self, tool_choice: Any) -> bool:
        return (
            isinstance(tool_choice, dict)
            and isinstance(tool_choice.get("function"), dict)
            and tool_choice["function"].get("name") == "execute_tool"
        )

    def _stateful_tool_continuation(self, body: dict) -> bool:
        if not body.get("previous_response_id"):
            return False
        items = body.get("input")
        if not isinstance(items, list):
            return False
        return any(
            isinstance(item, dict)
            and (
                item.get("type") == "function_call_output" or item.get("role") == "tool"
            )
            for item in items
        )

    def _terminal_broker_failure_after(
        self,
        messages: list[Any],
        start_index: int,
    ) -> bool:
        terminal_phrases = (
            "duplicate Tool Broker execution rejected",
            "total Tool Broker call budget exhausted",
            "Mandatory stop: do not execute another tool",
        )
        for message in messages[start_index + 1 :]:
            if not isinstance(message, dict):
                continue
            text = self._content_text(message.get("content"))
            if not text and message.get("type") == "function_call_output":
                text = self._content_text(message.get("output"))
            if any(phrase in text for phrase in terminal_phrases):
                return True
        return False

    def _release_stateful_search_choice(self, body: dict) -> None:
        if self._is_search_choice(body.get("tool_choice")) and (
            self._stateful_tool_continuation(body)
        ):
            body.pop("tool_choice", None)

    def _resolved_broker(self, metadata: Any, user: Any):
        if (
            not isinstance(metadata, dict)
            or not isinstance(user, dict)
            or not user.get("id")
        ):
            return None
        tools = metadata.get("tools")
        if not isinstance(tools, dict):
            return None
        owners = []
        tool_ids = []
        for name in ("search_tools", "execute_tool"):
            entry = tools.get(name)
            if not isinstance(entry, dict):
                return None
            wrapper = entry.get("callable")
            function = getattr(wrapper, "__function__", None)
            bound = getattr(wrapper, "__extra_params__", {})
            if (
                not isinstance(bound, dict)
                or not isinstance(bound.get("__user__"), dict)
                or bound["__user__"].get("id") != user["id"]
                or getattr(function, "__name__", None) != name
            ):
                return None
            owners.append(getattr(function, "__self__", None))
            tool_ids.append(entry.get("tool_id"))
        broker = owners[0]
        if (
            broker is None
            or owners[1] is not broker
            or not tool_ids[0]
            or tool_ids[0] != tool_ids[1]
            or getattr(broker, "_routing_protocol", None) != 8
        ):
            return None
        return broker

    async def _active_persisted_mcp_packet(
        self,
        body: dict,
        messages: list[Any],
        request: Any,
        user: Any,
        metadata: Any,
        event_emitter: Any = None,
        event_call: Any = None,
    ) -> tuple[Optional[str], int, bool]:
        broker = self._resolved_broker(metadata, user)
        if broker is None or request is None:
            return None, 0, False
        try:
            schemas = await broker._active_persisted_mcp_schemas(
                request,
                user,
                metadata,
                event_emitter,
                event_call,
            )
        except Exception:
            log.exception("Tool Broker could not restore retained MCP tools.")
            return None, 0, False
        if not isinstance(schemas, list) or not schemas:
            return None, 0, False
        message_id = (
            str(metadata.get("message_id"))
            if isinstance(metadata, dict) and metadata.get("message_id")
            else ""
        )
        user_index = self._latest_user_index(messages)
        current_message_continuation = bool(
            user_index is not None
            and (
                self._has_tool_call_after(messages, user_index, "search_tools")
                or self._has_tool_call_after(messages, user_index, "execute_tool")
                or self._stateful_tool_continuation(body)
            )
        )
        direct_followup = any(
            str(schema.get("origin_message_id", "")) != message_id
            and type(schema.get("origin_distance")) is int
            and 0 < schema["origin_distance"] <= 2
            for schema in schemas
            if isinstance(schema, dict)
        )
        tools = [
            {
                "server": schema["server"],
                "tool": schema["tool"],
                "description": schema["description"],
                "arguments": schema["arguments"],
            }
            for schema in schemas
            if (
                isinstance(schema, dict)
                and (
                    str(schema.get("origin_message_id", "")) != message_id
                    or current_message_continuation
                )
                and isinstance(schema.get("server"), str)
                and isinstance(schema.get("tool"), str)
                and isinstance(schema.get("description"), str)
                and isinstance(schema.get("arguments"), dict)
            )
        ]
        if not tools:
            return None, 0, False
        tools.sort(key=lambda item: item["tool"])
        context_scope = broker._active_mcp_context_scope(
            user,
            metadata,
            tools,
        )
        if context_scope is None:
            return None, 0, False
        payload = json.dumps(
            {
                "context_scope": context_scope,
                "guidance": ACTIVE_MCP_GUIDANCE,
                "tools": tools,
            },
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).replace("<", "\\u003c")
        packet = f"{ACTIVE_MCP_MARKER}\n{payload}\n{ACTIVE_MCP_END}"
        current_activation = any(
            str(schema.get("origin_message_id", "")) == message_id
            for schema in schemas
            if isinstance(schema, dict)
        )
        return (
            packet,
            len(tools),
            bool(
                direct_followup or (current_message_continuation and current_activation)
            ),
        )

    def _automatic_query(self, messages, user_index):
        text = self._content_text(messages[user_index].get("content")).strip()
        if not text or len(text) > 6_000 or AUTOMATIC_CATALOG_REQUEST_RE.search(text):
            return None
        intent = AUTOMATIC_VERBATIM_RE.sub(" ", text)
        if AUTOMATIC_NEGATION_RE.search(intent):
            return None
        if AUTOMATIC_FOLLOWUP_RE.search(text):
            previous = next(
                (
                    self._content_text(message.get("content"))
                    for message in reversed(messages[:user_index])
                    if isinstance(message, dict)
                    and message.get("role") == "user"
                    and not self._is_tool_result_user(
                        self._content_text(message.get("content"))
                    )
                ),
                "",
            ).strip()
            if not previous or len(previous) > 1_500:
                return None
            text = f"{text}\n{previous}"
        return text if len(text) <= 6_000 else None

    async def _automatic_route(
        self,
        body,
        messages,
        request,
        user,
        metadata,
        event_emitter,
        event_call,
    ) -> bool:
        if self.valves.automatic_discovery_mode == "off":
            return False
        if self.valves.automatic_discovery_mode != "on":
            raise ValueError("automatic_discovery_mode must be 'on' or 'off'.")
        broker = self._resolved_broker(metadata, user)
        if broker is None or request is None:
            log.debug(
                "Automatic Tool Broker routing unavailable: no authenticated native routing protocol."
            )
            return False
        user_index = self._latest_user_index(messages)
        if user_index is None:
            return False
        user_text = self._automatic_query(messages, user_index)
        if user_text is None:
            return False
        continuation = (
            self._has_tool_call_after(messages, user_index, "search_tools")
            or self._has_tool_call_after(messages, user_index, "execute_tool")
            or self._stateful_tool_continuation(body)
        )
        try:
            result = await broker._automatic_discovery(
                request=request,
                user=user,
                metadata=metadata,
                query=user_text,
                mode=self.valves.automatic_discovery_mode,
                preparation_seconds=self.valves.automatic_discovery_preparation_seconds,
                event_emitter=event_emitter,
                event_call=event_call,
                continuation=bool(continuation),
            )
        except (ValueError, RuntimeError):
            raise
        except Exception:
            log.exception("Automatic Tool Broker routing could not run.")
            result = {
                "status": "error",
                "reason": "Automatic Tool Broker routing is unavailable; use explicit discovery.",
            }
        status = result.get("status") if isinstance(result, dict) else None
        insert_at = self._current_turn_insert_at(messages)
        if status == "discovered":
            try:
                response = json.loads(result["response"])
                if not isinstance(response, dict) or not isinstance(
                    response.get("matches"), list
                ):
                    raise ValueError("Automatic discovery returned malformed matches.")
                scope = broker._turn_usage_key(user, metadata)[1]
            except (KeyError, TypeError, ValueError, json.JSONDecodeError):
                log.exception(
                    "Automatic Tool Broker discovery packet could not be prepared."
                )
                return False
            response["guidance"] = AUTOMATIC_DISCOVERY_GUIDANCE
            payload = json.dumps(
                {"scope": scope, **response},
                ensure_ascii=False,
                separators=(",", ":"),
            ).replace("<", "\\u003c")
            messages.insert(
                insert_at,
                {
                    "role": "system",
                    "content": f"{AUTOMATIC_DISCOVERY_MARKER}\n{payload}\n{AUTOMATIC_DISCOVERY_END}",
                },
            )
            terminal = self._terminal_broker_failure_after(messages, user_index)
            companion = (
                None
                if terminal
                else self._companion_execution_target(
                    broker, response, messages, user_index
                )
            )
            if companion is not None:
                required_packet = (
                    f"{REQUIRED_EXECUTION_MARKER}\n"
                    "A same-server discovery tool succeeded for this user message. "
                    "Call `execute_tool` now with the companion's exact identifier "
                    "and shown arguments; do not substitute another server or operation.\n"
                    + json.dumps(
                        {
                            "tool": companion.get("tool"),
                            "arguments": companion.get("arguments", {}),
                        },
                        ensure_ascii=False,
                        separators=(",", ":"),
                    ).replace("<", "\\u003c")
                    + f"\n{REQUIRED_EXECUTION_END}"
                )
                messages.insert(
                    insert_at + 1, {"role": "system", "content": required_packet}
                )
                if body.get("tool_choice") is None:
                    body["tool_choice"] = {
                        "type": "function",
                        "function": {"name": "execute_tool"},
                    }
            if event_emitter is not None and result.get("replayed") is not True:
                await event_emitter(
                    {
                        "type": "status",
                        "data": {
                            "description": self._automatic_status_description(response),
                            "done": True,
                        },
                    }
                )
            return True
        if (
            status in {"blocked", "error"}
            and event_emitter is not None
            and result.get("replayed") is not True
        ):
            await event_emitter(
                {
                    "type": "status",
                    "data": {
                        "description": result.get(
                            "reason", "Automatic Tool Broker routing unavailable."
                        ),
                        "done": True,
                    },
                }
            )
        return False

    async def request(
        self,
        body: dict,
        __tools__: Optional[list[dict]] = None,
        __request__: Any = None,
        __user__: Optional[dict] = None,
        __metadata__: Optional[dict] = None,
        __event_emitter__=None,
        __event_call__=None,
    ) -> dict:
        tools = __tools__ if isinstance(__tools__, list) else body.get("tools")
        if not self._has_broker(tools):
            return body
        source_messages = body.get("messages")
        if not isinstance(source_messages, list):
            self._release_stateful_search_choice(body)
            return body

        body = dict(body)
        messages = [
            (
                {
                    **message,
                    **(
                        {
                            "content": [
                                dict(item) if isinstance(item, dict) else item
                                for item in message["content"]
                            ]
                        }
                        if isinstance(message.get("content"), list)
                        else {}
                    ),
                }
                if isinstance(message, dict)
                else message
            )
            for message in source_messages
        ]
        cleaned_messages = []
        for message in messages:
            if (
                isinstance(message, dict)
                and message.get("role") == "system"
                and isinstance(message.get("content"), str)
            ):
                content = message["content"]
                # Remove each block's merge separator without trimming the user's prompt.
                for pattern in (
                    AUTOMATIC_DISCOVERY_BLOCK_RE,
                    AUTOMATIC_HINT_BLOCK_RE,
                    ACTIVE_MCP_BLOCK_RE,
                    REQUIRED_EXECUTION_BLOCK_RE,
                    POLICY_BLOCK_RE,
                ):
                    content = pattern.sub("", content)
                if not content.strip():
                    continue
                message["content"] = content
            cleaned_messages.append(message)
        messages = cleaned_messages
        body["messages"] = messages
        provider_tools = body.get("tools")
        if isinstance(provider_tools, list):
            body["tools"] = self._order_tools(provider_tools)

        insert_at = 0
        while (
            insert_at < len(messages)
            and isinstance(messages[insert_at], dict)
            and messages[insert_at].get("role") == "system"
        ):
            insert_at += 1
        messages.insert(insert_at, {"role": "system", "content": POLICY})

        latest_user = self._latest_user_index(messages)
        if (
            latest_user is not None
            and self._is_search_choice(body.get("tool_choice"))
            and self._has_tool_call_after(messages, latest_user, "search_tools")
        ):
            body.pop("tool_choice", None)
        if (
            latest_user is not None
            and self._is_execute_choice(body.get("tool_choice"))
            and self._has_tool_call_after(messages, latest_user, "execute_tool")
        ):
            body.pop("tool_choice", None)

        active_mcp_packet, _, _ = await self._active_persisted_mcp_packet(
            body,
            messages,
            __request__,
            __user__,
            __metadata__,
            __event_emitter__,
            __event_call__,
        )
        await self._automatic_route(
            body,
            messages,
            __request__,
            __user__,
            __metadata__,
            __event_emitter__,
            __event_call__,
        )
        if active_mcp_packet is not None:
            insert_at = self._current_turn_insert_at(messages)
            messages.insert(
                insert_at,
                {
                    "role": "system",
                    "content": active_mcp_packet,
                },
            )

        latest_user = self._latest_user_index(messages)
        if latest_user is not None and self._terminal_broker_failure_after(
            messages, latest_user
        ):
            provider_tools = body.get("tools")
            if isinstance(provider_tools, list):
                body["tools"] = [
                    tool
                    for tool in provider_tools
                    if self._tool_name(tool) not in {"search_tools", "execute_tool"}
                ]
            if self._is_search_choice(
                body.get("tool_choice")
            ) or self._is_execute_choice(body.get("tool_choice")):
                body.pop("tool_choice", None)
        return body
