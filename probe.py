"""Bounded, explicit Chat connection probes; credentials never leave the process."""
import json
import contextvars
import queue
import threading
import math
import os
from pathlib import Path
import re
import socket
import ssl
import time
import urllib.error
import urllib.request
from urllib.parse import quote

from gateway import _NoRedirect
from responses_adapter import AdapterError, UnsupportedSemantic, UpstreamError, convert_stream, translate_request
from cc_switch_model_discovery import model_id, model_url_candidates
from cc_switch_reference import reference_for_models
from storage import MODEL_ADAPTERS, validate_provider_url

MARKER = "PROVIDER_HUB_PROBE_OK"
MAX_METADATA = 1024 * 1024
PROBE_SOCKET_TIMEOUT_SECONDS = 75
PROBE_STREAM_TIMEOUT_SECONDS = 90
PROBE_JOB_TIMEOUT_SECONDS = 120
EFFORTS = {"none", "low", "medium", "high", "xhigh", "max", "ultra"}
SAFE_UPSTREAM_ERROR_TYPES = frozenset({
    "api_error", "authentication_error", "billing_error", "invalid_request_error",
    "not_found_error", "overloaded_error", "permission_error", "rate_limit_error",
    "server_error", "timeout_error", "upstream_error",
})
SAFE_REQUEST_ID = re.compile(r"(?:req_[A-Za-z0-9]{8,80}|[0-9a-fA-F]{8}-(?:[0-9a-fA-F]{4}-){3}[0-9a-fA-F]{12})\Z")


REQUEST_OBSERVER=contextvars.ContextVar("provider_hub_request_observer",default=None)


def _request_started(kind):
    observer=REQUEST_OBSERVER.get()
    if observer: observer(kind)


class ReadDeadline(ValueError):
    pass


def bounded_read(operation, deadline, cancel_check=None):
    """Only read-only probe/discovery operations may run here. Never persist in a worker.

    A slow socket may finish later, but its result is abandoned; this thread has
    no database/engine mutation callback and can never resume the save flow.
    """
    remaining=deadline-time.monotonic()
    if remaining <= 0:
        raise ReadDeadline("整组测试超时，未保存或修改连接。请减少所选模型后重试。")
    result=queue.Queue(maxsize=1)
    def run():
        try: result.put((True,operation()))
        except Exception as error: result.put((False,error))
    context=contextvars.copy_context()
    threading.Thread(target=lambda:context.run(run),daemon=True).start()
    while True:
        if cancel_check: cancel_check()
        remaining=deadline-time.monotonic()
        if remaining<=0:
            raise ReadDeadline("整组测试超时，未保存或修改连接。")
        try:
            ok,value=result.get(timeout=min(0.1,remaining));break
        except queue.Empty: continue
    if time.monotonic() >= deadline:
        raise ReadDeadline("整组测试超时，未保存或修改连接。")
    if not ok: raise value
    return value



class ProbeTimeout(Exception):
    pass


class ProbeResponseTooLarge(Exception):
    pass


def _network_failure(error):
    reason = error.reason if isinstance(error, urllib.error.URLError) else error
    if isinstance(reason, socket.gaierror): return {"ok": False, "code": "dns_failed"}
    if isinstance(reason, ssl.SSLError): return {"ok": False, "code": "tls_failed"}
    if isinstance(reason, (TimeoutError, socket.timeout)): return {"ok": False, "code": "network_timeout"}
    return {"ok": False, "code": "upstream_unavailable"}


def _append_upstream_evidence(message, result):
    evidence = []
    if result.get("upstream_error_type") in SAFE_UPSTREAM_ERROR_TYPES:
        evidence.append("服务商错误类别：" + result["upstream_error_type"])
    request_id = result.get("request_id")
    if isinstance(request_id, str) and SAFE_REQUEST_ID.fullmatch(request_id):
        evidence.append("请求标识：" + request_id)
    return message + ("（" + "；".join(evidence) + "）" if evidence else "")


