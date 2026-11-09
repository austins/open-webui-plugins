"""
title: Agent Efficiency
description: Measures outgoing model requests, protects current-turn native view_skill results and active MCP schemas, and conservatively compacts older tool results.
version: 1.3.0
required_open_webui_version: 0.11.3
"""

import hashlib
import json
import math
import re
import time
from typing import Any, Optional

from fastapi import Request
from pydantic import BaseModel, Field

METRICS_STATE_KEY = "_agent_efficiency_metrics_v1"
BROKER_USAGE_STATE_KEY = "_tool_broker_usage_v1"
ACTIVE_MCP_MARKER = "<tool_broker_active_mcp>"
ACTIVE_MCP_END = "</tool_broker_active_mcp>"
MAX_ACTIVE_MCP_SCHEMA_CHARS = 64_000
MAX_ACTIVE_MCP_SCHEMAS = 8
ACTIVE_MCP_CONTEXT_PROTOCOL = 1
SYNTHETIC_IMAGE_USER_PREFIX = (
    "Here are the images from the tool results above. Please analyze them."
)
COMPACTION_MARKER_RE = re.compile(
    r"^\[Earlier tool result compacted by Agent Efficiency; "
    r"[\d,]+ characters replaced; sha256:[0-9a-f]{16}\.\]$"
)


def _user_id(user: Any) -> str:
    if isinstance(user, dict):
        value = user.get("id")
    else:
        value = getattr(user, "id", None)
    text = str(value).strip() if value is not None else ""
    return text or "anonymous"


def _metadata(body: dict, metadata: Optional[dict]) -> dict:
    if isinstance(metadata, dict):
        return metadata
    value = body.get("metadata")
    return value if isinstance(value, dict) else {}


def _metric_key(
    body: dict,
    metadata: Optional[dict],
    user: Any,
) -> Optional[str]:
    user_id = _user_id(user)
    if user_id == "anonymous":
        return None
    meta = _metadata(body, metadata)
    chat_id = meta.get("chat_id") or body.get("chat_id") or "temporary"
    chat_id = str(chat_id)
    if len(chat_id) > 128:
        digest = hashlib.sha256(chat_id.encode("utf-8")).hexdigest()[:16]
        chat_id = f"{chat_id[:96]}:{digest}"
    return f"{user_id}:{chat_id}"


