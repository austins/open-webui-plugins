import asyncio
import copy
import json
import logging
from contextlib import contextmanager
from types import SimpleNamespace

import pytest

from context_extraction.functions import context_extraction as ce


@contextmanager
def logs(caplog, level):
    caplog.clear()
    with caplog.at_level(level, logger=ce.log.name):
        yield
    assert any(r.name == ce.log.name and r.levelno >= level for r in caplog.records)


def user(text):
    return {"role": "user", "content": text}


def assistant(text="", calls=None):
    message = {"role": "assistant", "content": text}
    if calls:
        message["tool_calls"] = [
            {
                "id": call_id,
                "type": "function",
                "function": {"name": name, "arguments": json.dumps(arguments)},
            }
            for call_id, name, arguments in calls
        ]
    return message


def tool(call_id, text):
    return {"role": "tool", "tool_call_id": call_id, "content": text}


def turn(number, output_chars=4_000, calls=1):
    messages = [user(f"request {number}")]
    for index in range(calls):
        call_id = f"c{number}_{index}"
        messages.append(assistant(calls=[(call_id, "read_file", {"path": f"src/f{number}_{index}.py"})]))
        messages.append(tool(call_id, f"output {number}.{index} " + "x" * output_chars))
    messages.append(assistant(f"answer {number}"))
    return messages


def history(turns, **kwargs):
    messages = []
    for number in range(1, turns + 1):
        messages += turn(number, **kwargs)
    return messages


def make_filter(threshold=10_000, **valves):
    flt = ce.Filter()
    flt.valves = flt.Valves(
        default_context_window=1_000_000, max_threshold_tokens=threshold, show_status=False, **valves
    )
    return flt


def run(flt, messages, metadata=None, request=None):
    body = {"messages": [{"role": "system", "content": "system prompt"}, *messages]}
    metadata = metadata or {"chat_id": "chat", "message_id": "m1"}
    return asyncio.run(
        flt.request(body, __metadata__=metadata, __user__={"id": "u1"}, __request__=request)
    )["messages"]


def assert_tool_pairs(messages):
    seen = set()
    for message in messages:
        for call in message.get("tool_calls") or []:
            seen.add(call["id"])
        if message.get("role") == "tool":
            assert message["tool_call_id"] in seen