def describe_probe_failure(result: dict) -> str:
    """Map only fixed diagnostic codes and HTTP statuses to safe user guidance."""
    code = result.get("code")
    status = result.get("http_status")
    if code == "upstream_rejected" and type(status) is int and 100 <= status <= 599:
        if status == 401:
            message = "服务商拒绝认证（HTTP 401），请检查密钥是否有效。"
        elif status == 403:
            message = "服务商拒绝请求（HTTP 403），请检查账号权限或线路策略。"
        elif status in {404, 405}:
            message = f"服务商未找到该接口或模型，或不支持该请求（HTTP {status}），请检查地址、模型和接口类型。"
        elif status in {400, 422}:
            message = f"服务商拒绝请求参数（HTTP {status}），请检查模型名称和接口类型。"
        elif status == 429:
            message = "服务商返回 HTTP 429，可能受到调用频率或使用限制，请稍后重试。"
        elif status >= 500:
            message = f"服务商或中转服务返回 HTTP {status}，请稍后重试。"
        else:
            message = f"服务商拒绝测试请求（HTTP {status}）。"
        return _append_upstream_evidence(message, result)
    messages = {
        "subscription_login_required": "Codex 订阅未登录或登录状态不可用。",
        "official_route_not_ready": "官方订阅线路尚未就绪。",
        "upstream_rejected": "服务商拒绝了测试请求，但未返回可用的状态码。",
        "model_access_denied": "服务商表示该密钥没有访问该模型的权限（HTTP 403），请检查令牌的模型权限或供应商分组。",
        "dns_failed": "无法解析服务商域名，请检查接口地址和网络。",
        "tls_failed": "HTTPS 安全连接未通过证书校验，请检查服务商证书或网络。",
        "network_timeout": "连接或接收测试响应超时，请检查网络和服务状态。",
        "upstream_unavailable": "无法连接服务商，请检查网络和接口地址。",
        "response_too_large": "服务商的测试响应超过允许大小，未能完成检查。",
        "response_protocol_unsupported": "已连接服务商，但返回格式包含测试程序尚不支持的字段。",
        "response_stream_invalid": "服务商的流式响应不完整或格式无效，未能完成检查。",
        "upstream_stream_error": "服务商在响应过程中报告了错误，未能完成检查。",
        "output_limit": "测试输出达到长度上限，模型尚未完成回复。",
        "content_filter": "测试回复被服务商的内容过滤中断。",
        "response_incomplete": "服务商的测试回复未完成。",
        "response_missing_completion": "已收到响应，但没有收到完整结束信号。",
        "reasoning_only": "服务商只返回了思考内容，没有返回最终测试回复。",
        "marker_missing": "服务商返回了回复，但测试结果与预期不符。",
        "response_unverified": "服务商的测试响应缺少预期结果。",
        "unsupported_effort": "此测试不支持所选思考档位。",
        "probe_protocol_unsupported": "此测试不支持当前接口类型。",
        "request_invalid": "无法构造兼容该接口的测试请求，请检查模型和接口类型。",
    }
    message = messages.get(code, "连接测试未通过，请检查接口配置或稍后重试。")
    detail = result.get("detail")
    protocol_details = {
        "unsupported_response_chunk_field": "服务商的响应包含当前不支持的顶层字段。",
        "unsupported_response_choice_field": "服务商的响应选项包含当前不支持的字段。",
        "unsupported_response_delta_field": "服务商的回复内容包含当前不支持的字段。",
        "unsupported_response_tool_field": "服务商的工具调用包含当前不支持的字段。",
        "unsupported_usage_field": "服务商的用量统计包含当前不支持的字段。",
        "unsupported_usage_detail_field": "服务商的用量明细包含当前不支持的字段。",
    }
    if code == "response_protocol_unsupported" and isinstance(detail, str) and detail in protocol_details:
        message = protocol_details[detail]
    stream_details = {
        "stream_truncated_missing_done": "已收到响应，但响应流缺少结束标记。",
        "done_without_finish_reason": "响应流结束时没有给出完成原因。",
        "sse_transport_error": "接收响应时连接中断，请检查网络或服务状态。",
        "empty_response": "服务商结束了测试，但没有返回有效回复。",
    }
    if code == "response_stream_invalid" and isinstance(detail, str) and detail in stream_details:
        message = stream_details[detail]
    if code in {"response_protocol_unsupported", "response_stream_invalid", "request_invalid"} and isinstance(detail, str) and re.fullmatch(r"[a-z0-9_]{1,80}", detail):
        message += f"（诊断码：{detail}）"
    return _append_upstream_evidence(message, result)


