import asyncio
import json
import sys
import types
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from context_extraction.tools import chat_recall as recall


def get_message_list(messages_map, message_id):
    chain = []
    while message_id in messages_map:
        chain.append(messages_map[message_id])
        message_id = messages_map[message_id].get("parentId")
    return list(reversed(chain))


def saved_messages():
    return {
        "u1": {"id": "u1", "role": "user", "content": "please inspect the config", "parentId": None},
        "a1": {
            "id": "a1",
            "role": "assistant",
            "parentId": "u1",
            "content": "",
            "output": [
                {"type": "reasoning", "content": [{"type": "output_text", "text": "hidden thoughts"}]},
                {
                    "type": "function_call",
                    "call_id": "fc1",
                    "name": "call_tool",
                    "arguments": json.dumps({"tool_name": "read_file", "params": {"path": "config.yaml"}}),
                },
                {
                    "type": "function_call_output",
                    "call_id": "fc1",
                    "output": [{"type": "input_text", "text": "port: 8080\n" + "filler " * 3000 + "\nretry_limit: 7"}],
                },
                {"type": "message", "content": [{"type": "output_text", "text": "The port is 8080."}]},
            ],
        },
        "u2": {"id": "u2", "role": "user", "content": "thanks", "parentId": "a1"},
        "a2": {"id": "a2", "role": "assistant", "content": "<think>x</think>You're welcome.", "parentId": "u2"},
    }


class FakeChats:
    def __init__(self, owner="user-1", rows=True):
        self.owner = owner
        self.rows = rows
        self.messages = saved_messages()

    async def get_chat_by_id_and_user_id(self, chat_id, user_id):
        if user_id != self.owner:
            return None
        return SimpleNamespace(chat={"history": {"messages": self.messages, "currentId": "a2"}})

    async def get_messages_map_by_chat_id(self, chat_id):
        return self.messages if self.rows else None