class TestContextExtraction:
    def test_below_threshold_leaves_request_unchanged(self):
        messages = history(2)
        body = {"messages": [{"role": "system", "content": "s"}, *messages]}
        original = body["messages"]
        result = asyncio.run(
            make_filter(threshold=100_000).request(body, __user__={"id": "u1"})
        )
        assert result["messages"] is original

    def test_stubs_old_tool_output_before_summarizing(self):
        messages = history(6, output_chars=6_000)
        original = copy.deepcopy(messages)
        output = run(make_filter(threshold=8_000), messages)

        assert messages == original
        assert ce.SUMMARY_HEADING not in output[1]["content"]
        stubs = [m for m in output if m.get("role") == "tool" and m["content"].startswith("[Earlier tool output")]
        assert stubs
        assert output[-2]["content"][:10] == "output 6.0"
        assert_tool_pairs(output)

    def test_summarizes_when_stubs_are_not_enough(self):
        messages = history(40, output_chars=100)
        output = run(make_filter(threshold=3_000), messages)

        assert output[1]["content"].startswith(ce.SUMMARY_HEADING)
        assert output[2]["content"] == ce.SUMMARY_ACKNOWLEDGEMENT
        assert output[3]["role"] == "user"
        assert output[-1]["content"] == "answer 40"
        assert "- request 1" in output[1]["content"]
        kept = int(output[3]["content"].split()[-1])
        assert f"- read src/f{kept - 1}_0.py" in output[1]["content"]
        assert_tool_pairs(output)

    def test_prefix_stays_stable_until_threshold_is_crossed_again(self):
        flt = make_filter(threshold=3_000)
        messages = history(40, output_chars=100)
        first = run(flt, messages)
        second = run(flt, [*messages, *turn(41, output_chars=100)])

        assert second[: len(first)] == first

    def test_cut_moves_forward_when_threshold_is_crossed_again(self):
        flt = make_filter(threshold=3_000)
        messages = history(40, output_chars=100)
        first = run(flt, messages)
        later = run(flt, history(80, output_chars=100))

        assert first[1]["content"] != later[1]["content"]
        assert "- request 1" in later[1]["content"]
        first_kept = int(first[3]["content"].split()[-1])
        later_kept = int(later[3]["content"].split()[-1])
        assert later_kept > first_kept
        assert f"- request {later_kept - 1}" in later[1]["content"]

    def test_tool_loop_rerun_matches_a_fresh_replay(self):
        request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace()))
        flt = make_filter(threshold=3_000)
        messages = history(40, output_chars=100) + [user("final request")]
        sent = run(flt, messages, request=request)

        new = [
            assistant(calls=[("loop1", "run_shell", {"command": "make test"})]),
            tool("loop1", "ok " + "y" * 300),
        ]
        rerun = run(flt, [*sent[1:], *new], request=request)
        fresh = run(make_filter(threshold=3_000), [*messages, *new])

        assert rerun == fresh

    def test_tool_loop_rerun_without_cache_is_sent_unchanged(self, caplog):
        flt = make_filter(threshold=3_000)
        sent = run(flt, history(40, output_chars=100), request=SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace())))
        with logs(caplog, logging.WARNING):
            rerun = run(make_filter(threshold=3_000), sent[1:])

        assert rerun == sent

    def test_long_turn_is_cut_mid_turn_with_its_user_message_kept(self):
        messages = turn(1, output_chars=50) + turn(2, output_chars=600, calls=40)[:-1]
        output = run(make_filter(threshold=3_000, stub_min_chars=10_000), messages)

        assert output[1]["content"].startswith(ce.SUMMARY_HEADING)
        assert output[3] == user("request 2")
        assert output[4]["role"] == "assistant"
        assert "tool_calls" in output[4]
        assert_tool_pairs(output)

    def test_notes_unwrap_dispatchers_and_never_copy_tool_output(self):
        messages = [
            user("fix the build"),
            assistant(
                "<think>secret plan</think>Looking.",
                calls=[
                    ("a", "call_tool", {"tool_name": "edit_file", "params": {"path": "app.py"}}),
                    ("b", "execute_tool", {"tool": "shell.run", "arguments": {"command": "pytest -q"}}),
                    ("c", "call_tool", {"calls": [{"tool_name": "read_file", "params": {"path": "app.py"}}]}),
                ],
            ),
            tool("a", "TOP-SECRET-OUTPUT"),
            tool("b", "Error: tests failed"),
            tool("c", "file body"),
        ]
        notes = ce.extract_notes(messages)

        assert "- changed app.py" in notes
        assert "- read app.py" not in notes
        assert "[Shell runs]\n- pytest -q" in notes
        assert "- shell.run pytest -q (failed)" in notes
        assert "- assistant: Looking." in notes
        assert "TOP-SECRET-OUTPUT" not in notes
        assert "secret plan" not in notes
        assert notes.endswith(ce.FOOTER)

    def test_incremental_notes_match_a_fresh_extraction_at_every_prefix(self):
        messages = [user("first ask")]
        for index in range(40):
            messages += [
                assistant(f"step {index}", calls=[(f"r{index}", "read_file", {"path": f"f{index % 30}.py"})]),
                tool(f"r{index}", "Error: missing" if index % 7 == 0 else "ok"),
                assistant(calls=[(f"e{index}", "write_file", {"path": f"f{index % 30}.py"}), (f"s{index}", "run", {"command": f"make {index}"})]),
                tool(f"e{index}", "ok"),
                tool(f"s{index}", json.dumps({"error": "boom"}) if index % 5 == 0 else "done"),
                assistant(calls=[(f"x{index}", "execute_code", {"code": f"print({index})\n" * (index % 4 + 1)})]),
                tool(f"x{index}", "Error: crashed" if index % 3 == 0 else str(index)),
                user(f"ask {index}"),
            ]
        extractor = ce.NoteExtractor(messages)
        for end in range(len(messages) + 1):
            assert extractor.advance(end).render() == ce.extract_notes(messages[:end]), end
        with pytest.raises(ValueError):
            extractor.advance(0)

    def test_keep_listed_tool_results_are_never_stubbed(self):
        messages = [user("load skill"), assistant(calls=[("s", "view_skill", {"name": "x"})]), tool("s", "skill " + "z" * 5_000)]
        messages += history(6, output_chars=6_000)
        output = run(make_filter(threshold=8_000), messages)

        assert any(m.get("tool_call_id") == "s" and m["content"].startswith("skill ") for m in output)

    def test_errors_fail_open(self, caplog):
        flt = make_filter()
        flt.plan = lambda *_: (_ for _ in ()).throw(RuntimeError("boom"))
        body = {"messages": history(1)}
        original = copy.deepcopy(body)
        with logs(caplog, logging.ERROR):
            result = asyncio.run(flt.request(body, __user__={"id": "u1"}))
        assert result == original

    def test_status_is_reported_for_the_current_request(self):
        flt = make_filter(threshold=3_000)
        flt.valves.show_status = True
        events = []

        async def emit(event):
            events.append(event)

        body = {"messages": [*history(3, output_chars=100), user("long paste " + "p" * 12_000)]}
        asyncio.run(flt.request(body, __user__={"id": "u1"}, __event_emitter__=emit))
        assert len(events) == 1
        assert "Context compacted" in events[0]["data"]["description"]

        events.clear()
        asyncio.run(flt.request({"messages": history(40, output_chars=100)}, __user__={"id": "u1"}, __event_emitter__=emit))
        assert events == []

    def test_stubs_name_the_call_ref_and_keep_the_error(self):
        assert ce._stub_text("x" * 3_000, "call_9") == (
            '[Earlier tool output omitted by Context Extraction: 3,000 characters. To read it again, call '
            'recall_read with ref "call_9" if that tool is available; repeat the call only if doing so is safe.]'
        )
        assert "Search it with recall_search" in ce._stub_text("x" * 3_000)
        failed = ce._stub_text("Error: disk full.\n" + "trace " * 1_000, "c")
        assert "It reported an error: Error: disk full. To read it again" in failed
        failed_json = ce._stub_text(json.dumps({"error": {"message": "quota exceeded"}, "detail": "d" * 3_000}), "c")
        assert "It reported an error: quota exceeded." in failed_json
        assert len(ce._stub_text("Error: " + "e" * 5_000, "c")) < 600

    def test_stubs_carry_refs_and_existing_stubs_are_not_stubbed_again(self):
        messages = history(6, output_chars=6_000)
        already = ce._stub_text("q" * 9_000, "c1_0") + " " + "pad " * 600
        messages[2] = tool("c1_0", already)
        output = run(make_filter(threshold=8_000), messages)

        stubs = {m["tool_call_id"]: m["content"] for m in output if m.get("role") == "tool" and m["content"].startswith(ce.STUB_PREFIX)}
        assert stubs["c1_0"] == already
        assert 'ref "c2_0"' in stubs["c2_0"]

    def test_note_steps_carry_refs_for_real_calls(self):
        notes = ce.extract_notes(
            [user("go"), assistant("Thinking.", calls=[("r1", "read_file", {"path": "a.py"})]), tool("r1", "Error: nope")]
        )
        assert "- read_file a.py (failed) [ref r1]" in notes
        assert "- assistant: Thinking.\n" in notes

    def test_note_steps_show_the_code_a_call_runs(self):
        code = "import json\n\nfor name in ['a.py', 'b.py']:\n    print(open(name).read()[:200])\n" + "#" * 300
        notes = ce.extract_notes(
            [
                user("go"),
                assistant(calls=[("x1", "execute_code", {"code": code}), ("q1", "search", {"query": "retry", "code": "x"})]),
                tool("x1", "file body"),
                tool("q1", "hits"),
            ]
        )
        step = next(line for line in notes.splitlines() if line.startswith("- execute_code "))
        assert step.startswith("- execute_code import json for name in ['a.py', 'b.py']: print(")
        assert step.endswith("… [ref x1]")
        assert "- search retry [ref q1]" in notes
        assert f"[{ce.COMMANDS}]" not in notes and f"[{ce.FILES}]" not in notes

    def test_old_tool_pictures_become_notes_but_user_pictures_and_recent_ones_stay(self):
        def pictures(text, count=1):
            return {
                "role": "user",
                "content": [{"type": "text", "text": text}]
                + [{"type": "image_url", "image_url": {"url": "data:image/png;base64,AAAA"}}] * count,
            }

        messages = [pictures("look at my photo")]
        for number in range(1, 9):
            messages += [
                user(f"request {number}"),
                assistant(calls=[(f"s{number}", "browser_screenshot", {"url": f"https://example.com/{number}"})]),
                tool(f"s{number}", "saved"),
                pictures(ce.SYNTHETIC_IMAGE_USER_PREFIX, 2),
                assistant(f"answer {number}"),
            ]
        flt = make_filter(threshold=8_000)
        output = run(flt, messages)

        assert output[1] == messages[0]
        notes = [m for m in output if isinstance(m["content"], str) and "pictures from the tool results above removed" in m["content"]]
        assert notes
        assert all(m["content"].startswith(ce.SYNTHETIC_IMAGE_USER_PREFIX) for m in notes)
        assert all(not ce._is_real_user(m) for m in notes)
        assert isinstance(output[-2]["content"], list)
        assert run(flt, messages) == output

        flt.valves.trim_tool_images = False
        assert not any(isinstance(m["content"], str) and "removed by Context" in m["content"] for m in run(flt, messages))

    def test_too_large_request_warns_even_with_nothing_to_compact(self, caplog):
        flt = ce.Filter()
        flt.valves = flt.Valves(show_status=False)
        events = []

        async def emit(event):
            events.append(event)

        def send(model, **valves):
            for name, value in valves.items():
                setattr(flt.valves, name, value)
            body = {"messages": [{"role": "system", "content": "s"}, user("paste " + "p" * 200_000)]}
            return asyncio.run(flt.request(body, __user__={"id": "u"}, __model__=model, __event_emitter__=emit))

        with logs(caplog, logging.WARNING):
            sent = send(provider_model(context=60_000))
        assert sent["messages"][1]["content"][:5] == "paste"
        assert len(events) == 1
        assert "Request too large for the model: ~50," in events[0]["data"]["description"]
        assert "leaves ~30,000 after the 30,000 kept for the reply" in events[0]["data"]["description"]

        events.clear()
        send({"id": "unknown"})
        assert events == []

        with logs(caplog, logging.WARNING), pytest.raises(ValueError):
            send(provider_model(context=60_000), stop_when_too_large=True)