def _http_error_evidence(error, key):
    """Keep only bounded, allowlisted diagnostics; never expose error text."""
    result = {"ok": False, "code": "upstream_rejected", "http_status": error.code}
    headers = error.headers
    if headers is not None:
        for name in ("request-id", "x-request-id"):
            request_id = headers.get(name)
            if isinstance(request_id, str) and SAFE_REQUEST_ID.fullmatch(request_id):
                secret = key.casefold()
                value = request_id.casefold()
                overlaps = bool(secret) and (secret in value if len(secret) < 8 else
                           any(secret[i:i + 8] in value for i in range(len(secret) - 7)))
                if not overlaps:
                    result["request_id"] = request_id
                    break
    try:
        raw = error.read(16385)
        data = json.loads(raw) if len(raw) <= 16384 else None
        detail = data.get("error") if isinstance(data, dict) else None
        if isinstance(detail, dict):
            error_type = detail.get("type")
            if (isinstance(error_type, str) and error_type in SAFE_UPSTREAM_ERROR_TYPES
                    and key not in error_type and error_type not in key):
                result["upstream_error_type"] = error_type
            message = detail.get("message")
            if (error.code == 403 and isinstance(message, str)
                    and re.search(r"\bthis token has no access to model\b", message, re.I)):
                result["code"] = "model_access_denied"
    except (AttributeError, OSError, TypeError, ValueError, UnicodeError):
        pass
    return result


def _bounded_lines(response):
    deadline = time.monotonic() + PROBE_STREAM_TIMEOUT_SECONDS
    total = 0
    while True:
        if time.monotonic() > deadline:
            raise ProbeTimeout()
        try:
            line = response.readline(65537)
        except (TimeoutError, socket.timeout):
            raise ProbeTimeout() from None
        if not line:
            return
        total += len(line)
        if len(line) > 65536 or total > MAX_METADATA:
            raise ProbeResponseTooLarge()
        yield line


def probe_transport(url, data, headers):
    _request_started("inference")
    request = urllib.request.Request(url, data=data, headers=headers, method="POST")
    return urllib.request.build_opener(_NoRedirect()).open(request, timeout=PROBE_SOCKET_TIMEOUT_SECONDS)


def _anthropic_root(base_url):
    """Match the embedded engine's handling of root, /v1 and /v1/messages."""
    return re.sub(r"/v1(?:/messages)?$", "", base_url.rstrip("/"), flags=re.IGNORECASE)


def _provider_headers(protocol, key):
    if protocol == "gemini_native":
        return {"x-goog-api-key": key}
    if protocol == "anthropic_messages":
        return {"x-api-key": key, "anthropic-version": "2023-06-01"}
    return {"Authorization": "Bearer " + key}


def _anthropic_probe_result(lines, effort):
    answer, started, completed, stop_reason, reasoning_seen = [], False, False, None, False
    for raw in lines:
        line = raw.decode("utf-8").strip()
        if not line.startswith("data:"):
            continue
        payload = line[5:].strip()
        if payload == "[DONE]":
            if not completed:
                return {"ok": False, "code": "response_missing_completion"}
            continue
        event = json.loads(payload)
        if not isinstance(event, dict):
            return {"ok": False, "code": "response_stream_invalid"}
        kind = event.get("type")
        if kind == "error":
            return {"ok": False, "code": "upstream_stream_error"}
        if kind == "message_start":
            started = True
        elif kind == "content_block_start":
            block = event.get("content_block")
            if isinstance(block, dict) and block.get("type") == "text":
                text = block.get("text", "")
                if not isinstance(text, str): raise ValueError("invalid text")
                answer.append(text)
            elif isinstance(block, dict) and block.get("type") in {"thinking", "redacted_thinking"}:
                reasoning_seen = True
        elif kind == "content_block_delta":
            delta = event.get("delta")
            if not isinstance(delta, dict): raise ValueError("invalid delta")
            if delta.get("type") == "text_delta":
                text = delta.get("text")
                if not isinstance(text, str): raise ValueError("invalid text")
                answer.append(text)
            elif delta.get("type") == "thinking_delta":
                reasoning_seen = True
        elif kind == "message_delta":
            delta = event.get("delta")
            if not isinstance(delta, dict): raise ValueError("invalid message delta")
            if delta.get("stop_reason") is not None:
                stop_reason = delta["stop_reason"]
        elif kind == "message_stop":
            completed = True
    if not started or not completed or stop_reason is None:
        return {"ok": False, "code": "response_missing_completion"}
    if stop_reason == "max_tokens": return {"ok": False, "code": "output_limit"}
    if stop_reason == "refusal": return {"ok": False, "code": "content_filter"}
    if stop_reason not in {"end_turn", "stop_sequence"}:
        return {"ok": False, "code": "response_incomplete"}
    if MARKER not in "".join(answer):
        return {"ok": False, "code": "reasoning_only" if reasoning_seen and not answer else "marker_missing"}
    return {"ok": True, "effort": effort}


