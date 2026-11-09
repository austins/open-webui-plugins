import copy
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

from obsolete.tool_broker.functions import tool_broker_policy as policy


class TestPolicyPrefix:
    def setup_method(self):
        self.filter = policy.Filter()
        self.filter._active_persisted_mcp_packet = AsyncMock(
            return_value=(None, 0, False)
        )
        self.filter._automatic_route = AsyncMock(return_value=False)
        self.tools = [
            {"type": "function", "function": {"name": name}}
            for name in ("search_tools", "execute_tool")
        ]
        self.user = {"role": "user", "content": "Verify these claims."}

    async def request(self, messages):
        body = {"messages": messages, "tools": self.tools}
        original = copy.deepcopy(body)
        result = await self.filter.request(body)
        assert body == original
        system = "\n".join(
            message["content"]
            for message in result["messages"]
            if message.get("role") == "system"
        )
        others = [
            message
            for message in result["messages"]
            if message.get("role") != "system"
        ]
        return [{"role": "system", "content": system}, *others]

    async def test_removes_only_owned_separator_for_each_block(self, subtests):
        blocks = [
            policy.POLICY,
            f"{policy.AUTOMATIC_DISCOVERY_MARKER}\n{{}}\n{policy.AUTOMATIC_DISCOVERY_END}",
            f"{policy.AUTOMATIC_HINT_MARKER}\n{{}}\n{policy.AUTOMATIC_HINT_END}",
            f"{policy.ACTIVE_MCP_MARKER}\n{{}}\n{policy.ACTIVE_MCP_END}",
            f"{policy.REQUIRED_EXECUTION_MARKER}\n{{}}\n{policy.REQUIRED_EXECUTION_END}",
        ]
        for base in ("User instructions", "\nUser instructions \n\n"):
            for block in blocks:
                with subtests.test(base=base, block=block):
                    result = await self.request(
                        [{"role": "system", "content": base + "\n" + block}, self.user]
                    )
                    assert result[0]["content"] == base + "\n" + policy.POLICY

    async def test_standalone_blocks_leave_no_empty_system_messages(self):
        result = await self.filter.request(
            {
                "messages": [
                    {"role": "system", "content": policy.POLICY},
                    self.user,
                ],
                "tools": self.tools,
            }
        )
        assert result["messages"] == [{"role": "system", "content": policy.POLICY}, self.user]

    async def test_preserves_unrelated_system_messages(self):
        messages = [
            {"role": "system", "content": "\nFirst instructions \n"},
            {"role": "system", "content": policy.POLICY},
            {"role": "system", "content": "Second instructions\n\n"},
            self.user,
        ]
        result = await self.request(messages)
        assert result[0]["content"] == "\nFirst instructions \n\nSecond instructions\n\n\n" + policy.POLICY

    async def test_policy_and_hint_remain_identical_across_tool_rounds(self):
        hint = (
            f"{policy.AUTOMATIC_HINT_MARKER}\n"
            '{"state":"hint_only_not_authorized"}\n'
            f"{policy.AUTOMATIC_HINT_END}"
        )

        async def add_hint(body, messages, *args):
            messages.insert(
                self.filter._current_turn_insert_at(messages),
                {"role": "system", "content": hint},
            )
            return False

        self.filter._automatic_route.side_effect = add_hint
        first = await self.request(
            [{"role": "system", "content": "Instructions\n</available_skills>"}, self.user]
        )
        current = first
        for _ in range(4):
            current = await self.request(current)
            assert current == first


