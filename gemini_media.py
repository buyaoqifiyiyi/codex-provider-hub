"""Send an explicitly selected local audio or video file to a Gemini-native route."""
import base64
import json
from pathlib import Path
import re
import socket
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener


# Inline requests include base64 overhead. Larger files need a separately
# supported upload or clipping workflow rather than a silent quality reduction.
MAX_MEDIA_BYTES = 20 * 1024 * 1024
MEDIA_TYPES = {
    ".mp4": "video/mp4", ".mov": "video/quicktime", ".mpeg": "video/mpeg",
    ".mpg": "video/mpeg", ".avi": "video/avi", ".webm": "video/webm",
    ".3gp": "video/3gpp", ".wav": "audio/wav", ".mp3": "audio/mp3",
    ".aac": "audio/aac", ".m4a": "audio/m4a", ".ogg": "audio/ogg",
    ".flac": "audio/flac", ".aiff": "audio/aiff",
}


class _SameOriginRedirect(HTTPRedirectHandler):
    def redirect_request(self, request, fp, code, message, headers, new_url):
        old, new = urlsplit(request.full_url), urlsplit(new_url)
        if (old.scheme, old.hostname, old.port) != (new.scheme, new.hostname, new.port):
            raise HTTPError(request.full_url, code, "redirect refused", headers, fp)
        return super().redirect_request(request, fp, code, message, headers, new_url)


def inspect_media(profile, secret, model, file_path, question, *, transport=None):
    """Return a bounded response without exposing credentials or raw errors."""
    if profile.get("protocol") != "gemini_native":
        return {"ok": False, "code": "GEMINI_NATIVE_REQUIRED", "message": "请选择已启用的 Gemini 原生连接。"}
    if not isinstance(question, str) or not question.strip() or len(question) > 10000:
        return {"ok": False, "code": "QUESTION_INVALID", "message": "请提供要分析的问题。"}
    if not isinstance(model, str) or not re.fullmatch(r"[A-Za-z0-9_.:/~-]{1,160}", model):
        return {"ok": False, "code": "MODEL_INVALID", "message": "模型名称无效。"}
    try:
        path = Path(file_path)
        if not path.is_absolute() or not path.is_file():
            raise ValueError()
        mime_type = MEDIA_TYPES.get(path.suffix.lower())
        if not mime_type:
            return {"ok": False, "code": "MEDIA_FORMAT_UNSUPPORTED", "message": "请选择支持的视频或音频文件。"}
        if path.stat().st_size > MAX_MEDIA_BYTES:
            return {"ok": False, "code": "MEDIA_TOO_LARGE", "message": "文件超过 20 MB，请先选择较短片段或压缩后再分析。"}
        data = path.read_bytes()
        if not data or len(data) > MAX_MEDIA_BYTES:
            return {"ok": False, "code": "MEDIA_INVALID", "message": "文件为空或超过 20 MB。"}
    except (OSError, TypeError, ValueError):
        return {"ok": False, "code": "MEDIA_UNREADABLE", "message": "无法读取本机音视频文件。"}

    # Native profile URLs have already been validated and normalized on save.
    wire_model = model.removeprefix("models/")
    url = profile["base_url"].rstrip("/") + "/v1beta/models/" + quote(wire_model, safe="") + ":generateContent"
    body = {"contents": [{"role": "user", "parts": [
        {"text": question.strip()},
        {"inlineData": {"mimeType": mime_type, "data": base64.b64encode(data).decode("ascii")}},
    ]}], "generationConfig": {"maxOutputTokens": 4096}}
    request = Request(url, data=json.dumps(body, ensure_ascii=False).encode("utf-8"),
                      headers={"Content-Type": "application/json", "x-goog-api-key": secret}, method="POST")
    try:
        open_request = transport or build_opener(_SameOriginRedirect()).open
        with open_request(request, timeout=120) as response:
            result = json.load(response)
    except HTTPError as error:
        return {"ok": False, "code": "MEDIA_UPSTREAM_REJECTED", "http_status": error.code,
                "message": "服务商未接受音视频请求，请检查该线路和模型是否支持原生多模态。"}
    except (OSError, URLError, socket.timeout, ValueError):
        return {"ok": False, "code": "MEDIA_REQUEST_FAILED", "message": "音视频请求未完成，请稍后重试。"}

    try:
        candidate = result["candidates"][0]
        parts = candidate["content"]["parts"]
        text = "\n".join(part["text"] for part in parts if isinstance(part, dict)
                         and not part.get("thought") and isinstance(part.get("text"), str)).strip()
        reason = candidate.get("finishReason")
    except (KeyError, IndexError, TypeError):
        return {"ok": False, "code": "MEDIA_RESPONSE_INVALID", "message": "服务商没有返回可识别的分析结果。"}
    if reason != "STOP" or not text:
        return {"ok": False, "code": "MEDIA_RESPONSE_INCOMPLETE", "message": "音视频分析未完整结束。",
                "answer": text}
    return {"ok": True, "model": model, "connection": profile.get("name", ""),
            "media_type": mime_type, "answer": text}