def probe_snapshot(route: dict, key: str, effort: str | None, *, transport=probe_transport) -> dict:
    if effort not in {None, "high", "max"}:
        return {"ok": False, "code": "unsupported_effort"}
    stage = "prepare"
    try:
        if route["protocol"] not in MODEL_ADAPTERS:
            return {"ok": False, "code": "probe_protocol_unsupported"}
        if route["protocol"] == "gemini_native":
            if effort is not None:
                return {"ok": False, "code": "unsupported_effort"}
            model = route["model"].removeprefix("models/")
            url = validate_provider_url(route["base_url"], "gemini_native") + "/v1beta/models/" + quote(model, safe="") + ":generateContent"
            payload = {"contents": [{"role": "user", "parts": [{"text": "Reply exactly " + MARKER}]}],
                       "generationConfig": {"maxOutputTokens": 256}}
            stage = "connect"
            response = transport(url, json.dumps(payload).encode(),
                                 {"Content-Type": "application/json", "Accept": "application/json",
                                  **_provider_headers("gemini_native", key)})
            stage = "response"
            with response:
                if getattr(response, "status", 200) != 200:
                    return {"ok": False, "code": "upstream_rejected", "http_status": response.status}
                raw = b"".join(_bounded_lines(response))
            document = json.loads(raw)
            if not isinstance(document, dict):
                return {"ok": False, "code": "response_stream_invalid"}
            if document.get("error"):
                return {"ok": False, "code": "upstream_stream_error"}
            candidates = document.get("candidates", [])
            if not isinstance(candidates, list):
                return {"ok": False, "code": "response_stream_invalid"}
            if not candidates:
                return {"ok": False, "code": "content_filter" if document.get("promptFeedback", {}).get("blockReason") else "response_missing_completion"}
            candidate = candidates[0]
            if not isinstance(candidate, dict):
                return {"ok": False, "code": "response_stream_invalid"}
            reason = candidate.get("finishReason")
            if reason != "STOP":
                return {"ok": False, "code": "output_limit" if reason == "MAX_TOKENS" else "response_incomplete"}
            text = "".join(part.get("text", "") for part in candidate.get("content", {}).get("parts", []) if not part.get("thought"))
            return {"ok": True, "effort": None} if MARKER in text else {"ok": False, "code": "marker_missing"}
        alias = route["model"]
        anthropic = route["protocol"] == "anthropic_messages"
        if anthropic and effort is not None:
            return {"ok": False, "code": "unsupported_effort"}
        body = {"model": alias, "input": "Reply exactly " + MARKER, "stream": True,
                "store": False, "max_output_tokens": 256}
        if route.get("official"):
            body["instructions"] = "Answer the user's short test request."
        if effort is not None:
            body["reasoning"] = {"effort": effort}
        payload = ({"model": alias, "messages": [{"role": "user", "content": "Reply exactly " + MARKER}],
                    "stream": True, "max_tokens": 256} if anthropic else
                   translate_request(body, route["model"], confirmed_reasoning_efforts={effort} if effort else ())
                   if route["protocol"] == "chat_completions" else body)
        headers = {"Content-Type": "application/json", "Accept": "text/event-stream",
                   **_provider_headers(route["protocol"], key)}
        url = (_anthropic_root(route["base_url"]) + "/v1/messages" if anthropic else
               route["base_url"] + ("/chat/completions" if route["protocol"] == "chat_completions" else "/responses"))
        stage = "connect"
        response = transport(url, json.dumps(payload, ensure_ascii=False).encode("utf-8"), headers)
        stage = "response"
        answer, completed, incomplete, reasoning_seen = [], False, None, False
        with response:
            if getattr(response, "status", 200) != 200:
                return {"ok": False, "code": "upstream_rejected"}
            if anthropic:
                return _anthropic_probe_result(_bounded_lines(response), effort)
            frames = (convert_stream(_bounded_lines(response), alias, allow_probe_extensions=True) if route["protocol"] == "chat_completions"
                      else _bounded_lines(response))
            for frame in frames:
                for line in frame.decode("utf-8").splitlines():
                    if line.startswith("data: "):
                        if line[6:] == "[DONE]":
                            continue
                        event = json.loads(line[6:])
                        if event.get("type") == "response.output_text.delta":
                            answer.append(event.get("delta", ""))
                        elif event.get("type") == "response.reasoning_text.delta":
                            reasoning_seen = True
                        elif event.get("type") == "response.incomplete":
                            response_info = event.get("response") if isinstance(event.get("response"), dict) else {}
                            details = response_info.get("incomplete_details") if isinstance(response_info.get("incomplete_details"), dict) else {}
                            incomplete = details.get("reason") or "unknown"
                        elif event.get("type") in {"response.failed", "error"}:
                            return {"ok": False, "code": "upstream_stream_error"}
                        completed |= event.get("type") == "response.completed"
        if incomplete == "max_output_tokens": return {"ok": False, "code": "output_limit"}
        if incomplete == "content_filter": return {"ok": False, "code": "content_filter"}
        if incomplete is not None: return {"ok": False, "code": "response_incomplete"}
        if not completed: return {"ok": False, "code": "response_missing_completion"}
        if MARKER not in "".join(answer):
            return {"ok": False, "code": "reasoning_only" if reasoning_seen and not answer else "marker_missing"}
        return {"ok": True, "effort": effort}
    except urllib.error.HTTPError as error:
        return _http_error_evidence(error, key)
    except ProbeTimeout:
        return {"ok": False, "code": "network_timeout"}
    except ProbeResponseTooLarge:
        return {"ok": False, "code": "response_too_large"}
    except urllib.error.URLError as error:
        return _network_failure(error)
    except (ssl.SSLError, socket.gaierror, OSError) as error:
        return _network_failure(error)
    except AdapterError as error:
        detail = error.code if isinstance(error.code, str) and re.fullmatch(r"[a-z0-9_]{1,80}", error.code) else "protocol_error"
        if stage == "response" and detail == "empty_response" and reasoning_seen:
            return {"ok": False, "code": "reasoning_only"}
        code = ("upstream_stream_error" if isinstance(error, UpstreamError) else
                "response_protocol_unsupported" if stage == "response" and isinstance(error, UnsupportedSemantic) else
                "response_stream_invalid" if stage == "response" else "request_invalid")
        return {"ok": False, "code": code, "detail": detail}
    except (ValueError, UnicodeError, KeyError, TypeError, AttributeError):
        return {"ok": False, "code": "response_stream_invalid" if stage == "response" else "request_invalid"}


