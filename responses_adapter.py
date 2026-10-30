"""Offline, strict Responses <-> Chat adapter; Python standard library only.

No file/network I/O, credentials, or globals containing provider configuration.
Documented semantic notices use Python warnings (normally written to stderr).
Independently implemented against the local Codex ResponseItem/SSE definitions.
This is a protocol subset, not a drop-in proxy or a compaction/decryption layer.

Request support: text messages, ordinary/custom/namespaced function tools,
client-executed tool_search and loaded tool declarations. Developer instructions
map to system only when no competing system tier exists. Same-tier instructions
are joined at the head for compatible Chat APIs. Mixed system/developer priority is rejected.
Custom inputs are wrapped in a JSON string property and restored exactly. Their
grammar constraint cannot be implemented by Chat: reject by default; explicit
allow_custom_grammar_downgrade=True keeps the definition in the description and
reports that generation is unconstrained. Native hosted web_search has no Chat
executor: reject unless the caller explicitly lists it in disabled_hosted_tools.
Disabling a declaration NEVER disables checking its history for opaque state.

Non-content preferences/metadata have documented, fixed-code notices: include
reasoning.encrypted_content requests future output, which is unavailable here;
client_metadata and prompt_cache_key are not sent to a different provider;
verbosity and automatic reasoning-summary preferences cannot be reproduced.
Notices appear as SemanticNotice warnings and TranslatedRequest.notices, never
as extra Chat payload fields. Assistant phase hints are unavailable in Chat,
but their text and order are preserved with a notice. Media, existing encrypted
state, summaries/compaction and server-side conversation state are rejected.
History ids remain correlation metadata only.
High/max are NEVER assumed supported: the caller must explicitly confirm them
for its actual upstream using confirmed_reasoning_efforts. No thinking toggle or
other vendor option is inferred from a model name.

convert_stream accepts physical SSE lines (str or UTF-8 bytes, e.g. iter_lines),
NOT arbitrary network byte chunks. Comments and SSE id/retry transport metadata
are ignored. A finish_reason AND [DONE] are required. Exceptions have fixed
messages and never echo upstream payloads. Consumers must handle AdapterError
even after receiving deltas, and must not treat a delta as a completed response.
Reasoning is emitted as a plaintext Responses reasoning item, not as ciphertext;
only round-tripping through an adapter that preserves that content is supported.
Mapped tools use stateless, reserved _cph_ aliases (maximum 64 characters), so the
existing two-argument convert_stream API needs no shared mutable request state.
Custom/tool-search argument JSON is buffered and validated before input/done
events; ordinary function arguments still stream incrementally. This is NOT a
grammar validator, hosted search engine, tool executor or full Codex compatibility
claim. Unknown aliases and truncated custom/search wrappers explicitly fail.

Design reference (independent implementation, no source copied): CC Switch's
transform_codex_chat.rs, MIT, Copyright (c) 2025 Jason Young. Wire shapes checked
against local Codex codex-api/common.rs, protocol/models.rs and tools/tool_spec.rs.
"""

import base64
import binascii
from copy import deepcopy
import json
import re
import warnings
from uuid import uuid4


class AdapterError(ValueError):
    """A fixed, payload-free protocol error; code is suitable for callers."""

    def __init__(self, code):
        self.code = code
        super().__init__(code)


class UnsupportedSemantic(AdapterError):
    pass


class StreamError(AdapterError):
    pass


class UpstreamError(StreamError):
    pass


class SemanticNotice(UserWarning):
    """A fixed-code disclosure of a non-equivalent preference or explicit opt-in."""


class TranslatedRequest(dict):
    """JSON-serializable Chat payload; notices are out-of-band, not API fields."""

    def __init__(self, payload, notices):
        super().__init__(payload)
        self.notices = tuple(dict.fromkeys(notices))


def _keys(value, allowed, *, unsupported="unsupported_field"):
    if not isinstance(value, dict):
        raise AdapterError("expected_object")
    if value.keys() - set(allowed):
        raise UnsupportedSemantic(unsupported)


def _string(value, *, empty=False):
    if not isinstance(value, str) or (not empty and not value):
        raise AdapterError("expected_string")
    return value


def _json(value):
    def invalid_constant(_):
        raise ValueError()

    def unique_object(pairs):
        result = {}
        for key, child in pairs:
            if key in result:
                raise ValueError()
            result[key] = child
        return result

    try:
        return json.loads(value, parse_constant=invalid_constant, object_pairs_hook=unique_object)
    except (ValueError, TypeError):
        raise AdapterError("invalid_json") from None


def _arguments(value):
    _string(value, empty=True)
    if not isinstance(_json(value), dict):
        raise AdapterError("function_arguments_must_be_object")
    return value


def _plain_state(value):
    if isinstance(value, dict):
        for key, child in value.items():
            if key in {"encrypted_content", "encrypted_function_args"}:
                if child not in (None, [], ""):
                    raise UnsupportedSemantic("encrypted_state")
            _plain_state(child)
    elif isinstance(value, list):
        for child in value:
            _plain_state(child)


