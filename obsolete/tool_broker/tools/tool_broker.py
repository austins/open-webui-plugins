"""
title: Tool Broker
description: Searches and executes accessible Open WebUI MCP tools through two stable tools.
version: 2.5.2
required_open_webui_version: 0.11.3
"""

import asyncio
import contextlib
import contextvars
import hashlib
import inspect
import json
import logging
import math
import re
import time
from collections import Counter
from typing import Any, Optional
from urllib.parse import parse_qsl, urlsplit
from uuid import uuid4

from fastapi import Request
from open_webui.config import Config
from open_webui.models.chats import Chats
from open_webui.models.users import Users
from open_webui.utils.access_control import has_connection_access
from open_webui.utils.mcp.client import MCPClient
from open_webui.utils.misc import is_string_allowed
from open_webui.utils.tools import build_tool_server_headers
from pydantic import BaseModel, Field

log = logging.getLogger(__name__)
STATE_KEY = "_tool_broker_state_v1"
USAGE_STATE_KEY = "_tool_broker_usage_v1"
RANKING_DIAGNOSTICS_STATE_KEY = "_tool_broker_ranking_diagnostics_v1"
CHAT_MCP_STATE_KEY = "_tool_broker_mcp_v2"
CHAT_MCP_STATE_VERSION = 2
MAX_CHAT_MCP_TOOLS = 16
MAX_CHAT_MCP_ACTIVATIONS = 256
MAX_CHAT_MCP_STATE_CHARS = 64_000
MAX_ACTIVE_MCP_SCHEMAS = 8
MAX_ACTIVE_MCP_SCHEMA_CHARS = 64_000
ACTIVE_MCP_CONTEXT_PROTOCOL = 1
MAX_ROUTING_DESCRIPTION_CHARS = 4_000
USAGE_TTL_SECONDS = 15 * 60
MAX_USAGE_RECORDS_PER_USER = 100
MAX_USAGE_RECORDS_GLOBAL = 1_000
RANKING_DIAGNOSTICS_TTL_SECONDS = 120 * 60
MAX_RANKING_RECORDS_PER_USER = 100
MAX_RANKING_RECORDS_GLOBAL = 1_000
MCP_DISCONNECT_TIMEOUT_SECONDS = 10
CHAT_MCP_LOCK_MAX = 256
ARTIFACT_TOOL = "artifact.read"
ARTIFACT_NOT_FOUND_ERROR = (
    "Error: artifact not found, expired, or owned by another user."
)
RESERVED_SERVER_ALIASES = {"artifact"}
SEARCH_TOKEN_RE = r"\.[a-z0-9]+|[a-z][a-z0-9]*(?:\+\+|#|\.[a-z][a-z0-9]*)?|[0-9]+"
RANKING_FENCED_CODE_RE = re.compile(r"```.*?```", re.DOTALL)
RANKING_INLINE_CODE_RE = re.compile(r"`[^`\n]*`")
RANKING_QUOTED_TEXT_RE = re.compile(
    r'"(?:\\.|[^"\\])*"|'
    r"\u201c[^\u201d]*\u201d|"
    r"\u2018[^\u2019]*\u2019|"
    r"\u201e[^\u201c]*\u201c|"
    r"\u00ab[^\u00bb]*\u00bb|"
    r"\u2039[^\u203a]*\u203a|"
    r"\u300c[^\u300d]*\u300d|"
    r"\u300e[^\u300f]*\u300f|"
    r"\uff02[^\uff02]*\uff02"
)
SEARCH_STOP_WORDS = {
    "a",
    "an",
    "and",
    "are",
    "available",
    "can",
    "do",
    "for",
    "from",
    "i",
    "in",
    "including",
    "into",
    "is",
    "it",
    "its",
    "me",
    "of",
    "on",
    "please",
    "that",
    "the",
    "these",
    "this",
    "those",
    "to",
    "tool",
    "tools",
    "up",
    "use",
    "using",
    "vs",
    "with",
}
SEARCH_SYNONYMS = {
    "add": {"create", "insert", "new"},
    "browse": {"enumerate", "list", "search"},
    "create": {"add", "insert", "new"},
    "delete": {"remove"},
    "docs": {"documentation"},
    "documentation": {"docs"},
    "edit": {"modify", "update"},
    "enumerate": {"browse", "list"},
    "fetch": {"get", "read", "retrieve"},
    "find": {"lookup", "query", "search"},
    "get": {"fetch", "read", "retrieve"},
    "insert": {"add", "create", "new"},
    "list": {"browse", "enumerate"},
    "lookup": {"find", "query", "search"},
    "modify": {"edit", "update"},
    "new": {"add", "create", "insert"},
    "query": {"find", "lookup", "search"},
    "read": {"fetch", "get", "retrieve"},
    "remove": {"delete"},
    "retrieve": {"fetch", "get", "read"},
    "search": {"find", "lookup", "query"},
    "summary": {"summarize"},
    "summarize": {"summary"},
    "update": {"edit", "modify"},
}
SEARCH_OPERATION_TERMS = (
    set(SEARCH_SYNONYMS)
    | {term for synonyms in SEARCH_SYNONYMS.values() for term in synonyms}
    | {
        "about",
        "address",
        "by",
        "per",
        "respond",
        "response",
        "us",
        "our",
        "or",
        "answer",
        "answers",
        "answering",
        "answered",
        "full",
        "text",
        "name",
        "id",
        "identifier",
        "reference",
        "source",
        "section",
        "passage",
        "information",
        "info",
        "data",
        "fact",
        "definition",
        "definitions",
        "detail",
        "current",
        "latest",
        "today",
        "recent",
        "explain",
        "compare",
        "comparison",
        "discover",
        "lookup",
        "look",
        "check",
        "article",
        "book",
        "post",
        "posts",
        "thread",
        "threads",
        "comment",
        "comments",
        "page",
        "document",
        "paper",
        "papers",
        "research",
        "writing",
        "writings",
        "record",
        "item",
        "entry",
        "entity",
        "content",
        "authoritative",
        "official",
        "technical",
        "review",
        "proofread",
        "critique",
        "inspect",
        "audit",
        "troubleshoot",
        "debug",
        "web",
        "site",
        "website",
        "service",
        "services",
        "app",
        "apps",
        "project",
        "projects",
        "overview",
        "summary",
        "summarize",
        "introduction",
        "intro",
        "version",
        "versions",
        "profile",
        "profiles",
        "concept",
        "concepts",
        "person",
        "people",
        "national",
        "change",
        "changes",
        "history",
        "specific",
        "particular",
        "certain",
        "exact",
        "given",
        "cite",
        "cited",
        "citing",
        "citation",
        "citations",
    }
)
# Meta-descriptor nouns that mean "information about X" without naming any
# subject themselves. Unlike operation verbs (find/search/get/...), which can
# legitimately overlap between a query and a tool's own wording, these nouns
# carry no topical signal at all. BM25 has no catalog-wide document-frequency
# safeguard against a word that is merely rare within *this* catalog but is
# still generic in ordinary English -- e.g. "info" appearing in only one or
# two tool names/descriptions can otherwise earn an inflated IDF-driven score
# purely from local rarity. Excluding these (plus the plain function words in
# SEARCH_STOP_WORDS) from BM25 indexing/query tokenization keeps BM25's
# subject signal aligned with the "generic words cannot establish subject
# relevance" invariant without suppressing genuine operation-word overlap.
BM25_NOISE_TERMS = {
    "info",
    "information",
    "data",
    "detail",
    "details",
    "fact",
    "facts",
    "content",
}
BM25_GENERIC_TERMS = SEARCH_STOP_WORDS | BM25_NOISE_TERMS
RESOURCE_SCOPE_TERMS = {
    "codebase",
    "framework",
    "library",
    "package",
    "project",
    "repo",
    "repository",
    "source",
}
FREEFORM_REFERENCE_TERMS = {
    "question",
    "query",
    "prompt",
    "topic",
    "text",
}
REFERENCE_COMPANION_TERMS = {
    "owner",
    "org",
    "organization",
    "user",
    "account",
}
RESOURCE_INTENT_TERMS = {
    "api",
    "architecture",
    "backend",
    "code",
    "codebase",
    "component",
    "documentation",
    "frontend",
    "guide",
    "implementation",
    "internal",
    "internals",
    "manual",
    "reference",
    "source",
}
REPOSITORY_ACTIVITY_TERMS = {
    "commit",
    "issue",
    "pull",
    "release",
}
SENSITIVE_URL_QUERY_TERMS = {
    "apikey",
    "authorization",
    "code",
    "credential",
    "key",
    "password",
    "secret",
    "sig",
    "signature",
    "token",
}
MAX_SOURCE_URLS = 5
MAX_SOURCE_URL_CHARS = 3_000
DISCOVERY_OPERATION_TERMS = frozenset({"discover", "find", "list", "search"})
SOURCE_URL_TARGET_TERMS = frozenset(
    {
        "article",
        "document",
        "id",
        "item",
        "or",
        "page",
        "post",
        "record",
        "resource",
        "source",
        "target",
        "thread",
        "uri",
        "url",
    }
)
SOURCE_REFERENCE_TARGET_TERMS = SOURCE_URL_TARGET_TERMS | frozenset(
    {"identifier", "reference", "slug"}
)
SOURCE_REFERENCE_MARKERS = frozenset({"id", "identifier", "reference", "slug"})
STANDALONE_SOURCE_URL_RE = re.compile(
    r"(?m)^[ \t]*(https?://[^\s<>()]+)[ \t]*$",
    re.IGNORECASE,
)
UPSTREAM_OMISSION_PATTERNS = (
    (
        "explicit_truncation_marker",
        re.compile(
            r"\[(?:body|content|output|text)\s+truncated\]",
            re.IGNORECASE,
        ),
    ),
    (
        "collapsed_records_marker",
        re.compile(
            r"\[\+\d+\s+more\s+(?:comments?|items?|repl(?:y|ies)|results?)\]",
            re.IGNORECASE,
        ),
    ),
)
UPSTREAM_OMISSION_GUIDANCE = (
    "The upstream tool explicitly omitted source content. Artifact reads can "
    "inspect only the returned payload and cannot recover what the upstream "
    "tool left out. Never infer or reconstruct omitted text. If that exact "
    "material is required and the authorized schema offers a materially "
    "different raw, full, or detail mode, execute that mode once; otherwise "
    "disclose the limitation."
)
MAX_ARTIFACT_READ_RANGES = 128
ARTIFACT_THREADED_SCAN_CHARS = 100_000
ROUTING_INDEX_MAX_ENTRIES = 2_000
ROUTING_INDEX_MAX_CHARS = 250_000
RANKING_FEATURE_INDEX_MAX_CHARS = ROUTING_INDEX_MAX_CHARS
ROUTING_INDEX_TOTAL_CHARS = 2_000_000
ROUTING_INDEX_MAX_USERS = 100
ROUTING_MAX_QUERY_CHARS = 6_000
ROUTING_MAX_CANDIDATES = 100
ROUTING_INDEX_TTL = 3_600
BM25_K1 = 1.2
BM25_B = 0.75
BM25_SCORE_SCALE = 10.0
BM25_NAME_WEIGHT = 4.0
BM25_SERVER_WEIGHT = 3.0
BM25_PURPOSE_WEIGHT = 2.0
BM25_PARAMETER_WEIGHT = 1.0
BM25_SCHEMA_VALUE_WEIGHT = 0.5
BM25_NGRAM_WEIGHT = 0.2
BM25_NGRAM_SIZE = 4
BM25_MAX_TERMS_PER_DOCUMENT = 512
CATALOG_MAX_PENDING = 100
# Authenticated users retaining a catalog-warm cooldown record. A warm is
# attempted at most once per user per catalog TTL window, not once per message.
CATALOG_WARM_MAX_USERS = 100
# Backoff before retrying a catalog warm that could not load every accessible
# server within its preparation budget. Doubles per consecutive partial warm,
# capped at the catalog TTL, so an unreachable server settles at one retry per
# TTL window instead of one per message.
CATALOG_WARM_RETRY_SECONDS = 300
# A warm prepares every accessible server at once. The narrow fan-out used for
# targeted preparation would need one wave per four servers, which cannot finish
# a large catalog inside the preparation budget.
CATALOG_WARM_CONCURRENCY = 24
_RANKING_TERMS_CACHE: contextvars.ContextVar[Optional[dict[str, frozenset[str]]]] = (
    contextvars.ContextVar("_tool_broker_ranking_terms_cache", default=None)
)
_RANKING_STEMS_CACHE: contextvars.ContextVar[Optional[dict[str, frozenset[str]]]] = (
    contextvars.ContextVar("_tool_broker_ranking_stems_cache", default=None)
)
_RANKING_FEATURES_CACHE: contextvars.ContextVar[
    Optional[dict[tuple[str, str], dict]]
] = contextvars.ContextVar("_tool_broker_ranking_features_cache", default=None)
ROUTING_STOP_TERMS = {
    "how",
    "what",
    "which",
    "where",
    "when",
    "why",
    "who",
    "you",
    "your",
    "would",
    "could",
    "should",
    "does",
    "tell",
    "show",
    "give",
    "help",
    "need",
    "want",
    "request",
    "regarding",
    "previous",
    "user",
}
# Distinct accessible servers whose own vocabulary must already carry a term
# before it counts as catalog-wide background wording rather than a subject.
COMMON_SUBJECT_MIN_SERVERS = 3
# A subject must be at least this long before a single accessible server's use
# of it is treated as distinctive. Short words collide by accident.
DISTINCT_SUBJECT_MIN_CHARS = 5
ROUTING_EXPLICIT_LOOKUP_RE = re.compile(
    r"^\s*(?:(?:please|now)\s+|(?:can|could|would|will)\s+you\s+(?:please\s+)?)?"
    r"(?:search|find|fetch|retrieve|look\s+up|verify|research)\b",
    re.IGNORECASE,
)


def _user_id(user: Any) -> str:
    if isinstance(user, dict):
        value = user.get("id")
    else:
        value = getattr(user, "id", None)
    text = str(value).strip() if value is not None else ""
    return text or "anonymous"


def _serialize(value: Any) -> str:
    if isinstance(value, str):
        return value
    try:
        return json.dumps(value, ensure_ascii=False, separators=(",", ":"))
    except (TypeError, ValueError):
        return str(value)


def _diagnostics_key(user: Any, metadata: Optional[dict]) -> Optional[str]:
    user_id = _user_id(user)
    if user_id == "anonymous":
        return None
    meta = metadata if isinstance(metadata, dict) else {}
    chat_id = str(meta.get("chat_id") or "temporary")
    if len(chat_id) > 128:
        digest = hashlib.sha256(chat_id.encode("utf-8")).hexdigest()[:16]
        chat_id = f"{chat_id[:96]}:{digest}"
    return f"{user_id}:{chat_id}"