def probe_effort(store, alias: str, effort: str, *, transport=probe_transport) -> dict:
    try:
        resolved = store.resolve_route(alias)
        if resolved is None:
            return {"ok": False, "code": "route_missing"}
        route, key = resolved
        result = probe_snapshot(route, key, effort, transport=transport)
        if result.get("ok"):
            store.confirm_effort(alias, route["_revision"], effort)
        return result
    except (ValueError, UnicodeError, KeyError, TypeError):
        return {"ok": False, "code": "response_or_route_invalid"}


def _capabilities(model: str, item: dict) -> dict:
    """Only accept unambiguous metadata for the exact requested model."""
    if not isinstance(item, dict) or item.get("id", item.get("slug")) != model:
        return {}
    result = {}
    metadata = item.get("metadata") if isinstance(item.get("metadata"), dict) else {}
    capabilities = metadata.get("capabilities") if isinstance(metadata.get("capabilities"), dict) else item.get("capabilities") if isinstance(item.get("capabilities"), dict) else {}
    metadata_limits = metadata.get("limits") if isinstance(metadata.get("limits"), dict) else {}
    raw_capabilities = item.get("capabilities") if isinstance(item.get("capabilities"), dict) else {}
    capability_limits = raw_capabilities.get("limits") if isinstance(raw_capabilities.get("limits"), dict) else {}
    model_meta = item.get("meta") if isinstance(item.get("meta"), dict) else {}
    windows = [metadata_limits.get("max_context_length"), capability_limits.get("max_context_window_tokens"),
               metadata.get("context_length"), *(item.get(name) for name in ("context_length", "context_size", "max_model_len", "max_context_length")),
               model_meta.get("n_ctx"), capabilities.get("context_length"), item.get("context_window")]
    for value in windows:
        if type(value) is int and 1 <= value <= 100_000_000:
            result["context_window"] = value
            break
    reasoning_parameters = next((value for value in (item.get("reasoning_parameters"), metadata.get("reasoning_parameters"),
                                capabilities.get("reasoning_parameters")) if isinstance(value, dict)), {})
    ladders = [item.get(name) for name in ("supported_reasoning_levels", "supported_reasoning_efforts", "reasoning_efforts")]
    ladders += [capabilities.get("reasoning_effort"), reasoning_parameters.get("efforts")]
    if capabilities.get("supports_reasoning") is False or capabilities.get("supports_reasoning_effort") is False:
        ladders = []
    for value in ladders:
        if isinstance(value, list):
            levels = [entry.get("effort") if isinstance(entry, dict) else entry for entry in value]
            if levels and all(isinstance(x, str) and x in EFFORTS for x in levels):
                levels = list(dict.fromkeys(levels))
                if levels:
                    result["reasoning_levels"] = levels
                break
    for name in ("default_reasoning_level", "default_reasoning_effort"):
        value = item.get(name)
        if isinstance(value, str) and value in result.get("reasoning_levels", []):
            result["default_effort"] = value
            break
    return result