def _text(content, kinds=("input_text", "output_text", "text")):
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        raise AdapterError("expected_text_content")
    parts = []
    for part in content:
        _keys(part, {"type", "text", "annotations", "logprobs"})
        if (part.get("type") not in kinds or part.get("annotations") not in (None, [])
                or part.get("logprobs") not in (None, [])):
            raise UnsupportedSemantic("unsupported_content")
        parts.append(_string(part.get("text"), empty=True))
    return "".join(parts)


def _dump(value):
    try:
        return json.dumps(value, ensure_ascii=False, allow_nan=False, separators=(",", ":"), sort_keys=True)
    except (TypeError, ValueError):
        raise AdapterError("invalid_json_value") from None


def _chat_name(kind, name, namespace=None):
    _string(name)
    if namespace is not None:
        _string(namespace)
    if name.startswith("_cph_"):
        raise UnsupportedSemantic("reserved_tool_alias")
    if kind == "function" and namespace is None:
        alias = name
    else:
        code = {"function": "f", "custom": "c", "tool_search": "s"}[kind]
        alias = "_cph_" + base64.urlsafe_b64encode(_dump([code, name, namespace]).encode()).decode().rstrip("=")
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", alias):
        raise UnsupportedSemantic("tool_name_not_representable")
    return alias


def _decode_name(alias):
    if not alias.startswith("_cph_"):
        _chat_name("function", alias)
        return "function", alias, None
    try:
        raw = alias[5:]
        decoded = _json(base64.b64decode(raw + "=" * (-len(raw) % 4), altchars=b"-_", validate=True).decode())
        if not isinstance(decoded, list) or len(decoded) != 3:
            raise ValueError()
        code, name, namespace = decoded
        kind = {"f": "function", "c": "custom", "s": "tool_search"}[code]
        if kind == "function" and namespace is None:
            raise ValueError()
        if kind == "tool_search" and (name != "tool_search" or namespace is not None):
            raise ValueError()
        if _chat_name(kind, name, namespace) != alias:
            raise ValueError()
        return kind, name, namespace
    except (AdapterError, ValueError, KeyError, TypeError, UnicodeError, binascii.Error):
        raise StreamError("invalid_tool_alias") from None


def _tool_definitions(tools, notices, disabled, allow_grammar):
    """Preserve executable identity; never guess a hosted implementation."""
    definitions = {}

    def visit(tool, namespace=None, namespace_description=""):
        if not isinstance(tool, dict):
            raise AdapterError("expected_tool_object")
        kind = tool.get("type")
        if kind in {"web_search", "web_search_preview"}:
            if kind not in disabled:
                raise UnsupportedSemantic("hosted_web_search_not_supported")
            notices.append("hosted_web_search_explicitly_disabled")
            return
        if kind == "namespace":
            _keys(tool, {"type", "name", "description", "tools"})
            if namespace is not None:
                raise UnsupportedSemantic("nested_tool_namespace")
            children = tool.get("tools")
            if not isinstance(children, list):
                raise AdapterError("expected_namespace_tools")
            description = tool.get("description", "")
            _string(description, empty=True)
            for child in children:
                visit(child, _string(tool.get("name")), description)
            return
        if kind not in {"function", "custom", "tool_search"}:
            raise UnsupportedSemantic("unsupported_tool_type")
        allowed = {"type", "name", "description", "defer_loading"}
        allowed |= {"format"} if kind == "custom" else {"parameters", "strict"}
        if kind == "tool_search":
            allowed |= {"execution"}
        _keys(tool, allowed)
        if tool.get("defer_loading") is not None and type(tool["defer_loading"]) is not bool:
            raise AdapterError("invalid_defer_loading")
        if tool.get("defer_loading") is True:
            notices.append("deferred_tool_exposed_eagerly")
        if kind == "tool_search" and (namespace is not None or tool.get("execution") != "client"):
            raise UnsupportedSemantic("server_tool_search_not_supported")
        name = "tool_search" if kind == "tool_search" else _string(tool.get("name"))
        if kind == "tool_search" and tool.get("name", name) != name:
            raise UnsupportedSemantic("invalid_tool_search_name")
        alias = _chat_name(kind, name, namespace)
        description = tool.get("description", "")
        _string(description, empty=True)
        function = {"name": alias, "description": description}
        if namespace_description:
            function["description"] = namespace_description + "\n\n" + description
        if kind == "custom":
            fmt = tool.get("format")
            if fmt is not None:
                if not isinstance(fmt, dict):
                    raise AdapterError("invalid_custom_format")
                if fmt.get("type") == "text":
                    _keys(fmt, {"type"})
                elif fmt.get("type") == "grammar":
                    _keys(fmt, {"type", "syntax", "definition"})
                    if fmt.get("syntax") not in {"lark", "regex"}:
                        raise UnsupportedSemantic("unsupported_custom_grammar_syntax")
                    _string(fmt.get("definition"))
                    if not allow_grammar:
                        raise UnsupportedSemantic("custom_grammar_requires_explicit_downgrade")
                    notices.append("custom_grammar_not_enforced")
                else:
                    raise UnsupportedSemantic("unsupported_custom_format")
            function["description"] += "\nReturn the original raw tool input exactly in the input string. Definition: " + _dump(tool)
            function["parameters"] = {"type": "object", "properties": {"input": {"type": "string"}},
                                      "required": ["input"], "additionalProperties": False}
        else:
            params = tool.get("parameters")
            if params is None:
                if kind == "tool_search":
                    raise AdapterError("tool_search_parameters_required")
                params = {"type": "object", "properties": {}}
            if not isinstance(params, dict) or params.get("type", "object") != "object":
                raise UnsupportedSemantic("non_object_function_parameters")
            function["parameters"] = deepcopy(params)
            if "strict" in tool:
                if type(tool["strict"]) is not bool:
                    raise AdapterError("invalid_tool_definition")
                function["strict"] = tool["strict"]
        translated = {"type": "function", "function": function}
        if alias in definitions and definitions[alias] != translated:
            raise AdapterError("conflicting_tool_definition")
        definitions[alias] = translated

    for tool in tools:
        visit(tool)
    return definitions