class Tools:
    _routing_protocol = 8

    class Valves(BaseModel):
        servers_json: str = Field(
            default="{}",
            description=(
                "Optional JSON allowlist mapping friendly aliases to existing Open WebUI MCP "
                'connection IDs, for example {"github":"github","files":"filesystem"}. '
                "Leave {} to discover all native MCP connections the user can access."
            ),
        )
        catalog_cache_seconds: int = Field(
            default=14_400,
            ge=0,
            description="Seconds to cache MCP schemas per user. Execution always rechecks access.",
        )
        maximum_catalog_chars: int = Field(
            default=1_000_000,
            ge=10_000,
            description="Maximum serialized characters cached for one MCP catalog.",
        )
        maximum_catalog_chars_per_user: int = Field(
            default=2_000_000,
            ge=10_000,
            description="Maximum serialized MCP catalog characters cached per user.",
        )
        maximum_catalog_chars_total: int = Field(
            default=20_000_000,
            ge=10_000,
            description="Maximum serialized MCP catalog characters cached across all users.",
        )
        maximum_catalogs_per_user: int = Field(
            default=50,
            ge=1,
            description="Maximum MCP catalogs cached per user.",
        )
        maximum_search_results: int = Field(
            default=3,
            ge=1,
            le=20,
            description="Maximum tools returned by one search.",
        )
        maximum_inventory_results: int = Field(
            default=50,
            ge=1,
            le=200,
            description="Maximum compact tool entries returned by one catalog inventory.",
        )
        maximum_search_calls_per_turn: int = Field(
            default=3,
            ge=1,
            description="Maximum tool searches allowed for one user message.",
        )
        maximum_execution_calls_per_turn: int = Field(
            default=18,
            ge=1,
            description="Maximum tool execution attempts allowed for one user message.",
        )
        maximum_total_calls_per_turn: int = Field(
            default=20,
            ge=1,
            description="Maximum combined broker searches and executions for one user message.",
        )
        maximum_artifact_reads_per_turn: int = Field(
            default=5,
            ge=1,
            description="Maximum artifact reads allowed for one user message.",
        )
        maximum_reads_per_artifact: int = Field(
            default=2,
            ge=1,
            description="Maximum reads allowed from one artifact per user message.",
        )
        mcp_timeout_seconds: int = Field(
            default=180,
            ge=30,
            le=600,
            description=(
                "Maximum seconds for an MCP connection or tool execution before "
                "the broker cancels it."
            ),
        )
        automatic_discovery_minimum_score: int = Field(
            default=85,
            ge=60,
            le=1_000,
            description="Conservative minimum relevance for automatic discovery, not a probability.",
        )
        automatic_discovery_hint_minimum_score: int = Field(
            default=55,
            ge=1,
            le=1_000,
            description=(
                "Minimum relevance for a non-forcing mid-confidence hint naming a "
                "candidate that did not clear automatic_discovery_minimum_score. "
                "Never triggers tool_choice forcing. Set at or above "
                "automatic_discovery_minimum_score to disable hints."
            ),
        )
        ranking_mode: str = Field(
            default="hybrid",
            description=(
                "Runtime MCP ranking mode: hybrid uses BM25 as the primary "
                "retriever with legacy corroboration/fallback, bm25 uses only "
                "BM25, and legacy is an emergency rollback mode."
            ),
        )
        minimum_diverse_match_score: int = Field(
            default=10,
            ge=1,
            description=(
                "Minimum relevance score required before reserving a result for a "
                "different source type."
            ),
        )
        minimum_secondary_score_ratio: float = Field(
            default=0.35,
            ge=0.05,
            le=1.0,
            description=(
                "Minimum fraction of the top score required for secondary "
                "search results. Cross-server results must also share matched "
                "subject evidence with the top result."
            ),
        )
        search_description_chars: int = Field(
            default=500,
            ge=100,
            description="Maximum description characters returned for each tool match.",
        )
        parameter_description_chars: int = Field(
            default=240,
            ge=80,
            description="Maximum description characters retained inside search-result schemas.",
        )
        maximum_search_schema_chars: int = Field(
            default=4_000,
            ge=500,
            description="Maximum serialized argument-schema characters retained per search match.",
        )
        maximum_search_properties: int = Field(
            default=50,
            ge=5,
            description="Maximum properties retained in each search-result schema object.",
        )
        maximum_search_enum_items: int = Field(
            default=25,
            ge=5,
            description="Maximum enum values retained in each search-result schema.",
        )
        maximum_search_schema_depth: int = Field(
            default=12,
            ge=3,
            description="Maximum nested depth retained in search-result schemas.",
        )
        maximum_inline_result_chars: int = Field(
            default=8_000,
            ge=2_000,
            description="Results above this size are stored temporarily and returned as a preview.",
        )
        artifact_preview_chars: int = Field(
            default=4_000,
            ge=1_000,
            description="Characters included in an externalized result preview.",
        )
        artifact_ttl_minutes: int = Field(
            default=60,
            ge=5,
            description="Minutes temporary full-result artifacts remain available.",
        )
        maximum_artifact_chars: int = Field(
            default=1_000_000,
            ge=10_000,
            description="Maximum characters retained in one temporary artifact.",
        )
        maximum_artifact_chars_per_user: int = Field(
            default=2_000_000,
            ge=10_000,
            description="Maximum total temporary artifact characters retained per user.",
        )
        maximum_artifact_chars_total: int = Field(
            default=20_000_000,
            ge=10_000,
            description="Maximum temporary artifact characters retained across all users.",
        )
        maximum_artifacts_per_user: int = Field(
            default=20,
            ge=1,
            description="Maximum temporary artifacts retained per user.",
        )
        show_status: bool = Field(
            default=True,
            description="Show status events while discovering and executing MCP tools.",
        )

    def __init__(self):
        self.valves = self.Valves()

    @contextlib.contextmanager
    def _ranking_cache_scope(self):
        terms_token = None
        stems_token = None
        if _RANKING_TERMS_CACHE.get() is None:
            terms_token = _RANKING_TERMS_CACHE.set({})
        if _RANKING_STEMS_CACHE.get() is None:
            stems_token = _RANKING_STEMS_CACHE.set({})
        try:
            yield
        finally:
            if stems_token is not None:
                _RANKING_STEMS_CACHE.reset(stems_token)
            if terms_token is not None:
                _RANKING_TERMS_CACHE.reset(terms_token)

    def _connection_enabled(self, connection: Optional[dict]) -> bool:
        if not isinstance(connection, dict):
            return False
        config = connection.get("config")
        return config is None or (
            isinstance(config, dict) and bool(config.get("enable", True))
        )

    def _connection_fingerprint(self, connection: dict) -> str:
        return hashlib.sha256(_serialize(connection).encode("utf-8")).hexdigest()

    async def _servers(self) -> tuple[dict[str, str], bool, dict[str, dict]]:
        try:
            configured = json.loads(self.valves.servers_json)
        except json.JSONDecodeError as error:
            raise ValueError(
                f"Tool Broker servers_json is invalid JSON: {error.msg}"
            ) from error

        if not isinstance(configured, dict):
            raise ValueError("Tool Broker servers_json must be a JSON object.")

        connections = await Config.get("tool_server.connections", [])
        connections_by_id = {
            str((connection.get("info") or {}).get("id")): connection
            for connection in connections
            if connection.get("type", "") == "mcp"
            and (connection.get("info") or {}).get("id")
        }
        servers = {}
        for raw_alias, raw_server_id in configured.items():
            alias = str(raw_alias).strip().lower()
            if not alias or not re.fullmatch(r"[a-z0-9_-]+", alias):
                raise ValueError(
                    "Tool Broker aliases may contain lowercase letters, numbers, underscores, and hyphens."
                )
            if alias in RESERVED_SERVER_ALIASES:
                raise ValueError(f"Tool Broker alias '{alias}' is reserved.")
            if not isinstance(raw_server_id, str) or not raw_server_id.strip():
                raise ValueError(
                    f"Tool Broker alias '{alias}' has an invalid MCP server ID."
                )

            server_id = raw_server_id.strip()
            if server_id.startswith("server:mcp:"):
                server_id = server_id[len("server:mcp:") :]
            servers[alias] = server_id
        if servers:
            return servers, True, connections_by_id

        for connection in connections:
            if connection.get("type", "") != "mcp" or not self._connection_enabled(
                connection
            ):
                continue
            info = connection.get("info") or {}
            server_id = str(info.get("id", "")).strip()
            if not server_id:
                continue

            label = str(info.get("name") or connection.get("name") or server_id).lower()
            alias_base = re.sub(r"[^a-z0-9_-]+", "-", label).strip("-_") or "mcp"
            alias = alias_base
            suffix = 2
            while alias in servers or alias in RESERVED_SERVER_ALIASES:
                alias = f"{alias_base}-{suffix}"
                suffix += 1
            servers[alias] = server_id
        return servers, False, connections_by_id

    def _prune_inactive_catalogs(
        self,
        request: Request,
        owner_id: str,
        active_server_ids: set[str],
    ) -> None:
        state = self._state(request)
        prefix = f"{owner_id}:"
        for key, catalog in list(state["catalogs"].items()):
            if catalog.get("owner_id") != owner_id and not key.startswith(prefix):
                continue
            if key.startswith(prefix) and key[len(prefix) :] not in active_server_ids:
                state["catalogs"].pop(key, None)

    def _state(self, request: Request) -> dict:
        state = getattr(request.app.state, STATE_KEY, None)
        if not isinstance(state, dict):
            state = {
                "catalogs": {},
                "artifacts": {},
            }
            setattr(request.app.state, STATE_KEY, state)
        now = time.time()
        state["catalogs"] = {
            key: value
            for key, value in state["catalogs"].items()
            if value.get("expires_at", 0) > now
        }
        state["artifacts"] = {
            key: value
            for key, value in state["artifacts"].items()
            if value.get("expires_at", 0) > now
        }
        indexes = state.setdefault("routing_indexes", {})
        for key, index in list(indexes.items()):
            if now - index.get("last_used", 0) >= ROUTING_INDEX_TTL:
                indexes.pop(key, None)
        return state

    def _usage_state(self, request: Request) -> dict:
        state = getattr(request.app.state, USAGE_STATE_KEY, None)
        if not isinstance(state, dict):
            state = {
                "lock": asyncio.Lock(),
                "records": {},
                "chat_mcp_locks": {},
            }
            setattr(request.app.state, USAGE_STATE_KEY, state)
        else:
            if not hasattr(state.get("lock"), "acquire"):
                state["lock"] = asyncio.Lock()
            if not isinstance(state.get("records"), dict):
                state["records"] = {}
            if not isinstance(state.get("chat_mcp_locks"), dict):
                state["chat_mcp_locks"] = {}
        return state

    @contextlib.asynccontextmanager
    async def _chat_mcp_write_lock(
        self,
        usage_state: dict,
        user_id: str,
        chat_id: str,
    ):
        key = self._usage_signature([user_id, str(chat_id)])
        async with usage_state["lock"]:
            locks = usage_state.setdefault("chat_mcp_locks", {})
            now = time.monotonic()
            for stale_key, record in list(locks.items()):
                lock = record.get("lock") if isinstance(record, dict) else None
                if (
                    stale_key != key
                    and isinstance(record, dict)
                    and int(record.get("ref_count", 0)) <= 0
                    and not getattr(lock, "locked", lambda: True)()
                    and (
                        len(locks) > CHAT_MCP_LOCK_MAX
                        or now - float(record.get("last_used", 0.0)) > USAGE_TTL_SECONDS
                    )
                ):
                    locks.pop(stale_key, None)
            record = locks.get(key)
            if not isinstance(record, dict) or not hasattr(
                record.get("lock"), "acquire"
            ):
                record = {"lock": asyncio.Lock(), "ref_count": 0, "last_used": now}
                locks[key] = record
            while len(locks) > CHAT_MCP_LOCK_MAX:
                removable = [
                    (lock_key, value)
                    for lock_key, value in locks.items()
                    if lock_key != key
                    and isinstance(value, dict)
                    and int(value.get("ref_count", 0)) <= 0
                    and hasattr(value.get("lock"), "locked")
                    and not value.get("lock").locked()
                ]
                if not removable:
                    break
                locks.pop(
                    min(removable, key=lambda item: item[1].get("last_used", 0.0))[0],
                    None,
                )
            record["ref_count"] = int(record.get("ref_count", 0)) + 1
            lock = record["lock"]
        acquired = False
        try:
            await lock.acquire()
            acquired = True
            yield
        finally:
            if acquired:
                lock.release()
            locks = usage_state.setdefault("chat_mcp_locks", {})
            record = locks.get(key)
            if isinstance(record, dict):
                record["ref_count"] = max(0, int(record.get("ref_count", 1)) - 1)
                record["last_used"] = time.monotonic()
                if (
                    len(locks) > CHAT_MCP_LOCK_MAX
                    and record["ref_count"] <= 0
                    and not record["lock"].locked()
                ):
                    locks.pop(key, None)

    def _ranking_diagnostics_state(self, request: Request) -> dict:
        state = getattr(request.app.state, RANKING_DIAGNOSTICS_STATE_KEY, None)
        if not isinstance(state, dict):
            state = {
                "lock": asyncio.Lock(),
                "records": {},
            }
            setattr(request.app.state, RANKING_DIAGNOSTICS_STATE_KEY, state)
        else:
            if not hasattr(state.get("lock"), "acquire"):
                state["lock"] = asyncio.Lock()
            if not isinstance(state.get("records"), dict):
                state["records"] = {}
        return state

    def _turn_usage_key(
        self,
        user: Any,
        metadata: Optional[dict],
    ) -> Optional[tuple[str, str]]:
        user_id = _user_id(user)
        if user_id == "anonymous":
            return None
        meta = metadata if isinstance(metadata, dict) else {}
        chat_id = meta.get("chat_id") or "missing-chat"
        message_id = meta.get("message_id") or "missing-message"
        raw_key = json.dumps(
            [user_id, str(chat_id), str(message_id)],
            ensure_ascii=False,
            separators=(",", ":"),
        )
        return user_id, hashlib.sha256(raw_key.encode("utf-8")).hexdigest()

    def _active_mcp_context_scope(
        self,
        user: Any,
        metadata: Optional[dict],
        tools: list[dict],
    ) -> Optional[str]:
        user_id = _user_id(user)
        chat_id = metadata.get("chat_id") if isinstance(metadata, dict) else None
        if user_id == "anonymous" or not chat_id or not tools:
            return None
        canonical = json.dumps(
            [
                ACTIVE_MCP_CONTEXT_PROTOCOL,
                user_id,
                str(chat_id),
                tools,
            ],
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()

    async def _owned_chat(
        self,
        user: Any,
        metadata: Optional[dict],
    ) -> Any:
        user_id = _user_id(user)
        chat_id = metadata.get("chat_id") if isinstance(metadata, dict) else None
        if user_id == "anonymous" or not chat_id:
            return None
        return await Chats.get_chat_by_id_and_user_id(
            str(chat_id),
            user_id,
        )

    def _chat_mcp_state(self, chat: Any) -> dict:
        chat_blob = getattr(chat, "chat", None)
        if not isinstance(chat_blob, dict):
            raise ValueError("The owned chat has no readable chat data.")
        raw = chat_blob.get(CHAT_MCP_STATE_KEY)
        if raw is None:
            return {
                "version": CHAT_MCP_STATE_VERSION,
                "tools": {},
                "activations": {},
            }
        if (
            not isinstance(raw, dict)
            or len(_serialize(raw).replace("<", "\\u003c")) > MAX_CHAT_MCP_STATE_CHARS
        ):
            raise ValueError("The chat's retained MCP state exceeds its size limit.")
        if (
            raw.get("version") != CHAT_MCP_STATE_VERSION
            or not isinstance(raw.get("tools"), dict)
            or not isinstance(raw.get("activations"), dict)
        ):
            raise ValueError("The chat contains malformed Tool Broker MCP state.")

        tools = {}
        for record_id, value in raw["tools"].items():
            if (
                not isinstance(record_id, str)
                or not record_id
                or not isinstance(value, dict)
                or not isinstance(value.get("tool"), str)
                or not value["tool"]
                or not isinstance(value.get("server"), str)
                or not value["server"]
                or not isinstance(value.get("server_id"), str)
                or not value["server_id"]
                or not isinstance(value.get("name"), str)
                or not value["name"]
                or value["tool"] != f"{value['server']}.{value['name']}"
                or record_id
                != self._usage_signature([value["tool"], value["server_id"]])
            ):
                raise ValueError("The chat contains an invalid retained MCP tool.")
            tools[record_id] = {
                "tool": value["tool"],
                "server": value["server"],
                "server_id": value["server_id"],
                "name": value["name"],
            }
        if len(tools) > MAX_CHAT_MCP_TOOLS:
            raise ValueError("The chat contains too many retained MCP tools.")

        activations = {}
        activation_count = 0
        for message_id, values in raw["activations"].items():
            if (
                not isinstance(message_id, str)
                or not message_id
                or not isinstance(values, list)
                or not values
            ):
                raise ValueError("The chat contains an invalid MCP activation.")
            normalized = []
            for record_id in values:
                if (
                    not isinstance(record_id, str)
                    or record_id not in tools
                    or record_id in normalized
                ):
                    raise ValueError("The chat contains an invalid MCP activation.")
                normalized.append(record_id)
            activation_count += len(normalized)
            activations[message_id] = normalized
        if activation_count > MAX_CHAT_MCP_ACTIVATIONS:
            overflow = activation_count - MAX_CHAT_MCP_ACTIVATIONS
            for message_id in list(activations):
                if overflow <= 0:
                    break
                values = activations[message_id]
                if overflow >= len(values):
                    overflow -= len(values)
                    activations.pop(message_id, None)
                else:
                    del values[:overflow]
                    overflow = 0
        return {
            "version": CHAT_MCP_STATE_VERSION,
            "tools": tools,
            "activations": activations,
            **(
                {"_trimmed_activations": True}
                if activation_count > MAX_CHAT_MCP_ACTIVATIONS
                else {}
            ),
        }

    def _active_chat_mcp_tools(
        self,
        chat: Any,
        metadata: Optional[dict],
    ) -> list[dict]:
        message_id = metadata.get("message_id") if isinstance(metadata, dict) else None
        if not message_id:
            return []
        state = self._chat_mcp_state(chat)
        chat_blob = getattr(chat, "chat", {})
        history = chat_blob.get("history")
        messages = history.get("messages") if isinstance(history, dict) else None
        messages = messages if isinstance(messages, dict) else {}
        cursor = str(message_id)
        visited = set()
        active_ids = set()
        active = []
        distance = 0
        while cursor and cursor not in visited:
            visited.add(cursor)
            for record_id in reversed(state["activations"].get(cursor, [])):
                value = state["tools"][record_id]
                tool = value["tool"]
                if tool in active_ids:
                    continue
                active_ids.add(tool)
                active.append(
                    {
                        **value,
                        "origin_message_id": cursor,
                        "origin_distance": distance,
                    }
                )
            message = messages.get(cursor)
            cursor = (
                str(message.get("parentId"))
                if isinstance(message, dict) and message.get("parentId")
                else ""
            )
            distance += 1
        active.reverse()
        return active

    async def _activate_persisted_mcp_tools(
        self,
        request: Request,
        user: Any,
        metadata: Optional[dict],
        tools: list[dict],
    ) -> tuple[int, Optional[str]]:
        chat_id = metadata.get("chat_id") if isinstance(metadata, dict) else None
        message_id = metadata.get("message_id") if isinstance(metadata, dict) else None
        if not chat_id or not message_id or not tools:
            return 0, None
        try:
            servers, _, _ = await self._servers()
        except ValueError as error:
            return 0, str(error)

        requested = []
        for value in tools:
            tool = value.get("tool") if isinstance(value, dict) else None
            server = value.get("server") if isinstance(value, dict) else None
            name = value.get("name") if isinstance(value, dict) else None
            if (
                not isinstance(tool, str)
                or not isinstance(server, str)
                or not isinstance(name, str)
                or tool != f"{server}.{name}"
                or server not in servers
            ):
                continue
            requested.append(
                {
                    "tool": tool,
                    "server": server,
                    "server_id": servers[server],
                    "name": name,
                }
            )
        if not requested:
            return 0, None

        usage_state = self._usage_state(request)
        usage_key = self._turn_usage_key(user, metadata)
        async with self._chat_mcp_write_lock(usage_state, _user_id(user), str(chat_id)):
            chat = await self._owned_chat(user, metadata)
            if chat is None:
                return (
                    0,
                    "The authenticated chat was unavailable for durable MCP retention.",
                )
            try:
                state = self._chat_mcp_state(chat)
            except ValueError as error:
                return 0, str(error)

            activation_id = str(message_id)
            trimmed = bool(state.pop("_trimmed_activations", False))
            activated = list(state["activations"].get(activation_id, []))
            activation_count = sum(
                len(values) for values in state["activations"].values()
            )
            added = 0
            retention_error = None
            for value in requested:
                tool = value["tool"]
                record_id = self._usage_signature([tool, value["server_id"]])
                if record_id in activated:
                    continue
                if activation_count >= MAX_CHAT_MCP_ACTIVATIONS:
                    retention_error = (
                        "The chat's retained MCP activation limit is exhausted."
                    )
                    break
                if record_id not in state["tools"]:
                    if len(state["tools"]) >= MAX_CHAT_MCP_TOOLS:
                        retention_error = (
                            "The chat's retained MCP tool limit is exhausted."
                        )
                        break
                    state["tools"][record_id] = value
                activated.append(record_id)
                activation_count += 1
                added += 1
            if not added and not trimmed:
                return 0, retention_error
            if activated:
                state["activations"][activation_id] = activated
            elif activation_id in state["activations"]:
                state["activations"].pop(activation_id, None)
            if (
                len(_serialize(state).replace("<", "\\u003c"))
                > MAX_CHAT_MCP_STATE_CHARS
            ):
                return 0, "The chat's retained MCP state character limit is exhausted."
            updated = await Chats.update_chat_by_id(
                str(chat_id),
                {CHAT_MCP_STATE_KEY: state},
                touch=False,
            )
            if updated is None:
                return 0, "Open WebUI did not persist the retained MCP tools."
        if usage_key is not None:
            async with usage_state["lock"]:
                usage_record = usage_state["records"].get(usage_key[1])
                if isinstance(usage_record, dict):
                    usage_record.pop("restored_mcp_schemas", None)
                    usage_record.pop("active_mcp_context_scope", None)
        return added, retention_error

    async def _active_persisted_mcp_schemas(
        self,
        request: Request,
        user: Any,
        metadata: Optional[dict],
        event_emitter=None,
        event_call=None,
    ) -> list[dict]:
        usage_key = self._turn_usage_key(user, metadata)
        usage_state = self._usage_state(request)
        if usage_key is not None:
            async with usage_state["lock"]:
                now = time.monotonic()
                record = usage_state["records"].get(usage_key[1])
                if (
                    isinstance(record, dict)
                    and now - record.get("last_used", 0.0) < USAGE_TTL_SECONDS
                    and "restored_mcp_schemas" in record
                ):
                    restored = record["restored_mcp_schemas"]
                    if isinstance(restored, list):
                        restored = [
                            {
                                **schema,
                                "description": self._bounded_text(
                                    schema.get("description", ""),
                                    self.valves.search_description_chars,
                                ),
                            }
                            for schema in restored
                            if isinstance(schema, dict)
                        ]
                        record["restored_mcp_schemas"] = restored
                        context_tools = sorted(
                            (
                                {
                                    **{
                                        key: value
                                        for key, value in schema.items()
                                        if key
                                        not in {
                                            "description",
                                            "origin_message_id",
                                            "origin_distance",
                                        }
                                    },
                                    "description": self._bounded_text(
                                        schema.get("description", ""),
                                        self.valves.search_description_chars,
                                    ),
                                }
                                for schema in restored
                                if isinstance(schema, dict)
                                and isinstance(schema.get("tool"), str)
                            ),
                            key=lambda item: item["tool"],
                        )
                        record["active_mcp_context_scope"] = (
                            self._active_mcp_context_scope(
                                user,
                                metadata,
                                context_tools,
                            )
                        )
                        return restored
                    return []
        chat = await self._owned_chat(user, metadata)
        if chat is None:
            return []
        try:
            handles = self._active_chat_mcp_tools(chat, metadata)
        except ValueError:
            log.exception("Tool Broker could not read retained chat MCP state.")
            return []
        if not handles:
            return []
        user_model = await Users.get_user_by_id(_user_id(user))
        if user_model is None:
            return []
        try:
            servers, _, connections_by_id = await self._servers()
        except ValueError:
            log.exception("Tool Broker could not resolve retained MCP servers.")
            return []

        active_server_ids = {
            server_id
            for server_id, connection in connections_by_id.items()
            if self._connection_enabled(connection)
        }
        self._prune_inactive_catalogs(
            request,
            str(user_model.id),
            active_server_ids,
        )
        catalogs = {}
        schemas = []
        for handle in handles:
            alias = handle["server"]
            server_id = servers.get(alias)
            connection = connections_by_id.get(server_id)
            if (
                server_id is None
                or server_id != handle["server_id"]
                or server_id not in active_server_ids
                or not self._connection_enabled(connection)
            ):
                continue
            if alias not in catalogs:
                try:
                    self._state(request)["catalogs"].pop(
                        f"{user_model.id}:{server_id}",
                        None,
                    )
                    catalogs[alias] = await self._catalog_for_server(
                        request,
                        alias,
                        server_id,
                        connection,
                        user_model,
                        user,
                        metadata,
                        event_emitter,
                        event_call,
                    )
                except PermissionError:
                    catalogs[alias] = []
                except Exception:
                    log.exception(
                        "Tool Broker could not revalidate retained MCP server '%s'.",
                        alias,
                    )
                    catalogs[alias] = []
            spec = next(
                (
                    item
                    for item in catalogs[alias]
                    if isinstance(item, dict) and item.get("name") == handle["name"]
                ),
                None,
            )
            if spec is None:
                continue
            parameters = spec.get("parameters", {"type": "object"})
            if not isinstance(parameters, dict):
                parameters = {"type": "object"}
            schema = {
                "server": alias,
                "tool": handle["tool"],
                "description": self._bounded_text(
                    str(spec.get("description", "") or ""),
                    self.valves.search_description_chars,
                ),
                "arguments": parameters,
                "origin_message_id": handle["origin_message_id"],
                "origin_distance": handle["origin_distance"],
            }
            try:
                json.dumps(schema, ensure_ascii=False, separators=(",", ":"))
            except (TypeError, ValueError):
                log.warning(
                    "Tool Broker omitted non-serializable retained MCP schema '%s'.",
                    handle["tool"],
                )
                continue
            schemas.append(schema)

        selected = []
        for schema in reversed(schemas):
            if len(selected) >= MAX_ACTIVE_MCP_SCHEMAS:
                break
            candidate = [
                {
                    key: value
                    for key, value in schema.items()
                    if key not in {"origin_message_id", "origin_distance"}
                },
                *[
                    {
                        key: value
                        for key, value in item.items()
                        if key not in {"origin_message_id", "origin_distance"}
                    }
                    for item in selected
                ],
            ]
            if (
                len(_serialize(candidate).replace("<", "\\u003c"))
                > MAX_ACTIVE_MCP_SCHEMA_CHARS
            ):
                continue
            selected.insert(0, schema)
        await self._record_discovered_tools(
            request,
            user,
            metadata,
            {schema["tool"] for schema in selected},
        )
        if usage_key is not None:
            async with usage_state["lock"]:
                now = time.monotonic()
                record = self._usage_record(
                    usage_state["records"],
                    usage_key[0],
                    usage_key[1],
                    now,
                )
                record["restored_mcp_schemas"] = selected
                context_tools = sorted(
                    (
                        {
                            key: value
                            for key, value in schema.items()
                            if key
                            not in {
                                "origin_message_id",
                                "origin_distance",
                            }
                        }
                        for schema in selected
                    ),
                    key=lambda item: item["tool"],
                )
                record["active_mcp_context_scope"] = self._active_mcp_context_scope(
                    user,
                    metadata,
                    context_tools,
                )
        return selected

    def _prune_usage_records(
        self,
        records: dict,
        now: float,
        user_id: str,
        current_key: str,
    ) -> None:
        for key, record in list(records.items()):
            if now - record.get("last_used", 0.0) >= USAGE_TTL_SECONDS:
                records.pop(key, None)

        user_records = sorted(
            (
                (key, record)
                for key, record in records.items()
                if record.get("user_id") == user_id and key != current_key
            ),
            key=lambda item: item[1].get("last_used", 0.0),
        )
        while len(user_records) >= MAX_USAGE_RECORDS_PER_USER:
            key, _ = user_records.pop(0)
            records.pop(key, None)

        while len(records) >= MAX_USAGE_RECORDS_GLOBAL and current_key not in records:
            oldest_key = min(
                records,
                key=lambda key: records[key].get("last_used", 0.0),
            )
            records.pop(oldest_key, None)

    def _usage_record(
        self,
        records: dict,
        user_id: str,
        key: str,
        now: float,
    ) -> dict:
        record = records.get(key)
        if not isinstance(record, dict):
            record = {
                "user_id": user_id,
                "last_used": now,
                "total_calls": 0,
                "search_calls": 0,
                "execution_calls": 0,
                "search_signatures": set(),
                "execution_signatures": set(),
                "discovered_tools": set(),
                "emitted_source_urls": set(),
                "artifact_read_calls": 0,
                "artifact_reads_by_id": {},
                "failed_artifact_ids": set(),
            }
            records[key] = record
        for count_key in (
            "total_calls",
            "search_calls",
            "execution_calls",
            "artifact_read_calls",
        ):
            if not isinstance(record.get(count_key), int):
                record[count_key] = 0
        for set_key in (
            "search_signatures",
            "execution_signatures",
            "discovered_tools",
            "emitted_source_urls",
            "failed_artifact_ids",
        ):
            if not isinstance(record.get(set_key), set):
                record[set_key] = set()
        if not isinstance(record.get("artifact_reads_by_id"), dict):
            record["artifact_reads_by_id"] = {}
        if not isinstance(record.get("artifact_read_ranges"), dict):
            record["artifact_read_ranges"] = {}
        if not isinstance(record.get("source_evidence_levels"), dict):
            record["source_evidence_levels"] = {}
        record["user_id"] = user_id
        record["last_used"] = now
        return record

    @staticmethod
    def _usage_signature(value: Any) -> str:
        try:
            serialized = json.dumps(
                value,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            )
        except (TypeError, ValueError):
            serialized = str(value)
        return hashlib.sha256(serialized.encode("utf-8")).hexdigest()

    async def _record_discovered_tools(
        self,
        request: Request,
        user: Any,
        metadata: Optional[dict],
        tools: set[str],
    ) -> None:
        usage_key = self._turn_usage_key(user, metadata)
        if usage_key is None or not tools:
            return
        user_id, key = usage_key
        state = self._usage_state(request)
        now = time.monotonic()
        async with state["lock"]:
            records = state["records"]
            self._prune_usage_records(records, now, user_id, key)
            record = self._usage_record(records, user_id, key, now)
            record["discovered_tools"].update(tools)

    async def _was_tool_discovered(
        self,
        request: Request,
        user: Any,
        metadata: Optional[dict],
        tool: str,
    ) -> bool:
        usage_key = self._turn_usage_key(user, metadata)
        if usage_key is None:
            return False
        _, key = usage_key
        state = self._usage_state(request)
        async with state["lock"]:
            record = state["records"].get(key)
            return (
                isinstance(record, dict)
                and isinstance(record.get("discovered_tools"), set)
                and tool in record["discovered_tools"]
            )

    async def _reserve_usage(
        self,
        request: Request,
        user: Any,
        metadata: Optional[dict],
        category: str,
        signature_value: Any,
    ) -> Optional[str]:
        usage_key = self._turn_usage_key(user, metadata)
        if usage_key is None:
            return None
        user_id, key = usage_key
        state = self._usage_state(request)
        now = time.monotonic()
        signature = self._usage_signature(signature_value)
        count_key = f"{category}_calls"
        signatures_key = f"{category}_signatures"
        category_limit = (
            self.valves.maximum_search_calls_per_turn
            if category == "search"
            else self.valves.maximum_execution_calls_per_turn
        )

        async with state["lock"]:
            records = state["records"]
            self._prune_usage_records(records, now, user_id, key)
            record = self._usage_record(records, user_id, key, now)
            if record["total_calls"] >= self.valves.maximum_total_calls_per_turn:
                return (
                    "Error: total Tool Broker call budget exhausted for this "
                    "user message. Mandatory stop: do not call either broker tool "
                    "again. Answer now from the evidence already collected and "
                    "disclose any remaining limitation."
                )

            record["total_calls"] += 1
            if record[count_key] >= category_limit:
                if category == "search":
                    recovery = (
                        "Do not search again; use a tool already returned by "
                        "search_tools or answer without another Tool Broker call."
                    )
                else:
                    recovery = (
                        "Mandatory stop: do not execute another tool. Answer "
                        "now from the evidence already collected and disclose any "
                        "remaining limitation."
                    )
                return (
                    f"Error: Tool Broker {category} limit exhausted for this "
                    f"user message. {recovery}"
                )

            record[count_key] += 1
            if signature in record[signatures_key]:
                return (
                    f"Error: duplicate Tool Broker {category} rejected for "
                    "this user message. Do not repeat equivalent calls. Use the "
                    "earlier result and continue the task."
                )
            record[signatures_key].add(signature)
        return None

    async def _covered_automatic_search(
        self,
        request: Request,
        user: Any,
        metadata: Optional[dict],
        query: str,
        limit: int,
    ) -> Optional[str]:
        usage_key = self._turn_usage_key(user, metadata)
        if usage_key is None:
            return None
        _, key = usage_key
        state = self._usage_state(request)
        async with state["lock"]:
            record = state["records"].get(key)
            routing = (
                record.get("automatic_routing") if isinstance(record, dict) else None
            )
            future = routing.get("future") if isinstance(routing, dict) else None
            if (
                not isinstance(routing, dict)
                or routing.get("coverage") != "complete"
                or not isinstance(future, asyncio.Future)
                or not future.done()
                or future.cancelled()
            ):
                return None
            result = future.result()
        if not isinstance(result, dict) or result.get("status") != "discovered":
            return None
        try:
            matches = json.loads(result["response"]).get("matches", [])
        except (KeyError, TypeError, ValueError, json.JSONDecodeError):
            return None
        requested_operations = (
            self._ranking_terms(query) - SEARCH_STOP_WORDS - ROUTING_STOP_TERMS
        ) & SEARCH_OPERATION_TERMS
        requested_subjects = (
            self._ranking_terms(self._ranking_context(query))
            - SEARCH_STOP_WORDS
            - SEARCH_OPERATION_TERMS
            - RESOURCE_INTENT_TERMS
            - ROUTING_STOP_TERMS
            - REPOSITORY_ACTIVITY_TERMS
        )
        non_numeric_subjects = {
            subject for subject in requested_subjects if not subject.isdigit()
        }
        if non_numeric_subjects:
            requested_subjects = non_numeric_subjects
        scored = []
        for match in matches:
            if not isinstance(match, dict):
                continue
            tool = match.get("tool")
            alias = match.get("server")
            if not isinstance(tool, str) or not isinstance(alias, str):
                continue
            prefix = f"{alias}."
            name = tool[len(prefix) :] if tool.startswith(prefix) else tool
            spec = {
                "name": name,
                "description": match.get("description", ""),
                "parameters": match.get("arguments", {"type": "object"}),
            }
            score = self._score(query, alias, spec)
            if score:
                declared_terms = self._ranking_terms(
                    " ".join(
                        (
                            alias,
                            name,
                            str(spec.get("description", "")),
                        )
                    )
                )
                operation_evidence = {
                    operation
                    for operation in requested_operations
                    if declared_terms.intersection(
                        {operation} | SEARCH_SYNONYMS.get(operation, set())
                    )
                    or any(
                        self._ranking_stems(operation) & self._ranking_stems(term)
                        for term in declared_terms
                    )
                }
                scored.append(
                    {
                        "score": score,
                        "server": alias,
                        "tool": tool,
                        "_operation_evidence": sorted(operation_evidence),
                        "_subject_evidence": sorted(
                            self._matched_subject_evidence(query, alias, spec)
                        ),
                    }
                )
        try:
            requested_limit = int(limit)
        except (TypeError, ValueError):
            requested_limit = self.valves.maximum_search_results
        selected = self._select_search_matches(
            scored,
            min(self.valves.maximum_search_results, max(1, requested_limit)),
        )
        covering = [
            item
            for item in selected
            if requested_subjects.intersection(item["_subject_evidence"])
            and (not requested_operations or item["_operation_evidence"])
        ]
        if (
            not selected
            or not requested_subjects
            or not covering
            or covering[0]["score"] < self.valves.automatic_discovery_minimum_score
        ):
            return None
        tools = [item["tool"] for item in covering]
        return "Error: " + _serialize(
            {
                "status": "blocked",
                "error": (
                    "Explicit search is already covered by complete automatic "
                    "discovery for this user message."
                ),
                "covered_tools": tools,
                "retry_allowed": False,
                "next_action": "use_automatic_discovery_results",
            }
        )

    async def _reserve_artifact_read(
        self,
        request: Request,
        user: Any,
        metadata: Optional[dict],
        artifact_id: str,
    ) -> Optional[str]:
        usage_key = self._turn_usage_key(user, metadata)
        if usage_key is None:
            return None
        user_id, key = usage_key
        state = self._usage_state(request)
        now = time.monotonic()
        async with state["lock"]:
            records = state["records"]
            self._prune_usage_records(records, now, user_id, key)
            record = self._usage_record(records, user_id, key, now)
            artifact_reads = record["artifact_reads_by_id"]
            artifact_count = artifact_reads.get(artifact_id, 0)
            failed_before = artifact_id in record["failed_artifact_ids"]
            if (
                record["artifact_read_calls"]
                >= self.valves.maximum_artifact_reads_per_turn
            ):
                return "Error: " + _serialize(
                    {
                        "status": "blocked",
                        "error": "artifact read limit exhausted for this user message.",
                        "retry_allowed": False,
                        "next_action": "answer_from_existing_evidence",
                        "guidance": (
                            "Use the preview and chunks already retrieved; disclose any "
                            "remaining evidence limitation."
                        ),
                    }
                )
            if artifact_count >= self.valves.maximum_reads_per_artifact:
                return "Error: " + _serialize(
                    {
                        "status": "blocked",
                        "error": f"read limit exhausted for artifact '{artifact_id}'.",
                        "retry_allowed": False,
                        "next_action": "use_existing_artifact_evidence",
                        "guidance": (
                            "Use its preview and existing chunks instead of paging "
                            "through the artifact."
                        ),
                    }
                )
            record["artifact_read_calls"] += 1
            artifact_reads[artifact_id] = artifact_count + 1
            if failed_before:
                return "Error: " + _serialize(
                    {
                        "status": "blocked",
                        "error": (
                            "This artifact identifier already failed in this user "
                            "message and must not be retried."
                        ),
                        "retry_allowed": False,
                        "next_action": "use_existing_evidence_or_correct_identifier",
                        "guidance": (
                            "Reuse the exact artifact_id from the successful "
                            "externalized result. Changing query or offset arguments "
                            "cannot make a missing, expired, or unowned artifact valid."
                        ),
                    }
                )
        return None

    async def _emit_source_event(
        self,
        request: Request,
        user: Any,
        metadata: Optional[dict],
        event_emitter,
        source: dict,
        level: int,
    ) -> bool:
        usage_key = self._turn_usage_key(user, metadata)
        if usage_key is None or event_emitter is None:
            return False
        user_id, key = usage_key
        state = self._usage_state(request)
        async with state["lock"]:
            now = time.monotonic()
            records = state["records"]
            self._prune_usage_records(records, now, user_id, key)
            record = self._usage_record(records, user_id, key, now)
            url = source["source"]["id"]
            levels = record["source_evidence_levels"]
            previous = levels.get(url, 1 if url in record["emitted_source_urls"] else 0)
            if previous >= level:
                return False
            try:
                await asyncio.wait_for(
                    event_emitter({"type": "source", "data": source}),
                    timeout=5,
                )
            except Exception:
                log.exception("Tool Broker could not emit source metadata.")
                return False
            levels[url] = level
            record["emitted_source_urls"].add(url)
        return True

    def _cache_catalog(
        self,
        state: dict,
        cache_key: str,
        owner_id: str,
        specs: list[dict],
        connection_fingerprint: str = "",
    ) -> None:
        characters = len(_serialize(specs))
        catalog_limit = min(
            self.valves.maximum_catalog_chars,
            self.valves.maximum_catalog_chars_per_user,
            self.valves.maximum_catalog_chars_total,
        )
        if characters > catalog_limit or self.valves.catalog_cache_seconds <= 0:
            state["catalogs"].pop(cache_key, None)
            return

        user_catalogs = sorted(
            (
                (key, value)
                for key, value in state["catalogs"].items()
                if key != cache_key
                and (
                    value.get("owner_id") == owner_id or key.startswith(f"{owner_id}:")
                )
            ),
            key=lambda item: item[1].get("last_accessed_at", 0),
        )
        user_characters = sum(
            item.get("characters", len(_serialize(item.get("specs", []))))
            for _, item in user_catalogs
        )
        while user_catalogs and (
            len(user_catalogs) >= self.valves.maximum_catalogs_per_user
            or user_characters + characters > self.valves.maximum_catalog_chars_per_user
        ):
            oldest_key, oldest = user_catalogs.pop(0)
            user_characters -= oldest.get(
                "characters",
                len(_serialize(oldest.get("specs", []))),
            )
            state["catalogs"].pop(oldest_key, None)

        all_catalogs = sorted(
            (
                (key, value)
                for key, value in state["catalogs"].items()
                if key != cache_key
            ),
            key=lambda item: item[1].get("last_accessed_at", 0),
        )
        total_characters = sum(
            item.get("characters", len(_serialize(item.get("specs", []))))
            for _, item in all_catalogs
        )
        while all_catalogs and (
            total_characters + characters > self.valves.maximum_catalog_chars_total
        ):
            oldest_key, oldest = all_catalogs.pop(0)
            total_characters -= oldest.get(
                "characters",
                len(_serialize(oldest.get("specs", []))),
            )
            state["catalogs"].pop(oldest_key, None)

        now = time.time()
        state["catalogs"][cache_key] = {
            "owner_id": owner_id,
            "specs": specs,
            "characters": characters,
            "connection_fingerprint": connection_fingerprint,
            "last_accessed_at": now,
            "expires_at": now + self.valves.catalog_cache_seconds,
            "routing_revision": uuid4().hex,
        }

    async def _emit_status(self, event_emitter, description: str, done: bool) -> None:
        if self.valves.show_status and event_emitter is not None:
            await event_emitter(
                {
                    "type": "status",
                    "data": {
                        "description": description,
                        "done": done,
                        "hidden": False,
                    },
                }
            )

    async def _emit_error(self, event_emitter, content: str) -> None:
        if event_emitter is not None:
            await event_emitter(
                {
                    "type": "notification",
                    "data": {
                        "type": "error",
                        "content": content,
                    },
                }
            )

    async def _emit_source_events(
        self,
        request: Request,
        user: Any,
        metadata: Optional[dict],
        event_emitter,
        tool: str,
        arguments: dict,
        result: Any,
    ) -> int:
        if event_emitter is None:
            return 0
        text = _serialize(result)
        if isinstance(result, str) and result.strip().lower().startswith(
            ("error:", "exception:", "traceback")
        ):
            return 0
        parsed = result
        while isinstance(parsed, str):
            try:
                parsed = json.loads(parsed)
            except (TypeError, ValueError, json.JSONDecodeError):
                break
        if isinstance(parsed, dict) and (
            parsed.get("error")
            or parsed.get("isError")
            or str(parsed.get("status", "")).lower() in {"error", "failed"}
            or parsed.get("success") is False
            or parsed.get("ok") is False
        ):
            return 0
        primary_url = self._retrieval_source_url(tool, arguments, text)
        if primary_url is None:
            return 0
        sources = self._source_records(text, primary_url)
        complete = not self._upstream_omission_reasons(result, text)
        emitted = 0
        for source in sources:
            url = source["url"]
            emitted += await self._emit_source_event(
                request,
                user,
                metadata,
                event_emitter,
                {
                    "source": {
                        "id": url,
                        "name": source["title"],
                        "type": "web",
                    },
                    "document": [source["content"]],
                    "metadata": [
                        {
                            "source": url,
                            "name": source["title"],
                            "url": url,
                            "evidence_type": "retrieved_page",
                            "content_complete": complete,
                        }
                    ],
                },
                3 if complete else 2,
            )
        return emitted

    @staticmethod
    def _consume_mcp_lifecycle_result(task: asyncio.Task) -> None:
        try:
            task.result()
        except asyncio.CancelledError:
            return
        except Exception as error:
            log.debug("MCP lifecycle task finished after cancellation: %s", error)

    async def _cancel_mcp_lifecycle(self, task: asyncio.Task, reason: str) -> None:
        if not task.done():
            task.cancel()
        done, pending = await asyncio.wait(
            {task},
            timeout=MCP_DISCONNECT_TIMEOUT_SECONDS,
        )
        if task in done:
            self._consume_mcp_lifecycle_result(task)
            return
        if pending:
            log.warning(
                "Tool Broker MCP lifecycle cleanup exceeded %s seconds after %s.",
                MCP_DISCONNECT_TIMEOUT_SECONDS,
                reason,
            )
            task.add_done_callback(self._consume_mcp_lifecycle_result)

    async def _disconnect(self, client) -> None:
        try:
            await client.disconnect()
        except asyncio.CancelledError:
            raise
        except Exception as error:
            log.warning("Tool Broker could not disconnect an MCP client: %s", error)

    async def _connect_client(
        self,
        request: Request,
        server_id: str,
        user_model: Any,
        user_context: Any,
        metadata: Optional[dict],
        event_emitter,
        event_call,
        client,
    ):
        meta = metadata if isinstance(metadata, dict) else {}
        extra_params = {
            "__request__": request,
            "__user__": (
                user_context
                if isinstance(user_context, dict)
                else {"id": user_model.id}
            ),
            "__metadata__": meta,
            "__event_emitter__": event_emitter,
            "__event_call__": event_call,
        }
        connections = await Config.get("tool_server.connections", [])
        connection = next(
            (
                item
                for item in connections
                if item.get("type") == "mcp"
                and (item.get("info") or {}).get("id") == server_id
            ),
            None,
        )
        if not self._connection_enabled(connection) or not await has_connection_access(
            user_model,
            connection,
        ):
            return None
        header_kwargs = {"server_id": server_id, "metadata": meta}
        if "extra_params" in inspect.signature(build_tool_server_headers).parameters:
            header_kwargs["extra_params"] = extra_params
        headers, _ = await build_tool_server_headers(
            connection,
            request,
            user_model,
            **header_kwargs,
        )
        await client.connect(url=connection.get("url", ""), headers=headers or None)
        specs = await client.list_tool_specs()
        filters = (connection.get("config") or {}).get("function_name_filter_list", "")
        if isinstance(filters, str):
            filters = filters.split(",")
        if filters:
            specs = [spec for spec in specs if is_string_allowed(spec["name"], filters)]
        return specs

    async def _run_mcp_lifecycle(
        self,
        request: Request,
        server_id: str,
        user_model: Any,
        user_context: Any,
        metadata: Optional[dict],
        event_emitter,
        event_call,
        operation,
    ):
        async def runner():
            client = MCPClient()
            try:
                specs = await self._connect_client(
                    request,
                    server_id,
                    user_model,
                    user_context,
                    metadata,
                    event_emitter,
                    event_call,
                    client,
                )
                if specs is None:
                    return None
                return await operation(client, specs)
            finally:
                await self._disconnect(client)

        task = asyncio.create_task(runner())
        try:
            done, _ = await asyncio.wait(
                {task},
                timeout=self.valves.mcp_timeout_seconds,
            )
            if task in done:
                if task.cancelled():
                    current = asyncio.current_task()
                    cancelling = (
                        current.cancelling()
                        if current is not None and hasattr(current, "cancelling")
                        else 0
                    )
                    if cancelling:
                        raise asyncio.CancelledError
                    raise RuntimeError("MCP operation was cancelled internally")
                return task.result()
            await self._cancel_mcp_lifecycle(task, "timeout")
            raise TimeoutError(
                "MCP operation timed out after "
                f"{self.valves.mcp_timeout_seconds} seconds."
            )
        except asyncio.CancelledError:
            await self._cancel_mcp_lifecycle(task, "cancellation")
            raise
        except asyncio.TimeoutError as error:
            raise TimeoutError(
                "MCP operation timed out after "
                f"{self.valves.mcp_timeout_seconds} seconds."
            ) from error

    async def _catalog_for_server(
        self,
        request: Request,
        alias: str,
        server_id: str,
        connection: Optional[dict],
        user_model: Any,
        user_context: Any,
        metadata: Optional[dict],
        event_emitter,
        event_call,
    ) -> list[dict]:
        state = self._state(request)
        cache_key = f"{user_model.id}:{server_id}"
        if not self._connection_enabled(connection) or not await has_connection_access(
            user_model,
            connection,
        ):
            state["catalogs"].pop(cache_key, None)
            raise PermissionError(
                f"MCP server '{alias}' is unavailable or access was denied."
            )

        connection_fingerprint = self._connection_fingerprint(connection)
        cached = state["catalogs"].get(cache_key)
        if cached and cached.get("connection_fingerprint") == connection_fingerprint:
            cached["last_accessed_at"] = time.time()
            return cached["specs"]
        if cached:
            state["catalogs"].pop(cache_key, None)

        pending = state.setdefault("catalog_pending", {})
        pending_key = (cache_key, connection_fingerprint)
        if pending_key in pending:
            specs, error = await asyncio.shield(pending[pending_key])
            if error:
                raise RuntimeError(
                    "Concurrent MCP catalog preparation failed."
                ) from error
            # Access or configuration may have changed while waiting.
            if not await has_connection_access(user_model, connection):
                state["catalogs"].pop(cache_key, None)
                raise PermissionError("MCP access changed during catalog preparation.")
            return specs
        if (
            len(pending) >= CATALOG_MAX_PENDING
            or sum(key[0].startswith(f"{user_model.id}:") for key in pending) >= 50
        ):
            raise RuntimeError("MCP catalog preparation concurrency limit reached.")
        future = asyncio.get_running_loop().create_future()
        pending[pending_key] = future
        try:
            specs = await self._load_catalog(
                request,
                alias,
                cache_key,
                server_id,
                connection_fingerprint,
                user_model,
                user_context,
                metadata,
                event_emitter,
                event_call,
            )
            if not await has_connection_access(user_model, connection):
                state["catalogs"].pop(cache_key, None)
                raise PermissionError("MCP access changed during catalog preparation.")
            future.set_result((specs, None))
            return specs
        except BaseException as error:
            future.set_result((None, error))
            raise
        finally:
            pending.pop(pending_key, None)

    async def _load_catalog(
        self,
        request,
        alias,
        cache_key,
        server_id,
        connection_fingerprint,
        user_model,
        user_context,
        metadata,
        event_emitter,
        event_call,
    ) -> list[dict]:
        async def list_specs(_client, specs):
            return specs

        specs = await self._run_mcp_lifecycle(
            request,
            server_id,
            user_model,
            user_context,
            metadata,
            event_emitter,
            event_call,
            list_specs,
        )
        if specs is None:
            raise PermissionError(
                f"MCP server '{alias}' is unavailable or access was denied."
            )

        self._cache_catalog(
            self._state(request),
            cache_key,
            str(user_model.id),
            specs,
            connection_fingerprint,
        )
        return specs

    @staticmethod
    def _ranking_terms(text: str) -> set[str]:
        cache = _RANKING_TERMS_CACHE.get()
        if cache is not None:
            cached = cache.get(text)
            if cached is not None:
                return set(cached)
        normalized_text = re.sub(r"(?<=[a-z0-9])['\u2019]s\b", "", text.lower())
        terms = set(re.findall(SEARCH_TOKEN_RE, normalized_text))
        if "docs" in terms:
            terms.remove("docs")
            terms.add("documentation")
        normalized = set()
        for term in terms:
            if any(character in term for character in ".+#"):
                # Compound technical tokens (e.g. "next.js", "c#") are not
                # ordinary plural nouns; normalizing them risks corrupting
                # the identifier (e.g. "next.js" -> "next.j").
                normalized.add(term)
            elif term in {"news", "series", "species"}:
                normalized.add(term)
            elif len(term) > 4 and term.endswith("ies"):
                normalized.add(term[:-3] + "y")
            elif (
                len(term) > 3
                and term.endswith("s")
                and not term.endswith(("ss", "us", "is"))
            ):
                normalized.add(term[:-1])
            else:
                normalized.add(term)
        if cache is not None:
            cache[text] = frozenset(normalized)
        return normalized

    @staticmethod
    def _parameter_words(name: str) -> set[str]:
        separated = re.sub(r"([a-z0-9])([A-Z])", r"\1 \2", name)
        return set(re.findall(r"[a-z0-9]+", separated.lower()))

    def _missing_reference_arguments(self, query: str, spec: dict) -> bool:
        parameters = spec.get("parameters")
        if not isinstance(parameters, dict):
            return False
        properties = parameters.get("properties")
        required = parameters.get("required")
        if not isinstance(properties, dict) or not isinstance(required, list):
            return False
        for name in required:
            if not isinstance(name, str):
                continue
            schema = properties.get(name)
            if not isinstance(schema, dict):
                continue
            words = self._parameter_words(name)
            items = schema.get("items")
            value_schema = items if isinstance(items, dict) else schema
            reference_description = " ".join(
                str(candidate.get("description", ""))
                for candidate in (schema, value_schema)
                if isinstance(candidate, dict)
            )
            described_reference = bool(
                re.search(
                    r"\b(?:[a-z]{1,12}[- ]?ids?|external identifiers?|"
                    r"record identifiers?|identifier values?)\b",
                    reference_description,
                    re.IGNORECASE,
                )
            )
            qualified_number = bool(
                words.intersection({"number", "numbers"})
                and words - {"number", "numbers"}
            )
            if words.intersection(
                {"url", "urls", "uri", "uris", "href"}
            ) or value_schema.get("format") in ("uri", "url"):
                if not re.search(r"https?://[^\s<>\"]+", query, re.IGNORECASE):
                    return True
            elif (
                words.intersection(
                    {
                        "id",
                        "ids",
                        "identifier",
                        "identifiers",
                        "uuid",
                        "sha",
                        "shas",
                        "hash",
                        "hashes",
                    }
                )
                or value_schema.get("format") == "uuid"
                or qualified_number
                or described_reference
            ):
                # A bare number could be a year, portion size, or count. Only
                # explicitly labeled IDs (or UUIDs) count as supplied references.
                uuids = re.findall(
                    r"\b[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}\b",
                    query,
                    re.IGNORECASE,
                )
                generic_reference_words = {
                    "id",
                    "ids",
                    "identifier",
                    "identifiers",
                    "uuid",
                    "sha",
                    "shas",
                    "hash",
                    "hashes",
                    "number",
                    "numbers",
                }
                separated_name = re.sub(r"([a-z0-9])([A-Z])", r"\1 \2", name)
                separated_name = re.sub(r"[_-]+", " ", separated_name).strip()
                semantic_labels = (
                    words - generic_reference_words if qualified_number else set()
                )
                labels = "|".join(
                    re.escape(label)
                    for label in sorted(
                        {
                            name,
                            separated_name,
                            *semantic_labels,
                            "id",
                            "ids",
                            "identifier",
                            "identifiers",
                            "uuid",
                            "sha",
                            "shas",
                            "hash",
                            "hashes",
                            "commit",
                        },
                        key=lambda label: (-len(label), label),
                    )
                )
                labeled = re.finditer(
                    rf"\b(?:{labels})\s*(?:[:=]\s*|\s+)"
                    r"[\"'\[]?([a-z0-9][a-z0-9_.:/-]*(?:\s*,\s*[a-z0-9][a-z0-9_.:/-]*)*)",
                    query,
                    re.IGNORECASE,
                )
                values = [
                    value
                    for match in labeled
                    for value in re.split(r"\s*,\s*", match.group(1))
                ]
                semantic_reference_values = []
                semantic_reference_labels = words - generic_reference_words
                if semantic_reference_labels and not qualified_number:
                    semantic_matches = re.finditer(
                        rf"\b(?:{'|'.join(map(re.escape, semantic_reference_labels))})"
                        r"\s*(?:[:=]\s*|\s+)"
                        r"[\"'\[]?([a-z0-9][a-z0-9_.:/-]*)",
                        query,
                        re.IGNORECASE,
                    )
                    semantic_reference_values = [
                        match.group(1)
                        for match in semantic_matches
                        if (
                            any(character.isdigit() for character in match.group(1))
                            or any(
                                separator in match.group(1)
                                for separator in ("_", ".", ":", "/")
                            )
                        )
                    ]
                description_prefixes = {
                    prefix.lower()
                    for prefix, _ in re.findall(
                        r"(?<![a-z0-9])([a-z]{1,12})[-_:]?(\d+)\b",
                        str(schema.get("description", ""))[:1_000],
                        re.IGNORECASE,
                    )
                }
                described_prefixed_values = [
                    value
                    for value in re.findall(
                        r"(?<![a-z0-9])[a-z]{1,12}[-_:]?\d+\b",
                        query,
                        re.IGNORECASE,
                    )
                    if re.match(r"[a-z]+", value, re.IGNORECASE).group(0).lower()
                    in description_prefixes
                ]
                prefixes = words - generic_reference_words
                prefixed_values = (
                    re.findall(
                        rf"(?<![a-z0-9])(?:{'|'.join(map(re.escape, prefixes))})"
                        r"[-_:][a-z0-9][a-z0-9_.:/-]*",
                        query,
                        re.IGNORECASE,
                    )
                    if prefixes
                    else []
                )
                value_type = value_schema.get("type")
                minimum = (
                    schema.get("minItems", 1) if schema.get("type") == "array" else 1
                )
                minimum = minimum if isinstance(minimum, int) and minimum > 0 else 1
                valid_values = [
                    value
                    for value in values
                    if (
                        value.isdigit()
                        if value_type == "integer"
                        else (
                            value.lower()
                            not in SEARCH_OPERATION_TERMS
                            | SEARCH_STOP_WORDS
                            | generic_reference_words
                            and (
                                not qualified_number
                                or any(character.isdigit() for character in value)
                            )
                        )
                    )
                ]
                supplied = (
                    value_type != "integer"
                    and (
                        len(uuids) >= minimum
                        or len(prefixed_values) >= minimum
                        or len(described_prefixed_values) >= minimum
                        or len(semantic_reference_values) >= minimum
                    )
                ) or (
                    value_schema.get("format") != "uuid"
                    and len(valid_values) >= minimum
                )
                if not supplied:
                    return True
        return False

    def _is_discovery_spec(self, spec: dict) -> bool:
        name_terms = self._ranking_terms(str(spec.get("name", "")))
        parameters = spec.get("parameters")
        properties = (
            parameters.get("properties") if isinstance(parameters, dict) else None
        )
        return (
            bool(
                name_terms.intersection(
                    {"search", "find", "lookup", "discover", "browse", "query"}
                )
            )
            and isinstance(properties, dict)
            and any(
                name in properties
                for name in ("query", "search", "q", "keywords", "term", "terms")
            )
        )

    @staticmethod
    def _ranking_stems(word: str) -> set[str]:
        cache = _RANKING_STEMS_CACHE.get()
        if cache is not None:
            cached = cache.get(word)
            if cached is not None:
                return set(cached)
        suffixes = (
            "ation",
            "ition",
            "tion",
            "ion",
            "ent",
            "ant",
            "ence",
            "ance",
            "ment",
            "al",
            "er",
            "or",
            "ing",
            "ity",
            "ed",
            "e",
        )
        stems = {word} | {
            word[: -len(suffix)]
            for suffix in suffixes
            if word.endswith(suffix) and len(word) - len(suffix) >= 5
        }
        if cache is not None:
            cache[word] = frozenset(stems)
        return stems

    def _explicit_subject_terms(self, query: str) -> set[str]:
        query = self._ranking_context(query)
        tokens = re.findall(
            r"(?:\.[A-Za-z][A-Za-z0-9]*|[A-Za-z][A-Za-z0-9]*"
            r"(?:[.+#-][A-Za-z0-9]+)*)",
            query,
        )

        def intrinsically_distinctive(token: str) -> bool:
            return (
                token.startswith(".")
                or any(character in token for character in ".+#-")
                or any(character.isupper() for character in token[1:])
            )

        explicit = set()
        for index, token in enumerate(tokens):
            distinctive = intrinsically_distinctive(token) or (
                token[:1].isupper()
                and index + 1 < len(tokens)
                and intrinsically_distinctive(tokens[index + 1])
            )
            if not distinctive:
                continue
            terms = self._ranking_terms(token)
            if any(character in token for character in ".+#-"):
                # A compound token (e.g. "next.js", "c#") is distinctive as a
                # whole, but splitting it can strip an ordinary word (e.g.
                # "next") that is not itself distinctive. Keep only the parts
                # that remain intrinsically distinctive plus the joined form,
                # so a bare word never leaks in as a "named identifier."
                joined = re.sub(r"[.+#-]", "", token).lower()
                terms = {term for term in terms if intrinsically_distinctive(term)} | {
                    joined
                }
            explicit.update(
                term
                for term in terms
                if term not in SEARCH_STOP_WORDS
                and term not in SEARCH_OPERATION_TERMS
                and term not in RESOURCE_INTENT_TERMS
            )
        return explicit

    def _ranking_context(self, query: str) -> str:
        unquoted = RANKING_FENCED_CODE_RE.sub(" ", query)
        if unquoted.count("`") % 2 == 0:
            unquoted = RANKING_INLINE_CODE_RE.sub(" ", unquoted)
        unquoted = RANKING_QUOTED_TEXT_RE.sub(" ", unquoted)
        subjects = (
            self._ranking_terms(unquoted)
            - SEARCH_STOP_WORDS
            - SEARCH_OPERATION_TERMS
            - RESOURCE_INTENT_TERMS
            - ROUTING_STOP_TERMS
        )
        return unquoted if any(not term.isdigit() for term in subjects) else query

    def _catalog_vocabulary(self, catalogs: dict[str, list[dict]]) -> frozenset[str]:
        vocabulary: set[str] = set()
        for alias, specs in catalogs.items():
            vocabulary |= self._ranking_terms(alias)
            for spec in specs:
                vocabulary |= self._ranking_terms(str(spec.get("name", "")))
                vocabulary |= self._ranking_terms(
                    str(spec.get("description", ""))[:600]
                )
        return frozenset(vocabulary)

    def _common_catalog_subjects(
        self, query: str, catalogs: dict[str, list[dict]]
    ) -> frozenset[str]:
        # A query subject that many unrelated servers already use in their
        # own vocabulary is background wording rather than a distinguishing
        # subject. Measuring this against the accessible catalog keeps the
        # rule generic: no fixed word list, and a small catalog simply never
        # reaches the threshold.
        subjects = (
            self._ranking_terms(query)
            - SEARCH_STOP_WORDS
            - SEARCH_OPERATION_TERMS
            - RESOURCE_INTENT_TERMS
            - ROUTING_STOP_TERMS
        )
        if not subjects or len(catalogs) < COMMON_SUBJECT_MIN_SERVERS:
            return frozenset()
        counts: dict[str, int] = {}
        for alias, specs in catalogs.items():
            stems: set[str] = set()
            for word in self._catalog_vocabulary({alias: specs}):
                stems |= self._ranking_stems(word)
            for term in subjects:
                # Match on shared stems so an inflected subject is measured
                # against the base word the catalog actually uses.
                if self._ranking_stems(term) & stems:
                    counts[term] = counts.get(term, 0) + 1
        return frozenset(
            term
            for term, count in counts.items()
            if count >= COMMON_SUBJECT_MIN_SERVERS
        )

    def _distinct_catalog_subjects(
        self, query: str, catalogs: dict[str, list[dict]]
    ) -> frozenset[str]:
        # The mirror of _common_catalog_subjects. A query subject that exactly
        # one accessible server uses in its own vocabulary is distinctive
        # evidence rather than background wording: "protein" reaching only a
        # nutrition server, or "kenya" only a development-data server, says
        # far more than the same word buried in many unrelated servers. It
        # lets a single descriptive match stand on its own, which the
        # two-match corroboration rule otherwise discards. Measured from the
        # accessible catalog, so no fixed word list is involved.
        subjects = (
            self._ranking_terms(query)
            - SEARCH_STOP_WORDS
            - SEARCH_OPERATION_TERMS
            - RESOURCE_INTENT_TERMS
            - ROUTING_STOP_TERMS
        )
        # Short tokens ("much", "data") collide by accident too easily to
        # carry a server's identity on one appearance.
        subjects = {
            term for term in subjects if len(term) >= DISTINCT_SUBJECT_MIN_CHARS
        }
        if not subjects or len(catalogs) < COMMON_SUBJECT_MIN_SERVERS:
            return frozenset()
        counts: dict[str, int] = {}
        for alias, specs in catalogs.items():
            stems: set[str] = set()
            for word in self._catalog_vocabulary({alias: specs}):
                stems |= self._ranking_stems(word)
            for term in subjects:
                if self._ranking_stems(term) & stems:
                    counts[term] = counts.get(term, 0) + 1
        return frozenset(term for term, count in counts.items() if count == 1)

    def _explicit_server_identities(
        self,
        query: str,
        catalogs: dict[str, list[dict]],
    ) -> frozenset[str]:
        identity_terms = set(
            self._distinct_catalog_subjects(query, catalogs)
        ) | self._explicit_subject_terms(query)
        if not identity_terms:
            return frozenset()
        candidates = {
            alias for alias in catalogs if self._ranking_terms(alias) & identity_terms
        }
        if not candidates:
            return frozenset()
        candidate_terms = {alias: self._ranking_terms(alias) for alias in candidates}
        ordered_subjects = []
        for token in re.findall(
            SEARCH_TOKEN_RE,
            self._ranking_context(query),
            flags=re.IGNORECASE,
        ):
            ordered_subjects.extend(
                term
                for term in self._ranking_terms(token)
                if term not in SEARCH_STOP_WORDS
                and term not in SEARCH_OPERATION_TERMS
                and term not in RESOURCE_INTENT_TERMS
                and term not in ROUTING_STOP_TERMS
                and not term.isdigit()
            )
        leading_identity_terms = set()
        all_candidate_terms = set().union(*candidate_terms.values())
        for term in ordered_subjects:
            if term not in all_candidate_terms:
                break
            leading_identity_terms.add(term)
        if not leading_identity_terms:
            return frozenset()
        return frozenset(
            alias
            for alias, alias_terms in candidate_terms.items()
            if alias_terms & leading_identity_terms
        )

    def _unmatched_named_subjects(
        self, query: str, catalog_vocabulary: frozenset[str]
    ) -> frozenset[str]:
        # A query subject absent from every accessible tool's own
        # vocabulary is, structurally, an unrecognized proper noun the
        # catalog cannot describe on its own terms (e.g. "acmeos" typed in
        # all lowercase). This is a catalog-relative property, not a
        # hardcoded product rule, and lets the resource-reference route fire
        # without depending on the user's capitalization.
        subjects = (
            self._ranking_terms(query)
            - SEARCH_STOP_WORDS
            - SEARCH_OPERATION_TERMS
            - RESOURCE_INTENT_TERMS
            - ROUTING_STOP_TERMS
        )
        return frozenset(
            term
            for term in subjects
            if len(term) >= 5 and term not in catalog_vocabulary
        )

    def _resource_reference_subjects(
        self,
        query: str,
        spec: dict,
        rare_subjects: frozenset[str] = frozenset(),
    ) -> set[str]:
        explicit = self._explicit_subject_terms(query) | set(rare_subjects)
        query_terms = self._ranking_terms(query)
        if not explicit or not query_terms.intersection(RESOURCE_INTENT_TERMS):
            return set()
        return explicit if self._has_resource_reference(spec) else set()

    def _has_resource_reference(self, spec: dict) -> bool:
        parameters = spec.get("parameters")
        properties = (
            parameters.get("properties") if isinstance(parameters, dict) else None
        )
        required = parameters.get("required") if isinstance(parameters, dict) else None
        if not isinstance(properties, dict) or not isinstance(required, list):
            return False
        required_names = [
            name for name in required if isinstance(name, str) and name in properties
        ]
        reference_names = [
            name
            for name in required_names
            if self._parameter_words(name).intersection(RESOURCE_SCOPE_TERMS)
        ]
        if not reference_names:
            return False
        # Beyond the resource reference itself, only an open free-text
        # argument (e.g. a natural-language question) may also be required.
        # A further required argument that names a specific sub-object
        # (a label, tag, commit, branch, or other identifier) means the
        # caller must already know that exact record; naming the project
        # alone does not supply it, so this is not a bare resource lookup.
        for name in required_names:
            if name in reference_names:
                continue
            words = self._parameter_words(name)
            if not (
                words.intersection(FREEFORM_REFERENCE_TERMS)
                or words.intersection(REFERENCE_COMPANION_TERMS)
            ):
                return False
        declared_terms = self._ranking_terms(
            str(spec.get("name", ""))
        ) | self._ranking_terms(str(spec.get("description", ""))[:600])
        return bool(declared_terms.intersection(RESOURCE_SCOPE_TERMS))

    def _matched_subject_evidence(
        self,
        query: str,
        alias: str,
        spec: dict,
        rare_subjects: frozenset[str] = frozenset(),
    ) -> set[str]:
        query_terms = self._ranking_terms(query) - SEARCH_STOP_WORDS
        non_numeric = {term for term in query_terms if not term.isdigit()}
        if non_numeric:
            query_terms = non_numeric
        subjects = (
            query_terms
            - SEARCH_OPERATION_TERMS
            - RESOURCE_INTENT_TERMS
            - ROUTING_STOP_TERMS
        )
        unquoted = (
            self._ranking_terms(self._ranking_context(query))
            - SEARCH_STOP_WORDS
            - SEARCH_OPERATION_TERMS
            - RESOURCE_INTENT_TERMS
            - ROUTING_STOP_TERMS
        )
        if non_numeric:
            unquoted = {term for term in unquoted if not term.isdigit()}
        if unquoted:
            subjects = unquoted
        description = str(spec.get("description", ""))
        purpose = re.split(r"(?<=[.!?])\s+", description, maxsplit=1)[0][:600]
        evidence_terms = (
            self._ranking_terms(alias)
            | self._ranking_terms(str(spec.get("name", "")))
            | self._ranking_terms(purpose)
        )
        return subjects & evidence_terms | self._resource_reference_subjects(
            query, spec, rare_subjects
        )

    @staticmethod
    def _bm25_words(value: Any) -> list[str]:
        if not isinstance(value, str) or not value:
            return []
        expanded = re.sub(r"(?<=[a-z0-9])(?=[A-Z])", " ", value)
        expanded = re.sub(r"[_/\\:-]+", " ", expanded)
        return [
            token.casefold()
            for token in re.findall(
                r"\.[^\W_]+|[^\W_]+(?:\+\+|#)?",
                expanded,
                flags=re.UNICODE,
            )
            if token
        ]

    @staticmethod
    def _bm25_ngrams(token: str) -> set[str]:
        base = token.lstrip(".").removesuffix("++").removesuffix("#")
        if (
            len(base) < BM25_NGRAM_SIZE
            or len(base) > 40
            or not any(character.isalpha() for character in base)
        ):
            return set()
        return {
            f"~{base[index:index + BM25_NGRAM_SIZE]}"
            for index in range(len(base) - BM25_NGRAM_SIZE + 1)
        }

    def _bm25_document_terms(self, alias: str, spec: dict) -> Counter:
        terms: Counter = Counter()

        def add(value: Any, weight: float, *, ngrams: bool = False) -> None:
            words = self._bm25_words(value)
            for word in words:
                if word in BM25_GENERIC_TERMS:
                    continue
                terms[word] += weight
                if ngrams:
                    for ngram in self._bm25_ngrams(word):
                        terms[ngram] += BM25_NGRAM_WEIGHT

        add(alias, BM25_SERVER_WEIGHT, ngrams=True)
        add(spec.get("name", ""), BM25_NAME_WEIGHT, ngrams=True)
        description = str(spec.get("description", ""))
        purpose = re.split(r"(?<=[.!?])\s+", description, maxsplit=1)[0][
            :MAX_ROUTING_DESCRIPTION_CHARS
        ]
        add(purpose, BM25_PURPOSE_WEIGHT)

        parameters = spec.get("parameters")
        properties = (
            parameters.get("properties")
            if isinstance(parameters, dict)
            and isinstance(parameters.get("properties"), dict)
            else {}
        )
        for property_name, schema in list(properties.items())[
            : self.valves.maximum_search_properties
        ]:
            add(property_name, BM25_PARAMETER_WEIGHT, ngrams=True)
            if not isinstance(schema, dict):
                continue
            add(schema.get("type"), BM25_SCHEMA_VALUE_WEIGHT)
            add(schema.get("format"), BM25_SCHEMA_VALUE_WEIGHT)
            enum = schema.get("enum")
            if isinstance(enum, list):
                for value in enum[: self.valves.maximum_search_enum_items]:
                    add(value, BM25_SCHEMA_VALUE_WEIGHT)

        if len(terms) > BM25_MAX_TERMS_PER_DOCUMENT:
            terms = Counter(
                dict(
                    sorted(
                        terms.items(),
                        key=lambda item: (-item[1], item[0]),
                    )[:BM25_MAX_TERMS_PER_DOCUMENT]
                )
            )
        return terms

    def _legacy_catalog_features(self, alias: str, spec: dict) -> dict:
        description = str(spec.get("description", "")).lower()
        purpose = re.split(r"(?<=[.!?])\s+", description, maxsplit=1)[0][:600]
        return {
            "alias_terms": frozenset(self._ranking_terms(alias)),
            "name_terms": frozenset(
                self._ranking_terms(str(spec.get("name", "")).lower())
            ),
            "description_terms": frozenset(self._ranking_terms(description)),
            "purpose_terms": frozenset(self._ranking_terms(purpose)),
            "discovery": self._is_discovery_spec(spec),
        }

    @staticmethod
    def _legacy_feature_characters(features: dict) -> int:
        total = 64
        for key, value in features.items():
            total += len(str(key)) + 8
            if isinstance(value, frozenset):
                total += sum(len(term) + 24 for term in value)
            else:
                total += 8
        return total

    def _build_bm25_index(
        self,
        catalogs: dict[str, list[dict]],
        revision: Optional[str] = None,
        include_legacy_features: bool = False,
    ) -> dict:
        rows = []
        postings: dict[str, list[tuple[int, float]]] = {}
        document_lengths = []
        resources = []
        companions: dict[str, set[int]] = {}
        legacy_features = {}
        feature_characters = 0
        cache_legacy_features = include_legacy_features
        characters = 0
        partial = False
        for alias, specs in sorted(catalogs.items()):
            for spec in specs:
                if not isinstance(spec, dict):
                    continue
                name = str(spec.get("name", ""))
                if not name:
                    continue
                if len(rows) >= ROUTING_INDEX_MAX_ENTRIES:
                    partial = True
                    break
                terms = self._bm25_document_terms(alias, spec)
                added = (
                    len(alias) + len(name) + sum(len(term) + 24 for term in terms) + 64
                )
                if characters + added > ROUTING_INDEX_MAX_CHARS:
                    partial = True
                    continue
                document_number = len(rows)
                rows.append((alias, name))
                document_length = sum(terms.values())
                document_lengths.append(document_length)
                if self._has_resource_reference(spec):
                    resources.append(document_number)
                if self._is_discovery_spec(spec):
                    companions.setdefault(alias, set()).add(document_number)
                if cache_legacy_features:
                    features = self._legacy_catalog_features(alias, spec)
                    feature_added = self._legacy_feature_characters(features)
                    if (
                        feature_characters + feature_added
                        <= RANKING_FEATURE_INDEX_MAX_CHARS
                    ):
                        legacy_features[(alias, name)] = features
                        feature_characters += feature_added
                    else:
                        cache_legacy_features = False
                characters += added
                for term, frequency in terms.items():
                    postings.setdefault(term, []).append((document_number, frequency))
            if len(rows) >= ROUTING_INDEX_MAX_ENTRIES:
                break
        return {
            "revision": revision,
            "rows": rows,
            "postings": postings,
            "document_lengths": document_lengths,
            "average_document_length": (
                sum(document_lengths) / len(document_lengths)
                if document_lengths
                else 0.0
            ),
            "resources": resources,
            "companions": companions,
            "legacy_features": legacy_features,
            "feature_characters": feature_characters,
            "characters": characters,
            "partial": partial,
            "last_used": time.time(),
        }

    def _bm25_scores(self, query: str, index: dict) -> dict[tuple[str, str], float]:
        rows = index.get("rows")
        postings = index.get("postings")
        lengths = index.get("document_lengths")
        average_length = index.get("average_document_length")
        if (
            not isinstance(rows, list)
            or not isinstance(postings, dict)
            or not isinstance(lengths, list)
            or len(rows) != len(lengths)
            or not isinstance(average_length, (int, float))
            or average_length <= 0
        ):
            return {}

        query_terms: Counter = Counter()
        query_words = {
            word
            for word in self._bm25_words(self._ranking_context(query))
            if word not in BM25_GENERIC_TERMS
            and (
                len(word.lstrip(".")) >= 3
                or word.startswith(".")
                or word.endswith(("#", "++"))
            )
        }
        for word in query_words:
            query_terms[word] = 1.0
            for ngram in self._bm25_ngrams(word):
                query_terms[ngram] = max(
                    query_terms.get(ngram, 0.0),
                    BM25_NGRAM_WEIGHT,
                )

        document_scores: dict[int, float] = {}
        matched_words: dict[int, set[str]] = {}
        document_count = len(rows)
        for term, query_weight in query_terms.items():
            term_postings = postings.get(term)
            if not isinstance(term_postings, list) or not term_postings:
                continue
            document_frequency = len(term_postings)
            inverse_document_frequency = math.log(
                1.0
                + (document_count - document_frequency + 0.5)
                / (document_frequency + 0.5)
            )
            for document_number, frequency in term_postings:
                denominator = frequency + BM25_K1 * (
                    1.0 - BM25_B + BM25_B * lengths[document_number] / average_length
                )
                contribution = (
                    inverse_document_frequency
                    * (frequency * (BM25_K1 + 1.0))
                    / denominator
                    * query_weight
                )
                document_scores[document_number] = (
                    document_scores.get(document_number, 0.0) + contribution
                )
                if not term.startswith("~"):
                    matched_words.setdefault(document_number, set()).add(term)
        return {
            rows[document_number]: score
            * (
                0.2
                + 0.8
                * len(matched_words.get(document_number, ()))
                / max(1, len(query_words))
            )
            for document_number, score in document_scores.items()
            if score > 0
        }

    def _score(
        self,
        query: str,
        alias: str,
        spec: dict,
        apply_reference_readiness: bool = True,
        rare_subjects: frozenset[str] = frozenset(),
        common_subjects: frozenset[str] = frozenset(),
        distinct_subjects: frozenset[str] = frozenset(),
    ) -> int:
        normalized_query = query.strip().lower()
        if not normalized_query:
            return 1

        name = str(spec.get("name", "")).lower()
        description = str(spec.get("description", "")).lower()
        tool = f"{alias}.{name}"
        if normalized_query in (name, tool):
            return 1000
        query_terms = self._ranking_terms(query) - SEARCH_STOP_WORDS
        non_numeric_terms = {term for term in query_terms if not term.isdigit()}
        if non_numeric_terms:
            query_terms = non_numeric_terms
        name_terms = self._ranking_terms(name)
        if (
            "documentation" in query_terms
            and name_terms.intersection(REPOSITORY_ACTIVITY_TERMS)
            and not query_terms.intersection(REPOSITORY_ACTIVITY_TERMS)
            and "documentation" not in self._ranking_terms(description)
        ):
            return 0
        subjects = (
            query_terms
            - SEARCH_OPERATION_TERMS
            - RESOURCE_INTENT_TERMS
            - ROUTING_STOP_TERMS
        )
        operations = query_terms & SEARCH_OPERATION_TERMS
        unquoted_subjects = (
            self._ranking_terms(self._ranking_context(query))
            - SEARCH_STOP_WORDS
            - SEARCH_OPERATION_TERMS
            - RESOURCE_INTENT_TERMS
            - ROUTING_STOP_TERMS
        )
        if non_numeric_terms:
            unquoted_subjects = {
                term for term in unquoted_subjects if not term.isdigit()
            }
        if unquoted_subjects:
            subjects = unquoted_subjects
        feature_cache = _RANKING_FEATURES_CACHE.get()
        features = (
            feature_cache.get((alias, str(spec.get("name", ""))))
            if isinstance(feature_cache, dict)
            else None
        )
        if isinstance(features, dict):
            alias_terms = set(features.get("alias_terms", ()))
            name_terms = set(features.get("name_terms", ()))
            description_terms = set(features.get("description_terms", ()))
            purpose_terms = set(features.get("purpose_terms", ()))
            discovery = bool(features.get("discovery"))
        else:
            alias_terms = self._ranking_terms(alias)
            name_terms = self._ranking_terms(name)
            description_terms = self._ranking_terms(description)
            purpose_terms = self._ranking_terms(
                re.split(r"(?<=[.!?])\s+", description, maxsplit=1)[0][:600]
            )
            discovery = self._is_discovery_spec(spec)
        missing_reference = self._missing_reference_arguments(query, spec)
        reference_required = self._missing_reference_arguments("", spec)
        matches = subjects & (alias_terms | name_terms | description_terms)
        named_identifiers = self._explicit_subject_terms(query)
        specific_identifiers = {
            term.lower()
            for term in re.findall(
                SEARCH_TOKEN_RE, self._ranking_context(query), flags=re.IGNORECASE
            )
            if re.search(r"[a-z][A-Z]", term)
        }
        resource_subjects = self._resource_reference_subjects(
            query, spec, rare_subjects
        )
        if not subjects:
            # Operation-only requests are broad browsing, not subject evidence.
            variants = operations | {
                synonym
                for operation in operations
                for synonym in SEARCH_SYNONYMS.get(operation, set())
            }
            return 10 if variants & name_terms else 0
        purpose_matches = subjects & purpose_terms
        # Share long stems only through grammatical suffixes, not arbitrary
        # fuzzy prefixes (which can confuse unrelated words).
        related = {
            term
            for term in subjects - purpose_matches
            if 6 <= len(term) <= 40
            and not any(
                self._ranking_stems(term) & self._ranking_stems(operation)
                for operation in SEARCH_OPERATION_TERMS
            )
            and any(
                6 <= len(word) <= 40
                and term[:5] == word[:5]
                and self._ranking_stems(term) & self._ranking_stems(word)
                for word in purpose_terms | name_terms | alias_terms
            )
        }
        technical_terms = named_identifiers | {
            term
            for term in subjects
            if term.startswith(".") or term.endswith(("#", "++"))
        }
        strong = (
            subjects & (alias_terms | name_terms)
            | purpose_matches
            | related
            | (matches & technical_terms)
            | (matches & distinct_subjects)
        )
        technical_only_strong = (
            bool(matches & technical_terms)
            and not (subjects & (alias_terms | name_terms))
            and not purpose_matches
            and not related
        )
        if technical_only_strong and len(matches) < min(2, len(subjects)):
            # A single all-caps acronym-style token (e.g. "REST") can
            # coincidentally match an ordinary English word buried deep in
            # an unrelated tool's description, with no other query subject
            # corroborating that overlap anywhere in the tool's own text.
            # Without at least one more matched subject, this is an
            # incidental collision, not real evidence.
            strong = set()
        suppressed_common_evidence = False
        suppressed_evidence_size = 0
        common_only_evidence = strong
        common_only_strong = (
            bool(strong)
            and not (matches & technical_terms)
            and not resource_subjects
            and strong <= common_subjects
            and (
                not (subjects & (alias_terms | name_terms))
                # A name or server identity match is ordinarily strong
                # evidence on its own. It is not when the matched term is
                # catalog-wide background vocabulary *and* the request also
                # names a subject no accessible tool knows at all: the tool
                # matched shared wording while being blind to the actual
                # topic. "How does AcmeOS work internally" reaching a
                # prose-"work" reader is that case, while "find scholarly
                # works" naming only vocabulary the catalog knows is not.
                or bool(rare_subjects - matches)
            )
        )
        # Corroboration has to come from outside that background vocabulary,
        # or from the tool's own declared purpose, name or server identity:
        # two catalog-wide words buried in body prose ("across", "call")
        # cannot vouch for each other, or a mortality-statistics server
        # answers a question about socket code purely on shared English. The
        # same words in a tool's opening sentence or name ("issues",
        # "repository" on a repository server) are that tool declaring the
        # subject, so they still corroborate.
        corroborating = (matches - common_subjects) | (
            matches & (purpose_terms | name_terms | alias_terms)
        )
        if common_only_strong and len(corroborating) < min(2, len(subjects)):
            # Background wording shared by many unrelated servers can land in
            # a tool's own summary in a completely different sense, either
            # directly ("structured filters", "structured address fields") or
            # through a word form ("how the API is organized" reaching a
            # GitHub "organization"). For a request naming several subjects,
            # one such catalog-wide common overlap - with no distinctive
            # identifier and nothing else corroborating it - is shared
            # vocabulary, not evidence that the tool serves the request.
            # Rarer subjects stay strong evidence on their own.
            strong = set()
            suppressed_common_evidence = True
            suppressed_evidence_size = max(len(matches), len(common_only_evidence))
        substantive_strong = {
            term
            for term in strong
            if term not in RESOURCE_INTENT_TERMS
            and not any(
                self._ranking_stems(term) & self._ranking_stems(operation)
                for operation in SEARCH_OPERATION_TERMS
            )
        }
        # Suppressing catalog-wide background wording says the evidence is too
        # weak to act on, not that the tool is irrelevant: the shared
        # word may well be used in exactly the requested sense ("geographic
        # coordinates" on a geocoder). Erasing it hides the tool from
        # explicit search too, so the model cannot choose it even when nothing
        # else matches. Demote it far below both the automatic-discovery floor
        # and the hint floor instead, leaving the decision where the judgement
        # is. Evidence reaching the tool only through a shared word stem
        # ("structured" -> read_wiki_structure) counts here too, or the
        # demotion would silently collapse back to zero for exactly the
        # tools it exists to keep visible.
        demoted_score = (
            min(18, 6 * max(1, suppressed_evidence_size))
            if suppressed_common_evidence
            else 0
        )
        if (
            specific_identifiers
            and not discovery
            and not reference_required
            and not resource_subjects
            and not specific_identifiers
            & (alias_terms | name_terms | description_terms)
        ):
            return 0
        if (
            named_identifiers
            and not resource_subjects
            and not named_identifiers & (alias_terms | name_terms | description_terms)
            and not substantive_strong
        ):
            return demoted_score
        if (
            not strong
            and not resource_subjects
            and len(matches) < min(2, len(subjects))
        ):
            return demoted_score
        if strong:
            score = (
                60
                + 24 * len(subjects & alias_terms)
                + 18 * len(subjects & name_terms)
                + 12 * len(purpose_matches)
                + 8 * len(related)
                + round(30 * len(strong) / len(subjects))
            )
        elif resource_subjects:
            score = 55
        else:
            score = min(18, 6 * len(matches))
        for operation in operations:
            variants = {operation} | SEARCH_SYNONYMS.get(operation, set())
            if variants & name_terms:
                score += 18
        if "list" in name_terms and not operations & {"list", "browse", "enumerate"}:
            score = max(1, round(score * 0.35))
        if apply_reference_readiness:
            if missing_reference:
                score = max(1, round(score * 0.45))
                if strong & alias_terms:
                    score = max(
                        score,
                        max(
                            1,
                            self.valves.automatic_discovery_minimum_score - 1,
                        ),
                    )
            elif reference_required:
                score += 30
        if (
            strong
            and len(strong) == 1
            and strong <= distinct_subjects
            and rare_subjects - matches
            and not resource_subjects
            and not (matches & technical_terms)
            and not (strong & alias_terms)
        ):
            # A subject only one server's metadata knows is that server's own
            # vocabulary, which is why it stands as evidence at all. But a
            # single such word can still be a false friend: "peer-reviewed
            # studies on statin side effects" reaches an education server's
            # "study_guide" because no other catalogue entry happens to use
            # "study", while the request's real subjects (statin, peer,
            # reviewed) are ones no accessible tool knows. One distinctive
            # word, with the actual topic invisible to the whole catalog, is
            # enough to offer the tool but not to act on it
            # unprompted, so keep it under the automatic-routing floor and
            # leave the choice with the model.
            #
            # Exempt a match carried by the server's own alias/identity
            # (e.g. "weather-mcp" for "the weather in..."): a server
            # naming itself after the subject is the accessible catalog's
            # strongest, least coincidental signal that it is the right
            # server, unlike a shared word landing in one tool's own name or
            # description body ("study_guide") purely by incidental overlap.
            score = min(
                score, max(1, self.valves.automatic_discovery_minimum_score - 1)
            )
        elif (
            strong
            and len(strong) == 1
            and len(subjects) == 2
            and len(subjects - matches - common_subjects) == 1
            and not resource_subjects
            and not (matches & technical_terms)
            and not any(
                ({operation} | SEARCH_SYNONYMS.get(operation, set())) & name_terms
                for operation in operations
            )
        ):
            # The same false-friend shape, reached from the opposite side: the
            # matched word is real vocabulary for this tool, but the
            # request pairs it with another subject the tool does not
            # cover. A tool matching only "law" in "explain contract
            # law" leaves "contract" unanswered, and one subject is full
            # coverage of a one-subject query, so the score stays high on
            # half the request.
            #
            # Deliberately limited to a two-subject request with exactly one
            # understood. That is the smallest shape where a coverage gap is
            # unambiguous. Longer requests carry incidental vocabulary that
            # looks identical to a gap but is not: "review this French
            # text for naturalness" can leave incidental qualifiers unmatched
            # by a tool that still handles the request, and matches a
            # smaller share of the request
            # than the contract-law case it must not resemble. Ratio cannot
            # separate the two; a clean two-subject split can.
            #
            # A tool asked to perform its own retrieval operation is
            # also exempt: "find scholarly works about climate change" leaves
            # "climate" unmatched by a work search, but that is the search
            # term it would be given, not a subject it fails to cover. This
            # keys on the requested operation naming the tool, not on a
            # blanket bonus for anything search-shaped.
            score = min(
                score, max(1, self.valves.automatic_discovery_minimum_score - 1)
            )
        return score

    def _rank_catalogs_legacy(
        self, query: str, catalogs: dict[str, list[dict]]
    ) -> list[tuple[str, dict, int]]:
        if _RANKING_TERMS_CACHE.get() is None:
            with self._ranking_cache_scope():
                return self._rank_catalogs_legacy(query, catalogs)
        explicit_servers = self._explicit_server_identities(query, catalogs)
        if explicit_servers:
            catalogs = {
                alias: specs
                for alias, specs in catalogs.items()
                if alias in explicit_servers
            }
        # A query subject unrecognized by every accessible tool's own
        # vocabulary can still unlock the resource-reference route (e.g. a
        # lowercase "acmeos" naming a project no catalog otherwise mentions).
        rare_subjects = self._unmatched_named_subjects(
            query, self._catalog_vocabulary(catalogs)
        )
        # A subject word carried by many unrelated servers' own vocabularies
        # is catalog-wide background vocabulary. This is measured from the
        # accessible catalog itself, not a fixed word list.
        common_subjects = self._common_catalog_subjects(query, catalogs)
        # The inverse signal: a subject only one accessible server knows at
        # all is that server's own vocabulary, so one descriptive match can
        # stand without corroboration.
        distinct_subjects = self._distinct_catalog_subjects(query, catalogs)
        scored = []
        for alias, specs in catalogs.items():
            for spec in specs:
                try:
                    scored.append(
                        (
                            alias,
                            spec,
                            self._score(
                                query,
                                alias,
                                spec,
                                rare_subjects=rare_subjects,
                                common_subjects=common_subjects,
                                distinct_subjects=distinct_subjects,
                            ),
                        )
                    )
                except (TypeError, ValueError, AttributeError):
                    log.exception(
                        "Tool Broker could not score metadata from '%s'.", alias
                    )
        direct = sorted(
            (entry for entry in scored if entry[2] > 0),
            key=lambda entry: (-entry[2], entry[0], str(entry[1].get("name", ""))),
        )
        if not direct or not query.strip():
            return direct

        alias, lookup, score = direct[0]
        # An exact request must stay first. Otherwise promote at most one
        # prerequisite, and only for the strongest directly relevant lookup.
        if score >= 1000:
            return direct
        lookup_raw_score = score
        displaced_ready = None
        if not self._missing_reference_arguments(query, lookup):
            missing_lookups = [
                (
                    entry,
                    self._score(
                        query,
                        entry[0],
                        entry[1],
                        apply_reference_readiness=False,
                        rare_subjects=rare_subjects,
                        common_subjects=common_subjects,
                    ),
                )
                for entry in direct[1:]
                if self._missing_reference_arguments(query, entry[1])
            ]
            missing_lookups = [
                (entry, raw_score)
                for entry, raw_score in missing_lookups
                if raw_score >= score
            ]
            if not missing_lookups:
                return direct
            # The leader can already be called with what the request supplies.
            # A prerequisite is being promoted for a *different*, unready
            # lookup, so record the ready leader: it keeps precedence below.
            displaced_ready = direct[0]
            (alias, lookup, lookup_score), lookup_raw_score = min(
                missing_lookups,
                key=lambda item: (
                    -item[1],
                    -item[0][2],
                    item[0][0],
                    str(item[0][1].get("name", "")),
                ),
            )
            score = lookup_score
        actions = {
            "fetch",
            "get",
            "retrieve",
            "read",
            "search",
            "find",
            "lookup",
            "discover",
            "browse",
            "query",
            "list",
        }
        lookup_terms = (
            self._ranking_terms(str(lookup.get("name", "")))
            - self._ranking_terms(alias)
            - actions
        )
        parameters = lookup.get("parameters")
        properties = (
            parameters.get("properties") if isinstance(parameters, dict) else None
        )
        required = parameters.get("required") if isinstance(parameters, dict) else None
        if isinstance(properties, dict) and isinstance(required, list):
            lookup_terms.update(
                term
                for name in required
                if isinstance(name, str) and name in properties
                for term in self._parameter_words(name)
                if term not in actions
            )

        def prerequisite_terms(candidate: dict) -> set[str]:
            purpose = re.split(
                r"(?<=[.!?])\s+",
                str(candidate.get("description", "")),
                maxsplit=1,
            )[0][:600]
            return (
                (
                    self._ranking_terms(str(candidate.get("name", "")))
                    | self._ranking_terms(purpose)
                )
                - self._ranking_terms(alias)
                - actions
            )

        prerequisites = [
            (candidate, candidate_score)
            for server, candidate, candidate_score in scored
            if server == alias
            and self._is_discovery_spec(candidate)
            and not self._missing_reference_arguments(query, candidate)
            and lookup_terms & prerequisite_terms(candidate)
        ]
        if not prerequisites:
            return direct
        prerequisite, _ = min(
            prerequisites,
            key=lambda entry: (
                -entry[1],
                -len(lookup_terms & prerequisite_terms(entry[0])),
                str(entry[0].get("name", "")),
            ),
        )
        promotion_score = max(direct[0][2], lookup_raw_score)
        if displaced_ready is not None and alias != displaced_ready[0]:
            # Promoting a prerequisite means offering a detour: a search step
            # that yields the ID some other lookup still needs. Within one
            # server that is just the provider's own two-step workflow, and
            # the pair may lead. Across servers it is not: the existing leader
            # answers the request directly with what the caller already
            # supplied, so a different provider's search-then-lookup pair is
            # offered beneath it instead of displacing it. Without this a
            # generic entity search outranked a tool whose declared
            # purpose was exactly the request (resolving a supplied DOI).
            promotion_score = max(1, min(promotion_score, displaced_ready[2] - 2))
        promoted = []
        for entry in direct:
            if entry[0] == alias and entry[1] is prerequisite:
                continue
            if entry[0] == alias and entry[1] is lookup:
                promoted.append((entry[0], entry[1], promotion_score))
            else:
                promoted.append(entry)
        return sorted(
            [(alias, prerequisite, promotion_score + 1), *promoted],
            key=lambda entry: (-entry[2], entry[0], str(entry[1].get("name", ""))),
        )

    def _ranking_mode(self) -> str:
        mode = (
            self.valves.ranking_mode.strip().lower()
            if isinstance(self.valves.ranking_mode, str)
            else ""
        )
        if mode not in {"hybrid", "bm25", "legacy"}:
            raise ValueError(
                "Tool Broker ranking_mode must be 'hybrid', 'bm25', or 'legacy'."
            )
        return mode

    def _rank_catalogs(
        self,
        query: str,
        catalogs: dict[str, list[dict]],
        bm25_scores: Optional[dict[tuple[str, str], float]] = None,
    ) -> list[tuple[str, dict, int]]:
        if _RANKING_TERMS_CACHE.get() is None:
            with self._ranking_cache_scope():
                return self._rank_catalogs(query, catalogs, bm25_scores)
        explicit_servers = self._explicit_server_identities(query, catalogs)
        if explicit_servers:
            catalogs = {
                alias: specs
                for alias, specs in catalogs.items()
                if alias in explicit_servers
            }
        mode = self._ranking_mode()
        if mode == "legacy" or not query.strip():
            return self._rank_catalogs_legacy(query, catalogs)
        legacy = self._rank_catalogs_legacy(query, catalogs) if mode == "hybrid" else []

        if bm25_scores is None:
            bm25_scores = self._bm25_scores(
                query,
                self._build_bm25_index(catalogs),
            )
        if not bm25_scores:
            return legacy if mode == "hybrid" else []

        specs_by_key = {
            (alias, str(spec.get("name", ""))): spec
            for alias, specs in catalogs.items()
            for spec in specs
            if isinstance(spec, dict) and spec.get("name")
        }
        normalized_query = query.strip().casefold()
        bm25_order = [
            key
            for key, _ in sorted(
                bm25_scores.items(),
                key=lambda item: (-item[1], item[0]),
            )
            if key in specs_by_key
        ]
        if (
            mode == "hybrid"
            and bm25_order
            and round(bm25_scores[bm25_order[0]] * BM25_SCORE_SCALE)
            < self.valves.automatic_discovery_hint_minimum_score
        ):
            return legacy
        legacy_by_key = {
            (alias, str(spec.get("name", ""))): score for alias, spec, score in legacy
        }
        legacy_order = list(legacy_by_key)
        if mode == "bm25":
            fused_order = bm25_order
        else:
            reciprocal_rank = {}
            for rank, key in enumerate(bm25_order, 1):
                reciprocal_rank[key] = reciprocal_rank.get(key, 0.0) + (
                    2.0 / (60 + rank)
                )
            for rank, key in enumerate(legacy_order, 1):
                reciprocal_rank[key] = reciprocal_rank.get(key, 0.0) + (
                    1.0 / (60 + rank)
                )
            fused_order = [
                key
                for key, _ in sorted(
                    reciprocal_rank.items(),
                    key=lambda item: (-item[1], item[0]),
                )
                if key in specs_by_key
            ]
            if fused_order:
                leading_server = fused_order[0][0]
                server_legacy = [
                    key for key in legacy_order if key[0] == leading_server
                ]
                if server_legacy:
                    preferred = server_legacy[0]
                    fused_order = [
                        preferred,
                        *(key for key in fused_order if key != preferred),
                    ]

        exact_key = next(
            (
                key
                for key in fused_order
                if normalized_query
                in {
                    key[1].casefold(),
                    f"{key[0]}.{key[1]}".casefold(),
                }
            ),
            None,
        )
        if exact_key is not None:
            fused_order = [
                exact_key,
                *(key for key in fused_order if key != exact_key),
            ]
        trusted_legacy_key = None
        if (
            mode == "hybrid"
            and exact_key is None
            and legacy
            and legacy[0][2] >= self.valves.automatic_discovery_minimum_score
            and (not bm25_order or bm25_order[0][0] != legacy[0][0])
        ):
            trusted_legacy_key = (
                legacy[0][0],
                str(legacy[0][1].get("name", "")),
            )
            if trusted_legacy_key in fused_order:
                fused_order = [
                    trusted_legacy_key,
                    *(key for key in fused_order if key != trusted_legacy_key),
                ]
        ready_reference_key = None
        if mode == "hybrid" and legacy:
            legacy_alias, legacy_spec, _ = legacy[0]
            if self._missing_reference_arguments(
                "", legacy_spec
            ) and not self._missing_reference_arguments(query, legacy_spec):
                ready_reference_key = (
                    legacy_alias,
                    str(legacy_spec.get("name", "")),
                )
                if ready_reference_key in fused_order:
                    fused_order = [
                        ready_reference_key,
                        *(key for key in fused_order if key != ready_reference_key),
                    ]

        bm25_leader_server = bm25_order[0][0] if bm25_order else None
        legacy_leader_server = legacy_order[0][0] if legacy_order else None
        leaders_agree = (
            bm25_leader_server is not None
            and bm25_leader_server == legacy_leader_server
        )
        ranked = []
        for key in fused_order:
            spec = specs_by_key.get(key)
            if spec is None:
                continue
            alias, name = key
            raw_score = bm25_scores.get(key, 0.0)
            if normalized_query in {name.casefold(), f"{alias}.{name}".casefold()}:
                score = 1000
            else:
                score = (
                    max(1, min(999, round(raw_score * BM25_SCORE_SCALE)))
                    if raw_score > 0
                    else 0
                )
                legacy_score = legacy_by_key.get(key, 0)
                if mode == "hybrid":
                    score = max(score, legacy_score)
                if score and self._missing_reference_arguments(query, spec):
                    score = (
                        max(1, round(score * 0.45))
                        if mode == "bm25" or key not in legacy_by_key
                        else min(score, legacy_score)
                    )
                if (
                    mode == "hybrid"
                    and bm25_order
                    and legacy_order
                    and not leaders_agree
                    and key != ready_reference_key
                    and key != trusted_legacy_key
                ):
                    score = min(
                        score,
                        max(
                            1,
                            self.valves.automatic_discovery_minimum_score - 1,
                        ),
                    )
            if score > 0:
                ranked.append((alias, spec, score))

        direct = sorted(
            ranked,
            key=lambda entry: -entry[2],
        )
        if not direct:
            return legacy if mode == "hybrid" else []
        if (
            mode == "hybrid"
            and direct[0][2] < self.valves.automatic_discovery_hint_minimum_score
        ):
            return legacy
        return direct

    def _bounded_text(self, value: Any, limit: int) -> Any:
        if not isinstance(value, str) or len(value) <= limit:
            return value
        return f"{value[: max(0, limit - 3)]}..."

    def _format_execution_error(self, error: BaseException, limit: int = 3) -> str:
        """Unwrap an ``ExceptionGroup``/``BaseExceptionGroup`` (raised by the
        anyio/asyncio ``TaskGroup`` MCP transports use internally) into its
        leaf sub-exception messages. ``str()`` on such a group alone reports
        only an outer summary and a sub-exception count (e.g. "unhandled
        errors in a TaskGroup (1 sub-exception)"), discarding the actual
        cause the model needs to avoid a blind retry and a human needs to
        debug the failure. Duck-types on an ``exceptions`` attribute so it
        also covers non-stdlib group types; every other exception's message
        is returned unchanged.
        """
        nested = getattr(error, "exceptions", None)
        if not isinstance(nested, (list, tuple)) or not nested:
            return str(error)
        leaves: list[str] = []
        seen: set[str] = set()

        def collect(current: BaseException, depth: int = 0) -> None:
            if depth >= 6 or len(leaves) >= limit:
                return
            children = getattr(current, "exceptions", None)
            if isinstance(children, (list, tuple)) and children:
                for child in children:
                    if isinstance(child, BaseException):
                        collect(child, depth + 1)
                return
            text = (
                f"{type(current).__name__}: {current}"
                if str(current)
                else type(current).__name__
            )
            if text not in seen:
                seen.add(text)
                leaves.append(text)

        for child in nested:
            if isinstance(child, BaseException):
                collect(child)
        if not leaves:
            return str(error)
        joined = "; ".join(leaves[:limit])
        remaining = len(leaves) - len(leaves[:limit])
        if remaining > 0:
            joined += f" (+{remaining} more)"
        return joined

    def _safe_source_url(self, value: Any) -> Optional[str]:
        if not isinstance(value, str) or not value or len(value) > 2_048:
            return None
        try:
            parsed = urlsplit(value)
            if (
                parsed.scheme.lower() not in {"http", "https"}
                or not parsed.netloc
                or parsed.username is not None
                or parsed.password is not None
            ):
                return None
            for name, _ in parse_qsl(parsed.query, keep_blank_values=True):
                terms = {term for term in re.split(r"[^a-z0-9]+", name.lower()) if term}
                if terms.intersection(SENSITIVE_URL_QUERY_TERMS):
                    return None
        except (TypeError, ValueError):
            return None
        return value

    def _source_url(self, arguments: dict) -> Optional[str]:
        direct = self._safe_source_url(arguments.get("url"))
        if direct is not None:
            return direct
        for name, value in arguments.items():
            if name == "url":
                continue
            terms = self._identifier_terms(name)
            if "url" in terms and terms <= SOURCE_URL_TARGET_TERMS:
                candidate = self._safe_source_url(value)
                if candidate is not None:
                    return candidate
        return None

    def _retrieval_reference_values(self, arguments: dict) -> list[str]:
        references = []
        for name, value in arguments.items():
            terms = self._identifier_terms(name)
            if (
                not terms.intersection(SOURCE_REFERENCE_MARKERS)
                or not terms <= SOURCE_REFERENCE_TARGET_TERMS
                or isinstance(value, bool)
                or not isinstance(value, (str, int))
            ):
                continue
            reference = str(value).strip()
            if 3 <= len(reference) <= 256 and self._safe_source_url(reference) is None:
                references.append(reference)
        return references

    def _returned_retrieval_url(
        self,
        text: str,
        references: list[str],
    ) -> Optional[str]:
        if not references:
            return None

        def matches_reference(url: str) -> bool:
            return any(
                re.search(
                    rf"(?<![a-z0-9]){re.escape(reference)}(?![a-z0-9])",
                    url,
                    re.IGNORECASE,
                )
                for reference in references
            )

        structured = self._source_records(text, None)
        structured_matches = [
            record["url"] for record in structured if matches_reference(record["url"])
        ]
        if len(structured_matches) == 1:
            return structured_matches[0]

        for match in STANDALONE_SOURCE_URL_RE.finditer(text[:4_000]):
            candidate = self._safe_source_url(match.group(1))
            if candidate is not None and matches_reference(candidate):
                return candidate
        return None

    def _retrieval_source_url(
        self,
        tool: str,
        arguments: dict,
        text: str,
    ) -> Optional[str]:
        if self._is_discovery_operation(tool):
            return None
        direct = self._source_url(arguments)
        if direct is not None:
            return direct
        return self._returned_retrieval_url(
            text,
            self._retrieval_reference_values(arguments),
        )

    @staticmethod
    def _identifier_terms(value: Any) -> set[str]:
        separated = re.sub(
            r"(?<=[a-z0-9])(?=[A-Z])",
            "_",
            str(value),
        )
        return {term for term in re.split(r"[^a-z0-9]+", separated.lower()) if term}

    def _is_discovery_operation(self, tool: str) -> bool:
        operation = str(tool).rsplit(".", 1)[-1]
        return bool(self._identifier_terms(operation) & DISCOVERY_OPERATION_TERMS)

    def _upstream_omission_reasons(
        self,
        result: Any,
        text: Optional[str] = None,
    ) -> list[str]:
        reasons = []
        parsed = result
        while isinstance(parsed, str):
            try:
                decoded = json.loads(parsed)
            except (TypeError, ValueError, json.JSONDecodeError):
                break
            if decoded == parsed:
                break
            parsed = decoded
        if isinstance(parsed, dict) and (
            parsed.get("content_complete") is False
            or parsed.get("truncated") is True
            or parsed.get("has_more_content") is True
        ):
            reasons.append("structured_incomplete_flag")
        evidence = text if isinstance(text, str) else _serialize(result)
        for reason, pattern in UPSTREAM_OMISSION_PATTERNS:
            if pattern.search(evidence):
                reasons.append(reason)
        return reasons

    @staticmethod
    def _artifact_omission_metadata(artifact: dict) -> dict:
        reasons = artifact.get("upstream_omission_reasons")
        if not isinstance(reasons, list) or not reasons:
            return {}
        return {
            "content_complete": False,
            "upstream_content_incomplete": True,
            "upstream_omission_reasons": list(reasons),
            "upstream_omission_guidance": UPSTREAM_OMISSION_GUIDANCE,
        }

    def _source_records(self, text: str, primary_url: Optional[str]) -> list[dict]:
        records = []
        seen = set()
        retained_chars = 0
        visited = 0

        def add(url: Any, title: Any, content: Any) -> None:
            nonlocal retained_chars
            safe_url = self._safe_source_url(url)
            if (
                safe_url is None
                or safe_url in seen
                or len(records) >= MAX_SOURCE_URLS
                or retained_chars + len(safe_url) > MAX_SOURCE_URL_CHARS
            ):
                return
            records.append(
                {
                    "url": safe_url,
                    "title": (
                        title[:300] if isinstance(title, str) and title else safe_url
                    ),
                    "content": content[:1_000] if isinstance(content, str) else "",
                }
            )
            seen.add(safe_url)
            retained_chars += len(safe_url)

        def visit(value: Any, depth: int = 0) -> None:
            nonlocal visited
            if depth >= 6 or visited >= 100 or len(records) >= MAX_SOURCE_URLS:
                return
            visited += 1
            if isinstance(value, str):
                if len(value) > self.valves.maximum_artifact_chars:
                    return
                try:
                    decoded = json.loads(value)
                except (ValueError, RecursionError):
                    return
                visit(decoded, depth + 1)
            elif isinstance(value, list):
                for item in value[:100]:
                    visit(item, depth + 1)
            elif isinstance(value, dict):
                if (
                    value.get("error")
                    or value.get("isError")
                    or value.get("success") is False
                    or value.get("ok") is False
                    or value.get("status") in ("error", "failed")
                ):
                    return
                url = value.get("contentUrl") or value.get("url") or value.get("link")
                if url:
                    add(
                        url,
                        value.get("title") or value.get("name"),
                        value.get("content")
                        or value.get("snippet")
                        or value.get("description")
                        or value.get("text"),
                    )
                    return
                for key in ("results", "items", "data", "sources"):
                    if key in value:
                        visit(value[key], depth + 1)
                if value.get("type") == "text":
                    visit(value.get("text"), depth + 1)

        if primary_url:
            heading = re.search(r"^#\s+(.+)$", text, re.MULTILINE)
            add(primary_url, heading.group(1) if heading else None, text)
        else:
            visit(text)
        return records

    def _source_urls(self, text: str, primary_url: Optional[str]) -> list[str]:
        return [source["url"] for source in self._source_records(text, primary_url)]

    def _compact_search_schema(self, value: Any, depth: int = 0) -> Any:
        if depth >= self.valves.maximum_search_schema_depth:
            return {
                "description": (
                    "Nested schema omitted from discovery; execution still uses the full schema."
                )
            }
        if isinstance(value, list):
            return [
                self._compact_search_schema(item, depth + 1)
                for item in value[: self.valves.maximum_search_enum_items]
            ]
        if not isinstance(value, dict):
            return value

        compacted = {}
        for key, item in value.items():
            if key == "$schema":
                continue
            if key == "description":
                compacted[key] = self._bounded_text(
                    item,
                    self.valves.parameter_description_chars,
                )
            elif key == "enum" and isinstance(item, list):
                compacted[key] = item[: self.valves.maximum_search_enum_items]
                if len(item) > self.valves.maximum_search_enum_items:
                    compacted["x-enum-values-omitted"] = (
                        len(item) - self.valves.maximum_search_enum_items
                    )
            elif key == "properties" and isinstance(item, dict):
                retained = list(item.items())[: self.valves.maximum_search_properties]
                compacted[key] = {
                    property_name: self._compact_search_schema(
                        property_schema,
                        depth + 1,
                    )
                    for property_name, property_schema in retained
                }
                if len(item) > len(retained):
                    compacted["x-properties-omitted"] = len(item) - len(retained)
            else:
                compacted[key] = self._compact_search_schema(item, depth + 1)

        if len(_serialize(compacted)) <= self.valves.maximum_search_schema_chars:
            return compacted

        properties = compacted.get("properties")
        summarized_properties = {}
        if isinstance(properties, dict):
            for name, schema in properties.items():
                if isinstance(schema, dict) and isinstance(schema.get("type"), str):
                    summarized_properties[name] = {"type": schema["type"]}
                else:
                    summarized_properties[name] = {}
        summary = {
            "type": compacted.get("type", "object"),
            "properties": summarized_properties,
            "required": compacted.get("required", []),
            "description": (
                "Detailed schema omitted from discovery; execution still validates the full schema."
            ),
        }
        if len(_serialize(summary)) <= self.valves.maximum_search_schema_chars:
            return summary
        return {
            "type": compacted.get("type", "object"),
            "required": compacted.get("required", [])[
                : self.valves.maximum_search_properties
            ],
            "description": (
                "Argument schema omitted from discovery because it exceeds the safety limit."
            ),
        }

    def _bound_search_response(self, response: dict) -> str:
        serialized = _serialize(response)
        if len(serialized) <= self.valves.maximum_inline_result_chars:
            return serialized

        for match in reversed(response.get("matches", [])):
            arguments = match.get("arguments")
            required = (
                arguments.get("required", []) if isinstance(arguments, dict) else []
            )
            match["arguments"] = {
                "type": "object",
                "required": required[: self.valves.maximum_search_properties],
                "description": (
                    "Detailed schema omitted because the complete search response exceeded "
                    "the inline safety limit."
                ),
            }
            serialized = _serialize(response)
            if len(serialized) <= self.valves.maximum_inline_result_chars:
                return serialized

        response.pop("available_servers", None)
        warnings = response.get("warnings")
        if isinstance(warnings, list):
            response["warnings"] = [
                self._bounded_text(warning, 240) for warning in warnings[:3]
            ]
            if len(warnings) > 3:
                response["warnings_omitted"] = len(warnings) - 3
        serialized = _serialize(response)
        if len(serialized) <= self.valves.maximum_inline_result_chars:
            return serialized

        matches = response.get("matches")
        first_match = matches[0] if isinstance(matches, list) and matches else {}
        minimal_match = {
            "server": self._bounded_text(first_match.get("server", ""), 160),
            "tool": self._bounded_text(
                first_match.get("tool", ""),
                500,
            ),
            "description": self._bounded_text(
                first_match.get("description", ""),
                240,
            ),
        }
        minimal = {
            "catalog_tools": response.get("catalog_tools", 0),
            "matching_tools": response.get("matching_tools", 0),
            "returned_tools": 1 if first_match else 0,
            "matches": [minimal_match] if first_match else [],
            "warnings": response.get("warnings", [])[:1],
            "warning": (
                "Search output was reduced to remain within the configured inline limit."
            ),
        }
        for key in ("discovery_origin", "catalog_coverage"):
            if key in response:
                minimal[key] = response[key]
        if "inventory" in response:
            # This is a truncated tool inventory, not a relevance match.
            # Preserve that identity so the model doesn't mistake the single
            # surviving item for the full/best answer to its query.
            minimal["inventory"] = response["inventory"]
            minimal["inventory_complete"] = False
            minimal["inventory_remaining"] = max(
                0,
                response.get("matching_tools", 0) - minimal["returned_tools"],
            )
            minimal["next_action"] = (
                "This inventory was truncated to fit the response size limit. "
                "Do not treat the single listed item as the answer to the user's "
                "request; use a specific relevance search instead."
            )
        serialized = _serialize(minimal)
        if len(serialized) <= self.valves.maximum_inline_result_chars:
            return serialized

        fallback = {
            "returned_tools": 1 if first_match else 0,
            "matches": (
                [
                    {
                        "tool": self._bounded_text(
                            first_match.get("tool", ""),
                            500,
                        )
                    }
                ]
                if first_match
                else []
            ),
            "warning": "Search output exceeded the configured inline limit.",
        }
        for key in ("discovery_origin", "catalog_coverage"):
            if key in response:
                fallback[key] = response[key]
        if "inventory" in response:
            fallback["inventory"] = response["inventory"]
            fallback["inventory_complete"] = False
        return _serialize(fallback)

    def _focused_argument_schema(
        self,
        parameters: dict,
        arguments: dict,
    ) -> str:
        properties = parameters.get("properties")
        if not isinstance(properties, dict):
            return ""

        required = parameters.get("required", [])
        required_names = [name for name in required if isinstance(name, str)]
        relevant_names = list(dict.fromkeys([*arguments.keys(), *required_names]))[
            : self.valves.maximum_search_properties
        ]
        relevant_properties = {
            name: self._compact_search_schema(properties[name])
            for name in relevant_names
            if name in properties
        }
        if not relevant_properties:
            return ""

        focused = {
            "type": "object",
            "properties": relevant_properties,
            "required": required_names,
            "additionalProperties": parameters.get("additionalProperties", True),
        }
        text = _serialize(focused)
        if len(text) > self.valves.maximum_search_schema_chars:
            focused = {
                "type": "object",
                "properties": {
                    name: (
                        {"type": schema.get("type")}
                        if isinstance(schema, dict)
                        and isinstance(schema.get("type"), str)
                        else schema
                    )
                    for name, schema in relevant_properties.items()
                },
                "required": required_names,
            }
            text = _serialize(focused)
        return self._bounded_text(
            text,
            self.valves.maximum_search_schema_chars,
        )

    @staticmethod
    def _match_sort_key(item: dict) -> tuple:
        rank_order = item.get("_rank_order")
        if not isinstance(rank_order, int) or rank_order < 0:
            rank_order = 1_000_000_000
        return (-item["score"], rank_order, item["tool"])

    def _select_search_matches(self, matches: list[dict], limit: int) -> list[dict]:
        ranked = sorted(matches, key=self._match_sort_key)
        if limit <= 1 or not ranked:
            return ranked[:limit]
        top = ranked[0]
        top_score = max(1, top["score"])
        top_evidence = set(top.get("_subject_evidence", ()))

        def sufficiently_relevant(match: dict) -> bool:
            if match is top:
                return True
            if match["score"] < self.valves.minimum_diverse_match_score:
                return False
            if match["score"] / top_score < self.valves.minimum_secondary_score_ratio:
                return False
            if match.get("server") == top.get("server"):
                return True
            evidence = set(match.get("_subject_evidence", ()))
            if "_subject_evidence" not in top or "_subject_evidence" not in match:
                return True
            if not top_evidence:
                return True
            return bool(top_evidence & evidence)

        return [match for match in ranked if sufficiently_relevant(match)][:limit]

    def _externalize_result(
        self,
        request: Request,
        user: Any,
        tool: str,
        arguments: dict,
        result: Any,
    ) -> str:
        text = _serialize(result)
        omission_reasons = self._upstream_omission_reasons(result, text)
        if (
            len(text) <= self.valves.maximum_inline_result_chars
            and not omission_reasons
        ):
            return text

        source_url = self._retrieval_source_url(tool, arguments, text)
        source_metadata = {"source_url": source_url} if source_url else {}
        if omission_reasons:
            source_metadata.update(
                {
                    "content_complete": False,
                    "upstream_content_incomplete": True,
                    "upstream_omission_reasons": omission_reasons,
                    "upstream_omission_guidance": UPSTREAM_OMISSION_GUIDANCE,
                }
            )
        source_urls = self._source_urls(text, source_url)
        if source_urls:
            source_metadata["source_urls"] = source_urls
            source_metadata["citation_guidance"] = (
                "When citing evidence from this result, copy relevant source_url "
                "or source_urls values verbatim without normalizing them. A URL "
                "extracted from search results identifies its source but does not "
                "by itself prove that the complete page was retrieved."
            )
        if "\ufffd" in text:
            source_metadata["text_integrity_warning"] = (
                "The upstream result contains Unicode replacement characters. "
                "Do not reproduce those glyphs or quote affected text verbatim; "
                "paraphrase it with unambiguous ASCII punctuation."
            )
        if len(text) <= self.valves.maximum_inline_result_chars:
            inline = _serialize(
                {
                    "inline_result": True,
                    "tool": tool,
                    "source_tool_already_executed": True,
                    **source_metadata,
                    "total_characters": len(text),
                    "result": result,
                    "evidence_guidance": (
                        "Use only the returned portions as evidence. The source "
                        "tool explicitly omitted other content. If an omitted fact "
                        "is required, use at most one materially different "
                        "authorized full, raw, detail, or selected-record retrieval. "
                        "Never repeat the same call; otherwise disclose the omission."
                    ),
                    "next_action": (
                        "answer_if_supported_else_use_fuller_retrieval_or_disclose"
                    ),
                }
            )
            if len(inline) <= self.valves.maximum_inline_result_chars:
                return inline
        owner_id = _user_id(user)
        if owner_id == "anonymous":
            return _serialize(
                {
                    "tool": tool,
                    **source_metadata,
                    "total_characters": len(text),
                    "preview": text[: self.valves.artifact_preview_chars],
                    "error": (
                        "The full result was not cached because Open WebUI did not "
                        "provide an authenticated user identity."
                    ),
                }
            )

        preview_limit = min(self.valves.artifact_preview_chars, len(text))
        head_size = round(preview_limit * 0.7)
        tail_size = preview_limit - head_size
        tail_start = len(text) - tail_size
        omitted_chars = max(0, tail_start - head_size)
        preview = text[:head_size]
        if tail_size:
            preview += (
                f"\n\n[... {len(text) - preview_limit} characters stored in artifact ...]\n\n"
                f"{text[-tail_size:]}"
            )

        artifact_limit = min(
            self.valves.maximum_artifact_chars,
            self.valves.maximum_artifact_chars_per_user,
            self.valves.maximum_artifact_chars_total,
        )
        if len(text) > artifact_limit:
            return _serialize(
                {
                    "tool": tool,
                    **source_metadata,
                    "total_characters": len(text),
                    "preview": preview,
                    "preview_head_end": head_size,
                    "preview_tail_start": tail_start,
                    "omitted_characters": omitted_chars,
                    "error": (
                        "The full result was not cached because it exceeds the "
                        f"{artifact_limit:,}-character artifact safety limit."
                    ),
                }
            )

        state = self._state(request)
        user_artifacts = [
            (item_key, item)
            for item_key, item in state["artifacts"].items()
            if item.get("owner_id") == owner_id or item_key.startswith(f"{owner_id}:")
        ]
        user_artifacts.sort(key=lambda item: item[1].get("last_accessed_at", 0))
        retained_chars = sum(
            item.get("characters", len(item.get("content", "")))
            for _, item in user_artifacts
        )
        while user_artifacts and (
            len(user_artifacts) >= self.valves.maximum_artifacts_per_user
            or retained_chars + len(text) > self.valves.maximum_artifact_chars_per_user
        ):
            oldest_key, oldest = user_artifacts.pop(0)
            retained_chars -= oldest.get(
                "characters",
                len(oldest.get("content", "")),
            )
            state["artifacts"].pop(oldest_key, None)

        all_artifacts = sorted(
            state["artifacts"].items(),
            key=lambda item: item[1].get("last_accessed_at", 0),
        )
        total_artifact_chars = sum(
            item.get("characters", len(item.get("content", "")))
            for _, item in all_artifacts
        )
        while all_artifacts and (
            total_artifact_chars + len(text) > self.valves.maximum_artifact_chars_total
        ):
            oldest_key, oldest = all_artifacts.pop(0)
            total_artifact_chars -= oldest.get(
                "characters",
                len(oldest.get("content", "")),
            )
            state["artifacts"].pop(oldest_key, None)

        artifact_id = uuid4().hex
        key = f"{owner_id}:{artifact_id}"
        now = time.time()
        state["artifacts"][key] = {
            "owner_id": owner_id,
            "content": text,
            "characters": len(text),
            "tool": tool,
            "source_url": source_url,
            "source_urls": source_urls,
            "upstream_omission_reasons": omission_reasons,
            "text_integrity_warning": source_metadata.get("text_integrity_warning"),
            "preview_head_end": head_size,
            "preview_tail_start": tail_start,
            "last_accessed_at": now,
            "expires_at": now + self.valves.artifact_ttl_minutes * 60,
        }

        first_read_limit = min(12_000, omitted_chars)
        return _serialize(
            {
                "externalized": True,
                "artifact_id": artifact_id,
                "tool": tool,
                "source_tool_already_executed": True,
                **source_metadata,
                "total_characters": len(text),
                "preview": preview,
                "preview_head_end": head_size,
                "preview_tail_start": tail_start,
                "omitted_characters": omitted_chars,
                "artifact_guidance": (
                    (
                        "Answer only from returned evidence. The artifact stores the "
                        "complete payload returned by the source tool, but that payload "
                        "explicitly declares upstream omissions. Artifact reads cannot "
                        "recover omitted source content. If an omitted fact is required, "
                        "make at most one materially different authorized raw, full, or "
                        "detail retrieval; otherwise disclose the limitation. Never "
                        "repeat the same source call."
                    )
                    if omission_reasons
                    else (
                        "Answer from the preview when it contains the needed information. "
                        "Do not read the artifact merely because it exists. If required "
                        "evidence is omitted, prefer one targeted artifact.read call with "
                        'arguments={"artifact_id":"'
                        f'{artifact_id}","query":"specific terms","limit":8000}}. '
                        "Otherwise read one supplied chunk and reassess. Hard limits allow "
                        "three artifact reads per message and two per artifact by default. "
                        "The source tool already ran and its full result is stored; "
                        "do not execute that source tool again."
                    )
                ),
                "next_step": (
                    (
                        "If the omitted upstream material is required, use one "
                        "materially different authorized raw, full, or detail mode. "
                        "If no such mode exists or it fails, disclose that the source "
                        "content was not inspected."
                    )
                    if omission_reasons
                    else (
                        f"Only if required evidence is omitted, call execute_tool "
                        f'with tool="{ARTIFACT_TOOL}". Prefer arguments='
                        f'{{"artifact_id":"{artifact_id}","query":"specific missing fact",'
                        f'"limit":8000}}. Use arguments={{"artifact_id":"{artifact_id}",'
                        f'"offset":{head_size},"limit":{first_read_limit}}} only when the '
                        "needed location is already known."
                    )
                ),
            }
        )

    def _normalize_mcp_result(self, result: Any) -> Any:
        if not isinstance(result, list) or not result:
            return result
        if all(
            isinstance(item, dict)
            and item.get("type") == "text"
            and isinstance(item.get("text"), str)
            for item in result
        ):
            return "\n".join(item["text"] for item in result)
        return result

    def _artifact_passages(self, content: str, query: str, limit: int) -> list[dict]:
        terms = sorted(
            {
                term
                for term in re.findall(SEARCH_TOKEN_RE, query.lower())
                if term not in SEARCH_STOP_WORDS and term not in SEARCH_OPERATION_TERMS
            }
        )[:24]
        if not terms:
            terms = [query.strip()]
        patterns = [
            re.compile(r"(?<!\w)" + re.escape(term) + r"(?!\w)", re.IGNORECASE)
            for term in terms
        ]
        # Treat JSON-escaped separators as boundaries without shifting offsets
        # or changing the evidence returned to the caller.
        searchable = re.sub(
            r'\\(?:u([0-9a-fA-F]{4})|[nrt"])',
            lambda match: (
                match.group(0)
                if match.group(1)
                and (
                    chr(int(match.group(1), 16)).isalnum()
                    or chr(int(match.group(1), 16)) == "_"
                )
                else " " * len(match.group(0))
            ),
            content,
        )
        window = min(1600, max(400, limit))
        stride = max(1, window * 3 // 4)
        candidates = []
        frequencies = [0] * len(terms)
        # Scan every bounded passage, rather than letting one common query term
        # consume the match allowance before later terms are considered.
        for start in range(0, len(content), stride):
            end = min(len(content), start + window)
            passage = searchable[start:end]
            matched = tuple(
                i for i, pattern in enumerate(patterns) if pattern.search(passage)
            )
            if matched:
                heading_coverage = 0
                for heading in re.findall(
                    r"(?m)^[ \t]{0,3}#{1,6}[ \t]+[^\r\n]*",
                    passage,
                ):
                    heading_coverage = max(
                        heading_coverage,
                        sum(bool(pattern.search(heading)) for pattern in patterns),
                    )
                events = []
                for i in matched:
                    events.extend(
                        (occurrence.start(), occurrence.end(), i)
                        for occurrence in list(patterns[i].finditer(passage))[:16]
                    )
                events.sort()
                counts = {}
                left = 0
                minimum_span = len(passage) + 1
                for event in events:
                    counts[event[2]] = counts.get(event[2], 0) + 1
                    while len(counts) == len(matched):
                        minimum_span = min(
                            minimum_span,
                            event[1] - events[left][0],
                        )
                        left_index = events[left][2]
                        counts[left_index] -= 1
                        if counts[left_index] == 0:
                            counts.pop(left_index)
                        left += 1
                candidates.append(
                    (start, end, matched, heading_coverage, minimum_span),
                )
                for i in matched:
                    frequencies[i] += 1
            if end == len(content):
                break
        ranked = sorted(
            candidates,
            key=lambda item: (
                -len(item[2]),
                -item[3],
                -sum(1 / frequencies[i] for i in item[2]),
                item[4],
                item[0],
            ),
        )
        selected = []
        retained = 0
        for start, end, _, _, _ in ranked:
            if retained >= limit or len(selected) >= 5:
                break
            if any(
                start < match["end"] and end > match["offset"] for match in selected
            ):
                continue
            end = min(end, start + limit - retained)
            selected.append(
                {
                    "offset": start,
                    "end": end,
                    "content": content[start:end],
                }
            )
            retained += end - start
        return selected

    async def _artifact_passages_for_read(
        self,
        content: str,
        query: str,
        limit: int,
    ) -> list[dict]:
        if len(content) >= ARTIFACT_THREADED_SCAN_CHARS:
            return await asyncio.to_thread(
                self._artifact_passages,
                content,
                query,
                limit,
            )
        return self._artifact_passages(content, query, limit)

    async def _read_artifact(self, request: Request, user: Any, arguments: dict) -> str:
        owner_id = _user_id(user)
        if owner_id == "anonymous":
            return "Error: artifact.read requires an authenticated Open WebUI user."

        artifact_id = arguments.get("artifact_id")
        if not isinstance(artifact_id, str) or not artifact_id:
            return "Error: artifact.read requires a non-empty artifact_id."

        try:
            offset = max(0, int(arguments.get("offset", 0)))
            limit = min(50_000, max(1, int(arguments.get("limit", 12_000))))
        except (TypeError, ValueError):
            return "Error: artifact.read offset and limit must be integers."

        key = f"{owner_id}:{artifact_id}"
        artifact = self._state(request)["artifacts"].get(key)
        if artifact is None:
            return ARTIFACT_NOT_FOUND_ERROR

        artifact["last_accessed_at"] = time.time()
        content = artifact["content"]
        omission_metadata = self._artifact_omission_metadata(artifact)
        upstream_incomplete = bool(omission_metadata)
        preview_tail_start = artifact.get("preview_tail_start", len(content))
        query = arguments.get("query")
        if query is not None:
            if not isinstance(query, str) or not query.strip():
                return "Error: artifact.read query must be a non-empty string."
            if len(query) > 200:
                return "Error: artifact.read query must not exceed 200 characters."
            if offset:
                return "Error: artifact.read query cannot be combined with a nonzero offset."
            matches = await self._artifact_passages_for_read(content, query, limit)
            return _serialize(
                {
                    "artifact_id": artifact_id,
                    "tool": artifact["tool"],
                    "source_tool_already_executed": True,
                    **omission_metadata,
                    **(
                        {"source_url": artifact["source_url"]}
                        if artifact.get("source_url")
                        else {}
                    ),
                    **(
                        {"source_urls": artifact["source_urls"]}
                        if artifact.get("source_urls")
                        else {}
                    ),
                    **(
                        {"text_integrity_warning": artifact["text_integrity_warning"]}
                        if artifact.get("text_integrity_warning")
                        else {}
                    ),
                    "query": query,
                    "match_count": len(matches),
                    "matches": matches,
                    "next_action": (
                        "answer_if_supported_else_use_fuller_retrieval_or_disclose"
                        if upstream_incomplete
                        else (
                            "answer_from_accumulated_evidence"
                            if matches
                            else "use_existing_artifact_evidence"
                        )
                    ),
                    "total_characters": len(content),
                    "artifact_guidance": (
                        (
                            "Passages cover only the payload returned by the source "
                            "tool; they cannot recover explicitly omitted upstream "
                            "content. Use supported evidence only. If the omitted fact "
                            "is required, make at most one materially different "
                            "authorized raw, full, or detail retrieval; otherwise "
                            "disclose the limitation."
                        )
                        if upstream_incomplete
                        else (
                            "Passages are ranked by distinct query-term coverage and "
                            "rarity, not proof of a claim. Use only relevant matches "
                            "with the existing preview. "
                            "Stop reading when required evidence is sufficient. The "
                            "source tool already ran; do not execute it again."
                        )
                    ),
                }
            )
        end = max(
            offset,
            min(len(content), preview_tail_start, offset + limit),
        )
        return _serialize(
            {
                "artifact_id": artifact_id,
                "tool": artifact["tool"],
                "source_tool_already_executed": True,
                **omission_metadata,
                **(
                    {"source_url": artifact["source_url"]}
                    if artifact.get("source_url")
                    else {}
                ),
                **(
                    {"source_urls": artifact["source_urls"]}
                    if artifact.get("source_urls")
                    else {}
                ),
                **(
                    {"text_integrity_warning": artifact["text_integrity_warning"]}
                    if artifact.get("text_integrity_warning")
                    else {}
                ),
                **(
                    {
                        "citation_guidance": (
                            "When citing evidence from this artifact, copy relevant "
                            "source_url or source_urls values verbatim. Search-result "
                            "URLs identify sources but do not by themselves establish "
                            "that complete pages were retrieved."
                        )
                    }
                    if artifact.get("source_urls")
                    else {}
                ),
                "offset": offset,
                "next_offset": end if end < preview_tail_start else None,
                "artifact_complete_for_reading": end >= preview_tail_start,
                "next_action": (
                    "answer_if_supported_else_use_fuller_retrieval_or_disclose"
                    if upstream_incomplete and end >= preview_tail_start
                    else (
                        "answer_from_accumulated_evidence"
                        if end >= preview_tail_start
                        else "read_next_chunk_only_if_required"
                    )
                ),
                "preview_tail_start": preview_tail_start,
                "total_characters": len(content),
                "content": content[offset:end],
                "artifact_guidance": (
                    (
                        "Use this chunk only as returned evidence. "
                        "artifact_complete_for_reading means the stored payload has "
                        "been exposed; it does not mean the upstream source was "
                        "complete. Artifact reads cannot recover explicitly omitted "
                        "content. Use one materially different authorized raw, full, "
                        "or detail retrieval only when the omitted fact is required; "
                        "otherwise disclose the limitation."
                    )
                    if upstream_incomplete
                    else (
                        "Use this chunk with the existing preview. Stop reading when the "
                        "required evidence is sufficient. Prefer a targeted query instead "
                        "of another sequential page. The source tool already ran; "
                        "do not execute it again. When artifact_complete_for_reading is "
                        "true, answer now from the accumulated evidence."
                    )
                ),
            }
        )

    async def _read_artifact_once(
        self,
        request: Request,
        user: Any,
        metadata: Optional[dict],
        arguments: dict,
    ) -> str:
        usage_key = self._turn_usage_key(user, metadata)
        if usage_key is None:
            return await self._read_artifact(request, user, arguments)
        user_id, key = usage_key
        state = self._usage_state(request)
        response = await self._read_artifact(request, user, arguments)
        async with state["lock"]:
            if response.startswith("Error:"):
                if response == ARTIFACT_NOT_FOUND_ERROR:
                    now = time.monotonic()
                    records = state["records"]
                    self._prune_usage_records(records, now, user_id, key)
                    record = self._usage_record(records, user_id, key, now)
                    artifact_id = arguments.get("artifact_id")
                    if isinstance(artifact_id, str):
                        record["failed_artifact_ids"].add(artifact_id)
                return response
            payload = json.loads(response)
            now = time.monotonic()
            records = state["records"]
            self._prune_usage_records(records, now, user_id, key)
            record = self._usage_record(records, user_id, key, now)
            artifact_id = payload["artifact_id"]
            history = record["artifact_read_ranges"]
            previous = history.get(artifact_id, [])
            spans = payload.get("matches")
            if spans is None:
                spans = [
                    {
                        "offset": payload["offset"],
                        "end": payload["offset"] + len(payload["content"]),
                        "content": payload["content"],
                    }
                ]
            fresh = []
            candidate_chars = 0
            for span in spans:
                start, end = span["offset"], span["end"]
                candidate_chars += end - start
                cursor = start
                for old_start, old_end in previous:
                    if old_end <= cursor:
                        continue
                    if old_start >= end:
                        break
                    if old_start > cursor:
                        fresh.append(
                            {
                                "offset": cursor,
                                "end": old_start,
                                "content": span["content"][
                                    cursor - start : old_start - start
                                ],
                            }
                        )
                    cursor = max(cursor, old_end)
                    if cursor >= end:
                        break
                if cursor < end:
                    fresh.append(
                        {
                            "offset": cursor,
                            "end": end,
                            "content": span["content"][cursor - start : end - start],
                        }
                    )

            merged = []
            for start, end in sorted(
                [*previous, *((span["offset"], span["end"]) for span in fresh)]
            ):
                if merged and start <= merged[-1][1]:
                    merged[-1] = (merged[-1][0], max(merged[-1][1], end))
                else:
                    merged.append((start, end))
            if len(merged) > MAX_ARTIFACT_READ_RANGES:
                return (
                    "Error: artifact range tracking limit reached. Stop reading "
                    "this artifact and use the evidence already returned."
                )
            history[artifact_id] = merged
            new_chars = sum(span["end"] - span["offset"] for span in fresh)
            omitted = candidate_chars - new_chars
            payload["new_characters"] = new_chars
            payload["omitted_repeated_characters"] = omitted
            payload["no_new_evidence"] = new_chars == 0 and candidate_chars > 0
            if omitted:
                payload.pop("content", None)
                payload["matches"] = fresh
                payload["match_count"] = len(fresh)
                payload["artifact_guidance"] = (
                    "No new evidence: all matching ranges were already returned "
                    "for this user message. Use the earlier text; do not retry "
                    "equivalent queries."
                    if not fresh
                    else "Previously returned ranges were omitted. Combine these "
                    "new ranges with the earlier evidence and stop when sufficient."
                )
                if payload.get("upstream_content_incomplete") is True:
                    payload["artifact_guidance"] += (
                        " The stored payload is still upstream-incomplete; "
                        "artifact reads cannot recover omitted source content."
                    )
                    payload["next_action"] = (
                        "answer_if_supported_else_use_fuller_retrieval_or_disclose"
                    )
            return _serialize(payload)

    def _routing_shortlist(self, request, owner_id, query, loaded):
        if _RANKING_TERMS_CACHE.get() is None:
            with self._ranking_cache_scope():
                return self._routing_shortlist(request, owner_id, query, loaded)
        state = self._state(request)
        catalogs = {alias: specs for alias, specs, _ in loaded}
        catalog_revisions = {
            id(catalog["specs"]): catalog.get("routing_revision")
            for catalog in state["catalogs"].values()
            if catalog.get("owner_id") == owner_id
        }
        revision = self._usage_signature(
            [
                self._routing_protocol,
                [
                    (
                        alias,
                        catalog_revisions.get(id(specs))
                        or self._usage_signature(specs),
                    )
                    for alias, specs in sorted(catalogs.items())
                ],
            ]
        )
        indexes = state["routing_indexes"]
        index = indexes.get(owner_id)
        reused = bool(index and index.get("revision") == revision)
        if not reused:
            index = self._build_bm25_index(
                catalogs,
                revision,
                include_legacy_features=self._ranking_mode() == "hybrid",
            )
            indexes.pop(owner_id, None)
            while indexes and (
                len(indexes) >= ROUTING_INDEX_MAX_USERS
                or sum(
                    item["characters"] + item.get("feature_characters", 0)
                    for item in indexes.values()
                )
                + index["characters"]
                + index.get("feature_characters", 0)
                > ROUTING_INDEX_TOTAL_CHARS
            ):
                indexes.pop(min(indexes, key=lambda key: indexes[key]["last_used"]))
            indexes[owner_id] = index
        index["last_used"] = time.time()

        bm25_scores = self._bm25_scores(query, index)
        row_numbers = {key: number for number, key in enumerate(index["rows"])}
        selected = {
            row_numbers[key]
            for key, _ in sorted(
                bm25_scores.items(),
                key=lambda item: (-item[1], item[0]),
            )[:ROUTING_MAX_CANDIDATES]
            if key in row_numbers
        }
        if self._ranking_mode() == "hybrid":
            features_token = _RANKING_FEATURES_CACHE.set(
                index.get("legacy_features")
                if isinstance(index.get("legacy_features"), dict)
                else None
            )
            try:
                legacy = self._rank_catalogs_legacy(query, catalogs)
            finally:
                _RANKING_FEATURES_CACHE.reset(features_token)
            selected.update(
                row_numbers[(alias, str(spec.get("name", "")))]
                for alias, spec, _ in legacy[:20]
                if (alias, str(spec.get("name", ""))) in row_numbers
            )
        subjects = self._ranking_terms(self._ranking_context(query))
        if self._explicit_subject_terms(query) and subjects & RESOURCE_INTENT_TERMS:
            selected.update(index["resources"])
        for alias in {index["rows"][number][0] for number in selected}:
            selected.update(index["companions"].get(alias, ()))
        diagnostic = {
            "index_entries": len(index["rows"]),
            "index_terms": len(index["postings"]),
            "index_reused": reused,
            "candidate_count": len(selected),
            "coverage": "partial" if index["partial"] else "complete",
            "ranking_mode": self._ranking_mode(),
        }
        if len(selected) > ROUTING_MAX_CANDIDATES:
            diagnostic["reason"] = "Candidate set too broad for automatic discovery."
            return [], diagnostic, bm25_scores
        keys = {index["rows"][number] for number in selected}
        narrowed = [
            (
                alias,
                [spec for spec in specs if (alias, str(spec.get("name", ""))) in keys],
                warning,
            )
            for alias, specs, warning in loaded
        ]
        return narrowed, diagnostic, bm25_scores

    async def _routing_snapshot(self, request, user_model):
        """Partition the user's accessible MCP servers into warm and cold."""
        servers, _, connections = await self._servers()
        owner_id = str(user_model.id)
        self._prune_inactive_catalogs(
            request,
            owner_id,
            {
                server_id
                for server_id, connection in connections.items()
                if self._connection_enabled(connection)
            },
        )
        state = self._state(request)
        loaded, cold, scope = [], [], []
        for alias, server_id in servers.items():
            connection = connections.get(server_id)
            key = f"{owner_id}:{server_id}"
            if not self._connection_enabled(
                connection
            ) or not await has_connection_access(user_model, connection):
                state["catalogs"].pop(key, None)
                continue
            fingerprint = self._connection_fingerprint(connection)
            scope.append(f"{server_id}:{fingerprint}")
            cached = state["catalogs"].get(key)
            if cached and cached.get("connection_fingerprint") == fingerprint:
                cached["last_accessed_at"] = time.time()
                loaded.append((alias, cached["specs"], None))
            else:
                state["catalogs"].pop(key, None)
                cold.append((alias, server_id, connection))
        digest = hashlib.sha256(_serialize(sorted(scope)).encode("utf-8")).hexdigest()
        return loaded, cold, digest

    def _claim_catalog_warm(
        self,
        request: Request,
        owner_id: str,
        scope: str,
    ) -> bool:
        """Reserve this user's catalog warm for the current backoff window.

        The reservation is recorded synchronously, before any awaited work, so
        concurrent requests for the same user cannot both claim it. It is
        recorded pessimistically, with a short retry window that survives a
        crash or cancellation mid-preparation; only a fully successful warm is
        extended to the catalog TTL by ``_settle_catalog_warm``. Repeated
        partial warms back off exponentially toward that TTL, so an unreachable
        server is never retried on every later message.
        """
        state = self._state(request)
        warm = state.setdefault("catalog_warm", {})
        now = time.time()
        ttl = max(1, int(self.valves.catalog_cache_seconds))
        for key, record in list(warm.items()):
            # Keep a lapsed record briefly so its backoff survives the next
            # claim; drop it once the user has been idle well past the window.
            if record.get("expires_at", 0) + ttl <= now:
                warm.pop(key, None)
        record = warm.get(owner_id)
        matched = bool(record) and record.get("scope") == scope
        if matched and record.get("expires_at", 0) > now:
            return False
        attempts = int(record.get("attempts", 0)) + 1 if matched else 1
        warm.pop(owner_id, None)
        while len(warm) >= CATALOG_WARM_MAX_USERS:
            warm.pop(next(iter(warm)), None)
        warm[owner_id] = {
            "scope": scope,
            "attempts": attempts,
            "expires_at": now
            + min(
                ttl,
                CATALOG_WARM_RETRY_SECONDS * 2 ** min(attempts - 1, 8),
            ),
        }
        return True

    def _settle_catalog_warm(
        self,
        request: Request,
        owner_id: str,
        scope: str,
        complete: bool,
        progressed: bool = False,
    ) -> None:
        """Record the outcome of a claimed catalog warm.

        A complete warm is extended to the catalog TTL. A partial warm that
        still loaded servers keeps the short retry window, because each attempt
        caches what it finished and the next one only has to load the
        remainder; doubling the wait there would punish progress. Only a warm
        that loaded nothing keeps the doubled backoff recorded when it was
        claimed, so an unreachable catalog settles at one retry per TTL window.
        """
        record = self._state(request).setdefault("catalog_warm", {}).get(owner_id)
        if not record or record.get("scope") != scope:
            return
        now = time.time()
        ttl = max(1, int(self.valves.catalog_cache_seconds))
        if complete:
            record["attempts"] = 0
            prefix = f"{owner_id}:"
            expiries = [
                entry["expires_at"]
                for key, entry in self._state(request)["catalogs"].items()
                if key.startswith(prefix)
                and isinstance(entry.get("expires_at"), (int, float))
            ]
            # Each server's catalog expires on its own schedule, so lapse the
            # claim when the earliest one goes cold rather than a fixed window
            # from now. A server reloaded mid-window would otherwise hold the
            # claim open past its peers' expiry and leave routing blind.
            record["expires_at"] = min([now + ttl, *expiries])
        elif progressed:
            record["attempts"] = 1
            record["expires_at"] = now + min(ttl, CATALOG_WARM_RETRY_SECONDS)

    def _release_catalog_warm(self, request: Request, owner_id: str) -> None:
        """Return an unused claim so a blocked request does not consume the
        user's warm window."""
        self._state(request).setdefault("catalog_warm", {}).pop(owner_id, None)

    async def _prepare_routing(
        self,
        request,
        user_model,
        user,
        metadata,
        query,
        seconds,
        event_emitter,
        event_call,
        reserve_discovery,
    ):
        deadline = time.monotonic() + seconds
        owner_id = str(user_model.id)
        async with asyncio.timeout(seconds):
            loaded, cold, scope = await self._routing_snapshot(request, user_model)
        narrow, diagnostic, bm25_scores = self._routing_shortlist(
            request, owner_id, query, loaded
        )
        matches, warnings, available, count = self._discovery_matches(
            query, narrow, bm25_scores
        )
        strong = (
            bool(matches)
            and max(item["score"] for item in matches)
            >= self.valves.automatic_discovery_minimum_score
        )
        if cold:
            diagnostic["coverage"] = "partial"
        subjects = (
            self._ranking_terms(self._ranking_context(query))
            - SEARCH_STOP_WORDS
            - SEARCH_OPERATION_TERMS
            - ROUTING_STOP_TERMS
        )
        cold_candidates = [
            item
            for item in cold
            if ROUTING_EXPLICIT_LOOKUP_RE.search(query)
            or subjects & (self._ranking_terms(item[0]) - SEARCH_OPERATION_TERMS)
        ]
        ready = not strong and "reason" not in diagnostic
        warm_claimed = bool(
            ready and cold and self._claim_catalog_warm(request, owner_id, scope)
        )
        if warm_claimed:
            cold_candidates = cold
        if cold_candidates and ready:
            usage_error = await reserve_discovery()
            if usage_error:
                if warm_claimed:
                    self._release_catalog_warm(request, owner_id)
                diagnostic["blocked_reason"] = usage_error
                return matches, warnings, available, count, diagnostic
            semaphore = asyncio.Semaphore(
                CATALOG_WARM_CONCURRENCY if warm_claimed else 4
            )

            async def prepare(item):
                alias, server_id, connection = item
                async with semaphore:
                    try:
                        specs = await self._catalog_for_server(
                            request,
                            alias,
                            server_id,
                            connection,
                            user_model,
                            user,
                            metadata,
                            event_emitter,
                            event_call,
                        )
                        return alias, specs, None
                    except Exception:
                        log.exception(
                            "Automatic Tool Broker catalog preparation failed."
                        )
                        return (
                            alias,
                            [],
                            "One MCP catalog was unavailable during automatic discovery.",
                        )

            prepared = []
            done = set()
            tasks = []
            warm_settled = False
            try:
                task_count = len(cold_candidates[:50])
                if warm_claimed:
                    await self._emit_status(
                        event_emitter,
                        (
                            f"Preparing {task_count} MCP tool catalog"
                            if task_count == 1
                            else f"Preparing {task_count} MCP tool catalogs"
                        ),
                        False,
                    )
                tasks = [
                    asyncio.create_task(prepare(item)) for item in cold_candidates[:50]
                ]
                done, _ = await asyncio.wait(
                    tasks, timeout=max(0, deadline - time.monotonic())
                )
                prepared = [task.result() for task in tasks if task in done]
                loaded.extend(prepared)
                for task in tasks:
                    if not task.done():
                        task.cancel()
                await asyncio.gather(*tasks, return_exceptions=True)
                if warm_claimed:
                    ready_count = sum(1 for _, _, warning in prepared if not warning)
                    await self._emit_status(
                        event_emitter,
                        f"Prepared {ready_count} of {len(tasks)} MCP tool catalogs",
                        True,
                    )
                    self._settle_catalog_warm(
                        request,
                        owner_id,
                        scope,
                        complete=(
                            len(done) == len(tasks) == len(cold)
                            and not any(warning for _, _, warning in prepared)
                        ),
                        progressed=any(not warning for _, _, warning in prepared),
                    )
                    warm_settled = True
            except BaseException:
                for task in tasks:
                    if not task.done():
                        task.cancel()
                if tasks:
                    await asyncio.gather(*tasks, return_exceptions=True)
                if warm_claimed and not warm_settled:
                    self._release_catalog_warm(request, owner_id)
                raise
            narrow, diagnostic, bm25_scores = self._routing_shortlist(
                request, owner_id, query, loaded
            )
            if warm_claimed:
                diagnostic["catalog_warmed"] = True
            if len(done) < len(cold) or any(warning for _, _, warning in loaded):
                diagnostic["coverage"] = "partial"
            matches, warnings, available, count = self._discovery_matches(
                query, narrow, bm25_scores
            )
        if diagnostic["coverage"] == "partial":
            warnings.append(
                "Automatic discovery used a partial catalog. Explicit "
                "search_tools remains available for requirements not "
                "covered by these matches."
            )
        return matches, warnings, available, count, diagnostic

    async def _record_routing_diagnostics(self, request, user, metadata, diagnostic):
        key = _diagnostics_key(user, metadata)
        if key is None:
            return
        owner_id = key.split(":", 1)[0]
        state = self._ranking_diagnostics_state(request)
        now = time.time()
        async with state["lock"]:
            records = state["records"]
            for record_key, record in list(records.items()):
                if (
                    not isinstance(record, dict)
                    or now - record.get("updated_at_epoch", 0)
                    >= RANKING_DIAGNOSTICS_TTL_SECONDS
                ):
                    records.pop(record_key, None)
            owner_prefix = f"{owner_id}:"
            owner_records = sorted(
                (
                    (record_key, record)
                    for record_key, record in records.items()
                    if record_key.startswith(owner_prefix) and record_key != key
                ),
                key=lambda item: item[1].get("updated_at_epoch", 0),
            )
            while len(owner_records) >= MAX_RANKING_RECORDS_PER_USER:
                records.pop(owner_records.pop(0)[0], None)
            other_records = sorted(
                (
                    (record_key, record)
                    for record_key, record in records.items()
                    if record_key != key
                ),
                key=lambda item: item[1].get("updated_at_epoch", 0),
            )
            while (
                other_records
                and key not in records
                and len(records) >= MAX_RANKING_RECORDS_GLOBAL
            ):
                records.pop(other_records.pop(0)[0], None)
            records[key] = {
                "updated_at_epoch": now,
                "routing": dict(diagnostic),
            }

    def _automatic_discovery_hint(self, matches: list) -> Optional[dict]:
        """Return a compact, non-actionable hint about the top candidate when
        it is relevant but did not clear ``automatic_discovery_minimum_score``.

        The hint is informational only: it never authorizes execution, never
        appears in the discovered-tool allowlist, and must not carry a
        raw numeric score (which could read as a false precision guarantee).
        """
        if not matches:
            return None
        top = min(matches, key=self._match_sort_key)
        score = top.get("score", 0)
        if (
            score < self.valves.automatic_discovery_hint_minimum_score
            or score >= self.valves.automatic_discovery_minimum_score
        ):
            return None
        tool = top.get("tool")
        if not isinstance(tool, str) or not tool:
            return None
        return {
            "tool": tool,
            "description": self._bounded_text(
                top.get("description", ""),
                self.valves.search_description_chars,
            ),
        }

    async def _automatic_discovery(
        self,
        *,
        request,
        user,
        metadata,
        query,
        mode="on",
        preparation_seconds=4.0,
        event_emitter=None,
        event_call=None,
        continuation=False,
    ) -> dict:
        if mode != "on":
            raise ValueError("Automatic discovery mode must be on.")
        if (
            _user_id(user) == "anonymous"
            or not isinstance(metadata, dict)
            or not metadata.get("chat_id")
            or not metadata.get("message_id")
        ):
            return {
                "status": "skipped",
                "reason": "Authenticated chat/message metadata is required.",
            }
        if (
            not isinstance(query, str)
            or not query.strip()
            or len(query) > ROUTING_MAX_QUERY_CHARS
        ):
            return {
                "status": "skipped",
                "reason": "Automatic discovery query is empty or oversized.",
            }
        user_model = await Users.get_user_by_id(_user_id(user))
        if user_model is None:
            raise ValueError(
                "Automatic discovery could not resolve the authenticated user."
            )
        user_id, key = self._turn_usage_key(user, metadata)
        state = self._usage_state(request)
        signature = self._usage_signature(
            [query, mode, self.valves.servers_json, self._routing_protocol]
        )
        active_tool_ids = set()
        async with state["lock"]:
            now = time.monotonic()
            self._prune_usage_records(state["records"], now, user_id, key)
            record = self._usage_record(state["records"], user_id, key, now)
            restored_schemas = record.get("restored_mcp_schemas")
            if isinstance(restored_schemas, list):
                active_tool_ids = {
                    schema["tool"]
                    for schema in restored_schemas
                    if isinstance(schema, dict) and isinstance(schema.get("tool"), str)
                }
            previous = record.get("automatic_routing")
            if previous:
                if previous["signature"] != signature:
                    return {
                        "status": "skipped",
                        "reason": "This message already has a different routing decision.",
                    }
                future = previous["future"]
                leader = False
            else:
                if continuation:
                    return {
                        "status": "skipped",
                        "reason": "No active routing decision for this continuation.",
                    }
                future = asyncio.get_running_loop().create_future()
                record["automatic_routing"] = {
                    "signature": signature,
                    "future": future,
                    "protocol": self._routing_protocol,
                }
                leader = True
        if not leader:
            return {**await asyncio.shield(future), "replayed": True}

        started = time.monotonic()
        diagnostic = {
            "status": "no_match",
            "mode": mode,
            "coverage": "partial",
            "index_entries": 0,
            "candidate_count": 0,
            "returned_count": 0,
            "index_reused": False,
        }
        result = {"status": "error", "reason": "Automatic discovery was cancelled."}
        reserved = False
        usage_error = None

        async def reserve_discovery():
            nonlocal reserved, usage_error
            if not reserved:
                reserved = True
                usage_error = await self._reserve_usage(
                    request,
                    user,
                    metadata,
                    "search",
                    {
                        "query": " ".join(query.lower().split()),
                        "limit": min(3, self.valves.maximum_search_results),
                        "catalog_scope": "automatic",
                    },
                )
            return usage_error

        try:
            matches, warnings, available, count, details = await self._prepare_routing(
                request,
                user_model,
                user,
                metadata,
                query,
                min(10.0, max(0.1, float(preparation_seconds))),
                event_emitter,
                event_call,
                reserve_discovery,
            )
            diagnostic.update(details)
            if details.get("blocked_reason"):
                result = {
                    "status": "blocked",
                    "reason": diagnostic.pop("blocked_reason"),
                }
                diagnostic["status"] = "blocked"
            elif (
                matches
                and max(item["score"] for item in matches)
                >= self.valves.automatic_discovery_minimum_score
            ):
                diagnostic["status"] = "discovered"
                error = await reserve_discovery()
                if error:
                    result = {"status": "blocked", "reason": error}
                    diagnostic["status"] = "blocked"
                else:
                    selected_matches = self._select_search_matches(matches, 3)
                    if selected_matches and all(
                        match.get("tool") in active_tool_ids
                        for match in selected_matches
                    ):
                        result = {"status": "active"}
                        diagnostic["status"] = "active"
                    else:
                        response = await self._finish_discovery(
                            query,
                            3,
                            matches,
                            warnings,
                            available,
                            count,
                            request,
                            user,
                            metadata,
                            None,
                            maximum_chars=16_000,
                            catalog_coverage=diagnostic["coverage"],
                        )
                        diagnostic["returned_count"] = len(
                            json.loads(response).get("matches", [])
                        )
                        result = (
                            {"status": "discovered", "response": response}
                            if diagnostic["returned_count"]
                            else {"status": "no_match"}
                        )
                        if not diagnostic["returned_count"]:
                            diagnostic["status"] = "no_match"
            else:
                result = {"status": "no_match"}
                hint = (
                    None if active_tool_ids else self._automatic_discovery_hint(matches)
                )
                if hint is not None:
                    result["hint"] = hint
                    diagnostic["hint_tool"] = hint["tool"]
        except asyncio.CancelledError:
            result = {"status": "error", "reason": "Automatic discovery was cancelled."}
            raise
        except Exception:
            log.exception(
                "Automatic Tool Broker discovery failed; explicit discovery is still available."
            )
            diagnostic["status"] = "error"
            result = {
                "status": "error",
                "reason": (
                    "Automatic discovery failed or reached its preparation deadline. "
                    "Use explicit discovery if needed."
                ),
            }
        finally:
            diagnostic["elapsed_ms"] = round((time.monotonic() - started) * 1000, 2)
            if result["status"] == "error":
                diagnostic["status"] = "error"
            async with state["lock"]:
                current = state["records"].get(key)
                routing = (
                    current.get("automatic_routing")
                    if isinstance(current, dict)
                    else None
                )
                if isinstance(routing, dict) and routing.get("future") is future:
                    routing["coverage"] = diagnostic.get("coverage", "partial")
            future.set_result(result)
            await self._record_routing_diagnostics(request, user, metadata, diagnostic)
        return result

    async def search_tools(
        self,
        query: str,
        limit: int = 3,
        catalog: str = "",
        __request__: Optional[Request] = None,
        __user__: Any = None,
        __metadata__: Optional[dict] = None,
        __event_emitter__=None,
        __event_call__=None,
    ) -> str:
        """
        Searches accessible Open WebUI MCP tools. Call this before work requiring external or changing information or a domain-specific operation because complete MCP schemas are not otherwise listed in the prompt. Each new user message starts a fresh authorization scope; revalidated MCP tools in an authenticated active-tool packet are already authorized, while uncovered requirements need a new search. Skip discovery for reliably self-contained requests. If one match clearly fits, execute it immediately for read-only work. Ask before destructive operations or when required arguments are ambiguous. Duplicate searches are rejected and the default hard guards allow three searches within twenty combined Tool Broker calls.
        A harness-provided automatic discovery packet with matches for the current message already satisfies discovery; do not repeat this search for requirements it covers.
        For an MCP tool inventory, call once with an empty query and set catalog to "mcp" or "all". Inventory is compact, executes nothing, and omits argument schemas.
        :param query: Natural-language description of the operation needed. Must be non-empty unless catalog inventory is requested.
        :param limit: Maximum relevance matches; catalog inventories use their separate bounded ceiling.
        :param catalog: Empty for relevance search, or "mcp" or "all" for a compact MCP inventory.
        :return: JSON containing matching MCP tools and unavailable server warnings.
        """
        if __request__ is None:
            message = "Tool Broker requires the Open WebUI request context."
            await self._emit_error(__event_emitter__, message)
            return f"Error: {message}"
        try:
            user_model = await Users.get_user_by_id(_user_id(__user__))
            if user_model is None:
                raise ValueError(
                    "Tool Broker could not resolve the current Open WebUI user."
                )
        except (ValueError, RuntimeError) as error:
            await self._emit_error(__event_emitter__, str(error))
            return f"Error: {error}"

        normalized_query = (
            " ".join(query.lower().split()) if isinstance(query, str) else query
        )
        normalized_catalog = (
            catalog.strip().lower() if isinstance(catalog, str) else catalog
        )
        usage_error = await self._reserve_usage(
            __request__,
            __user__,
            __metadata__,
            "search",
            {"query": normalized_query, "limit": limit, "catalog": normalized_catalog},
        )
        if usage_error:
            return usage_error
        if not isinstance(query, str):
            return "Error: Tool Broker search query must be a string."
        if not isinstance(catalog, str) or normalized_catalog not in {"", "mcp", "all"}:
            return 'Error: Tool Broker catalog must be empty, "mcp", or "all".'
        if not query.strip() and not normalized_catalog:
            return (
                "Error: Empty relevance searches are not supported. For an inventory, "
                'set catalog to "mcp" or "all"; otherwise provide a specific '
                "operation in query."
            )
        if not normalized_catalog:
            covered_error = await self._covered_automatic_search(
                __request__, __user__, __metadata__, query, limit
            )
            if covered_error:
                return covered_error
        try:
            servers, explicitly_configured, connections_by_id = await self._servers()
        except (ValueError, RuntimeError) as error:
            await self._emit_error(__event_emitter__, str(error))
            return f"Error: {error}"

        self._prune_inactive_catalogs(
            __request__,
            str(user_model.id),
            {
                server_id
                for server_id, connection in connections_by_id.items()
                if self._connection_enabled(connection)
            },
        )
        await self._emit_status(
            __event_emitter__, "Searching available MCP tools", False
        )

        async def load_server(alias: str, server_id: str):
            try:
                specs = await self._catalog_for_server(
                    __request__,
                    alias,
                    server_id,
                    connections_by_id.get(server_id),
                    user_model,
                    __user__,
                    __metadata__,
                    __event_emitter__,
                    __event_call__,
                )
                return alias, specs, None
            except Exception as error:
                log.exception(
                    "Tool Broker could not load MCP catalog '%s': %s", alias, error
                )
                warning = (
                    str(error)
                    if explicitly_configured
                    else "One MCP server was unavailable, denied, or requires authentication."
                )
                return alias, [], warning

        loaded_servers = await asyncio.gather(
            *(load_server(alias, server_id) for alias, server_id in servers.items())
        )
        if normalized_catalog:
            ranked_servers = loaded_servers
            bm25_scores = None
        else:
            _, _, bm25_scores = self._routing_shortlist(
                __request__,
                str(user_model.id),
                query,
                loaded_servers,
            )
            ranked_servers = loaded_servers
        matches, warnings, available_servers, catalog_count = self._discovery_matches(
            query, ranked_servers, bm25_scores
        )
        return await self._finish_discovery(
            query,
            limit,
            matches,
            warnings,
            available_servers,
            catalog_count,
            __request__,
            __user__,
            __metadata__,
            __event_emitter__,
            inventory_catalog=normalized_catalog or None,
        )

    def _discovery_matches(
        self,
        query: str,
        loaded_servers: list,
        bm25_scores: Optional[dict[tuple[str, str], float]] = None,
    ) -> tuple[list, list, list, int]:
        matches = []
        warnings = []
        available_servers = []
        catalog_tool_count = 0
        mcp_catalogs = {alias: specs for alias, specs, _ in loaded_servers}
        rare_subjects = self._unmatched_named_subjects(
            query, self._catalog_vocabulary(mcp_catalogs)
        )
        ranked_catalogs = self._rank_catalogs(query, mcp_catalogs, bm25_scores)
        catalog_scores = {
            (alias, id(spec)): score for alias, spec, score in ranked_catalogs
        }
        catalog_ranks = {
            (alias, id(spec)): rank
            for rank, (alias, spec, _) in enumerate(ranked_catalogs)
        }
        for alias, specs, warning in loaded_servers:
            if warning and warning not in warnings:
                warnings.append(warning)
            if specs:
                available_servers.append(alias)
                catalog_tool_count += len(specs)
            for spec in specs:
                try:
                    score = catalog_scores.get((alias, id(spec)), 0)
                    if query.strip() and score == 0:
                        continue
                    matches.append(
                        {
                            "score": score,
                            "_rank_order": catalog_ranks.get(
                                (alias, id(spec)),
                                1_000_000_000,
                            ),
                            "server": alias,
                            "tool": f'{alias}.{spec.get("name", "")}',
                            "description": self._bounded_text(
                                spec.get("description", ""),
                                self.valves.search_description_chars,
                            ),
                            "arguments": self._compact_search_schema(
                                spec.get("parameters", {"type": "object"})
                            ),
                            "_subject_evidence": sorted(
                                self._matched_subject_evidence(
                                    query, alias, spec, rare_subjects
                                )
                            ),
                        }
                    )
                except Exception:
                    log.exception(
                        "Tool Broker could not prepare search metadata for '%s.%s'.",
                        alias,
                        spec.get("name", "unknown"),
                    )
        return matches, warnings, available_servers, catalog_tool_count

    async def _finish_discovery(
        self,
        query,
        limit,
        matches,
        warnings,
        available_servers,
        catalog_tool_count,
        __request__,
        __user__,
        __metadata__,
        __event_emitter__=None,
        maximum_chars=None,
        catalog_coverage=None,
        inventory_catalog=None,
    ) -> str:
        try:
            requested_limit = int(limit)
        except (TypeError, ValueError):
            requested_limit = self.valves.maximum_search_results
        result_limit = min(self.valves.maximum_search_results, max(1, requested_limit))
        if inventory_catalog:
            inventory_matches = list(matches)
            selected_matches = sorted(
                inventory_matches, key=lambda item: item.get("tool", "")
            )[: self.valves.maximum_inventory_results]
        else:
            inventory_matches = []
            selected_matches = self._select_search_matches(matches, result_limit)
        selected_mcp = {
            match["tool"]: {
                "tool": match["tool"],
                "server": match["server"],
                "name": match["tool"].split(".", 1)[1],
            }
            for match in selected_matches
            if (
                not inventory_catalog
                and isinstance(match.get("server"), str)
                and isinstance(match.get("tool"), str)
                and "." in match["tool"]
            )
        }
        for match in selected_matches:
            match.pop("score", None)
            match.pop("_rank_order", None)
            match.pop("_subject_evidence", None)
            if inventory_catalog:
                match.pop("arguments", None)
        await self._emit_status(
            __event_emitter__,
            (
                f"Found {len(selected_matches)} MCP tool"
                if len(selected_matches) == 1
                else f"Found {len(selected_matches)} MCP tools"
            ),
            True,
        )
        response = {
            "catalog_tools": catalog_tool_count,
            "matching_tools": len(matches),
            "returned_tools": len(selected_matches),
            "matches": selected_matches,
            "warnings": warnings,
            "next_action": (
                "If one match clearly fits the requested operation, execute it now. "
                "Use execute_tool only with an exact identifier shown in matches; "
                "call directly available tools by their own names. Ask before destructive "
                "work or when required arguments are ambiguous."
            ),
        }
        query_terms = set(re.findall(r"[a-z0-9]+", query.lower()))
        if not query.strip() or query_terms.intersection(
            {
                "available",
                "tools",
                "catalog",
                "inventory",
                "server",
                "servers",
            }
        ):
            response["available_servers"] = available_servers
        if inventory_catalog:
            response.update(
                {
                    "inventory": inventory_catalog,
                    "inventory_complete": len(selected_matches)
                    == len(inventory_matches),
                    "inventory_remaining": max(
                        0, len(inventory_matches) - len(selected_matches)
                    ),
                    "next_action": (
                        "Present this MCP tool inventory without executing anything. "
                        "If inventory_complete is false, explain that the bounded inventory "
                        "was truncated and use a specific relevance search in a new user message."
                    ),
                }
            )
            response["matching_tools"] = len(inventory_matches)
            for match in response["matches"]:
                match["description"] = self._bounded_text(
                    match.get("description", ""), 240
                )
            while (
                response["matches"]
                and len(_serialize(response)) > self.valves.maximum_inline_result_chars
            ):
                response["matches"].pop()
            response["returned_tools"] = len(response["matches"])
            response["inventory_remaining"] = len(inventory_matches) - len(
                response["matches"]
            )
            response["inventory_complete"] = not response["inventory_remaining"]
        if maximum_chars is not None:
            response["discovery_origin"] = "harness"
            response["catalog_coverage"] = catalog_coverage
            while (
                response["matches"]
                and len(_serialize(response).replace("<", "\\u003c")) > maximum_chars
            ):
                response["matches"].pop()
            response["returned_tools"] = len(response["matches"])
        bounded_response = self._bound_search_response(response)
        if maximum_chars is not None:
            bounded_response = bounded_response.replace("<", "\\u003c")
            if len(bounded_response) > maximum_chars:
                bounded_response = _serialize(
                    {
                        "matches": [],
                        "returned_tools": 0,
                        "warnings": [
                            "Automatic discovery exceeded its response-size limit."
                        ],
                        "discovery_origin": "harness",
                        "catalog_coverage": catalog_coverage,
                    }
                )
        try:
            returned = json.loads(bounded_response)
            returned_tools = {
                match["tool"]
                for match in returned.get("matches", [])
                if isinstance(match, dict)
                and isinstance(match.get("tool"), str)
                and match["tool"]
            }
        except (TypeError, ValueError, json.JSONDecodeError):
            returned_tools = set()
        retained_mcp = [
            selected_mcp[tool] for tool in selected_mcp if tool in returned_tools
        ]
        if retained_mcp:
            _, retention_error = await self._activate_persisted_mcp_tools(
                __request__, __user__, __metadata__, retained_mcp
            )
            if retention_error:
                log.warning(
                    "Tool Broker could not retain selected MCP tools: %s",
                    retention_error,
                )
                await self._emit_error(__event_emitter__, retention_error)
        if not inventory_catalog:
            await self._record_discovered_tools(
                __request__, __user__, __metadata__, returned_tools
            )
        return bounded_response

    async def execute_tool(
        self,
        tool: str,
        arguments: dict,
        __request__: Optional[Request] = None,
        __user__: Any = None,
        __metadata__: Optional[dict] = None,
        __event_emitter__=None,
        __event_call__=None,
    ) -> str:
        """
        Executes one MCP tool returned by search_tools for the current user message. Revalidated MCP tools restored in an authenticated active-tool packet are also authorized and may be reused without another search. Search again only for requirements the active set does not cover. For externalized results, answer from the preview when possible and use artifact.read only for omitted information needed to complete the request. Use the smallest number of executions needed and synthesize once sufficient evidence is available. Per user message, identical executions are rejected and the default hard guards allow eighteen executions within twenty combined Tool Broker calls. MCP connections and executions time out after a configurable 180 seconds by default.
        Identifiers in an authenticated harness automatic discovery packet for this message are also authorized; no additional search is needed for them.
        :param tool: Exact tool identifier returned by Tool Broker discovery, or artifact.read. Do not pass a directly available Open WebUI tool name; call that tool directly.
        :param arguments: Arguments matching the tool schema. For artifact.read, provide artifact_id with either a targeted query and limit or a supplied offset and limit.
        :return: The tool result or an artifact preview when the complete result is large.
        """
        if __request__ is None:
            message = "Tool Broker requires the Open WebUI request context."
            await self._emit_error(__event_emitter__, message)
            return f"Error: {message}"

        try:
            user_model = await Users.get_user_by_id(_user_id(__user__))
            if user_model is None:
                raise ValueError(
                    "Tool Broker could not resolve the current Open WebUI user."
                )
        except (ValueError, RuntimeError) as error:
            await self._emit_error(__event_emitter__, str(error))
            return f"Error: {error}"

        discovery_authorized = tool == ARTIFACT_TOOL or (
            isinstance(tool, str)
            and bool(tool.strip())
            and await self._was_tool_discovered(
                __request__,
                __user__,
                __metadata__,
                tool,
            )
        )
        usage_error = await self._reserve_usage(
            __request__,
            __user__,
            __metadata__,
            "execution",
            {
                "tool": tool,
                "arguments": arguments,
                "discovery_authorized": discovery_authorized,
            },
        )
        if usage_error:
            return usage_error

        if not isinstance(tool, str) or not tool.strip():
            return "Error: Tool Broker tool must be a non-empty string."
        if not isinstance(arguments, dict):
            return "Error: Tool Broker arguments must be a JSON object."
        if tool == ARTIFACT_TOOL:
            unsupported = sorted(
                set(arguments) - {"artifact_id", "offset", "limit", "query"}
            )
            if unsupported:
                return (
                    "Error: artifact.read received unsupported arguments: "
                    f"{', '.join(unsupported)}. Allowed arguments: "
                    "artifact_id, limit, offset, query."
                )
            artifact_id = arguments.get("artifact_id")
            if not isinstance(artifact_id, str) or not artifact_id:
                return "Error: artifact.read requires a non-empty artifact_id."
            artifact_usage_error = await self._reserve_artifact_read(
                __request__,
                __user__,
                __metadata__,
                artifact_id,
            )
            if artifact_usage_error:
                return artifact_usage_error
            return await self._read_artifact_once(
                __request__, __user__, __metadata__, arguments
            )

        if not discovery_authorized:
            return (
                f"Error: tool '{tool}' was not returned by "
                "Tool Broker discovery or restored from the revalidated active MCP set "
                "for this user message. Run a focused tool search for the "
                "required operation, then execute only an exact identifier from "
                "that result."
            )

        try:
            servers, _, connections_by_id = await self._servers()
        except ValueError as error:
            await self._emit_error(__event_emitter__, str(error))
            return f"Error: {error}"

        active_server_ids = {
            server_id
            for server_id, connection in connections_by_id.items()
            if self._connection_enabled(connection)
        }
        self._prune_inactive_catalogs(
            __request__,
            str(user_model.id),
            active_server_ids,
        )

        if "." not in tool:
            return (
                "Error: execute_tool accepts only an exact identifier returned by "
                "search_tools, using the <server-alias>.<tool-name> format. Do not "
                "invent a server prefix. If this is a directly available tool, call that "
                "tool directly instead of using execute_tool."
            )
        alias, tool_name = tool.split(".", 1)
        server_id = servers.get(alias)
        if (
            server_id is None
            or server_id not in active_server_ids
            or not self._connection_enabled(connections_by_id.get(server_id))
        ):
            return (
                f"Error: MCP server alias '{alias}' is unavailable, inaccessible, or not "
                "configured. Use only an exact tool identifier returned by the most "
                "recent search_tools result; do not guess aliases. If the intended "
                "operation is a directly available tool, call that tool directly."
            )

        await self._emit_status(
            __event_emitter__,
            f"Executing MCP tool: {tool}",
            False,
        )

        parameters = {}
        final_status = f"Finished MCP tool: {tool}"

        async def call_selected_tool(client, specs):
            nonlocal parameters
            spec = next(
                (item for item in specs if item.get("name") == tool_name),
                None,
            )
            if spec is None:
                return (
                    f"Error: tool '{tool}' was not found. "
                    "Use only an exact tool identifier from the most recent "
                    "search_tools result. Do not retry this guessed identifier "
                    "or repeat an equivalent search."
                )

            parameters = spec.get("parameters", {})
            if not isinstance(parameters, dict):
                parameters = {}
            properties = parameters.get("properties")
            declared_extras = parameters.get("additionalProperties")
            known_properties = (
                set(properties)
                if isinstance(properties, dict) and properties
                else set()
            )
            extras_allowed = declared_extras is True or isinstance(
                declared_extras, dict
            )
            closed_schema = declared_extras is False or (
                bool(known_properties) and not extras_allowed
            )
            unknown = sorted(set(arguments) - known_properties)
            if unknown and closed_schema:
                allowed_text = (
                    ", ".join(sorted(known_properties)) if known_properties else "none"
                )
                required_names = parameters.get("required", [])
                required_text = (
                    ", ".join(
                        str(name) for name in required_names if isinstance(name, str)
                    )
                    or "none"
                )
                return (
                    f"Error: tool '{tool}' received unsupported arguments: "
                    f"{', '.join(unknown)}. Allowed arguments: {allowed_text}. "
                    f"Required arguments: {required_text}. This tool has no "
                    f"such parameter, so do not send {', '.join(unknown)} to it "
                    "again with any value. Retry once using only the allowed "
                    "argument names exactly as written."
                )
            filtered_arguments = dict(arguments)

            required = parameters.get("required", [])
            missing = [name for name in required if name not in filtered_arguments]
            if missing:
                return (
                    f"Error: tool '{tool}' is missing required arguments: "
                    f"{', '.join(missing)}."
                )

            result = await client.call_tool(
                tool_name,
                function_args=filtered_arguments,
            )
            normalized_result = self._normalize_mcp_result(result)
            response = self._externalize_result(
                __request__,
                __user__,
                tool,
                arguments,
                normalized_result,
            )
            await self._emit_source_events(
                __request__,
                __user__,
                __metadata__,
                __event_emitter__,
                tool,
                arguments,
                normalized_result,
            )
            return response

        try:
            response = await self._run_mcp_lifecycle(
                __request__,
                server_id,
                user_model,
                __user__,
                __metadata__,
                __event_emitter__,
                __event_call__,
                call_selected_tool,
            )
            if response is None:
                final_status = f"Failed MCP tool: {tool}"
                message = f"MCP server '{alias}' is unavailable or access was denied."
                await self._emit_error(__event_emitter__, message)
                return f"Error: {message}"
            return response
        except TimeoutError as error:
            final_status = f"Timed out MCP tool: {tool}"
            return (
                f"Error: MCP tool '{tool}' timed out after "
                f"{self.valves.mcp_timeout_seconds} seconds: {error} Mandatory stop: "
                "do not retry the identical call in this user message. "
                "Continue from existing evidence or disclose the timeout."
            )
        except Exception as error:
            final_status = f"Failed MCP tool: {tool}"
            log.exception("MCP tool '%s' failed.", tool)
            error_text = self._bounded_text(self._format_execution_error(error), 3_000)
            schema_guidance = self._focused_argument_schema(
                parameters,
                arguments,
            )
            message = f"MCP tool '{tool}' failed: {error_text}"
            if schema_guidance:
                message += "\nFocused argument schema for retry: " f"{schema_guidance}"
            return f"Error: {message}"
        finally:
            await self._emit_status(
                __event_emitter__,
                final_status,
                True,
            )