def _turn_id(body: dict, metadata: Optional[dict]) -> Optional[str]:
    meta = _metadata(body, metadata)
    value = meta.get("message_id") or body.get("id")
    if value:
        return str(value)

    messages = body.get("messages")
    if not isinstance(messages, list):
        return None
    user_messages = _real_user_messages(messages)
    if not user_messages:
        return None
    latest = user_messages[-1]
    latest_id = latest.get("id") or latest.get("message_id")
    if latest_id:
        return f"derived:{latest_id}"
    try:
        content = json.dumps(
            latest.get("content"),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
    except (TypeError, ValueError):
        content = str(latest.get("content"))
    fingerprint = hashlib.sha256(
        json.dumps(
            [len(user_messages), content],
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()
    return f"derived:{fingerprint}"


def _text(value: Any) -> str:
    if isinstance(value, str):
        return value
    if value is None:
        return ""
    try:
        return json.dumps(value, ensure_ascii=False, separators=(",", ":"))
    except (TypeError, ValueError):
        return str(value)


def _content_text(content: Any) -> str:
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return ""
    return "\n".join(
        item.get("text", "")
        for item in content
        if isinstance(item, dict) and isinstance(item.get("text"), str)
    )


def _is_tool_result_user_content(content: Any) -> bool:
    stripped = _content_text(content).strip()
    return (
        stripped.startswith("<tool_response>") and stripped.endswith("</tool_response>")
    ) or stripped.startswith(SYNTHETIC_IMAGE_USER_PREFIX)


def _real_user_messages(messages: list[Any]) -> list[dict]:
    return [
        message
        for message in messages
        if (
            isinstance(message, dict)
            and message.get("role") == "user"
            and not _is_tool_result_user_content(message.get("content"))
        )
    ]


def _message_chars(messages: list[dict]) -> int:
    return sum(
        len(_text(message.get("content")))
        for message in messages
        if isinstance(message, dict)
    )


def _tool_names(tools: list[dict]) -> list[str]:
    names = []
    for tool in tools:
        function = tool.get("function") if isinstance(tool, dict) else None
        name = function.get("name") if isinstance(function, dict) else None
        if isinstance(name, str) and name:
            names.append(name)
    return names


def _tool_schema_sizes(tools: list[dict]) -> list[dict]:
    sizes = []
    for index, tool in enumerate(tools):
        function = tool.get("function") if isinstance(tool, dict) else None
        name = function.get("name") if isinstance(function, dict) else None
        sizes.append(
            {
                "name": name if isinstance(name, str) and name else f"tool_{index + 1}",
                "characters": len(_text(tool)),
            }
        )
    return sorted(
        sizes,
        key=lambda item: (-item["characters"], item["name"]),
    )


def _active_mcp_context_scope(
    user_id: str,
    chat_id: str,
    tools: list[dict],
) -> str:
    canonical = json.dumps(
        [ACTIVE_MCP_CONTEXT_PROTOCOL, user_id, chat_id, tools],
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _active_mcp_schemas(
    messages: list[dict],
    metadata: Optional[dict],
    user: Any,
    request: Optional[Request],
) -> dict[str, int]:
    user_id = _user_id(user)
    if (
        user_id == "anonymous"
        or not isinstance(metadata, dict)
        or not metadata.get("chat_id")
        or not metadata.get("message_id")
    ):
        return {}
    usage_key = hashlib.sha256(
        json.dumps(
            [
                user_id,
                str(metadata["chat_id"]),
                str(metadata["message_id"]),
            ],
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()
    usage_state = (
        getattr(request.app.state, BROKER_USAGE_STATE_KEY, None)
        if request is not None
        else None
    )
    usage_records = (
        usage_state.get("records") if isinstance(usage_state, dict) else None
    )
    usage_record = (
        usage_records.get(usage_key) if isinstance(usage_records, dict) else None
    )
    schemas = {}
    for message in messages:
        if not isinstance(message, dict) or message.get("role") != "system":
            continue
        content = message.get("content")
        if not isinstance(content, str):
            continue
        cursor = 0
        while True:
            start = content.find(ACTIVE_MCP_MARKER, cursor)
            if start < 0:
                break
            payload_start = start + len(ACTIVE_MCP_MARKER) + 1
            end = content.find(f"\n{ACTIVE_MCP_END}", payload_start)
            if end < 0:
                break
            cursor = end + len(ACTIVE_MCP_END) + 1
            payload_text = content[payload_start:end]
            if len(payload_text.replace("<", "\\u003c")) > (
                MAX_ACTIVE_MCP_SCHEMA_CHARS + 4_000
            ):
                continue
            try:
                payload = json.loads(payload_text)
            except (TypeError, ValueError, json.JSONDecodeError):
                continue
            if (
                not isinstance(payload, dict)
                or set(payload) != {"context_scope", "guidance", "tools"}
                or not isinstance(payload.get("guidance"), str)
                or not isinstance(payload.get("tools"), list)
                or not 1 <= len(payload["tools"]) <= MAX_ACTIVE_MCP_SCHEMAS
            ):
                continue
            encoded = json.dumps(
                payload["tools"],
                ensure_ascii=False,
                separators=(",", ":"),
            ).replace("<", "\\u003c")
            if len(encoded) > MAX_ACTIVE_MCP_SCHEMA_CHARS:
                continue
            parsed = {}
            valid = True
            for schema in payload["tools"]:
                if (
                    not isinstance(schema, dict)
                    or set(schema) != {"server", "tool", "description", "arguments"}
                    or not isinstance(schema.get("server"), str)
                    or not schema["server"]
                    or not isinstance(schema.get("tool"), str)
                    or not schema["tool"].startswith(f"{schema['server']}.")
                    or not isinstance(schema.get("description"), str)
                    or not isinstance(schema.get("arguments"), dict)
                    or schema["tool"] in parsed
                ):
                    valid = False
                    break
                parsed[schema["tool"]] = len(
                    json.dumps(
                        schema,
                        ensure_ascii=False,
                        sort_keys=True,
                        separators=(",", ":"),
                    ).replace("<", "\\u003c")
                )
            expected_scope = _active_mcp_context_scope(
                user_id,
                str(metadata["chat_id"]),
                payload["tools"],
            )
            if (
                valid
                and payload.get("context_scope") == expected_scope
                and isinstance(usage_record, dict)
                and usage_record.get("active_mcp_context_scope") == expected_scope
            ):
                schemas.update(parsed)
    return schemas


def _native_view_skill_result_indexes(
    messages: list[dict],
    latest_user_index: int,
) -> set[int]:
    call_ids = set()
    for index, message in enumerate(messages):
        if (
            index <= latest_user_index
            or not isinstance(message, dict)
            or message.get("role") != "assistant"
        ):
            continue
        tool_calls = message.get("tool_calls")
        if not isinstance(tool_calls, list):
            continue
        for call in tool_calls:
            function = call.get("function") if isinstance(call, dict) else None
            if not isinstance(function, dict) or function.get("name") != "view_skill":
                continue
            call_id = call.get("id")
            if isinstance(call_id, str) and call_id:
                call_ids.add(call_id)

    indexes = set()
    for index, message in enumerate(messages):
        if (
            index <= latest_user_index
            or not isinstance(message, dict)
            or message.get("role") != "tool"
        ):
            continue
        direct_name = message.get("name") or message.get("tool_name")
        call_id = message.get("tool_call_id")
        if direct_name == "view_skill" or (
            isinstance(call_id, str) and call_id in call_ids
        ):
            indexes.add(index)
    return indexes


class Filter:
    class Valves(BaseModel):
        priority: int = Field(
            default=0,
            description="Filter execution priority. Lower values run first.",
        )
        trim_enabled: bool = Field(
            default=True,
            description="Trim oversized older tool results in the provider payload only.",
        )
        max_tool_result_chars: int = Field(
            default=20_000,
            ge=1_000,
            description="Maximum characters retained for each older tool result.",
        )
        max_historical_tool_result_chars: int = Field(
            default=12_000,
            ge=0,
            description=(
                "Combined character budget for tool results older than the protected newest "
                "results. Set to 0 to disable the combined budget."
            ),
        )
        keep_recent_tool_results: int = Field(
            default=6,
            ge=0,
            description=(
                "Number of newest tool-result messages never trimmed. Current-turn "
                "native view_skill results are protected independently."
            ),
        )
        retained_head_percent: int = Field(
            default=60,
            ge=10,
            le=90,
            description="Percentage of a truncated result reserved for its beginning.",
        )
        metrics_ttl_minutes: int = Field(
            default=120,
            ge=5,
            description="Minutes to retain in-memory diagnostics after the latest request.",
        )
        maximum_metrics_per_user: int = Field(
            default=100,
            ge=5,
            description="Maximum in-memory diagnostic entries retained per user.",
        )
        maximum_metrics_total: int = Field(
            default=1_000,
            ge=10,
            description="Maximum in-memory diagnostic entries retained across all users.",
        )

    def __init__(self):
        self.valves = self.Valves()

    def _truncate(
        self,
        content: Any,
        limit: Optional[int] = None,
    ) -> tuple[str, int]:
        text = _text(content)
        limit = self.valves.max_tool_result_chars if limit is None else max(1, limit)
        if len(text) <= limit:
            return text, 0

        marker_template = (
            "\n\n[... {omitted} characters omitted by Agent Efficiency ...]\n\n"
        )
        marker = marker_template.format(omitted=0)
        available = max(0, limit - len(marker))
        head_size = math.floor(available * self.valves.retained_head_percent / 100)
        tail_size = available - head_size
        omitted = len(text) - head_size - tail_size
        marker = marker_template.format(omitted=omitted)

        available = max(0, limit - len(marker))
        head_size = math.floor(available * self.valves.retained_head_percent / 100)
        tail_size = available - head_size
        omitted = len(text) - head_size - tail_size
        marker = marker_template.format(omitted=omitted)

        tail = text[-tail_size:] if tail_size else ""
        return f"{text[:head_size]}{marker}{tail}", omitted

    @staticmethod
    def _compaction_marker(content: str) -> str:
        digest = hashlib.sha256(content.encode("utf-8")).hexdigest()[:16]
        return (
            "[Earlier tool result compacted by Agent Efficiency; "
            f"{len(content):,} characters replaced; sha256:{digest}.]"
        )

    def _store_metrics(
        self,
        request: Optional[Request],
        key: str,
        metrics: dict,
    ) -> None:
        if request is None:
            return

        cache = self._metrics_cache(request, key)
        now = time.time()
        previous = cache.get(key, {})
        metrics["provider_call_count"] = previous.get("provider_call_count", 0) + 1
        metrics["turn_started_at_epoch"] = previous.get("turn_started_at_epoch", now)
        metrics["turn_id"] = previous.get("turn_id")
        metrics["updated_at_epoch"] = now
        cache[key] = metrics

    def _metrics_cache(
        self,
        request: Request,
        key: str,
    ) -> dict:
        cache = getattr(request.app.state, METRICS_STATE_KEY, None)
        if not isinstance(cache, dict):
            cache = {}
            setattr(request.app.state, METRICS_STATE_KEY, cache)
        for malformed_key in [
            item_key for item_key, value in cache.items() if not isinstance(value, dict)
        ]:
            cache.pop(malformed_key, None)
        now = time.time()
        expires_before = now - self.valves.metrics_ttl_minutes * 60
        stale_keys = [
            item_key
            for item_key, value in cache.items()
            if value.get("updated_at_epoch", 0) < expires_before
        ]
        for stale_key in stale_keys:
            cache.pop(stale_key, None)

        owner_prefix = f"{key.split(':', 1)[0]}:"
        owner_entries = sorted(
            (
                (item_key, value)
                for item_key, value in cache.items()
                if item_key.startswith(owner_prefix) and item_key != key
            ),
            key=lambda item: item[1].get("updated_at_epoch", 0),
        )
        while len(owner_entries) >= self.valves.maximum_metrics_per_user:
            oldest_key, _ = owner_entries.pop(0)
            cache.pop(oldest_key, None)

        all_entries = sorted(
            ((item_key, value) for item_key, value in cache.items() if item_key != key),
            key=lambda item: item[1].get("updated_at_epoch", 0),
        )
        key_is_new = key not in cache
        while all_entries and (
            len(cache) > self.valves.maximum_metrics_total
            or (key_is_new and len(cache) >= self.valves.maximum_metrics_total)
        ):
            oldest_key, _ = all_entries.pop(0)
            cache.pop(oldest_key, None)
        return cache

    async def inlet(
        self,
        body: dict,
        __metadata__: Optional[dict] = None,
        __user__: Any = None,
        __request__: Optional[Request] = None,
    ) -> dict:
        if __request__ is None:
            return body

        key = _metric_key(body, __metadata__, __user__)
        if key is None:
            return body
        cache = self._metrics_cache(__request__, key)
        now = time.time()
        turn_id = _turn_id(body, __metadata__)
        previous = cache.get(key)
        if previous is None or previous.get("turn_id") != turn_id:
            cache[key] = {
                "provider_call_count": 0,
                "turn_id": turn_id,
                "turn_started_at_epoch": now,
                "updated_at_epoch": now,
            }
        else:
            previous["updated_at_epoch"] = now
        return body

    async def request(
        self,
        body: dict,
        __metadata__: Optional[dict] = None,
        __user__: Any = None,
        __request__: Optional[Request] = None,
        __features__: Optional[dict] = None,
    ) -> dict:
        messages = body.get("messages")
        if not isinstance(messages, list):
            messages = []
        else:
            # The incoming messages may be the same dict objects as stored
            # chat history (e.g. when Policy isn't installed/enabled to
            # clone them first). Shallow-copy each message dict so the
            # in-place content mutations below never touch stored history.
            messages = [
                dict(message) if isinstance(message, dict) else message
                for message in messages
            ]
            body["messages"] = messages

        tools = body.get("tools")
        if not isinstance(tools, list):
            tools = []

        tool_message_indexes = [
            index
            for index, message in enumerate(messages)
            if isinstance(message, dict) and message.get("role") == "tool"
        ]
        latest_user_index = max(
            (
                index
                for index, message in enumerate(messages)
                if (
                    isinstance(message, dict)
                    and message.get("role") == "user"
                    and not _is_tool_result_user_content(message.get("content"))
                )
            ),
            default=-1,
        )
        native_view_skill_indexes = _native_view_skill_result_indexes(
            messages,
            latest_user_index,
        )
        protected_indexes = set(
            tool_message_indexes[-self.valves.keep_recent_tool_results :]
            if self.valves.keep_recent_tool_results
            else []
        )
        protected_indexes.update(native_view_skill_indexes)

        tool_result_chars_before = sum(
            len(_text(messages[index].get("content"))) for index in tool_message_indexes
        )
        original_text_by_index = {
            index: messages[index]["content"]
            for index in tool_message_indexes
            if isinstance(messages[index].get("content"), str)
        }
        trimmed_results = 0
        budget_compacted_results = 0
        trimmed_indexes = set()
        historical_indexes = [
            index for index in tool_message_indexes if index not in protected_indexes
        ]

        if self.valves.trim_enabled:
            for index in historical_indexes:
                if not isinstance(messages[index].get("content"), str):
                    continue
                truncated, omitted = self._truncate(messages[index].get("content"))
                if omitted:
                    messages[index]["content"] = truncated
                    trimmed_indexes.add(index)

            budget = self.valves.max_historical_tool_result_chars
            historical_chars = sum(
                len(messages[index].get("content"))
                for index in historical_indexes
                if isinstance(messages[index].get("content"), str)
            )
            if budget and historical_chars > budget:
                for index in historical_indexes:
                    if historical_chars <= budget:
                        break

                    if not isinstance(messages[index].get("content"), str):
                        continue
                    content = _text(messages[index].get("content"))
                    if COMPACTION_MARKER_RE.fullmatch(content):
                        continue
                    marker = self._compaction_marker(original_text_by_index[index])
                    maximum_saving = len(content) - len(marker)
                    if maximum_saving <= 0:
                        continue

                    saved = len(content) - len(marker)
                    if saved:
                        messages[index]["content"] = marker
                        historical_chars -= saved
                        trimmed_indexes.add(index)
                        budget_compacted_results += 1

        tool_result_chars_after = sum(
            len(_text(messages[index].get("content"))) for index in tool_message_indexes
        )
        historical_tool_result_chars_after = sum(
            len(_text(messages[index].get("content"))) for index in historical_indexes
        )
        trimmed_results = len(trimmed_indexes)
        omitted_chars = max(0, tool_result_chars_before - tool_result_chars_after)
        tool_schema_chars = len(_text(tools))
        message_chars = _message_chars(messages)
        features = __features__ if isinstance(__features__, dict) else {}
        active_features = sorted(
            key for key, enabled in features.items() if enabled is True
        )
        active_mcp_schemas = _active_mcp_schemas(
            messages,
            __metadata__,
            __user__,
            __request__,
        )

        metrics = {
            "message_count": len(messages),
            "message_chars": message_chars,
            "system_chars": sum(
                len(_text(message.get("content")))
                for message in messages
                if isinstance(message, dict) and message.get("role") == "system"
            ),
            "tool_count": len(tools),
            "tool_names": _tool_names(tools),
            "tool_schema_sizes": _tool_schema_sizes(tools),
            "tool_schema_chars": tool_schema_chars,
            "tool_result_count": len(tool_message_indexes),
            "tool_result_chars_before": tool_result_chars_before,
            "tool_result_chars_after": tool_result_chars_after,
            "trimmed_results": trimmed_results,
            "protected_native_view_skill_results": len(native_view_skill_indexes),
            "protected_native_view_skill_chars": sum(
                len(_text(messages[index].get("content")))
                for index in native_view_skill_indexes
            ),
            "active_mcp_schemas": len(active_mcp_schemas),
            "active_mcp_schema_chars": sum(active_mcp_schemas.values()),
            "budget_compacted_results": budget_compacted_results,
            "omitted_chars": omitted_chars,
            "estimated_input_tokens": math.ceil(
                (message_chars + tool_schema_chars) / 4
            ),
            "active_features": active_features,
            "trim_enabled": self.valves.trim_enabled,
            "max_tool_result_chars": self.valves.max_tool_result_chars,
            "max_historical_tool_result_chars": (
                self.valves.max_historical_tool_result_chars
            ),
            "historical_tool_result_chars_after": (historical_tool_result_chars_after),
            "keep_recent_tool_results": self.valves.keep_recent_tool_results,
        }
        key = _metric_key(body, __metadata__, __user__)
        if key is not None:
            self._store_metrics(__request__, key, metrics)
        return body

    async def outlet(
        self,
        body: dict,
        __metadata__: Optional[dict] = None,
        __user__: Any = None,
        __request__: Optional[Request] = None,
    ) -> dict:
        if __request__ is None:
            return body

        key = _metric_key(body, __metadata__, __user__)
        if key is None:
            return body
        cache = getattr(__request__.app.state, METRICS_STATE_KEY, None)
        if not isinstance(cache, dict):
            return body
        metrics = cache.get(key)
        if isinstance(metrics, dict):
            started_at = metrics.get("turn_started_at_epoch", time.time())
            metrics["turn_duration_seconds"] = round(
                max(0.0, time.time() - started_at),
                3,
            )
            metrics["updated_at_epoch"] = time.time()
        return body