def translate_request(body: dict, upstream_model: str, *,
                      confirmed_reasoning_efforts=(), disabled_hosted_tools=(),
                      allow_custom_grammar_downgrade=False) -> dict:
    """Translate without mutating body; capability confirmation is caller-owned.

    confirmed_reasoning_efforts is an explicit iterable containing high/max only,
    after external verification of this particular gateway, model and account.
    Its default is empty. This function performs no capability discovery.
    disabled_hosted_tools may explicitly disable web_search/web_search_preview
    declarations only. Unmappable existing history is always rejected. Custom
    grammar downgrade is a separate, default-off opt-in, never a compatibility
    claim. See module documentation and returned .notices for preference limits.
    """
    _keys(body, {"model", "input", "instructions", "tools", "tool_choice",
                 "parallel_tool_calls", "max_output_tokens", "temperature", "top_p",
                 "stream", "store", "reasoning", "include", "truncation",
                 "client_metadata", "prompt_cache_key", "text"})
    if body.get("stream", True) is not True or body.get("store", False) is not False:
        raise UnsupportedSemantic("requires_stateless_streaming")
    notices = []
    include = body.get("include")
    if include is not None:
        if not isinstance(include, list) or any(x != "reasoning.encrypted_content" for x in include):
            raise UnsupportedSemantic("unsupported_include")
        if include:
            notices.append("encrypted_reasoning_output_unavailable")
    if body.get("truncation", "disabled") != "disabled":
        raise UnsupportedSemantic("unsupported_response_option")
    for key in ("client_metadata", "prompt_cache_key"):
        value = body.get(key)
        if value is not None:
            if key == "client_metadata":
                if not isinstance(value, dict) or any(not isinstance(k, str) or not isinstance(v, str) for k, v in value.items()):
                    raise AdapterError("invalid_client_metadata")
            else:
                _string(value)
            notices.append(key + "_not_forwarded")
    text = body.get("text")
    if text is not None:
        _keys(text, {"verbosity", "format"})
        if text.get("verbosity") is not None:
            if text["verbosity"] not in {"low", "medium", "high"}:
                raise AdapterError("invalid_text_verbosity")
            notices.append("verbosity_hint_unavailable")
        if text.get("format") is not None:
            if not isinstance(text["format"], dict) or text["format"].get("type") != "text":
                raise UnsupportedSemantic("structured_output_not_supported")
            _keys(text["format"], {"type"})
    if isinstance(disabled_hosted_tools, str):
        raise AdapterError("invalid_disabled_hosted_tools")
    disabled = set(disabled_hosted_tools)
    if disabled - {"web_search", "web_search_preview"} or type(allow_custom_grammar_downgrade) is not bool:
        raise AdapterError("invalid_tool_policy")
    if isinstance(confirmed_reasoning_efforts, str):
        raise AdapterError("invalid_reasoning_capabilities")
    confirmed = set(confirmed_reasoning_efforts)
    if confirmed - {"high", "max"}:
        raise AdapterError("invalid_reasoning_capabilities")
    result = {"model": _string(upstream_model), "messages": [], "stream": True,
              "stream_options": {"include_usage": True}}
    if body.get("reasoning") is not None:
        reasoning = body["reasoning"]
        _keys(reasoning, {"effort", "summary", "context"})
        effort = reasoning.get("effort")
        if effort is not None and (effort not in {"high", "max"} or effort not in confirmed):
            raise UnsupportedSemantic("reasoning_effort_not_confirmed")
        if effort is not None:
            result["reasoning_effort"] = effort
        if reasoning.get("summary") not in (None, "auto", "none"):
            raise UnsupportedSemantic("reasoning_summary_mode_not_supported")
        if reasoning.get("context") not in (None, "auto"):
            raise UnsupportedSemantic("reasoning_context_not_supported")
        if reasoning.get("summary") is not None:
            notices.append("reasoning_summary_preference_unavailable")
        if reasoning.get("context") is not None:
            notices.append("reasoning_context_auto_only")
    for key in ("parallel_tool_calls", "temperature", "top_p", "max_output_tokens"):
        if key in body:
            value = body[key]
            if key == "parallel_tool_calls":
                if type(value) is not bool:
                    raise AdapterError("invalid_parallel_tool_calls")
            elif key == "max_output_tokens":
                if type(value) is not int or value <= 0:
                    raise AdapterError("invalid_token_limit")
            elif type(value) not in (int, float) or not (0 <= value <= (2 if key == "temperature" else 1)):
                raise AdapterError("invalid_sampling_option")
            result["max_tokens" if key == "max_output_tokens" else key] = value

    items = body.get("input")
    if isinstance(items, str):
        items = [{"role": "user", "content": items}]
    if not isinstance(items, list) or not items:
        raise AdapterError("expected_nonempty_input")
    _plain_state(items)
    for item in items:
        if not isinstance(item, dict):
            raise AdapterError("expected_input_item")
        if item.get("type") in {"compaction", "compaction_summary", "context_compaction", "compaction_trigger"}:
            raise UnsupportedSemantic("compaction_not_supported")
    declared_tools = body.get("tools", [])
    if not isinstance(declared_tools, list):
        raise AdapterError("expected_tools_array")
    declared_tools = list(declared_tools)
    for item in items:
        if item.get("type") in {"additional_tools", "tool_search_output"}:
            loaded = item.get("tools")
            if not isinstance(loaded, list):
                raise AdapterError("expected_loaded_tools_array")
            declared_tools.extend(loaded)
    definitions = _tool_definitions(declared_tools, notices, disabled, allow_custom_grammar_downgrade)
    if definitions:
        result["tools"] = list(definitions.values())
    if "tool_choice" in body:
        choice = body["tool_choice"]
        if isinstance(choice, dict):
            _keys(choice, {"type", "name", "namespace"})
            kind = choice.get("type")
            if kind not in {"function", "custom", "tool_search"}:
                raise UnsupportedSemantic("unsupported_tool_choice")
            name = "tool_search" if kind == "tool_search" else choice.get("name")
            alias = _chat_name(kind, name, choice.get("namespace"))
            if alias not in definitions:
                raise UnsupportedSemantic("tool_choice_not_available")
            choice = {"type": "function", "function": {"name": alias}}
        elif not isinstance(choice, str) or choice not in {"auto", "none", "required"}:
            raise UnsupportedSemantic("unsupported_tool_choice")
        if not definitions and choice == "required":
            raise AdapterError("tool_choice_without_tools")
        if definitions:
            result["tool_choice"] = choice
        else:
            notices.append("empty_tool_choice_has_no_effect")
    if not definitions and "parallel_tool_calls" in result:
        result.pop("parallel_tool_calls")
        notices.append("parallel_tool_calls_without_tools")

    messages = result["messages"]
    if "instructions" in body and body["instructions"] is not None:
        messages.append({"role": "system", "content": _string(body["instructions"], empty=True)})
    has_system = bool(body.get("instructions")) or any(i.get("role") == "system" for i in items if i.get("type", "message") == "message")
    has_developer = any(i.get("role") == "developer" for i in items if i.get("type", "message") == "message")
    if has_system and has_developer:
        raise UnsupportedSemantic("mixed_system_developer_priority")
    if has_developer:
        notices.append("developer_mapped_to_only_system_tier")
    pending_reasoning = ""
    pending_calls = {}
    seen_calls = set()
    results_started = False

    def assistant(message):
        nonlocal pending_reasoning
        if pending_reasoning:
            if message.get("reasoning_content"):
                raise UnsupportedSemantic("ambiguous_reasoning_history")
            message["reasoning_content"] = pending_reasoning
            pending_reasoning = ""
        return message

    for item in items:
        if not isinstance(item, dict):
            raise AdapterError("expected_input_item")
        kind = item.get("type", "message")
        if kind in {"compaction", "compaction_summary", "context_compaction", "compaction_trigger"}:
            raise UnsupportedSemantic("compaction_not_supported")
        _plain_state(item)
        common = {"type", "id", "status", "encrypted_content", "encrypted_function_args"}
        if item.get("status") not in (None, "completed"):
            raise UnsupportedSemantic("unfinished_history_item")
        if kind == "reasoning":
            _keys(item, common | {"summary", "content"})
            if item.get("summary") not in (None, []):
                raise UnsupportedSemantic("reasoning_summary_not_full_content")
            if pending_calls:
                raise AdapterError("reasoning_inside_pending_tool_results")
            pending_reasoning += _text(item.get("content"), ("reasoning_text", "text"))
        elif kind == "message":
            _keys(item, common | {"role", "content", "reasoning_content", "phase"})
            role = item.get("role")
            if role not in {"system", "developer", "user", "assistant"}:
                raise UnsupportedSemantic("unsupported_message_semantics")
            phase = item.get("phase")
            if phase is not None:
                if role != "assistant" or phase not in {"commentary", "partial_answer", "final_answer"}:
                    raise UnsupportedSemantic("unsupported_message_semantics")
                notices.append("assistant_phase_hint_unavailable")
            if pending_calls:
                raise AdapterError("missing_tool_results")
            message = {"role": "system" if role == "developer" else role, "content": _text(item.get("content"))}
            if "reasoning_content" in item:
                if role != "assistant":
                    raise UnsupportedSemantic("reasoning_requires_assistant")
                message["reasoning_content"] = _string(item["reasoning_content"], empty=True)
            if role == "assistant":
                assistant(message)
            elif pending_reasoning:
                raise AdapterError("orphan_reasoning")
            messages.append(message)
        elif kind in {"function_call", "custom_tool_call", "tool_search_call"}:
            allowed = {"call_id", "name", "namespace", "arguments"}
            if kind == "custom_tool_call":
                allowed = {"call_id", "name", "namespace", "input"}
            elif kind == "tool_search_call":
                allowed = {"call_id", "execution", "arguments"}
            _keys(item, common | allowed)
            tool_kind = {"function_call": "function", "custom_tool_call": "custom", "tool_search_call": "tool_search"}[kind]
            if kind == "tool_search_call" and item.get("execution") != "client":
                raise UnsupportedSemantic("server_tool_search_not_supported")
            name = "tool_search" if kind == "tool_search_call" else _string(item.get("name"))
            namespace = item.get("namespace")
            alias = _chat_name(tool_kind, name, namespace)
            if kind == "custom_tool_call":
                arguments = _dump({"input": _string(item.get("input"), empty=True)})
            elif kind == "tool_search_call":
                if not isinstance(item.get("arguments"), dict):
                    raise AdapterError("tool_search_arguments_must_be_object")
                arguments = _dump(item["arguments"])
            else:
                arguments = _arguments(item.get("arguments"))
            if pending_calls and results_started:
                raise AdapterError("interleaved_tool_batches")
            call_id = _string(item.get("call_id"))
            if call_id in seen_calls:
                raise AdapterError("duplicate_call_id")
            seen_calls.add(call_id)
            pending_calls[call_id] = (kind, name, namespace)
            results_started = False
            call = {"id": call_id, "type": "function", "function": {
                "name": alias, "arguments": arguments}}
            if not messages or messages[-1]["role"] != "assistant":
                messages.append(assistant({"role": "assistant", "content": None}))
            elif pending_reasoning:
                # A distinct reasoning item marks a new assistant segment.
                messages.append(assistant({"role": "assistant", "content": None}))
            messages[-1].setdefault("tool_calls", []).append(call)
        elif kind in {"function_call_output", "custom_tool_call_output", "tool_search_output"}:
            allowed = {"call_id", "output", "name", "namespace"}
            if kind == "tool_search_output":
                allowed = {"call_id", "execution", "tools"}
            _keys(item, common | allowed)
            call_id = _string(item.get("call_id"))
            if call_id not in pending_calls:
                raise AdapterError("unmatched_tool_result")
            original_kind, name, namespace = pending_calls.pop(call_id)
            if kind != original_kind.replace("_call", "_call_output") and not (original_kind == "tool_search_call" and kind == "tool_search_output"):
                raise AdapterError("mismatched_tool_result_kind")
            if ((item.get("name") is not None and item["name"] != name)
                    or (item.get("namespace") is not None and item["namespace"] != namespace)):
                raise AdapterError("mismatched_tool_result_identity")
            if kind == "tool_search_output":
                if item.get("execution") != "client":
                    raise UnsupportedSemantic("server_tool_search_not_supported")
                output = _dump(item["tools"])
            else:
                output = _text(item.get("output"))
            results_started = True
            messages.append({"role": "tool", "tool_call_id": call_id, "content": output})
        elif kind == "additional_tools":
            _keys(item, {"type", "id", "role", "tools"})
            if item.get("role") not in {"system", "developer"}:
                raise UnsupportedSemantic("unsupported_additional_tools_role")
        else:
            raise UnsupportedSemantic("unsupported_input_item")
    if pending_calls or pending_reasoning:
        raise AdapterError("unfinished_history")
    system_messages = [m for m in messages if m["role"] == "system"]
    if system_messages:
        if len(system_messages) > 1 or messages[0]["role"] != "system":
            notices.append("same_tier_instructions_joined_at_head")
        result["messages"] = [{"role": "system", "content": "\n\n".join(m["content"] for m in system_messages)},
                              *[m for m in messages if m["role"] != "system"]]
    translated = TranslatedRequest(result, notices)
    for notice in translated.notices:
        warnings.warn(notice, SemanticNotice, stacklevel=2)
    return translated