def _merge_reference(result: dict, reference: dict, model: str) -> None:
    """Fill missing model metadata only; reference sources are not probe evidence."""
    sources = reference.get("sources") if isinstance(reference.get("sources"), dict) else {}
    used = {}
    for field in ("context_window", "reasoning_levels", "supports_image"):
        value = reference.get(field)
        valid = (type(value) is int and 1 <= value <= 100_000_000 if field == "context_window" else
                 isinstance(value, list) and bool(value) and all(isinstance(v, str) and v in EFFORTS for v in value) if field == "reasoning_levels" else
                 type(value) is bool)
        if field not in result and valid and sources.get(field) in {'preset', 'models.dev:same-provider', 'models.dev:vendor-reference'}:
            result[field] = value
            used[field] = sources[field]
    if used:
        result["reference_sources"] = used


def discover_capabilities(route: dict, key: str, *, opener=None, reference_lookup=None) -> dict:
    """Inspect the provider's own model endpoints without sending chat contents."""
    if route["protocol"] == "gemini_native":
        # Native token limits are metadata, not proof of enabled modalities.
        model = route["model"].removeprefix("models/")
        result = fetch_model_ids(route["base_url"], key, protocol="gemini_native", opener=opener,
                                 reference_lookup=reference_lookup)
        return next(({k: row[k] for k in ("context_window",) if k in row}
                     for row in result.get("models", []) if row["id"] == model), {})
    if route["protocol"] not in MODEL_ADAPTERS:
        return {}
    use_reference = opener is None or reference_lookup is not None
    opener = opener or urllib.request.build_opener(_NoRedirect())
    base, model = route["base_url"], route["model"]
    if route["protocol"] == "anthropic_messages":
        base = _anthropic_root(base) + "/v1"
    result = {}
    for suffix in ("/models/" + quote(model, safe=""), "/models"):
        try:
            request = urllib.request.Request(base + suffix, headers={**_provider_headers(route["protocol"], key),
                                              "Accept": "application/json"}, method="GET")
            _request_started("metadata")
            with opener.open(request, timeout=4) as response:
                if getattr(response, "status", 200) != 200:
                    continue
                raw = response.read(MAX_METADATA + 1)
            if len(raw) > MAX_METADATA:
                continue
            document = json.loads(raw)
            items = document if isinstance(document, list) else document.get("data") if isinstance(document, dict) else None
            items = items if isinstance(items, list) else [document]
            matches = [item for item in items if isinstance(item, dict) and item.get("id", item.get("slug")) == model]
            if len(matches) != 1:
                continue
            for key_name, value in _capabilities(model, matches[0]).items():
                result.setdefault(key_name, value)
            if all(name in result for name in ("context_window", "reasoning_levels", "default_effort")):
                break
        except (urllib.error.URLError, OSError, ValueError, UnicodeError, TypeError):
            continue
    if use_reference:
        try:
            reference = (reference_lookup or reference_for_models)(base, [model]).get(model, {})
            if isinstance(reference, dict): _merge_reference(result, reference, model)
        except Exception:
            pass  # A public reference is optional and cannot invalidate provider discovery.
    return result


