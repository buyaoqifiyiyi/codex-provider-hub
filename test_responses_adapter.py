"""Synthetic offline protocol tests; no provider configuration or network I/O."""
from copy import deepcopy
import json
import unittest
import warnings

from responses_adapter import (AdapterError, StreamError, UnsupportedSemantic,
                               UpstreamError, SemanticNotice, convert_stream, translate_request)


DISPLAY = "fixture-route/fixture-model"
TOOL = {"type": "function", "name": "read_file", "description": "Read a fixture",
        "parameters": {"type": "object", "properties": {"path": {"type": "string"}},
                       "required": ["path"], "additionalProperties": False}, "strict": True}


def codex_first_request():
    """Native field/type structure, reconstructed with synthetic values only.

    Source: codex-api/common.rs ResponsesApiRequest; tools/tool_spec.rs;
    tools/responses_api.rs FreeformTool; tool_search_spec.rs client parameters.
    No captured user request, credential or session identifier is used.
    """
    return {
        "client_metadata": {"x-codex-turn-metadata": "synthetic-metadata"},
        "include": ["reasoning.encrypted_content"],
        "input": [
            {"type": "message", "role": "developer", "content": [
                {"type": "input_text", "text": "Use synthetic fixtures only."}]},
            {"type": "message", "role": "user", "content": [
                {"type": "input_text", "text": "Inspect the fixture."}]},
        ],
        "model": DISPLAY, "parallel_tool_calls": True,
        "prompt_cache_key": "synthetic-cache-key", "reasoning": {"effort": "high", "summary": "auto"},
        "store": False, "stream": True, "text": {"verbosity": "low"}, "tool_choice": "auto",
        "tools": [
            {"type": "custom", "name": "apply_patch", "description": "Apply a synthetic patch",
             "format": {"type": "grammar", "syntax": "lark", "definition": 'start: "synthetic-patch"'}},
            deepcopy(TOOL),
            {"type": "tool_search", "execution": "client", "description": "Search deferred tools",
             "parameters": {"type": "object", "properties": {"query": {"type": "string"},
                             "limit": {"type": "number"}}, "required": ["query"], "additionalProperties": False}},
            {"type": "web_search", "external_web_access": True},
        ],
    }


def chunk(delta=None, finish=None, usage=None, choices=None):
    result = {"id": "chatcmpl_synthetic", "object": "chat.completion.chunk",
              "created": 123, "model": "upstream-model", "choices": choices if choices is not None else [
                  {"index": 0, "delta": delta or {}, "finish_reason": finish}]}
    if usage is not None:
        result["usage"] = usage
    return result


def lines(*chunks, done=True):
    for value in chunks:
        yield ("data: " + json.dumps(value, ensure_ascii=False)).encode("utf-8")
        yield b""
    if done:
        yield b"data: [DONE]"
        yield b""


def events(source):
    frames = list(convert_stream(source, DISPLAY))
    result = []
    for frame in frames:
        assert isinstance(frame, bytes) and frame.endswith(b"\n\n")
        tag, data, _ = frame.decode().split("\n", 2)
        payload = json.loads(data.removeprefix("data: "))
        assert tag == "event: " + payload["type"]
        result.append(payload)
    return result


class AdapterTestCase(unittest.TestCase):
    def setUp(self):
        manager = warnings.catch_warnings(record=True)
        self.notices = manager.__enter__()
        self.addCleanup(manager.__exit__, None, None, None)
        warnings.simplefilter("always", SemanticNotice)