def _sse_payloads(lines):
    data = []
    event = "message"
    try:
        for raw in lines:
            if isinstance(raw, bytes):
                raw = raw.decode("utf-8")
            if not isinstance(raw, str):
                raise StreamError("expected_sse_line")
            line = raw.rstrip("\r\n")
            if "\n" in line or "\r" in line:
                raise StreamError("expected_physical_lines_not_chunks")
            if not line:
                if data:
                    yield event, "\n".join(data)
                data, event = [], "message"
            elif line.startswith(":"):
                continue
            else:
                field, _, value = line.partition(":")
                value = value[1:] if value.startswith(" ") else value
                if field == "data":
                    data.append(value)
                elif field == "event":
                    event = value
                elif field not in {"id", "retry"}:
                    raise UnsupportedSemantic("unsupported_sse_field")
        if data:
            yield event, "\n".join(data)
    except (UnicodeError, OSError):
        raise StreamError("sse_transport_error") from None


def _usage(value):
    _keys(value, {"prompt_tokens", "completion_tokens", "total_tokens",
                  "prompt_tokens_details", "completion_tokens_details",
                  "prompt_cache_hit_tokens", "prompt_cache_miss_tokens"},
          unsupported="unsupported_usage_field")
    for key in ("prompt_tokens", "completion_tokens"):
        if type(value.get(key)) is not int or value[key] < 0:
            raise StreamError("invalid_usage")
    total = value.get("total_tokens", value["prompt_tokens"] + value["completion_tokens"])
    if type(total) is not int or total != value["prompt_tokens"] + value["completion_tokens"]:
        raise StreamError("invalid_usage")
    result = {"input_tokens": value["prompt_tokens"], "output_tokens": value["completion_tokens"],
              "total_tokens": total}
    # These fields are a breakdown of prompt_tokens; they are
    # statistical metadata, not additional tokens.
    cache_hit = value.get("prompt_cache_hit_tokens")
    cache_miss = value.get("prompt_cache_miss_tokens")
    for count in (cache_hit, cache_miss):
        if count is not None and (type(count) is not int or not 0 <= count <= value["prompt_tokens"]):
            raise StreamError("invalid_usage")
    if cache_hit is not None and cache_miss is not None and cache_hit + cache_miss != value["prompt_tokens"]:
        raise StreamError("invalid_usage")
    for source, target, key in (("prompt_tokens_details", "input_tokens_details", "cached_tokens"),
                                ("completion_tokens_details", "output_tokens_details", "reasoning_tokens")):
        detail = value.get(source)
        if detail is not None:
            _keys(detail, {key}, unsupported="unsupported_usage_detail_field")
            if key in detail:
                if type(detail[key]) is not int or detail[key] < 0:
                    raise StreamError("invalid_usage")
                result[target] = {key: detail[key]}
    if cache_hit is not None:
        cached = result.get("input_tokens_details", {}).get("cached_tokens")
        if cached is not None and cached != cache_hit:
            raise StreamError("invalid_usage")
        result["input_tokens_details"] = {"cached_tokens": cache_hit}
    return result