class TestAutomaticRouting:
    def setup_method(self):
        self.filter = policy.Filter()
        self.broker = SimpleNamespace(
            _automatic_discovery=AsyncMock(),
            _turn_usage_key=Mock(return_value=("user", "scope")),
        )
        self.filter._resolved_broker = Mock(return_value=self.broker)

    async def route(self, content, result, event_emitter=None):
        self.broker._automatic_discovery.return_value = result
        body = {
            "messages": [
                {"role": "system", "content": "Instructions"},
                {"role": "user", "content": content},
            ]
        }
        routed = await self.filter._automatic_route(
            body, body["messages"], object(), {"id": "user"}, {}, event_emitter, None
        )
        return body, routed

    async def test_apostrophe_variants_skip_discovery(self, subtests):
        for apostrophe in ("'", "\u2018", "\u2019", ""):
            with subtests.test(apostrophe=apostrophe):
                self.broker._automatic_discovery.reset_mock()
                text = f"Don{apostrophe}t use any tools, which laptop is better for travel?"
                body, routed = await self.route(text, {"status": "no_match"})
                assert not routed
                assert len(body["messages"]) == 2
                self.broker._automatic_discovery.assert_not_awaited()

    async def test_quoted_negation_is_not_a_tool_opt_out(self, subtests):
        for text in (
            'Search for "don\u2019t use any tools" in documentation',
            "Search for \u2018don\u2019t use tools\u2019 in documentation",
            "Search for `don't use tools` in documentation",
        ):
            with subtests.test(text=text):
                assert self.filter._automatic_query([{"role": "user", "content": text}], 0) == text

    async def test_no_match_hints_do_not_change_messages_or_tool_choice(self, subtests):
        for tool in (
            "open-meteo.openmeteo_get_marine",
            "sec-edgar.secedgar_get_beneficial_owners",
            "pubmed.pubmed_lookup_citation",
        ):
            with subtests.test(tool=tool):
                emitter = AsyncMock()
                body, routed = await self.route(
                    "What is the difference between TCP and UDP?",
                    {"status": "no_match", "hint": {"tool": tool, "description": "Hint"}},
                    emitter,
                )
                assert not routed
                assert body["messages"] == [
                    {"role": "system", "content": "Instructions"},
                    {
                        "role": "user",
                        "content": "What is the difference between TCP and UDP?",
                    },
                ]
                assert "tool_choice" not in body
                emitter.assert_not_awaited()
                self.broker._turn_usage_key.assert_not_called()

    async def test_confident_discovery_still_injects_authorized_packet(self):
        response = {"matches": [{"tool": "weather.forecast", "description": "Forecast"}]}
        body, routed = await self.route(
            "Find tomorrow's weather forecast.",
            {"status": "discovered", "response": json.dumps(response)},
        )
        assert routed
        packet = body["messages"][1]["content"]
        assert packet.startswith(policy.AUTOMATIC_DISCOVERY_MARKER)
        payload = json.loads(packet.split("\n", 1)[1].rsplit("\n", 1)[0])
        assert payload["scope"] == "scope"
        assert payload["matches"] == response["matches"]
        assert "tool_choice" not in body

    async def test_no_match_hints_preserve_prefix_across_user_turns(self):
        self.filter._active_persisted_mcp_packet = AsyncMock(
            return_value=(None, 0, False)
        )
        body = {
            "messages": [
                {"role": "system", "content": "Instructions"},
                {"role": "user", "content": "Compare TCP and UDP."},
            ],
            "tools": [
                {"type": "function", "function": {"name": name}}
                for name in ("search_tools", "execute_tool")
            ],
        }
        prefix = None
        for tool in ("marine.weather", "sec.owners", "pubmed.citation"):
            self.broker._automatic_discovery.return_value = {
                "status": "no_match",
                "hint": {"tool": tool, "description": "Below-threshold candidate"},
            }
            result = await self.filter.request(body, __request__=object())
            system = "\n".join(
                message["content"]
                for message in result["messages"]
                if message["role"] == "system"
            )
            messages = [{"role": "system", "content": system}] + [
                message
                for message in result["messages"]
                if message["role"] != "system"
            ]
            if prefix is not None:
                assert messages[: len(prefix)] == prefix
            prefix = copy.deepcopy(messages)
            body = {
                **body,
                "messages": messages
                + [
                    {"role": "assistant", "content": "Comparison."},
                    {"role": "user", "content": "Which is better for travel?"},
                ],
            }

    async def test_failures_still_emit_status(self, subtests):
        for status in ("blocked", "error"):
            with subtests.test(status=status):
                emitter = AsyncMock()
                body, routed = await self.route(
                    "Find the weather forecast.",
                    {"status": status, "reason": "Discovery unavailable"},
                    emitter,
                )
                assert not routed
                assert len(body["messages"]) == 2
                emitter.assert_awaited_once_with(
                    {
                        "type": "status",
                        "data": {"description": "Discovery unavailable", "done": True},
                    }
                )