class RequestTests(AdapterTestCase):
    def test_known_assistant_phases_preserve_text_and_disclose_the_lost_hint(self):
        for phase in ("commentary", "partial_answer", "final_answer"):
            with self.subTest(phase=phase), self.assertWarnsRegex(Warning, "assistant_phase_hint_unavailable"):
                result = translate_request({"input": [{"role": "user", "content": "before"},
                    {"type": "message", "role": "assistant", "phase": phase, "content": "answer"},
                    {"role": "user", "content": "after"}]}, "model")
            self.assertEqual([(m["role"], m["content"]) for m in result["messages"]],
                             [("user", "before"), ("assistant", "answer"), ("user", "after")])
            self.assertIn("assistant_phase_hint_unavailable", result.notices)

    def test_gpt_output_text_empty_logprobs_are_safe_to_replay(self):
        for value in (None, []):
            result = translate_request({"input": [{"role": "assistant", "content": [
                {"type": "output_text", "text": "previous reply", "annotations": [], "logprobs": value}]}]}, "model")
            self.assertEqual(result["messages"][0]["content"], "previous reply")
        with self.assertRaises(UnsupportedSemantic):
            translate_request({"input": [{"role": "assistant", "content": [
                {"type": "output_text", "text": "reply", "logprobs": [{"token": "x"}]}]}]}, "model")

    def test_messages_tools_and_parallel_round_trip(self):
        body = {"model": DISPLAY, "instructions": "Keep fixture data private", "stream": True,
                "store": False, "tools": [TOOL], "tool_choice": {"type": "function", "name": "read_file"},
                "parallel_tool_calls": True, "max_output_tokens": 256, "temperature": 0, "top_p": 1,
                "input": [{"role": "system", "content": "Use only synthetic files"},
                          {"role": "user", "content": [{"type": "input_text", "text": "Read two files"}]},
                          {"type": "reasoning", "summary": [], "content": [{"type": "reasoning_text", "text": "Check both"}], "encrypted_content": None},
                          {"type": "message", "role": "assistant", "content": [{"type": "output_text", "text": "Reading", "annotations": []}]},
                          {"type": "function_call", "name": "read_file", "call_id": "call_a", "arguments": '{"path":"a"}'},
                          {"type": "function_call", "name": "read_file", "call_id": "call_b", "arguments": '{"path":"b"}', "encrypted_function_args": []},
                          {"type": "function_call_output", "call_id": "call_b", "output": "B"},
                          {"type": "function_call_output", "call_id": "call_a", "output": [{"type": "input_text", "text": "A"}]},
                          {"role": "user", "content": "Continue"}]}
        original = deepcopy(body)
        result = translate_request(body, "fixture-model")
        self.assertEqual(body, original)
        self.assertEqual(result["model"], "fixture-model")
        self.assertEqual(result["max_tokens"], 256)
        self.assertEqual(result["tools"][0]["function"]["parameters"], TOOL["parameters"])
        self.assertEqual(result["tool_choice"]["function"]["name"], "read_file")
        self.assertEqual([m["role"] for m in result["messages"]], ["system", "user", "assistant", "tool", "tool", "user"])
        self.assertEqual(result["messages"][0]["content"], "Keep fixture data private\n\nUse only synthetic files")
        self.assertEqual(result["messages"][2]["reasoning_content"], "Check both")
        self.assertEqual([t["id"] for t in result["messages"][2]["tool_calls"]], ["call_a", "call_b"])
        result["tools"][0]["function"]["parameters"]["required"].append("other")
        self.assertEqual(body, original)

    def test_effort_requires_explicit_actual_upstream_confirmation(self):
        for effort in ("high", "max"):
            body = {"input": "fixture", "reasoning": {"effort": effort}}
            with self.assertRaisesRegex(UnsupportedSemantic, "reasoning_effort_not_confirmed"):
                translate_request(body, "fixture-model")
            self.assertEqual(translate_request(body, "fixture-model", confirmed_reasoning_efforts={effort})["reasoning_effort"], effort)
        with self.assertRaises(UnsupportedSemantic):
            translate_request({"input": "fixture", "reasoning": {"effort": "ultra"}}, "model", confirmed_reasoning_efforts={"max"})

    def test_reject_opaque_or_unsupported_semantics(self):
        inputs = [
            {"type": "compaction", "encrypted_content": "synthetic-opaque"},
            {"type": "compaction_trigger"},
            {"type": "reasoning", "summary": [], "encrypted_content": "synthetic-opaque"},
            {"type": "reasoning", "summary": [{"type": "summary_text", "text": "only summary"}]},
            {"type": "agent_message", "author": "/root", "recipient": "/root/worker", "content": []},
            {"role": "user", "content": [{"type": "input_image", "image_url": "synthetic"}]},
            {"role": "assistant", "phase": "unrecognized", "content": "fixture"},
            {"role": "user", "phase": "final_answer", "content": "fixture"},
            {"role": "assistant", "content": "fixture", "unexpected": "fixture"},
        ]
        for item in inputs:
            with self.subTest(item=item), self.assertRaises(UnsupportedSemantic):
                translate_request({"input": [item]}, "model")
        for extra in ({"previous_response_id": "resp_synthetic"}, {"store": True},
                      {"include": ["unknown.future.field"]}, {"stream": False},
                      {"truncation": "auto"}, {"tools": [{"type": "web_search"}]},
                      {"text": {"format": {"type": "json_schema"}}}):
            with self.subTest(extra=extra), self.assertRaises(UnsupportedSemantic):
                translate_request({"input": "fixture", **extra}, "model")

    def test_bad_tool_history_and_arguments_are_rejected(self):
        call = {"type": "function_call", "name": "read_file", "call_id": "call_a", "arguments": "{}"}
        output = {"type": "function_call_output", "call_id": "call_a", "output": "fixture"}
        cases = [[output], [call], [call, call, output], [call, output, output],
                 [call, {"role": "user", "content": "break adjacency"}, output],
                 [{**call, "arguments": "not-json"}, output], [{**call, "arguments": "[]"}, output],
                 [{**call, "namespace": "functions"}, {**output, "namespace": "other"}],
                 [{"type": "reasoning", "summary": [], "content": [{"type": "reasoning_text", "text": "orphan"}]}]]
        for history in cases:
            with self.subTest(history=history), self.assertRaises(AdapterError):
                translate_request({"input": history}, "model")