def _new_tool_item(call_id, chat_name):
    kind, name, namespace = _decode_name(chat_name)
    item = {"id": {"function": "fc_", "custom": "ctc_", "tool_search": "tsc_"}[kind] + uuid4().hex,
            "type": {"function": "function_call", "custom": "custom_tool_call", "tool_search": "tool_search_call"}[kind],
            "call_id": call_id, "status": "in_progress"}
    if kind == "tool_search":
        item.update(execution="client", arguments={})
    else:
        item["name"] = name
        if namespace is not None:
            item["namespace"] = namespace
        item["arguments" if kind == "function" else "input"] = ""
    return item


def _finish_tool_arguments(state, incomplete):
    item = state["item"]
    if item["type"] == "function_call":
        if not incomplete:
            _arguments(state["arguments"])
        item["arguments"] = state["arguments"]
        return
    try:
        parsed = _json(_arguments(state["arguments"]))
    except AdapterError:
        raise StreamError("structured_tool_input_truncated" if incomplete else "invalid_structured_tool_arguments") from None
    if item["type"] == "custom_tool_call":
        if set(parsed) != {"input"} or not isinstance(parsed["input"], str):
            raise StreamError("invalid_custom_input_wrapper")
        item["input"] = parsed["input"]
    else:
        item["arguments"] = parsed