def fetch_model_ids(base_url: str, key: str, *, protocol="chat_completions", opener=None, reference_lookup=None) -> dict:
    """Fetch provider model IDs without following redirects or returning secrets."""
    use_reference = opener is None or reference_lookup is not None
    opener = opener or urllib.request.build_opener(_NoRedirect())
    try:
        if protocol == "gemini_native":
            urls = [validate_provider_url(base_url, protocol) + "/v1beta/models"]
        elif protocol == "anthropic_messages":
            root = _anthropic_root(base_url)
            urls = list(dict.fromkeys(model_url_candidates(root + "/v1") + model_url_candidates(root)))
        else:
            urls = model_url_candidates(base_url)
    except (TypeError, ValueError):
        return {"ok": False, "code": "model_list_unavailable"}
    for index, url in enumerate(urls):
        try:
            request = urllib.request.Request(url, headers={**_provider_headers(protocol, key),
                                             "Accept": "application/json"}, method="GET")
            with opener.open(request, timeout=12) as response:
                if getattr(response, "status", 200) != 200:
                    return {"ok": False, "code": "provider_rejected"}
                raw = response.read(4 * MAX_METADATA + 1)
            if len(raw) > 4 * MAX_METADATA:
                return {"ok": False, "code": "model_list_too_large"}
            document = json.loads(raw)
            data = document.get("data") if isinstance(document, dict) else None
            catalog = document.get("models") if isinstance(document, dict) else None
            is_catalog = not isinstance(data, list) and isinstance(catalog, list)
            rows = document if isinstance(document, list) else data if isinstance(data, list) else catalog
            if not isinstance(rows, list):
                return {"ok": False, "code": "model_list_unsupported"}
            result, seen, cursors, truncated, pages = [], set(), set(), False, 0
            while True:
                pages += 1
                for row in rows:
                    if protocol == "gemini_native":
                        methods = row.get("supportedGenerationMethods", []) if isinstance(row, dict) else []
                        if not isinstance(methods, list) or (methods and "generateContent" not in methods):
                            continue
                        name = row.get("name") if isinstance(row, dict) else None
                        model = name.removeprefix("models/") if isinstance(name, str) else None
                    else:
                        model = model_id(row, catalog=is_catalog)
                    if isinstance(model, str) and re.fullmatch(r"[A-Za-z0-9_.:/~-]{1,160}", model) and model not in seen and (not key or key not in model):
                        if len(result) == 5000:
                            truncated = True
                            break
                        seen.add(model)
                        item = {"id": model}
                        if isinstance(row, dict):
                            owner = row.get("owned_by", row.get("ownedBy"))
                            if (isinstance(owner, str) and 0 < len(owner) <= 128
                                    and not any(ord(ch) < 32 or ord(ch) == 127 for ch in owner)
                                    and (not key or key not in owner)):
                                item["owned_by"] = owner
                            item.update(_capabilities(model, row))
                            if protocol == "gemini_native":
                                limit = row.get("inputTokenLimit")
                                if type(limit) is int and 1 <= limit <= 100_000_000:
                                    item["context_window"] = limit
                            modalities = row.get("modalities")
                            if (isinstance(modalities, dict) and isinstance(modalities.get("input"), list)
                                    and modalities["input"] and all(isinstance(value, str) for value in modalities["input"])):
                                item["supports_image"] = "image" in modalities["input"]
                        result.append(item)
                if protocol == "gemini_native":
                    cursor = document.get("nextPageToken") if isinstance(document, dict) else None
                    if not cursor: break
                else:
                    if protocol != "anthropic_messages" or not isinstance(document, dict) or document.get("has_more") is not True:
                        break
                    cursor = document.get("last_id")
                if (truncated or pages >= 50 or not isinstance(cursor, str) or not cursor
                        or len(cursor) > 160 or cursor in cursors):
                    truncated = True
                    break
                cursors.add(cursor)
                try:
                    next_request = urllib.request.Request(url + ("?pageToken=" if protocol == "gemini_native" else "?after_id=") + quote(cursor, safe=""),
                                                         headers={**_provider_headers(protocol, key),
                                                                  "Accept": "application/json"}, method="GET")
                    with opener.open(next_request, timeout=12) as response:
                        if getattr(response, "status", 200) != 200: raise ValueError("model page unavailable")
                        raw = response.read(4 * MAX_METADATA + 1)
                    if len(raw) > 4 * MAX_METADATA: raise ValueError("model page too large")
                    document = json.loads(raw)
                    rows = document.get("models" if protocol == "gemini_native" else "data") if isinstance(document, dict) else None
                    if not isinstance(rows, list): raise ValueError("invalid model page")
                    is_catalog = False
                except (urllib.error.URLError, OSError, ValueError, UnicodeError, TypeError):
                    truncated = True
                    break
            if result and use_reference and protocol != "gemini_native":
                try:
                    references = (reference_lookup or reference_for_models)(base_url, [item["id"] for item in result])
                    for item in result:
                        reference = references.get(item["id"], {})
                        if isinstance(reference, dict): _merge_reference(item, reference, item["id"])
                except Exception:
                    pass  # Model IDs remain usable when the optional reference is unavailable.
            return {"ok": True, "models": result, **({"truncated": True} if truncated else {})} if result else {"ok": False, "code": "model_list_empty"}
        except urllib.error.HTTPError as error:
            if error.code in (404, 405) and index + 1 < len(urls):
                continue
            return {"ok": False, "code": "provider_rejected", "http_status": error.code}
        except (urllib.error.URLError, OSError, ValueError, UnicodeError, TypeError):
            return {"ok": False, "code": "model_list_unavailable"}
    return {"ok": False, "code": "model_list_unavailable"}