class CodexShapeTests(AdapterTestCase):
    def test_native_first_request_requires_explicit_non_equivalent_tool_policies(self):
        body = codex_first_request()
        original = deepcopy(body)
        capabilities = {"confirmed_reasoning_efforts": {"high"}}
        with self.assertRaisesRegex(UnsupportedSemantic, "custom_grammar_requires_explicit_downgrade"):
            translate_request(body, "fixture-model", **capabilities)
        with self.assertRaisesRegex(UnsupportedSemantic, "hosted_web_search_not_supported"):
            translate_request(body, "fixture-model", allow_custom_grammar_downgrade=True, **capabilities)
        translated = translate_request(body, "fixture-model", disabled_hosted_tools={"web_search"},
                                       allow_custom_grammar_downgrade=True, **capabilities)
        self.assertEqual(body, original)
        self.assertEqual(set(body), set("client_metadata include input model parallel_tool_calls prompt_cache_key reasoning store stream text tool_choice tools".split()))
        self.assertEqual(translated["messages"][0], {"role": "system", "content": "Use synthetic fixtures only."})
        self.assertEqual(len(translated["tools"]), 3)
        self.assertTrue(all(t["type"] == "function" for t in translated["tools"]))
        self.assertEqual(translated["reasoning_effort"], "high")
        self.assertIn("custom_grammar_not_enforced", translated.notices)
        self.assertIn("hosted_web_search_explicitly_disabled", translated.notices)
        self.assertIn("encrypted_reasoning_output_unavailable", translated.notices)
        self.assertIn("reasoning_summary_preference_unavailable", translated.notices)
        self.assertIn("verbosity_hint_unavailable", translated.notices)
        self.assertIn("developer_mapped_to_only_system_tier", translated.notices)
        self.assertEqual({str(n.message) for n in self.notices}, set(translated.notices))
        serialized = json.dumps(translated)
        self.assertNotIn("synthetic-metadata", serialized)
        self.assertNotIn("synthetic-cache-key", serialized)
        self.assertNotIn("notices", serialized)
        self.assertNotIn("include", translated)

    def test_basic_codex_fields_and_null_controls_need_no_tool_downgrade(self):
        body = codex_first_request()
        body.update(tools=[deepcopy(TOOL)], reasoning=None, text=None)
        translated = translate_request(body, "model")
        self.assertEqual(translated["tools"][0]["function"]["name"], "read_file")
        self.assertNotIn("reasoning_effort", translated)
        self.assertNotIn("custom_grammar_not_enforced", translated.notices)
        body["reasoning"] = {"effort": "max", "summary": "auto", "context": "auto"}
        with self.assertRaisesRegex(UnsupportedSemantic, "reasoning_effort_not_confirmed"):
            translate_request(body, "model")
        self.assertEqual(translate_request(body, "model", confirmed_reasoning_efforts={"max"})["reasoning_effort"], "max")

    def test_no_tool_native_request_has_explicit_preference_notices(self):
        body = codex_first_request()
        body.update(tools=[], reasoning={}, text={"format": {"type": "text"}, "verbosity": "medium"})
        result = translate_request(body, "model")
        self.assertNotIn("tools", result)
        self.assertNotIn("parallel_tool_calls", result)
        self.assertNotIn("tool_choice", result)
        self.assertIn("empty_tool_choice_has_no_effect", result.notices)
        self.assertIn("parallel_tool_calls_without_tools", result.notices)
        with self.assertRaisesRegex(UnsupportedSemantic, "structured_output_not_supported"):
            translate_request({**body, "text": {"format": {"type": "json_schema", "name": "fixture", "schema": {"type": "object"}, "strict": True}}}, "model")

    def test_existing_opaque_history_cannot_be_bypassed_by_preferences_or_tool_policies(self):
        body = codex_first_request()
        histories = [
            {"type": "reasoning", "summary": [], "encrypted_content": "synthetic-secret-state"},
            {"type": "message", "role": "assistant", "content": [], "encrypted_content": "synthetic-secret-state"},
            {"type": "function_call", "call_id": "call_a", "name": "read_file", "arguments": "{}",
             "encrypted_function_args": ["synthetic-secret-state"]},
            {"type": "compaction", "encrypted_content": "synthetic-secret-state"},
            {"type": "compaction_trigger"},
            {"type": "agent_message", "author": "/root", "recipient": "/root/worker",
             "content": [{"type": "encrypted_text", "encrypted_content": "synthetic-secret-state"}]},
            {"type": "web_search_call", "status": "completed", "action": {"type": "search", "query": "synthetic"}},
        ]
        for item in histories:
            with self.subTest(kind=item["type"]), self.assertRaises(UnsupportedSemantic) as error:
                translate_request({**body, "input": [*body["input"], item]}, "model",
                                  confirmed_reasoning_efforts={"high"}, disabled_hosted_tools={"web_search"},
                                  allow_custom_grammar_downgrade=True)
            self.assertNotIn("synthetic-secret-state", str(error.exception))

    def test_developer_only_tier_preserved_and_competing_system_rejected(self):
        body = {"input": [{"role": "developer", "content": "First instruction"},
                          {"role": "user", "content": "Task"},
                          {"role": "developer", "content": "Updated instruction"}]}
        result = translate_request(body, "model")
        self.assertEqual(result["messages"], [{"role": "system", "content": "First instruction\n\nUpdated instruction"},
                                              {"role": "user", "content": "Task"}])
        for extra in ({"instructions": "Higher priority"},
                      {"input": [{"role": "system", "content": "Higher priority"}, *body["input"]]}):
            with self.assertRaisesRegex(UnsupportedSemantic, "mixed_system_developer_priority"):
                translate_request({**body, **extra}, "model")

    def test_custom_tool_wrapper_restores_exact_input_without_shared_context(self):
        raw = '*** Begin Patch\n*** Add File: fixture.txt\n+你好 "quoted" \\ path\n*** End Patch\n'
        custom = {"type": "custom", "name": "apply_patch", "description": "Apply a patch", "format": {"type": "text"}}
        request = translate_request({"model": DISPLAY, "input": "synthetic", "tools": [custom],
                                     "tool_choice": {"type": "custom", "name": "apply_patch"}}, "model")
        alias = request["tools"][0]["function"]["name"]
        self.assertLessEqual(len(alias), 64)
        self.assertEqual(request["tool_choice"]["function"]["name"], alias)
        arguments = json.dumps({"input": raw}, ensure_ascii=True)
        chunks = [chunk({"tool_calls": [{"index": 0, "id": "call_patch", "function": {"name": alias, "arguments": arguments[:7]}}]})]
        chunks += [chunk({"tool_calls": [{"index": 0, "function": {"arguments": piece}}]}) for piece in arguments[7:]]
        output_events = events(lines(*chunks, chunk(finish="tool_calls")))
        item = output_events[-1]["response"]["output"][0]
        self.assertEqual(item["type"], "custom_tool_call")
        self.assertEqual(item["name"], "apply_patch")
        self.assertEqual(item["input"], raw)
        self.assertNotIn("arguments", item)
        kinds = [x["type"] for x in output_events]
        self.assertLess(kinds.index("response.output_item.added"), kinds.index("response.custom_tool_call_input.delta"))
        self.assertLess(kinds.index("response.custom_tool_call_input.done"), kinds.index("response.output_item.done"))
        self.assertEqual("".join(x["delta"] for x in output_events if x["type"] == "response.custom_tool_call_input.delta"), raw)
        replay = translate_request({"input": [{"role": "user", "content": "synthetic"}, item,
                                    {"type": "custom_tool_call_output", "call_id": "call_patch", "output": "applied synthetic fixture"}],
                                    "tools": [custom]}, "model")
        self.assertEqual(json.loads(replay["messages"][1]["tool_calls"][0]["function"]["arguments"]), {"input": raw})
        self.assertEqual(replay["messages"][1]["tool_calls"][0]["function"]["name"], alias)

    def test_client_tool_search_loads_namespaced_tools_then_roundtrips(self):
        search = codex_first_request()["tools"][2]
        request = translate_request({"input": "Find synthetic tools", "tools": [search], "tool_choice": {"type": "tool_search"}}, "model")
        alias = request["tools"][0]["function"]["name"]
        result = events(lines(chunk({"tool_calls": [{"index": 0, "id": "call_search", "function": {
            "name": alias, "arguments": '{"query":"read fixture","limit":2}'}}]}, "tool_calls")))
        item = result[-1]["response"]["output"][0]
        self.assertEqual(item["type"], "tool_search_call")
        self.assertEqual(item["execution"], "client")
        self.assertEqual(item["arguments"], {"query": "read fixture", "limit": 2})
        namespace = {"type": "namespace", "name": "functions", "description": "Fixture utilities", "tools": [deepcopy(TOOL)]}
        history = [{"role": "user", "content": "Find synthetic tools"}, item,
                   {"type": "tool_search_output", "call_id": "call_search", "execution": "client",
                    "status": "completed", "tools": [namespace]}]
        loaded = translate_request({"input": history, "tools": [search]}, "model")
        self.assertEqual(len(loaded["tools"]), 2)
        self.assertEqual(json.loads(loaded["messages"][-1]["content"]), [namespace])
        tool_alias = loaded["tools"][1]["function"]["name"]
        call = events(lines(chunk({"tool_calls": [{"index": 0, "id": "call_file", "function": {
            "name": tool_alias, "arguments": '{"path":"synthetic.txt"}'}}]}, "tool_calls")))[-1]["response"]["output"][0]
        self.assertEqual(call["namespace"], "functions")
        self.assertEqual(call["name"], "read_file")
        followup = translate_request({"input": [*history, call, {"type": "function_call_output",
                                         "call_id": "call_file", "name": "read_file", "namespace": "functions", "output": "fixture data"}],
                                      "tools": [search]}, "model")
        self.assertEqual(followup["messages"][-2]["tool_calls"][0]["function"]["name"], tool_alias)

    def test_additional_tools_carrier_and_deferred_metadata_are_explicit(self):
        tool = {**deepcopy(TOOL), "defer_loading": True}
        result = translate_request({"input": [{"type": "additional_tools", "role": "system", "tools": [tool]},
                                              {"role": "user", "content": "synthetic"}], "tools": [tool]}, "model")
        self.assertEqual(len(result["tools"]), 1)
        self.assertEqual(result["messages"], [{"role": "user", "content": "synthetic"}])
        self.assertIn("deferred_tool_exposed_eagerly", result.notices)
        with self.assertRaisesRegex(AdapterError, "conflicting_tool_definition"):
            translate_request({"input": "fixture", "tools": [TOOL, {**TOOL, "description": "Conflicting definition"}]}, "model")

    def test_native_nullable_tool_result_identity_and_wrong_kind(self):
        call = {"type": "custom_tool_call", "call_id": "call_exec", "name": "exec", "input": "synthetic code"}
        result = {"type": "custom_tool_call_output", "call_id": "call_exec", "name": None, "output": "fixture-result"}
        translated = translate_request({"input": [call, result]}, "model")
        self.assertEqual(translated["messages"][-1]["content"], "fixture-result")
        with self.assertRaisesRegex(AdapterError, "mismatched_tool_result_kind"):
            translate_request({"input": [call, {**result, "type": "function_call_output"}]}, "model")
        with self.assertRaisesRegex(AdapterError, "invalid_defer_loading"):
            translate_request({"input": "fixture", "tools": [{**TOOL, "defer_loading": 1}]}, "model")

    def test_custom_and_search_malformed_or_truncated_wrappers_never_execute(self):
        specs = [{"type": "custom", "name": "exec"}, codex_first_request()["tools"][2]]
        for spec in specs:
            alias = translate_request({"input": "fixture", "tools": [spec]}, "model")["tools"][0]["function"]["name"]
            invalid = ['{"input":', '{"input":"one","input":"two"}'] if spec["type"] == "custom" else ['{"query":']
            if spec["type"] == "custom":
                invalid += ['{"input":"value","unknown":"cannot discard"}', '{"input":{}}']
            for value in invalid:
                emitted = []
                with self.subTest(tool=spec["type"], arguments=value), self.assertRaises(AdapterError):
                    for frame in convert_stream(lines(chunk({"tool_calls": [{"index": 0, "id": "call_synthetic", "function": {
                        "name": alias, "arguments": value}}]}, "tool_calls")), DISPLAY):
                        emitted.append(frame)
                self.assertFalse(any(b"event: response.output_item.done\n" in f for f in emitted))
            with self.assertRaisesRegex(StreamError, "structured_tool_input_truncated"):
                events(lines(chunk({"tool_calls": [{"index": 0, "id": "call_synthetic", "function": {
                    "name": alias, "arguments": "{"}}]}, "length")))

    def test_unrepresentable_tool_semantics_and_aliases_fail_explicitly(self):
        bad_tools = [
            {"type": "tool_search", "execution": "server", "parameters": {"type": "object"}},
            {"type": "function", "name": "_cph_reserved", "parameters": {"type": "object"}},
            {"type": "function", "name": "x" * 65, "parameters": {"type": "object"}},
            {"type": "namespace", "name": "n" * 60, "tools": [TOOL]},
            {"type": "computer_use_preview"},
        ]
        for tool in bad_tools:
            with self.subTest(tool=tool), self.assertRaises(UnsupportedSemantic):
                translate_request({"input": "fixture", "tools": [tool]}, "model")
        with self.assertRaisesRegex(StreamError, "invalid_tool_alias"):
            events(lines(chunk({"tool_calls": [{"index": 0, "id": "call_synthetic", "function": {
                "name": "_cph_invalid", "arguments": "{}"}}]}, "tool_calls")))
        with self.assertRaisesRegex(UnsupportedSemantic, "tool_choice_not_available"):
            translate_request({"input": "fixture", "tools": [TOOL], "tool_choice": {"type": "custom", "name": "exec"}}, "model")
        with self.assertRaises(UnsupportedSemantic):
            translate_request({"input": "fixture", "reasoning": {"context": "current_turn"}}, "model")