def provider_model(model_id="qwen", context=135_680, max_output=None, params=None, base=None):
    listing = {"id": model_id, "context_length": context}
    if max_output:
        listing["top_provider"] = {"context_length": context, "max_completion_tokens": max_output}
    model = {"id": model_id, **listing, "openai": listing, "info": {"params": params or {}}}
    if base:
        model = {"id": model_id, "info": {"base_model_id": base, "params": params or {}}}
    return model


class TestBudget:
    def budget(self, model=None, body=None, request=None, **valves):
        flt = ce.Filter()
        flt.valves = flt.Valves(**valves)
        return flt.budget(body or {}, model, request)

    def test_provider_window_keeps_half_for_the_reply_when_no_output_cap_is_set(self):
        result = self.budget(provider_model())
        assert (result["window"], result["output"], result["threshold"]) == (135_680, 67_840, 54_272)

    def test_output_cap_comes_from_max_tokens_then_the_provider(self):
        assert self.budget(provider_model(params={"max_tokens": 32_000}))["threshold"] == 82_944
        assert self.budget(provider_model(max_output=16_000))["threshold"] == 95_744
        assert self.budget(provider_model(max_output=16_000), body={"max_tokens": 8_000})["threshold"] == 102_144

    def test_output_cap_of_half_the_window_or_more_is_ignored(self):
        assert self.budget(provider_model(params={"max_tokens": 100_000}))["output"] == 67_840
        assert self.budget(provider_model(max_output=16_000, params={"max_tokens": 100_000}))["output"] == 67_840

    def test_workspace_model_reads_the_base_models_provider_listing(self):
        request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(MODELS={"qwen": provider_model()})))
        assert self.budget(provider_model("coder", base="qwen"), request=request)["window"] == 135_680

    def test_explicit_windows_override_the_provider(self):
        model = provider_model("qwen3:8b")
        assert self.budget(model, model_context_windows="other=1, qwen3:8b=32_768")["window"] == 32_768
        assert self.budget(provider_model(params={"num_ctx": 40_000}))["window"] == 40_000

    def test_unknown_window_falls_back_to_the_default(self):
        result = self.budget({"id": "local"}, default_context_window=64_000)
        assert (result["window"], result["threshold"]) == (64_000, 25_600)

    def test_model_compaction_threshold_and_cap_set_the_threshold_directly(self):
        assert self.budget(provider_model(params={"compact_token_threshold": 70_000}))["threshold"] == 70_000
        assert self.budget(provider_model(), max_threshold_tokens=30_000)["threshold"] == 30_000

    def test_request_compacts_by_the_models_window(self):
        flt = ce.Filter()
        flt.valves = flt.Valves(show_status=True)
        messages = history(3, output_chars=6_000) + [user("long paste " + "p" * 60_000)]
        events = []

        async def emit(event):
            events.append(event)

        def send(context):
            body = {"messages": [{"role": "system", "content": "system prompt"}, *copy.deepcopy(messages)]}
            return asyncio.run(
                flt.request(
                    body,
                    __metadata__={"chat_id": f"c{context}", "message_id": "m"},
                    __user__={"id": "u"},
                    __model__=provider_model(context=context),
                    __event_emitter__=emit,
                )
            )["messages"]

        size = lambda sent: sum(len(ce._content_text(m.get("content"))) for m in sent)
        original = size([{"content": "system prompt"}, *messages])
        assert size(send(200_000)) == original
        assert size(send(40_000)) < original
        assert "threshold ~16,000" in events[-1]["data"]["description"]