def probe_jev(route: dict, key: str, *, transport=probe_transport) -> dict:
    payload = {"model": route["model"], "state": "2 + 2 = 4",
               "questions": {"check": {"type": "noul", "instructions": "Is the statement true?"}}}
    try:
        with transport(route["base_url"], json.dumps(payload).encode("utf-8"),
                       {"Content-Type": "application/json", "Authorization": "Bearer " + key}) as response:
            if getattr(response, "status", 200) != 200:
                return {"ok": False, "code": "upstream_rejected"}
            raw = response.read(MAX_METADATA + 1)
        if len(raw) > MAX_METADATA:
            return {"ok": False, "code": "response_too_large"}
        result = json.loads(raw)
        data = result.get("data", result) if isinstance(result, dict) else None
        answer = data.get("answers", {}).get("check") if isinstance(data, dict) and isinstance(data.get("answers"), dict) else None
        number = answer.get("noul") if isinstance(answer, dict) else None
        if isinstance(result, dict) and result.get("code", 0) != 0:
            return {"ok": False, "code": "upstream_stream_error"}
        if (not isinstance(result, dict) or type(number) not in {int, float}
                or not math.isfinite(number) or not 0 <= number <= 1):
            return {"ok": False, "code": "response_unverified"}
        return {"ok": True}
    except urllib.error.HTTPError as error:
        return {"ok": False, "code": "upstream_rejected", "http_status": error.code}
    except urllib.error.URLError as error:
        return _network_failure(error)
    except (ssl.SSLError, socket.gaierror, OSError) as error:
        return _network_failure(error)
    except (ValueError, UnicodeError, TypeError):
        return {"ok": False, "code": "response_stream_invalid"}


def official_capabilities(root: Path, model: str) -> dict:
    try:
        catalog = json.loads((root / "baseline-catalog.json").read_text(encoding="utf-8"))
        matches = [item for item in catalog.get("models", []) if isinstance(item, dict) and item.get("slug") == model]
        return _capabilities(model, matches[0]) if len(matches) == 1 else {}
    except (OSError, ValueError, TypeError):
        return {}


def probe_official(model: str, root: Path, *, codex_home: Path | None = None,
                   service_status=None, capability_reader=None, opener=None) -> dict:
    """Use the existing subscription session through the authenticated local Hub."""
    import ocx_service
    try:
        codex_home = Path(codex_home) if codex_home else Path(os.environ.get("CODEX_HOME") or Path.home()/".codex")
        auth = json.loads((codex_home / "auth.json").read_text(encoding="utf-8-sig"))
        tokens = auth.get("tokens", {})
        if auth.get("auth_mode") != "chatgpt" or not isinstance(tokens, dict):
            return {"ok": False, "code": "subscription_login_required"}
        access, account = tokens.get("access_token"), tokens.get("account_id")
        if not isinstance(access, str) or not access or not isinstance(account, str) or not account:
            return {"ok": False, "code": "subscription_login_required"}
        state = (service_status or ocx_service.status)(root)
        if not state.get("running") or not state.get("official_route_ready") or type(state.get("port")) is not int:
            return {"ok": False, "code": "official_route_not_ready"}
        capability = (capability_reader or ocx_service._read_capability)(root)
        management = ocx_service._read_admin_capability(root)
        local_opener = opener or urllib.request.build_opener(urllib.request.ProxyHandler({}), _NoRedirect())
        def local_transport(url, data, headers):
            _request_started("inference")
            headers.update({"X-Provider-Hub-Token": capability, "X-Provider-Hub-Management-Probe":management, "ChatGPT-Account-ID": account,
                            "OpenAI-Beta": "responses=experimental", "originator": "codex_cli_rs"})
            return local_opener.open(urllib.request.Request(url, data=data, headers=headers, method="POST"), timeout=25)
        route = {"protocol": "responses", "model": model,
                 "base_url": f"http://127.0.0.1:{state['port']}/v1", "official": True}
        return probe_snapshot(route, access, None, transport=local_transport)
    except (OSError, ValueError, TypeError, KeyError):
        return {"ok": False, "code": "subscription_login_required"}