class StreamTests(AdapterTestCase):
    def test_text_reasoning_usage_and_event_order(self):
        usage = {"prompt_tokens": 10, "completion_tokens": 4, "total_tokens": 14,
                 "prompt_tokens_details": {"cached_tokens": 2}, "completion_tokens_details": {"reasoning_tokens": 1}}
        result = events(lines(chunk({"role": "assistant", "reasoning_content": "想"}),
                              chunk({"reasoning_content": "一下"}), chunk({"content": "你"}),
                              chunk({"content": "好"}, "stop"), chunk(choices=[], usage=usage)))
        kinds = [e["type"] for e in result]
        self.assertEqual(kinds[:2], ["response.created", "response.in_progress"])
        self.assertEqual(kinds[-1], "response.completed")
        self.assertEqual([e["sequence_number"] for e in result], list(range(len(result))))
        final = result[-1]["response"]
        self.assertEqual(final["model"], DISPLAY)
        self.assertEqual(final["created_at"], 123)
        self.assertEqual(final["output"][0]["content"][0]["text"], "想一下")
        self.assertEqual(final["output"][1]["content"][0]["text"], "你好")
        self.assertEqual(final["usage"]["input_tokens_details"], {"cached_tokens": 2})
        self.assertEqual(result[0]["response"]["status"], "in_progress")
        self.assertEqual(result[0]["response"]["output"], [])
        replay = translate_request({"input": [{"role": "user", "content": "fixture"}, *final["output"]]}, "model")
        self.assertEqual(replay["messages"][-1]["reasoning_content"], "想一下")

    def test_parallel_interleaved_function_arguments_and_tool_roundtrip(self):
        def tool(index, **fields):
            return {"tool_calls": [{"index": index, **fields}]}
        result = events(lines(
            chunk({"reasoning_content": "Read fixtures"}),
            chunk(tool(0, id="call_a", type="function", function={"name": "read_", "arguments": ""})),
            chunk(tool(0, function={"name": "file", "arguments": '{"path":'})),
            chunk(tool(1, id="call_b", type="function", function={"name": "read_file", "arguments": '{"path":"b"}'})),
            chunk(tool(0, function={"arguments": '"a"}'})), chunk(finish="tool_calls")))
        output = result[-1]["response"]["output"]
        self.assertEqual([x["type"] for x in output], ["reasoning", "function_call", "function_call"])
        calls = output[1:]
        self.assertEqual([json.loads(x["arguments"]) for x in calls], [{"path": "a"}, {"path": "b"}])
        added = {}
        for e in result:
            if e["type"] == "response.output_item.added":
                added[e["item"]["id"]] = e["sequence_number"]
            if e["type"] == "response.function_call_arguments.delta":
                self.assertLess(added[e["item_id"]], e["sequence_number"])
        history = [{"role": "user", "content": "read"}, *output,
                   *[{"type": "function_call_output", "call_id": c["call_id"], "output": "fixture-result"} for c in reversed(calls)]]
        chat = translate_request({"input": history, "tools": [TOOL]}, "model")
        self.assertEqual(chat["messages"][1]["reasoning_content"], "Read fixtures")
        self.assertEqual(len(chat["messages"][1]["tool_calls"]), 2)
        followup = events(lines(chunk({"content": "Both read"}, "stop")))
        self.assertEqual(followup[-1]["response"]["output"][0]["content"][0]["text"], "Both read")

    def test_error_never_echoes_upstream_payload(self):
        secret = "synthetic-sensitive-error-body"
        for source in (lines({"error": {"message": secret, "code": "synthetic"}}),
                       ["event: error", "data: " + json.dumps({"message": secret}), ""]):
            with self.assertRaises(UpstreamError) as error:
                events(source)
            self.assertNotIn(secret, str(error.exception))

    def test_truncation_and_malformed_protocol_never_complete(self):
        sources = [lines(chunk({"content": "partial"}), done=False),
                   lines(chunk({"content": "answer"}, "stop"), done=False),
                   lines(chunk({"content": "partial"})),
                   ["data: not-json", ""], [b"data: \xff", b""],
                   lines(chunk({"refusal": "not-supported"}, "stop")),
                   lines(chunk({"content": "text"}, "stop"), chunk({"content": "late"})),
                   lines(chunk(choices=[{"index": 0, "delta": {}}, {"index": 1, "delta": {}}])),
                   lines(chunk({"tool_calls": [{"index": 0, "id": "call_a", "function": {"name": "read_file", "arguments": "{"}}]}, "tool_calls"))]
        for source in sources:
            emitted = []
            with self.subTest(source=type(source)), self.assertRaises(AdapterError):
                for frame in convert_stream(source, DISPLAY):
                    emitted.append(frame)
            self.assertFalse(any(b"event: response.completed\n" in frame for frame in emitted))

    def test_length_and_content_filter_are_incomplete(self):
        for reason in ("length", "content_filter"):
            result = events(lines(chunk({"content": "partial"}, reason)))
            self.assertEqual(result[-1]["type"], "response.incomplete")
            self.assertEqual(result[-1]["response"]["status"], "incomplete")
            self.assertNotIn("response.completed", [x["type"] for x in result])
        result = events(lines(chunk({"tool_calls": [{"index": 0, "id": "call_a", "function": {"name": "read_file", "arguments": "{"}}]}, "length")))
        self.assertNotIn("response.function_call_arguments.done", [x["type"] for x in result])
        self.assertFalse(any(x["type"] == "response.output_item.done" for x in result))

    def test_tool_identity_only_truncation_is_retained_without_executable_done(self):
        result = events(lines(chunk({"tool_calls": [{"index": 0, "id": "call_a",
                            "function": {"name": "read_file", "arguments": ""}}]}, "length")))
        output = result[-1]["response"]["output"]
        self.assertEqual(output[0]["call_id"], "call_a")
        self.assertEqual(output[0]["arguments"], "")
        self.assertEqual(output[0]["status"], "incomplete")
        self.assertNotIn("response.function_call_arguments.done", [x["type"] for x in result])
        self.assertNotIn("response.output_item.done", [x["type"] for x in result])
        with self.assertRaises(StreamError):
            events(lines(chunk({"tool_calls": [{"index": 0, "function": {"name": "read_file"}}]}, "length")))

    def test_nullable_optional_chat_fields(self):
        result = events(lines(chunk({"role": "assistant", "content": None,
                                     "reasoning_content": None, "tool_calls": None}),
                              chunk({"role": None, "content": "fixture", "tool_calls": None}, "stop")))
        self.assertEqual(result[-1]["response"]["output"][0]["content"][0]["text"], "fixture")
        result = events(lines(
            chunk({"tool_calls": [{"index": 0, "id": "call_a", "type": "function",
                                    "function": {"name": "read_file", "arguments": None}}]}),
            chunk({"tool_calls": [{"index": 0, "id": None, "type": None,
                                    "function": {"name": None, "arguments": "{}"}}]}, "tool_calls")))
        self.assertEqual(result[-1]["response"]["output"][0]["arguments"], "{}")

    def test_reasoning_only_is_not_a_completed_answer(self):
        with self.assertRaisesRegex(StreamError, "empty_response"):
            events(lines(chunk({"reasoning_content": "unfinished thought"}, "stop")))

    def test_multiline_sse_comments_crlf_and_physical_line_contract(self):
        data = json.dumps(chunk({"content": "fixture"}, "stop"), indent=2)
        source = [": keepalive\r\n", "id: transport-id\r\n", "retry: 1000\r\n"]
        source += ["data: " + line + "\r\n" for line in data.splitlines()]
        source += ["\r\n", "data: [DONE]\r\n"]
        self.assertEqual(events(source)[-1]["type"], "response.completed")
        with self.assertRaises(StreamError):
            events(["data: {}\n\ndata: [DONE]\n\n"])

    def test_transport_exception_is_explicit(self):
        def broken():
            yield from lines(chunk({"content": "partial"}), done=False)
            raise OSError("synthetic transport detail")
        with self.assertRaisesRegex(StreamError, "sse_transport_error"):
            events(broken())


if __name__ == "__main__":
    unittest.main()
