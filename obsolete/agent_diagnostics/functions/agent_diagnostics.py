"""
title: Agent Diagnostics
description: Shows context, efficiency, native view_skill, Tool Broker routing, and MCP latency diagnostics.
version: 0.12.2
required_open_webui_version: 0.11.3
"""

import asyncio
import hashlib
import inspect
import logging
import math
import re
import time
from typing import Any, Optional

from fastapi import Request
from open_webui.config import Config
from open_webui.models.users import Users
from open_webui.utils.access_control import has_connection_access

try:
    from open_webui.utils.tools import connect_mcp_server
except ImportError:
    from open_webui.utils.middleware import connect_mcp_server
from pydantic import BaseModel, Field

METRICS_STATE_KEY = "_agent_efficiency_metrics_v1"
RANKING_DIAGNOSTICS_STATE_KEY = "_tool_broker_ranking_diagnostics_v1"
RANKING_DIAGNOSTICS_TTL_SECONDS = 120 * 60
MCP_PROBE_ACTIVE_STATE_KEY = "_agent_diagnostics_active_mcp_probes_v1"
log = logging.getLogger(__name__)


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


class Action:
    actions = [
        {
            "id": "context_report",
            "name": "Context report",
        },
        {
            "id": "efficiency_report",
            "name": "Efficiency report",
        },
        {
            "id": "optimization_report",
            "name": "Optimization report",
        },
        {
            "id": "tool_schema_report",
            "name": "Tool schema report",
        },
        {
            "id": "view_skill_report",
            "name": "view_skill report",
        },
        {
            "id": "tool_broker_routing_report",
            "name": "Tool Broker routing report",
        },
        {
            "id": "mcp_latency_report",
            "name": "MCP latency report",
        },
    ]

    class Valves(BaseModel):
        priority: int = Field(
            default=0,
            description="Action display priority. Lower values appear first.",
        )
        stale_after_seconds: int = Field(
            default=300,
            ge=30,
            description="Metrics older than this are identified as stale.",
        )
        schema_warning_chars: int = Field(
            default=12_000,
            ge=1_000,
            description="Recommend reducing active tools above this schema size.",
        )
        context_warning_tokens: int = Field(
            default=32_000,
            ge=1_000,
            description="Identify unusually large estimated provider input above this value.",
        )
        provider_call_warning: int = Field(
            default=5,
            ge=2,
            description="Identify unusually long provider/tool loops above this call count.",
        )
        tool_result_warning_chars: int = Field(
            default=40_000,
            ge=1_000,
            description="Recommend output controls above this retained tool-result size.",
        )
        mcp_probe_timeout_seconds: int = Field(
            default=30,
            ge=5,
            le=180,
            description="Maximum time to wait for each diagnostic MCP catalog connection.",
        )

    def __init__(self):
        self.valves = self.Valves()

    async def _notify(self, event_emitter, content: str, level: str = "info") -> None:
        if event_emitter is not None:
            await event_emitter(
                {
                    "type": "notification",
                    "data": {
                        "type": level,
                        "content": content,
                    },
                }
            )

    async def _status(
        self,
        event_emitter: Any,
        description: str,
        done: bool,
        hidden: bool = False,
    ) -> None:
        if event_emitter is not None:
            await event_emitter(
                {
                    "type": "status",
                    "data": {
                        "description": description,
                        "done": done,
                        "hidden": hidden,
                    },
                }
            )

    @staticmethod
    def _begin_mcp_probe(request: Request, user_id: str) -> bool:
        active = getattr(request.app.state, MCP_PROBE_ACTIVE_STATE_KEY, None)
        if not isinstance(active, set):
            active = set()
            setattr(request.app.state, MCP_PROBE_ACTIVE_STATE_KEY, active)
        if user_id in active:
            return False
        active.add(user_id)
        return True

    @staticmethod
    def _end_mcp_probe(request: Request, user_id: str) -> None:
        active = getattr(request.app.state, MCP_PROBE_ACTIVE_STATE_KEY, None)
        if isinstance(active, set):
            active.discard(user_id)

    def _age_seconds(self, metrics: dict) -> Optional[int]:
        updated_at = metrics.get("updated_at_epoch")
        if not isinstance(updated_at, (int, float)):
            return None
        return max(0, round(time.time() - updated_at))

    @staticmethod
    def _diagnostic_count(value: Any) -> Optional[int]:
        if type(value) is int and 0 <= value <= 2**63 - 1:
            return value
        return None

    @staticmethod
    def _routing_report(routing: Any) -> str:
        if not isinstance(routing, dict):
            return ""
        status = routing.get("status")
        mode = routing.get("mode")
        if (
            status
            not in (
                "skipped",
                "discovered",
                "active",
                "no_match",
                "error",
                "blocked",
            )
            or mode != "on"
        ):
            return ""

        counts = {
            name: (
                f"{count:,}"
                if (count := Action._diagnostic_count(routing.get(name))) is not None
                else "unavailable"
            )
            for name in (
                "index_entries",
                "index_terms",
                "candidate_count",
                "returned_count",
            )
        }
        ranking_mode = routing.get("ranking_mode")
        if ranking_mode not in ("hybrid", "bm25", "legacy"):
            ranking_mode = "unavailable"
        elapsed = routing.get("elapsed_ms")
        elapsed_text = "unavailable"
        if (
            type(elapsed) in (int, float)
            and 0 <= elapsed <= 1.7976931348623157e308
            and math.isfinite(elapsed)
        ):
            elapsed_text = (
                f"{elapsed:.1f} ms" if elapsed < 1_000_000 else f"{elapsed:.3g} ms"
            )
        reused = routing.get("index_reused")
        cache = "unavailable"
        if type(reused) is bool:
            cache = "reused" if reused else "not reused"
        coverage = routing.get("coverage")
        if coverage not in ("complete", "partial"):
            coverage = "unavailable"
        hint_text = ""
        hint_tool = routing.get("hint_tool")
        if (
            status == "no_match"
            and isinstance(hint_tool, str)
            and len(hint_tool) <= 256
            and re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:/-]*", hint_tool)
        ):
            hint_text = (
                f" Hinted candidate: {hint_tool} "
                "(below the automatic-discovery threshold; not discovered or authorized)."
            )
        warm_text = ""
        if routing.get("catalog_warmed") is True:
            warm_text = (
                " This turn warmed the user's cold MCP catalog, so later "
                "messages rank against a complete catalog."
            )
        return (
            f" Automatic routing: {status} ({mode}); ranker: {ranking_mode}; "
            f"cache: {cache}; elapsed: {elapsed_text}; "
            f"index entries/terms: {counts['index_entries']}/{counts['index_terms']}; "
            f"candidates: {counts['candidate_count']}; results: {counts['returned_count']}; "
            f"coverage: {coverage}.{hint_text}{warm_text}"
        )

    def _age_text(self, metrics: dict) -> str:
        age = self._age_seconds(metrics)
        if age is None:
            return "metrics age unavailable"
        freshness = "stale" if age > self.valves.stale_after_seconds else "fresh"
        return f"metrics age: {age} seconds ({freshness})"

    @staticmethod
    def _connection_enabled(connection: Any) -> bool:
        if not isinstance(connection, dict):
            return False
        config = connection.get("config")
        return config is None or (
            isinstance(config, dict) and bool(config.get("enable", True))
        )

    @staticmethod
    def _connection_label(connection: dict, server_id: str) -> str:
        info = connection.get("info") or {}
        value = info.get("name") or connection.get("name") or server_id
        return " ".join(str(value).split())[:100] or server_id

    async def _disconnect_mcp(self, client: Any) -> None:
        try:
            await asyncio.wait_for(client.disconnect(), timeout=10)
        except asyncio.TimeoutError:
            log.warning("MCP latency diagnostic client disconnect timed out.")
        except Exception as error:
            log.warning(
                "MCP latency diagnostic client disconnect failed (%s).",
                type(error).__name__,
            )

    async def _probe_mcp(
        self,
        connection: dict,
        user_model: Any,
        user_context: Any,
        metadata: Optional[dict],
        request: Request,
        event_emitter: Any,
        event_call: Any,
    ) -> dict:
        info = connection.get("info") or {}
        server_id = str(info.get("id", "")).strip()
        label = self._connection_label(connection, server_id)
        started = time.perf_counter()
        client = None
        try:
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
            connection_args = (request, server_id, user_model)
            if "metadata" in inspect.signature(connect_mcp_server).parameters:
                connection_args += (meta,)
            result = await asyncio.wait_for(
                connect_mcp_server(*connection_args, extra_params),
                timeout=self.valves.mcp_probe_timeout_seconds,
            )
            if result is None:
                raise PermissionError("MCP connection was unavailable.")
            client, specs = result
            return {
                "label": label,
                "milliseconds": round((time.perf_counter() - started) * 1_000),
                "status": "ok",
                "tool_count": len(specs) if isinstance(specs, list) else 0,
            }
        except asyncio.TimeoutError:
            return {
                "label": label,
                "milliseconds": round((time.perf_counter() - started) * 1_000),
                "status": "timeout",
                "tool_count": 0,
            }
        except asyncio.CancelledError:
            raise
        except Exception as error:
            return {
                "label": label,
                "milliseconds": round((time.perf_counter() - started) * 1_000),
                "status": type(error).__name__,
                "tool_count": 0,
            }
        finally:
            if client is not None:
                await self._disconnect_mcp(client)

    async def _mcp_latency_report(
        self,
        metadata: Optional[dict],
        user_context: Any,
        request: Request,
        event_emitter: Any,
        event_call: Any,
    ) -> None:
        user_model = await Users.get_user_by_id(_user_id(user_context))
        if user_model is None:
            await self._notify(
                event_emitter,
                "MCP latency diagnostics require an authenticated Open WebUI user.",
                "error",
            )
            return

        try:
            configured = await Config.get("tool_server.connections", [])
        except Exception as error:
            await self._notify(
                event_emitter,
                "Could not read MCP connection configuration "
                f"({type(error).__name__}).",
                "error",
            )
            return
        connections = []
        seen_ids = set()
        access_check_failures = 0
        for connection in configured if isinstance(configured, list) else []:
            info = connection.get("info") if isinstance(connection, dict) else None
            server_id = str((info or {}).get("id", "")).strip()
            if (
                not server_id
                or server_id in seen_ids
                or connection.get("type", "") != "mcp"
                or not self._connection_enabled(connection)
            ):
                continue
            try:
                accessible = await has_connection_access(user_model, connection)
            except Exception as error:
                access_check_failures += 1
                log.warning(
                    "MCP latency diagnostic access check failed (%s).",
                    type(error).__name__,
                )
                continue
            if not accessible:
                continue
            seen_ids.add(server_id)
            connections.append(connection)

        if not connections:
            suffix = (
                f" {access_check_failures} access check(s) failed."
                if access_check_failures
                else ""
            )
            await self._notify(
                event_emitter,
                "No enabled MCP connections are accessible to this user." + suffix,
                "warning",
            )
            return

        await self._notify(
            event_emitter,
            f"MCP latency probe started for {len(connections)} accessible connections. "
            "Results will appear when it completes.",
        )
        await self._status(
            event_emitter,
            f"Probing {len(connections)} accessible MCP catalog connections concurrently...",
            False,
        )
        started = time.perf_counter()
        try:
            results = await asyncio.gather(
                *(
                    self._probe_mcp(
                        connection,
                        user_model,
                        user_context,
                        metadata,
                        request,
                        event_emitter,
                        event_call,
                    )
                    for connection in connections
                )
            )
        finally:
            await self._status(
                event_emitter,
                "MCP catalog probe complete.",
                True,
                hidden=True,
            )
        wall_milliseconds = round((time.perf_counter() - started) * 1_000)
        results.sort(key=lambda item: item["milliseconds"], reverse=True)

        lines = []
        failures = 0
        for result in results:
            status = result["status"]
            if status == "ok":
                detail = f"{result['tool_count']} tools"
            elif status == "timeout":
                failures += 1
                detail = (
                    f"timed out after {self.valves.mcp_probe_timeout_seconds} seconds"
                )
            else:
                failures += 1
                detail = f"failed ({status})"
            lines.append(
                f"- {result['label']}: {result['milliseconds']:,} ms; {detail}"
            )

        content = (
            f"Uncached MCP catalog latency, slowest first "
            f"({len(results)} probed concurrently; {wall_milliseconds:,} ms wall time):\n"
            + "\n".join(lines)
        )
        if access_check_failures:
            content += (
                f"\n{access_check_failures} additional access check(s) failed "
                "and were not probed."
            )
        await self._notify(
            event_emitter,
            content,
            "warning" if failures else "info",
        )

    async def action(
        self,
        body: dict,
        __id__: Optional[str] = None,
        __metadata__: Optional[dict] = None,
        __user__: Any = None,
        __request__: Optional[Request] = None,
        __event_emitter__=None,
        __event_call__=None,
    ):
        if __request__ is None:
            await self._notify(
                __event_emitter__,
                "Diagnostics are unavailable because the request context was not provided.",
                "error",
            )
            return None

        key = _metric_key(body, __metadata__, __user__)
        if key is None:
            await self._notify(
                __event_emitter__,
                "Diagnostics require an authenticated Open WebUI user.",
                "error",
            )
            return None

        if __id__ == "mcp_latency_report":
            user_id = _user_id(__user__)
            if not self._begin_mcp_probe(__request__, user_id):
                await self._notify(
                    __event_emitter__,
                    "An MCP latency probe is already running for this user.",
                    "warning",
                )
                return None
            try:
                await self._mcp_latency_report(
                    __metadata__,
                    __user__,
                    __request__,
                    __event_emitter__,
                    __event_call__,
                )
            finally:
                self._end_mcp_probe(__request__, user_id)
            return None

        cache = getattr(__request__.app.state, METRICS_STATE_KEY, {})
        metrics = cache.get(key) if isinstance(cache, dict) else None

        if __id__ != "tool_broker_routing_report" and (
            not isinstance(metrics, dict) or not metrics
        ):
            await self._notify(
                __event_emitter__,
                "No diagnostics are available yet. Send a message with the Agent Efficiency Filter enabled.",
                "warning",
            )
            return None
        if not isinstance(metrics, dict):
            metrics = {}

        if __id__ == "context_report":
            report = (
                f"Estimated input: {metrics.get('estimated_input_tokens', 0):,} tokens "
                f"({metrics.get('message_chars', 0):,} message characters); "
                f"{metrics.get('message_count', 0)} messages; "
                f"{metrics.get('system_chars', 0):,} system characters; "
                f"{metrics.get('tool_result_count', 0)} tool results using "
                f"{metrics.get('tool_result_chars_after', 0):,} characters; "
                f"stable active MCP context contains "
                f"{self._diagnostic_count(metrics.get('active_mcp_schemas')) or 0} "
                f"schemas using "
                f"{self._diagnostic_count(metrics.get('active_mcp_schema_chars')) or 0:,} "
                f"characters; "
                f"historical tool results use "
                f"{metrics.get('historical_tool_result_chars_after', 0):,} of "
                f"{metrics.get('max_historical_tool_result_chars', 0):,} budget characters; "
                f"{self._age_text(metrics)}."
            )
            await self._notify(__event_emitter__, report)
            return None

        if __id__ == "efficiency_report":
            names = metrics.get("tool_names") or []
            displayed_names = ", ".join(names[:8])
            if len(names) > 8:
                displayed_names += f", and {len(names) - 8} more"
            if not displayed_names:
                displayed_names = "none"

            report = (
                f"Provider calls this turn: {metrics.get('provider_call_count', 0)}; "
                f"tools: {metrics.get('tool_count', 0)} ({displayed_names}); "
                f"tool schemas: {metrics.get('tool_schema_chars', 0):,} characters; "
                f"trimmed results: {metrics.get('trimmed_results', 0)}; "
                f"budget compactions: {metrics.get('budget_compacted_results', 0)}; "
                f"characters omitted from outgoing context: {metrics.get('omitted_chars', 0):,}; "
                f"turn duration: {metrics.get('turn_duration_seconds', 0)} seconds; "
                f"{self._age_text(metrics)}."
            )
            await self._notify(__event_emitter__, report)
            return None

        if __id__ == "optimization_report":
            recommendations = []
            confirmations = []
            age = self._age_seconds(metrics)

            if age is not None and age > self.valves.stale_after_seconds:
                recommendations.append(
                    "Send a new message before evaluating settings because these metrics are stale."
                )

            schema_chars = metrics.get("tool_schema_chars", 0)
            if schema_chars > self.valves.schema_warning_chars:
                recommendations.append(
                    f"Reduce default active tools: schemas use {schema_chars:,} characters, "
                    f"above the {self.valves.schema_warning_chars:,}-character guideline."
                )

            input_tokens = metrics.get("estimated_input_tokens", 0)
            if input_tokens > self.valves.context_warning_tokens:
                recommendations.append(
                    f"Provider input is approximately {input_tokens:,} tokens. Verify that "
                    "Open WebUI Context Compaction is enabled for long conversations."
                )

            provider_calls = metrics.get("provider_call_count", 0)
            if provider_calls > self.valves.provider_call_warning:
                recommendations.append(
                    f"This turn used {provider_calls} provider calls. Prefer direct task requests "
                    "and avoid asking the model to enumerate tools before acting."
                )

            result_chars = metrics.get("tool_result_chars_after", 0)
            if result_chars > self.valves.tool_result_warning_chars:
                recommendations.append(
                    f"Retained tool results use {result_chars:,} characters. Use bounded MCP "
                    "queries or artifact paging for large responses."
                )

            if not metrics.get("trim_enabled", True):
                recommendations.append("Enable Agent Efficiency trimming.")
            elif metrics.get("max_historical_tool_result_chars", 0) == 0:
                recommendations.append(
                    "Set a nonzero historical tool-result budget to control accumulated results."
                )

            compacted = metrics.get("budget_compacted_results", 0)
            omitted = metrics.get("omitted_chars", 0)
            if compacted:
                confirmations.append(
                    f"Historical compaction is active: {compacted} result(s) compacted and "
                    f"{omitted:,} characters omitted."
                )
            if schema_chars <= self.valves.schema_warning_chars:
                confirmations.append(
                    f"Tool-schema size is within the default guideline at {schema_chars:,} characters."
                )

            if recommendations:
                content = "Recommendations:\n- " + "\n- ".join(recommendations)
                if confirmations:
                    content += "\n\nWorking as intended:\n- " + "\n- ".join(
                        confirmations
                    )
                level = "warning"
            else:
                content = "No configuration changes recommended. " + (
                    " ".join(confirmations)
                    if confirmations
                    else "Current request metrics are within the default guidelines."
                )
                level = "info"

            await self._notify(__event_emitter__, content, level)
            return None

        if __id__ == "tool_schema_report":
            sizes = metrics.get("tool_schema_sizes") or []
            if not sizes:
                await self._notify(
                    __event_emitter__,
                    "Per-tool schema measurements are unavailable. Send a new message with "
                    "Agent Efficiency v0.2.1 or newer enabled.",
                    "warning",
                )
                return None

            total = metrics.get("tool_schema_chars", 0)
            lines = []
            for item in sizes:
                if not isinstance(item, dict):
                    continue
                characters = item.get("characters", 0)
                estimated_tokens = round(characters / 4)
                lines.append(
                    f"- {item.get('name', 'unknown')}: {characters:,} characters "
                    f"(~{estimated_tokens:,} tokens)"
                )

            content = (
                f"Tool schemas: {len(lines)} tools using {total:,} characters "
                f"(~{round(total / 4):,} tokens), largest first:\n" + "\n".join(lines)
            )
            await self._notify(__event_emitter__, content)
            return None

        if __id__ == "view_skill_report":
            protected_results = (
                self._diagnostic_count(
                    metrics.get("protected_native_view_skill_results")
                )
                or 0
            )
            protected_chars = (
                self._diagnostic_count(metrics.get("protected_native_view_skill_chars"))
                or 0
            )
            content = (
                f"Current-turn protected native view_skill results: "
                f"{protected_results} ({protected_chars:,} characters). "
                "Older view_skill results follow ordinary historical compaction."
            )
            await self._notify(__event_emitter__, content)
            return None

        if __id__ == "tool_broker_routing_report":
            ranking_state = getattr(
                __request__.app.state,
                RANKING_DIAGNOSTICS_STATE_KEY,
                {},
            )
            ranking_records = (
                ranking_state.get("records", {})
                if isinstance(ranking_state, dict)
                else {}
            )
            ranking = (
                ranking_records.get(key) if isinstance(ranking_records, dict) else None
            )
            if isinstance(ranking, dict):
                updated_at = ranking.get("updated_at_epoch")
                now = time.time()
                if (
                    type(updated_at) not in (int, float)
                    or not 0 <= updated_at <= now
                    or now - updated_at >= RANKING_DIAGNOSTICS_TTL_SECONDS
                ):
                    ranking = None
            if isinstance(ranking, dict):
                routing_report = self._routing_report(ranking.get("routing"))
                if routing_report:
                    await self._notify(
                        __event_emitter__,
                        "Tool Broker" + routing_report,
                    )
                    return None
            await self._notify(
                __event_emitter__,
                "No current Tool Broker routing diagnostics are available.",
                "warning",
            )
            return None

        await self._notify(
            __event_emitter__,
            f"Unknown diagnostics action: {__id__}",
            "error",
        )
        return None