class TestChatRecall:
    @pytest.fixture(autouse=True)
    def open_webui(self):
        self.chats = FakeChats()
        modules = {
            "open_webui": types.ModuleType("open_webui"),
            "open_webui.models": types.ModuleType("open_webui.models"),
            "open_webui.models.chats": types.ModuleType("open_webui.models.chats"),
            "open_webui.utils": types.ModuleType("open_webui.utils"),
            "open_webui.utils.misc": types.ModuleType("open_webui.utils.misc"),
            "open_webui.tasks": types.ModuleType("open_webui.tasks"),
        }
        modules["open_webui.models.chats"].Chats = self.chats
        modules["open_webui.utils.misc"].get_message_list = get_message_list

        async def no_streams(redis, chat_id):
            return []

        modules["open_webui.tasks"].get_response_streams_by_chat_id = no_streams
        with patch.dict(sys.modules, modules):
            self.tools = recall.Tools()
            yield

    def search(self, query, user_id="user-1", chat_id="chat-1", message_id="a2", match="any", count=5):
        return asyncio.run(
            self.tools.recall_search(
                query, match, count, __user__={"id": user_id}, __chat_id__=chat_id, __message_id__=message_id
            )
        )

    def read(self, message=0, count=1, offset=0, user_id="user-1", chat_id="chat-1", ref="", message_id="a2"):
        return asyncio.run(
            self.tools.recall_read(
                message, count, offset, ref, __user__={"id": user_id}, __chat_id__=chat_id, __message_id__=message_id
            )
        )

    def append(self, *messages):
        parent = "a2"
        for message in messages:
            self.chats.messages[message["id"]] = {**message, "parentId": parent}
            parent = message["id"]
        return parent

    def test_search_finds_compacted_tool_output_with_unwrapped_tool_name(self):
        result = json.loads(self.search("retry_limit"))

        assert result["messages_in_chat"] == 4
        assert result["results"][0]["message"] == 2
        assert result["results"][0]["part"] == "tool result"
        assert result["results"][0]["tool"] == "read_file"
        assert "retry_limit: 7" in result["results"][0]["snippet"]

    def test_all_mode_requires_every_keyword_and_reasoning_is_skipped(self):
        assert json.loads(self.search("port nonexistent", match="all"))["results"] == []
        assert json.loads(self.search("port nonexistent"))["matches"] == 2
        assert json.loads(self.search("hidden thoughts"))["results"] == []
        matches = json.loads(self.search("port 8080"))["results"]
        assert {(m["message"], m["part"]) for m in matches} == {(2, "tool result"), (2, "text")}

    def test_read_pages_long_messages(self):
        self.tools.valves.max_read_chars = 1_000
        first = self.read(2)

        assert first.startswith("## Message 2 (assistant)\n\n[tool call: read_file]\n")
        assert '"path": "config.yaml"' in first
        assert "Continue with recall_read(message=2, count=1, offset=1000)" in first
        assert "port: 8080" in first
        rest = self.read(2, offset=1_000)
        while "Continue with" in rest:
            offset = int(rest.rsplit("offset=", 1)[1].split(")")[0])
            rest = self.read(2, offset=offset)
        assert rest.endswith("The port is 8080.")

    def test_read_strips_think_blocks_and_checks_range(self):
        assert self.read(4) == "## Message 4 (assistant)\n\nYou're welcome."
        assert self.read(9) == "Error: message must be between 1 and 4 in this chat, or pass a tool call ref."

    def test_other_users_chat_is_refused(self):
        assert self.search("port", user_id="someone-else") == "Error: The current chat was not found for this user."

    def test_temporary_and_missing_chats_are_refused(self):
        assert self.search("port", chat_id="local:abc").startswith("Error: This chat is not saved")
        assert self.search("port", chat_id=None).startswith("Error: This chat is not saved")

    def test_falls_back_to_embedded_history(self):
        self.chats.rows = False
        assert json.loads(self.search("retry_limit"))["results"][0]["message"] == 2

    def test_uses_current_branch_when_message_is_not_saved_yet(self):
        result = json.loads(self.search("welcome", message_id="unsaved"))
        assert result["results"][0]["message"] == 4

    def test_reads_a_tool_call_by_ref_in_pages(self):
        self.tools.valves.max_read_chars = 1_000
        first = self.read(ref="fc1")

        assert first.startswith("## Message 2 (assistant), tool call fc1\n\n[tool call: read_file]\n")
        assert 'Continue with recall_read(ref="fc1", offset=1000)' in first
        assert "The port is 8080." not in first
        rest = self.read(ref="fc1", offset=1_000)
        while "Continue with" in rest:
            rest = self.read(ref="fc1", offset=int(rest.rsplit("offset=", 1)[1].split(")")[0]))
        assert rest.endswith("retry_limit: 7")
        assert self.read(ref='[ref "fc1"]')[:40] == first[:40]

    def test_search_results_name_the_ref_and_reused_ids_are_qualified(self):
        assert json.loads(self.search("retry_limit"))["results"][0]["ref"] == "fc1"
        self.append(
            {"id": "u3", "role": "user", "content": "again"},
            {
                "id": "a3",
                "role": "assistant",
                "content": "",
                "output": [
                    {"type": "function_call", "call_id": "fc1", "name": "read_file", "arguments": "{}"},
                    {"type": "function_call_output", "call_id": "fc1", "output": "retry_limit: 9"},
                ],
            },
        )
        result = asyncio.run(
            self.tools.recall_search("retry_limit", __user__={"id": "user-1"}, __chat_id__="c", __message_id__="a3")
        )
        assert [r["ref"] for r in json.loads(result)["results"]] == ["6/fc1", "2/fc1"]

        read = lambda ref: asyncio.run(
            self.tools.recall_read(ref=ref, __user__={"id": "user-1"}, __chat_id__="c", __message_id__="a3")
        )
        assert read("fc1") == "Error: 2 tool calls in this chat have the ID fc1. Pass one of these refs instead: 2/fc1, 6/fc1."
        assert read("6/fc1").endswith("retry_limit: 9")
        assert "filler" in read("2/fc1")

    def test_unknown_ref_is_reported(self):
        self.live_reply([{"chat_id": "chat-1", "message_id": "a2", "output": self.live_output()}])
        assert self.run_live("recall_read", ref="call_new") == (
            'Error: No tool call in this chat, including the reply in progress, has the ref "call_new". '
            "Check the ref, or look for the detail with recall_search."
        )

    def test_unknown_ref_says_when_the_reply_in_progress_was_not_checked(self):
        unchecked = (
            'Error: No saved tool call in this chat has the ref "call_new", and the reply in progress could not be '
            "checked. Check the ref, or look for the detail with recall_search."
        )
        other_reply = [{"chat_id": "chat-1", "message_id": "other", "output": self.live_output()}]
        for streams in ([], other_reply, RuntimeError("redis down")):
            self.live_reply(streams)
            assert self.run_live("recall_read", ref="call_new") == unchecked

    def live_reply(self, streams, request=None):
        """Give a2 the output of a reply in progress, which Open WebUI has not saved to the chat yet."""
        calls = []

        async def get_response_streams_by_chat_id(redis, chat_id):
            calls.append((redis, chat_id))
            if isinstance(streams, Exception):
                raise streams
            return streams

        sys.modules["open_webui.tasks"].get_response_streams_by_chat_id = get_response_streams_by_chat_id
        return calls

    def live_output(self, text="build id: 4242"):
        return [
            {"type": "function_call", "call_id": "live1", "name": "execute_code", "arguments": '{"code": "x"}'},
            {"type": "function_call_output", "call_id": "live1", "output": [{"type": "input_text", "text": text}]},
        ]

    def run_live(self, method, *args, request=None, **kwargs):
        return asyncio.run(
            getattr(self.tools, method)(
                *args, __user__={"id": "user-1"}, __chat_id__="chat-1", __message_id__="a2", __request__=request, **kwargs
            )
        )

    def test_reads_and_searches_the_reply_in_progress_without_redis(self):
        calls = self.live_reply([{"chat_id": "chat-1", "message_id": "a2", "output": self.live_output()}])
        request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(redis=None)))
        saved = self.chats.messages["a2"]

        assert self.run_live("recall_read", ref="live1", request=request).endswith("[tool result: execute_code]\nbuild id: 4242")
        result = json.loads(self.run_live("recall_search", "4242", request=request))
        assert (result["results"][0]["message"], result["results"][0]["ref"]) == (4, "live1")
        assert calls == [(None, "chat-1"), (None, "chat-1")]
        assert self.run_live("recall_read", ref="fc1", request=request).startswith("## Message 2 (assistant), tool call fc1")
        assert self.chats.messages["a2"] is saved and "output" not in saved

    def test_passes_redis_to_open_webui_when_configured(self):
        redis = object()
        calls = self.live_reply([{"chat_id": "chat-1", "message_id": "a2", "output": self.live_output()}])
        request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(redis=redis)))

        assert "build id: 4242" in self.run_live("recall_read", ref="live1", request=request)
        assert calls == [(redis, "chat-1")]

    def test_ignores_streams_of_other_replies_and_failures(self):
        self.live_reply([{"chat_id": "chat-1", "message_id": "other", "output": self.live_output()}])
        assert self.run_live("recall_read", ref="live1").startswith("Error: No saved tool call")

        self.live_reply(RuntimeError("redis down"))
        assert self.run_live("recall_read", ref="fc1").startswith("## Message 2 (assistant), tool call fc1")

    def test_other_users_never_reach_the_reply_in_progress(self):
        calls = self.live_reply([{"chat_id": "chat-1", "message_id": "a2", "output": self.live_output()}])
        result = asyncio.run(
            self.tools.recall_read(ref="live1", __user__={"id": "someone-else"}, __chat_id__="chat-1", __message_id__="a2")
        )
        assert result == "Error: The current chat was not found for this user."
        assert calls == []

    def test_search_leaves_out_recall_calls_and_the_latest_user_message(self):
        last = self.append(
            {
                "id": "a3",
                "role": "assistant",
                "content": "",
                "output": [
                    {"type": "function_call", "call_id": "r1", "name": "recall_search", "arguments": '{"query": "zebra"}'},
                    {"type": "function_call_output", "call_id": "r1", "output": '{"results": ["zebra"]}'},
                    {"type": "function_call", "call_id": "r2", "name": "call_tool", "arguments": '{"tool_name": "chat_recall_recall_read", "params": {"ref": "zebra"}}'},
                    {"type": "function_call_output", "call_id": "r2", "output": "zebra"},
                ],
            },
            {"id": "u3", "role": "user", "content": "where is the zebra?"},
        )
        result = asyncio.run(
            self.tools.recall_search("zebra", __user__={"id": "user-1"}, __chat_id__="c", __message_id__=last)
        )
        assert json.loads(result)["results"] == []
        assert "zebra" in self.read(5, message_id=last)

    def test_ranking_prefers_parts_with_more_matched_words(self):
        self.append(
            {"id": "a3", "role": "assistant", "content": "The configuration lives in settings."},
            {"id": "u3", "role": "user", "content": "Policies are in the deploy configuration file."},
            {"id": "a4", "role": "assistant", "content": "ok"},
            {"id": "u4", "role": "user", "content": "deploy config policy"},
            {"id": "a5", "role": "assistant", "content": ""},
        )
        results = json.loads(
            asyncio.run(
                self.tools.recall_search(
                    "deploy config policy", __user__={"id": "user-1"}, __chat_id__="c", __message_id__="a5"
                )
            )
        )["results"]
        assert results[0]["message"] == 6
        assert {r["message"] for r in results[1:]} == {1, 2, 5}
        assert 8 not in [r["message"] for r in results]

    def test_stop_word_only_query_falls_back_to_substring_matching(self):
        result = json.loads(self.search("the"))
        assert result["match"] == "all"
        assert result["matches"] > 0

    def test_unicode_search_folds_accents_and_keeps_snippet_offsets(self):
        self.chats.messages["u2"] = {**self.chats.messages["u2"], "content": "Straße " * 200 + "near the CAFÉ in İstanbul"}
        self.append({"id": "u3", "role": "user", "content": "next"}, {"id": "a3", "role": "assistant", "content": "ok"})
        result = json.loads(
            asyncio.run(
                self.tools.recall_search("cafe istanbul", __user__={"id": "user-1"}, __chat_id__="c", __message_id__="a3")
            )
        )["results"][0]
        assert result["message"] == 3
        assert "CAFÉ in İstanbul" in result["snippet"]


class TestTokenizer:
    def test_folding(self):
        assert recall.fold("Café")[0] == "cafe"
        assert recall.fold("İstanbul")[0] == "istanbul"
        folded, origin = recall.fold("aßb")
        assert (folded, origin) == ("assb", [0, 1, 1, 2, 3])

    def test_words_in_several_scripts(self):
        assert recall.tokenize("readFile retry_limit HTTPServer files policies") == ["read", "file", "retry", "limit", "httpserver", "file", "policy"]
        assert recall.tokenize("東京都に") == ["東京", "京都", "都に"]
        assert recall.tokenize("한국어") == ["한국", "국어"]
        assert recall.tokenize("नमस्ते दुनिया") == ["नमस्ते", "दुनिया"]
        assert recall.tokenize("Ελληνικά café") == ["ελληνικα", "cafe"]
        assert recall.tokenize("the a x 7") == ["7"]
