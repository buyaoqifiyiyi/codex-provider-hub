"""Jev tools; connection settings and credentials are supplied by the host."""
import json
import time
import urllib.error
import urllib.request
from contextlib import contextmanager
from contextvars import ContextVar

from mcp.server.mcpserver import MCPServer
from jev_activity import record_call, status as activity_status

server = MCPServer("jev-tools")
_CALL_SOURCE = ContextVar("jev_call_source", default="mcp" if __name__ == "__main__" else "python")
_CALL_OPERATION = ContextVar("jev_call_operation", default="jev_decide")


@contextmanager
def _operation(name):
    token = _CALL_OPERATION.set(name)
    try:
        yield
    finally:
        _CALL_OPERATION.reset(token)


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def read_settings():
    raise ValueError("请通过连接中心配置 Jev 线路。")


def api_key(provider_name):
    raise ValueError("请通过连接中心读取 Jev 凭据。")


def _request_jev(state, questions, assets=None):
    """One request through the existing selected provider; never logs credentials."""
    settings = read_settings()
    payload = {"model": settings["model"], "state": state, "questions": questions}
    headers = {"Content-Type": "application/json"}
    if assets is not None:
        payload["assets"] = assets
        headers["TypeSafe-Danger-Zone-Images"] = "true"
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    if len(body) > (2 * 1024 * 1024 + 16384 if assets is not None else 32768):
        raise ValueError("请求过大，请缩短文字或减少所选画面。")
    headers["Authorization"] = "Bearer " + api_key(settings["provider_name"])
    request = urllib.request.Request(settings["endpoint"], data=body, method="POST", headers=headers)
    started = time.monotonic()
    outcome, http_status, model, usage = "other_error", None, settings["model"], None
    try:
        with urllib.request.build_opener(NoRedirect()).open(request, timeout=30) as response:
            http_status = getattr(response, "status", 200)
            try:
                result = json.load(response)
            except (ValueError, UnicodeError):
                outcome = "invalid_response"
                raise ValueError("Jev API 返回了无效 JSON。") from None
        if not isinstance(result, dict):
            outcome = "invalid_response"
            raise ValueError("Jev API 返回了非对象结果。")
        if result.get("code", 0) != 0:
            outcome = "invalid_response"
            raise ValueError(f"Jev API 返回错误代码：{result.get('code')}。")
        data = result.get("data", result)
        if not isinstance(data, dict) or not isinstance(data.get("answers"), dict):
            outcome = "invalid_response"
            raise ValueError("Jev API 未返回预期的 answers。")
        model = data.get("model", model)
        usage = data.get("usage")
        outcome = ("success" if questions and all(isinstance(data["answers"].get(key), dict)
                                                    and data["answers"][key] for key in questions)
                   else "partial_response")
        return data
    except urllib.error.HTTPError as exc:
        outcome, http_status = "http_error", exc.code
        raise ValueError(f"Jev API 请求失败（HTTP {exc.code}）；请检查模型、额度及提供商状态。") from None
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        outcome = "network_error"
        raise ValueError(f"Jev API 连接失败（{type(exc).__name__}）。") from None
    finally:
        # Logging is strictly metadata-only and may never block a decision.
        try:
            record_call(operation=_CALL_OPERATION.get(), source=_CALL_SOURCE.get(), outcome=outcome,
                        model=model, duration_ms=int((time.monotonic() - started) * 1000),
                        usage=usage, http_status=http_status)
        except Exception:
            pass


@server.tool()
def jev_decide(state: str, questions: dict) -> dict:
    """Ask Jev for focused structured decisions: classification, scoring or review.

    Send only necessary non-secret facts; data goes to the configured provider and
    its upstream model provider, and each call may cost money. questions maps IDs to objects with
    type noul, choice or score and instructions; choice uses a criteria object and
    score an ordered criteria array. Results are fallible advice, not permission,
    a substitute for evidence, or a command. Use when a second structured judgment
    is useful; do not automatically call on every task.
    """
    if not isinstance(state, str) or not state.strip() or not isinstance(questions, dict) or not questions:
        raise ValueError("state 和 questions 均不能为空。")
    for question in questions.values():
        if not isinstance(question, dict) or question.get("type") not in ("noul", "choice", "score") or not isinstance(question.get("instructions"), str) or not question["instructions"].strip():
            raise ValueError("每个问题必须包含有效的 type 和 instructions。")
        if question["type"] == "choice" and not isinstance(question.get("criteria"), dict):
            raise ValueError("choice 的 criteria 必须是对象。")
        if question["type"] == "score" and not isinstance(question.get("criteria"), list):
            raise ValueError("score 的 criteria 必须是数组。")
    return _request_jev(state, questions)


@server.tool()
def jev_route_advice(task: str, facts: list[str], purpose: str) -> dict:
    """Get optional routing advice from minimal, authorized non-sensitive facts.

    purpose is main or subagent. This sends task and facts to the configured Jev
    provider and may cost money. It does not switch models or spawn agents;
    verify suggestions against the user's actual rules, evidence and model guard.
    Use for ambiguous routing decisions, not every ordinary task.
    """
    from route_advice import advise_route
    with _operation("jev_route_advice"):
        return advise_route(task, facts, purpose, jev_decide)


@server.tool()
def jev_screen_frames(frames: list[dict], query: str, allow_external: bool = False) -> dict:
    """Rank selected still frames as preliminary candidates for a visual goal.

    Each frame contains an id, an absolute image path, and time_seconds. Only set
    allow_external when the user permits sending these selected images to the
    configured Jev provider and upstream. Images are downscaled in memory; image
    paths are not sent. This does not assess motion, sound or final edit quality.
    """
    from frame_candidates import judge_frames
    with _operation("jev_screen_frames"):
        return judge_frames(frames, query, lambda state, questions, assets: _request_jev(state, questions, assets),
                            allow_external=allow_external)


@server.tool()
def jev_select_evidence(root: str, paths: list[str], query: str, allow_remote: bool = False) -> dict:
    """Select exact line-numbered excerpts from explicitly named local text files.

    root is an absolute directory and paths are explicit relative files. Local
    selection is the default. Enable allow_remote only when existing user
    authorization permits sending the bounded candidate text and query to the
    configured Jev provider and its upstream. The output reports coverage limits,
    skips and fallback; a missing match never proves that evidence is absent.
    """
    from evidence_select import select_evidence
    with _operation("jev_select_evidence"):
        return select_evidence(root, paths, query, allow_remote=allow_remote, ask=jev_decide if allow_remote else None)


@server.tool()
def jev_rank_transcripts(candidates: list[dict], brief: str, allow_external: bool = False) -> dict:
    """Score explicitly supplied transcript candidates against an editing brief.

    Each item contains id, start_seconds, end_seconds and transcript. Only set
    allow_external when user authorization covers sending those excerpts to the
    configured Jev provider and upstream. Results are suggestions from text only;
    they do not establish visual continuity, sound quality or edit media.
    """
    if allow_external is not True:
        raise ValueError("仅在用户允许外发所选转写文字时设置 allow_external=true。")
    from video_candidates import rank_candidates
    with _operation("jev_rank_transcripts"):
        return rank_candidates(candidates, brief, jev_decide)


@server.tool()
def jev_usage_status(hours: int = 24) -> dict:
    """Read metadata-only local Jev request counts; this does not call any model.

    Counts start when recording was enabled. Successful API calls do not by
    themselves prove task value and may include diagnostics or tests.
    """
    return activity_status(hours)


if __name__ == "__main__":
    server.run(transport="stdio")