def convert_stream(lines, requested_model: str, *, allow_probe_extensions=False):
    """Yield complete UTF-8 SSE frames, retaining the caller's model display id.

    One choice only. Plaintext reasoning, text and indexed function argument
    deltas are supported, including interleaved parallel calls. Reserved aliases
    reconstruct custom/search/namespaced calls without shared state. Custom and
    search arguments are buffered until valid complete JSON is available. Upstream errors,
    malformed JSON, unknown semantics and premature EOF raise AdapterError.
    The probe-only option accepts a provider's matched stop token/string as
    auxiliary metadata; normal conversation conversion remains strict.
    length/content_filter finish reasons produce response.incomplete, never
    response.completed. Partial function calls do not get a done event.
    """
    _string(requested_model)
    response = {"id": "resp_" + uuid4().hex, "object": "response", "created_at": 0,
                "model": requested_model, "status": "in_progress", "output": [], "usage": None}
    sequence = 0
    items = []
    text_item = None
    reasoning_item = None
    calls = {}
    finish = None
    started = False

    def event(kind, **fields):
        nonlocal sequence
        payload = {"type": kind, "sequence_number": sequence, **fields}
        sequence += 1
        return ("event: " + kind + "\ndata: " + json.dumps(payload, ensure_ascii=False,
                allow_nan=False, separators=(",", ":")) + "\n\n").encode("utf-8")

    def add(item):
        items.append(item)
        return event("response.output_item.added", output_index=len(items) - 1, item=item)

    def position(item):
        return {"item_id": item["id"], "output_index": items.index(item)}

    for sse_event, data in _sse_payloads(lines):
        if sse_event == "error":
            raise UpstreamError("upstream_error")
        if sse_event != "message":
            raise UnsupportedSemantic("unsupported_upstream_event")
        if data == "[DONE]":
            if finish is None:
                raise StreamError("done_without_finish_reason")
            break
        chunk = _json(data)
        if not isinstance(chunk, dict):
            raise StreamError("invalid_chat_chunk")
        if "error" in chunk:
            raise UpstreamError("upstream_error")
        _keys(chunk, {"id", "object", "created", "model", "choices", "usage", "system_fingerprint", "service_tier"},
              unsupported="unsupported_response_chunk_field")
        if chunk.get("object", "chat.completion.chunk") != "chat.completion.chunk":
            raise UnsupportedSemantic("unsupported_upstream_object")
        tier = chunk.get("service_tier")
        if tier is not None:
            if not isinstance(tier, str) or tier not in {"auto", "default", "flex", "scale", "priority"}:
                raise UnsupportedSemantic("unsupported_service_tier")
            if response.get("service_tier", tier) != tier:
                raise StreamError("changed_service_tier")
            response["service_tier"] = tier
        if not started:
            created = chunk.get("created", 0)
            if type(created) is not int or created < 0:
                raise StreamError("invalid_created_time")
            response["created_at"] = created
            yield event("response.created", response=response)
            yield event("response.in_progress", response=response)
            started = True
        if chunk.get("usage") is not None:
            response["usage"] = _usage(chunk["usage"])
        choices = chunk.get("choices")
        if not isinstance(choices, list) or len(choices) > 1:
            raise UnsupportedSemantic("single_choice_only")
        if not choices:
            if chunk.get("usage") is None:
                raise StreamError("empty_chat_chunk")
            continue
        choice = choices[0]
        allowed_choice = {"index", "delta", "finish_reason", "logprobs"}
        if allow_probe_extensions:
            allowed_choice.add("matched_stop")
        _keys(choice, allowed_choice, unsupported="unsupported_response_choice_field")
        matched = choice.get("matched_stop")
        if allow_probe_extensions and matched is not None and not (
                type(matched) is int and matched >= 0 or isinstance(matched, str) and len(matched) <= 1024):
            raise UnsupportedSemantic("invalid_matched_stop")
        if type(choice.get("index")) is not int or choice["index"] != 0:
            raise UnsupportedSemantic("single_choice_only")
        if choice.get("logprobs") is not None:
            raise UnsupportedSemantic("logprobs_not_supported")
        if finish is not None:
            raise StreamError("choice_after_finish_reason")
        delta = choice.get("delta")
        _keys(delta, {"role", "content", "reasoning_content", "tool_calls"},
              unsupported="unsupported_response_delta_field")
        if delta.get("role") not in (None, "assistant"):
            raise UnsupportedSemantic("unsupported_delta_role")
        for key in ("reasoning_content", "content"):
            value = delta.get(key)
            if value is None or value == "":
                continue
            _string(value)
            if key == "reasoning_content":
                if text_item is not None or any(c.get("item") for c in calls.values()):
                    raise UnsupportedSemantic("reasoning_after_answer_started")
                if reasoning_item is None:
                    reasoning_item = {"id": "rs_" + uuid4().hex, "type": "reasoning", "summary": [],
                                      "content": [{"type": "reasoning_text", "text": ""}]}
                    yield add(reasoning_item)
                    yield event("response.reasoning_part.added", **position(reasoning_item), content_index=0,
                                part=reasoning_item["content"][0])
                reasoning_item["content"][0]["text"] += value
                yield event("response.reasoning_text.delta", **position(reasoning_item), content_index=0, delta=value)
            else:
                if text_item is None:
                    text_item = {"id": "msg_" + uuid4().hex, "type": "message", "role": "assistant",
                                 "status": "in_progress", "content": [{"type": "output_text", "text": "", "annotations": []}]}
                    yield add(text_item)
                    yield event("response.content_part.added", **position(text_item), content_index=0,
                                part=text_item["content"][0])
                text_item["content"][0]["text"] += value
                yield event("response.output_text.delta", **position(text_item), content_index=0, delta=value)
        tool_deltas = delta.get("tool_calls")
        if tool_deltas is None:
            tool_deltas = []
        if not isinstance(tool_deltas, list):
            raise StreamError("invalid_tool_deltas")
        for call in tool_deltas:
            _keys(call, {"index", "id", "type", "function"}, unsupported="unsupported_response_tool_field")
            index = call.get("index")
            if type(index) is not int or index < 0 or call.get("type") not in (None, "function"):
                raise UnsupportedSemantic("unsupported_tool_delta")
            state = calls.setdefault(index, {"call_id": None, "name": "", "arguments": "", "item": None})
            if call.get("id") is not None:
                call_id = _string(call["id"])
                if state["call_id"] is not None and state["call_id"] != call_id:
                    raise StreamError("changed_call_id")
                if any(c is not state and c["call_id"] == call_id for c in calls.values()):
                    raise StreamError("duplicate_call_id")
                state["call_id"] = call_id
            function = call.get("function", {})
            _keys(function, {"name", "arguments"}, unsupported="unsupported_response_tool_field")
            if function.get("name") is not None:
                name = _string(function["name"], empty=True)
                if state["item"] is not None:
                    if name and name != state["name"]:
                        raise UnsupportedSemantic("name_fragment_after_arguments")
                else:
                    state["name"] += name
            arguments = function.get("arguments")
            if arguments is None:
                arguments = ""
            _string(arguments, empty=True)
            if arguments:
                if not state["call_id"] or not state["name"]:
                    raise StreamError("arguments_before_tool_identity")
                if state["item"] is None:
                    state["item"] = _new_tool_item(state["call_id"], state["name"])
                    yield add(state["item"])
                state["arguments"] += arguments
                if state["item"]["type"] == "function_call":
                    state["item"]["arguments"] = state["arguments"]
                    yield event("response.function_call_arguments.delta", **position(state["item"]), delta=arguments)
        finish = choice.get("finish_reason")
        if finish is not None:
            if finish not in {"stop", "tool_calls", "length", "content_filter"}:
                raise UnsupportedSemantic("unsupported_finish_reason")
            if (finish == "tool_calls" and not calls) or (finish == "stop" and calls):
                raise StreamError("finish_reason_tool_mismatch")
    else:
        raise StreamError("stream_truncated_missing_done")

    incomplete = finish in {"length", "content_filter"}
    # Validate the entire call batch before advertising any executable item done.
    for state in calls.values():
        if not state["call_id"] or not state["name"]:
            raise StreamError("incomplete_tool_identity")
        if state["item"] is None:
            state["item"] = _new_tool_item(state["call_id"], state["name"])
        _finish_tool_arguments(state, incomplete)
    if not incomplete and text_item is None and not calls:
        raise StreamError("empty_response")
    for state in calls.values():
        if state["item"] not in items:
            yield add(state["item"])
    for item in items:
        if item["type"] == "message":
            item["status"] = "incomplete" if incomplete else "completed"
            yield event("response.output_text.done", **position(item), content_index=0, text=item["content"][0]["text"])
            yield event("response.content_part.done", **position(item), content_index=0, part=item["content"][0])
        elif item["type"] == "reasoning":
            yield event("response.reasoning_text.done", **position(item), content_index=0, text=item["content"][0]["text"])
            yield event("response.reasoning_part.done", **position(item), content_index=0, part=item["content"][0])
        else:
            item["status"] = "incomplete" if incomplete else "completed"
            if incomplete:
                continue
            if item["type"] == "custom_tool_call":
                yield event("response.custom_tool_call_input.delta", **position(item), call_id=item["call_id"], delta=item["input"])
                yield event("response.custom_tool_call_input.done", **position(item), call_id=item["call_id"], input=item["input"])
            elif item["type"] == "function_call":
                yield event("response.function_call_arguments.done", **position(item), arguments=item["arguments"])
        yield event("response.output_item.done", output_index=items.index(item), item=item)
    response["output"] = items
    response["status"] = "incomplete" if incomplete else "completed"
    if incomplete:
        response["incomplete_details"] = {"reason": "max_output_tokens" if finish == "length" else "content_filter"}
    yield event("response.incomplete" if incomplete else "response.completed", response=response)
