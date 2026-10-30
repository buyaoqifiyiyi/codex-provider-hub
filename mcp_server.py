"""Local-only Codex provider management MCP server."""
from pathlib import Path
from contextlib import closing
import base64
import hmac
import json
import os
import re
import subprocess
import threading
import time
import tomllib
import uuid
from urllib.parse import urlsplit

from mcp.server.mcpserver import MCPServer
from mcp.types import Icon, ToolAnnotations

from storage import MODEL_ADAPTERS, ProfileStore, PROTOCOLS, validate_provider_url, validate_model_settings, validate_models, merge_discovered_model_settings
import ocx_service
from probe import bounded_read as _bounded_read, ReadDeadline
from gemini_media import inspect_media, MEDIA_TYPES, MAX_MEDIA_BYTES
from capability_service import public_evidence, ensure_capability, _matching
from capability_probes import probe_capability, probe_gemini_snapshot
from public_evaluations import read_references
from jev_hub import JevCredentials, load_jev

ICONS = [Icon(src="data:image/png;base64," + base64.b64encode(
             (Path(__file__).parent / "assets" / name).read_bytes()).decode("ascii"),
              mimeType="image/png", sizes=["128x128"], theme=theme)
         for name, theme in (("provider-hub.png", "light"), ("provider-hub-dark.png", "dark"))]
server = MCPServer("codex-provider-hub", icons=ICONS)
_jev_tools = load_jev(JevCredentials(ProfileStore()))
for _jev_tool_name in ("jev_decide", "jev_route_advice", "jev_select_evidence",
                       "jev_rank_transcripts", "jev_screen_frames", "jev_usage_status"):
    server.add_tool(getattr(_jev_tools, _jev_tool_name), name=_jev_tool_name)
RESOURCE = "ui://codex-provider-hub/manager.html"
UI = Path(__file__).with_name("ui") / "manager.html"
_pretests = {}
_pretests_lock = threading.Lock()
SAMPLE_RECOMMEND_SECONDS = 45
MAX_AUTOMATIC_SAMPLE_CANDIDATES = 2


# The synchronous UI has a 120s timeout. Reserve time for compensating engine
# rollback; larger sets need a cancellable asynchronous job API, not late writes.
SAVE_READ_SECONDS = 15
SAVE_COMMIT_SECONDS = 100



def _remember_pretest(provider_id: str, revision: str) -> None:
    with _pretests_lock:
        now = time.monotonic()
        for key in list(_pretests):
            if _pretests[key][1] <= now:
                del _pretests[key]
        _pretests[provider_id] = (revision, now + 60)


def _consume_pretest(provider_id: str) -> str | None:
    with _pretests_lock:
        item = _pretests.pop(provider_id, None)
    return item[0] if item and item[1] > time.monotonic() else None


def _candidate(store, name, protocol, base_url, model, key, provider_id):
    if not isinstance(name, str) or not name.strip() or len(name) > 80:
        raise ValueError("供应商名称须为 1 至 80 个字符。")
    if protocol not in PROTOCOLS:
        raise ValueError("不支持该接口类型。")
    if not isinstance(model, str) or not re.fullmatch(r"[A-Za-z0-9_.:/~-]{1,160}", model):
        raise ValueError("模型名称无效。")
    if not isinstance(key, str) or len(key) > 8192 or any(c in key for c in "\r\n\x00"):
        raise ValueError("密钥格式无效。")
    fingerprint = store.edit_fingerprint(provider_id) if provider_id else None
    if provider_id and fingerprint is None:
        raise ValueError("要更新的供应商不存在。")
    if protocol == "codex_subscription":
        if key:
            raise ValueError("Codex 订阅登录使用现有账户，不需要 API 密钥。")
        return {"protocol": protocol, "base_url": "", "model": model}, "", fingerprint
    endpoint = validate_provider_url(base_url, protocol)
    previous = next((p for p in store.list() if p["id"] == provider_id), None) if provider_id else None
    if provider_id and previous is None:
        raise ValueError("要更新的供应商不存在。")
    if not key.strip() and previous:
        old, new = urlsplit(previous["base_url"]), urlsplit(endpoint)
        if previous["protocol"] != protocol or (old.scheme, old.hostname, old.port) != (new.scheme, new.hostname, new.port):
            raise ValueError("变更接口域名或类型时请重新输入密钥。")
        key = store.get_secret(provider_id)
    if not key.strip():
        raise ValueError("该接口需要密钥。")
    if provider_id and store.edit_fingerprint(provider_id) != fingerprint:
        raise ValueError("连接在读取期间发生变化，请重新操作。")
    return {"protocol": protocol, "base_url": endpoint, "model": model}, key.strip(), fingerprint


def _snapshot():
    store = ProfileStore()
    with closing(store._connect()) as db:
        pending_cleanup = db.execute("SELECT count(*) FROM pending_credential_cleanup").fetchone()[0]
    active_models = [binding["id"] for alias in store.engine_aliases() if (binding := store.get_engine_binding(alias))]
    service = ocx_service.status()
    configured = False
    window_override = compact_override = None
    config_inspection_available = False
    try:
        home = Path(os.environ.get("CODEX_HOME") or Path.home() / ".codex")
        config = tomllib.loads((home / "config.toml").read_text(encoding="utf-8-sig"))
        config_inspection_available = True
        window_override = config.get("model_context_window")
        compact_override = config.get("model_auto_compact_token_limit")
        selected_profile = config.get("profiles", {}).get(config.get("profile"), {})
        if isinstance(selected_profile, dict):
            window_override = selected_profile.get("model_context_window", window_override)
            compact_override = selected_profile.get("model_auto_compact_token_limit", compact_override)
        route = config.get("model_providers", {}).get("custom", {})
        headers = route.get("http_headers", {})
        candidate = next((value for key, value in headers.items() if key.lower() == "x-provider-hub-token"), "")
        token = ocx_service._read_capability(store.root)
        configured = bool(service.get("running") and config.get("model_provider") == "custom"
                          and route.get("base_url") == f"http://127.0.0.1:{service['port']}/v1"
                          and isinstance(candidate, str) and candidate.isascii() and hmac.compare_digest(candidate, token))
    except (OSError, ValueError, TypeError, KeyError, AttributeError):
        pass
    profiles = store.list()
    if configured and service.get("official_route_ready"):
        active_models.extend(item["id"] for item in profiles
                             if item["protocol"] == "codex_subscription" and not item["disabled"])
    return {"profiles": profiles, "active_models": active_models, "active_jev": store.get_jev_profile_id(),
            "service": service, "routing_active": configured, "pending_credential_cleanup": pending_cleanup,
            "global_context_override": window_override, "global_compact_override": compact_override,
            "config_inspection_available": config_inspection_available}


@server.resource(RESOURCE, mime_type="text/html;profile=mcp-app", name="provider-manager")
def manager_ui() -> str:
    return UI.read_text(encoding="utf-8")


@server.tool(name="open_provider_manager", title="模型连接",
             meta={"ui": {"resourceUri": RESOURCE},
                   "openai/ui": {"entrypoints": [{"type": "global"}, {"type": "thread"}]}},
             annotations=ToolAnnotations(readOnlyHint=True), icons=ICONS)
def open_provider_manager() -> dict:
    """Open the local provider manager. Never ask the user to paste a secret into chat."""
    return _snapshot()


@server.tool(name="list_providers", title="查看模型连接", annotations=ToolAnnotations(readOnlyHint=True))
def list_providers() -> dict:
    """List local profile metadata and whether a secret is saved; never returns secret values."""
    return _snapshot()


@server.tool(name="analyze_gemini_media", title="用 Gemini 分析视频或音频")
def analyze_gemini_media(file_path: str, question: str, connection_name: str = "", model: str = "",
                         media_purpose: str = "auto") -> dict:
    """Analyze a user-requested local video or audio with an enabled Gemini-native connection.

    The selected file is sent to that connection's provider and may incur API usage.
    If several Gemini-native connections are enabled, specify the connection name.
    media_purpose is video, speech, audio (non-speech sound), or auto. The first
    actual use may also send a tiny synthetic sample to the same provider; its
    result is cached by model and route, and is not a comprehensive evaluation.
    """
    try:
        store = ProfileStore()
        profiles = [p for p in store.list() if p.get("protocol") == "gemini_native" and not p.get("disabled")]
        if connection_name:
            profiles = [p for p in profiles if p.get("name") == connection_name]
        if not profiles:
            return {"ok": False, "code": "GEMINI_CONNECTION_MISSING", "message": "请先启用 Gemini 原生连接。"}
        if len(profiles) != 1:
            return {"ok": False, "code": "GEMINI_CONNECTION_AMBIGUOUS", "message": "有多个 Gemini 原生连接，请指定连接名称。"}
        provider_id = profiles[0]["id"]
        selected_model = model or profiles[0]["model"]
        if selected_model not in profiles[0].get("models", []):
            return {"ok": False, "code": "GEMINI_MODEL_UNAVAILABLE", "message": "该连接中没有所选模型。"}
        try:
            profile, secret, target_identity = store.resolve_media_capability(provider_id, selected_model)
        except ValueError:
            return {"ok": False, "code": "GEMINI_CONNECTION_UNAVAILABLE", "message": "连接已停用或不可用。"}
        if profile.get("protocol") != "gemini_native" or selected_model not in profile.get("models", []):
            return {"ok": False, "code": "GEMINI_MODEL_UNAVAILABLE", "message": "该连接中没有所选模型。"}
        try:
            store.capability_identity(provider_id, selected_model, None, channel='gemini_media',
                                      expected_identity=target_identity)
        except ValueError:
            return {"ok": False, "code": "GEMINI_CONNECTION_CHANGED", "message": "连接刚刚发生变化，请重新选择。"}
        if media_purpose not in {'auto','video','speech','audio'}:
            return {"ok": False, "code": "MEDIA_PURPOSE_INVALID", "message": "请选择视频、语音或非语音声音用途。"}
        purpose = _media_purpose(file_path, question, media_purpose)
        sample = None
        if purpose:
            sample = _start_media_sample(store, profile, secret, selected_model, purpose, target_identity)
        try:
            store.capability_identity(provider_id, selected_model, None, channel='gemini_media',
                                      expected_identity=target_identity)
        except ValueError:
            return {"ok": False, "code": "GEMINI_CONNECTION_CHANGED", "message": "连接刚刚发生变化，请重新选择。"}
        if store.is_disabled(provider_id):
            return {"ok": False, "code": "GEMINI_CONNECTION_UNAVAILABLE", "message": "连接已停用或不可用。"}
        result = inspect_media(profile, secret, selected_model, file_path, question)
        if sample:
            result['capability_sample'] = sample
        return result
    except Exception:
        # Upstream and storage errors may contain credential material.
        return {"ok": False, "code": "GEMINI_MEDIA_FAILED", "message": "无法读取连接或完成媒体分析。"}


def _scheduling(action: str, payload: dict, *, timeout: float = 30) -> dict:
    """Run the shared scheduling implementation, without passing credentials."""
    try:
        # An MCP server may be shared across chats. Its process environment is
        # not proof of which chat invoked this request; only the spawn hook can
        # authenticate the actual caller and its current route.
        env = os.environ.copy()
        if action == 'recommend':
            env.pop('CODEX_THREAD_ID', None)
            env.pop('CODEX_SESSION_ID', None)
        result = subprocess.run(['node', str(Path(__file__).parent / 'scripts' / 'scheduling.cjs'), action],
                                input=json.dumps(payload, ensure_ascii=False), capture_output=True, text=True,
                                encoding='utf-8', timeout=max(0.01, timeout), check=True,
                                env=env,
                                creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
        value = json.loads(result.stdout)
        return value if isinstance(value, dict) else {'ok':False, 'error':'调度结果格式不正确。'}
    except (OSError, ValueError, subprocess.SubprocessError):
        return {'ok':False, 'error':'无法读取本机调度状态，请检查模型目录及连接数据库。'}


def _guard_status() -> dict:
    from guard_verification import check_hooks, query_host_hooks
    home=Path(os.environ.get('CODEX_HOME') or Path.home()/'.codex')
    try:
        version=json.loads((Path(__file__).parent/'.codex-plugin'/'plugin.json').read_text(encoding='utf-8'))['version']
        installed=home/'plugins'/'cache'/'codex-provider-hub-local'/'codex-provider-hub'/version
        exists=(installed/'hooks'/'hooks.json').is_file()
        trusted=None
        if exists:
            inventory=json.loads((ProfileStore().root/'guard-inventory.json').read_text(encoding='utf-8'))
            trusted=check_hooks(query_host_hooks(home,Path.cwd()),installed,inventory)
        return {'installed':exists,'trusted':trusted,'session_coverage':'unknown'}
    except Exception:
        return {'installed':False if 'exists' not in locals() else exists,
                'trusted':None,'session_coverage':'unknown'}


def _public_evidence(store, provider_id: str, model: str) -> list:
    """Expose outcomes and dates, never the credential-derived identity."""
    return public_evidence(store,provider_id,model)


def _media_purpose(file_path: str, question: str, purpose: str) -> str | None:
    """Do not test a route for a malformed or unrequested media file."""
    try:
        file = Path(file_path)
        mime = MEDIA_TYPES.get(file.suffix.lower())
        if (not file.is_absolute() or not file.is_file() or not mime or
                not 0 < file.stat().st_size <= MAX_MEDIA_BYTES or
                not isinstance(question, str) or not question.strip() or len(question) > 10000):
            return None
    except (TypeError, ValueError, OSError):
        return None
    if purpose != 'auto':
        return purpose
    if mime.startswith('video/'):
        return 'video'
    return 'speech' if re.search(r'转写|转录|字幕|台词|说了|讲话|说话|语音|speech|transcri', question, re.I) else 'audio'


def _start_media_sample(store, profile: dict, secret: str, model: str, purpose: str,
                        target_identity: dict) -> dict:
    try:
        provider_id = profile['id']
        store.capability_identity(provider_id, model, None, channel='gemini_media',
                                  expected_identity=target_identity)
        cached = _matching(store, provider_id, model, purpose, 'gemini_media', None, int(time.time()), target_identity)
        if cached:
            store.capability_identity(provider_id, model, None, channel='gemini_media',
                                      expected_identity=target_identity)
            return {'purpose':purpose, 'status':cached['status'], 'cached':True}
        root = store.root
        # Retain only the chosen target for the background request. A later
        # edit must not move the test to another address or credential.
        selected_profile = dict(profile)
        def run():
            try:
                worker_store = ProfileStore(root)
                ensure_capability(worker_store, provider_id, model, purpose, 'gemini_media', None,
                    lambda:probe_gemini_snapshot(worker_store, selected_profile, secret, model, purpose),
                    expected_identity=target_identity)
            except Exception:
                pass  # Never log a provider error or credential from a background check.
        threading.Thread(target=run, name='provider-hub-media-sample', daemon=True).start()
        return {'purpose':purpose, 'status':'testing', 'cached':False}
    except Exception:
        return {'purpose':purpose, 'status':'unknown'}


def _route_evidence(store) -> dict:
    result = {}
    for alias in store.engine_aliases():
        binding = store.get_engine_binding(alias)
        if binding and not store.is_disabled(binding['id']):
            result[alias] = _public_evidence(store, binding['id'], binding['model'])
    return result


def _public_route_references(store, only=None) -> tuple[dict, bool]:
    """Look up public model names only; never send a connection or key."""
    if os.environ.get('HF_HUB_OFFLINE', '').lower() in {'1','true','yes','on'}:
        return {}, False
    result, pending = {}, False
    for alias in store.engine_aliases():
        if only is not None and alias not in only:
            continue
        binding = store.get_engine_binding(alias)
        if not binding or store.is_disabled(binding['id']):
            continue
        try:
            records, waiting = read_references(store.root, [binding['model']])
            result[alias] = records.get(binding['model'], {'status':'missing'})
            pending |= waiting
        except (ValueError, OSError):
            # Some provider IDs are not valid Hugging Face repository names.
            result[alias] = {'status':'missing'}
    return result, pending


@server.tool(name='get_public_model_references', title='查看公开模型评测',
             meta={'ui':{'visibility':['app']}}, annotations=ToolAnnotations(readOnlyHint=False))
def get_public_model_references() -> dict:
    try:
        refs, pending = _public_route_references(ProfileStore())
        return {'ok':True, 'references':refs, 'pending':pending}
    except Exception:
        return {'ok':False, 'references':{}, 'pending':False}


@server.tool(name='get_scheduling_overview', title='查看调度规则', meta={'ui':{'visibility':['app']}},
             annotations=ToolAnnotations(readOnlyHint=False))
def get_scheduling_overview() -> dict:
    try:
        store=ProfileStore()
        profiles=store.list()
        safe_profiles=[{'id':p['id'], 'name':p['name'], 'protocol':p['protocol'],
                        'models':p['models'], 'disabled':p['disabled']} for p in profiles]
        evidence = _route_evidence(store)
        references, pending = _public_route_references(store)
        result=_scheduling('overview',{'providers':safe_profiles,'evidence':evidence,'references':references})
        if result.get('ok'):
            for candidate in result.get('candidates', []):
                candidate['evidence'] = evidence.get(candidate.get('model'), [])
                candidate['public_reference'] = references.get(candidate.get('model'), {'status':'missing'})
            result['public_reference_pending'] = pending
            result['guard']=_guard_status()
        return result
    except Exception:
        return {'ok':False,'error':'无法读取本机调度状态，请检查连接数据库。'}


@server.tool(name='save_scheduling_preferences', title='保存调度偏好', meta={'ui':{'visibility':['app']}})
def save_scheduling_preferences(preferences: dict | None = None, mode: str = 'current') -> dict:
    if mode in {'auto','manual'} and preferences is None:
        return _scheduling('mode', {'mode':mode})
    if mode not in {'current','manual'} or preferences is None:
        return {'ok':False,'error':'调度模式或手动偏好无效。'}
    return _scheduling('save', {'preferences':preferences,'mode':mode})


def _recommend_subagent_model(task_type: str, requested_model: str = '', requested_effort: str = '',
                              purpose: str = 'text') -> dict:
    """Recommend for bounded, boundary or critical work and a specific purpose.

    purpose is text, tool, image, video, speech, or audio (non-speech sound).
    The first tool/image use can send one small synthetic test via the chosen
    route and incur usage. Video and audio are handled by analyze_gemini_media,
    not assumed to be deliverable to a subagent. A user-specified model is
    never silently replaced. This is advice; the spawn guard remains authoritative.
    """
    if bool(requested_model) != bool(requested_effort):
        return {'ok':False,'error':'明确指定模型时必须同时指定思考强度。'}
    if task_type not in {'bounded','boundary','critical'} or purpose not in {'text','tool','image','video','speech','audio'}:
        return {'ok':False,'code':'TASK_OR_PURPOSE_INVALID','reason':'请选择有效的任务类型和用途。'}
    try:
        store = ProfileStore()
        if purpose in {'video','speech','audio'}:
            # The current host has no video subagent attachment and its audio
            # path is distinct from the native Gemini media tool.
            profiles = [p for p in store.list() if p['protocol'] == 'gemini_native' and not p['disabled']]
            if requested_model:
                binding = store.get_engine_binding(requested_model)
                profiles = [p for p in profiles if (binding and binding['id'] == p['id']) or requested_model in p['models']]
            if not profiles:
                return {'ok':False,'code':'GEMINI_CONNECTION_MISSING',
                        'reason':'音视频分析需要已启用的 Gemini 原生连接。'}
            if len(profiles) != 1:
                return {'ok':False,'code':'GEMINI_CONNECTION_AMBIGUOUS',
                        'reason':'有多个 Gemini 原生连接，请指定连接。'}
            profile = profiles[0]
            chosen = (binding['model'] if requested_model and binding and binding['id'] == profile['id']
                      else requested_model if requested_model in profile['models'] else profile['model'])
            return {'ok':False,'code':'MEDIA_TOOL_REQUIRED','purpose':purpose,
                    'reason':'音视频请调用 Gemini 媒体分析工具；这条路径不代表子代理已接收音视频。',
                    'tool':'analyze_gemini_media','connection':profile['name'],'model':chosen,
                    'evidence':_public_evidence(store, profile['id'], chosen)}
        requested={'model':requested_model,'effort':requested_effort} if requested_model else None
        references, _ = _public_route_references(store)
        payload={'task':task_type,'requested':requested,'purpose':purpose,
                 'evidence':_route_evidence(store),'references':references}
        deadline = time.monotonic() + SAMPLE_RECOMMEND_SECONDS
        result = _scheduling('recommend', payload, timeout=min(30, max(0.01, deadline-time.monotonic())))
        if purpose not in {'tool','image'} or not result.get('ok'):
            return result
        seen = set()
        previous = []
        for attempt in range(MAX_AUTOMATIC_SAMPLE_CANDIDATES):
            alias, effort = result.get('model'), result.get('effort')
            if (alias, effort) in seen or deadline - time.monotonic() < 1:
                return {**result, 'ok':False, 'code':'PURPOSE_SAMPLE_PENDING', 'purpose_status':'pending_test',
                        'reason':'用途样例尚未完成，请稍后再检查。', 'previous_samples':previous}
            seen.add((alias, effort))
            binding = store.get_engine_binding(alias) if isinstance(alias, str) else None
            if not binding:
                # Official subscription models have a separate route; the
                # third-party gateway probe cannot test it on their behalf.
                if isinstance(alias, str) and alias not in store.known_engine_aliases():
                    return {**result, 'purpose_status':'not_tested', 'sample_note':'此线路的用途样例未测试。',
                            'previous_samples':previous}
                return {**result, 'ok':False, 'code':'PURPOSE_SAMPLE_PENDING', 'purpose_status':'pending_test',
                        'reason':'备用模型的用途尚未验证。', 'previous_samples':previous}
            if store.is_disabled(binding['id']):
                return {**result, 'ok':False, 'code':'MODEL_DISABLED', 'reason':'连接已停用。'}
            sample = ensure_capability(store, binding['id'], binding['model'], purpose, 'subagent_gateway', effort,
                        lambda:probe_capability(store, alias, purpose, effort, deadline=deadline,
                                                expected_binding=binding), alias=alias)
            result['capability_sample'] = sample
            result['purpose_status'] = {'passed':'sample_passed','failed':'sample_failed',
                                        'error':'test_error'}.get(sample.get('status'),'pending_test')
            if sample.get('status') == 'passed':
                if previous: result['previous_samples'] = previous
                return result
            previous.append({'model':alias, 'effort':effort, 'status':sample.get('status'),
                             'error_code':sample.get('error_code')})
            if requested or sample.get('status') not in {'failed','error'} or attempt + 1 >= MAX_AUTOMATIC_SAMPLE_CANDIDATES:
                return {**result, 'ok':False, 'code':'PURPOSE_SAMPLE_UNVERIFIED',
                        'reason':'用途样例未通过或尚未完成；不会将其标记为已验证。', 'previous_samples':previous}
            if deadline-time.monotonic() < 1:
                return {**result, 'ok':False, 'code':'PURPOSE_SAMPLE_PENDING',
                        'reason':'本次用途测试时间已用完，请稍后再检查。', 'previous_samples':previous}
            payload['evidence'] = _route_evidence(store)
            payload['excluded'] = [{'model':model, 'effort':level} for model,level in seen]
            result = _scheduling('recommend', payload, timeout=min(30, max(0.01, deadline-time.monotonic())))
            if not result.get('ok'):
                return {**result, 'previous_samples':previous}
    except Exception:
        return {'ok':False,'code':'SCHEDULING_UNAVAILABLE','reason':'无法读取当前连接或完成用途检查。'}


@server.tool(name='recommend_subagent_model', title='按当前线路推荐子代理模型',
             annotations=ToolAnnotations(readOnlyHint=False))
def recommend_subagent_model(task_type: str, requested_model: str = '', requested_effort: str = '',
                             purpose: str = 'text') -> dict:
    """Recommend by task and purpose; public evaluations are optional references.

    task_type is bounded, boundary, or critical; purpose is text, tool, image,
    video, speech, or audio. Enabled models can participate in critical tasks.
    The first tool/image sample may incur usage. Video and audio use the
    separate Gemini media tool; dispatch and security checks remain authoritative.
    Public scores match names only and do not verify an active provider's model.
    """
    result = _recommend_subagent_model(task_type, requested_model, requested_effort, purpose)
    if isinstance(result, dict) and isinstance(result.get('model'), str):
        try:
            references, pending = _public_route_references(ProfileStore(), {result['model']})
            if result['model'] in references:
                result['public_reference'] = references[result['model']]
                result['public_reference_pending'] = pending
        except Exception:
            # Reference lookup can never block a recommendation.
            pass
    return result


@server.tool(name="fetch_models", title="获取模型列表", meta={"ui": {"visibility": ["app"]}})
def fetch_models(protocol: str, base_url: str = "", key: str = "", provider_id: str = "") -> dict:
    """Discover models for the selected connection without saving it."""
    try:
        store = ProfileStore()
        existing = next((item for item in store.list() if item["id"] == provider_id), None) if provider_id else None
        if provider_id and existing is None:
            return {"ok": False, "message": "连接不存在，请重新选择。"}
        if existing and existing["disabled"]:
            return {"ok": False, "message": "连接已停用，未查询模型列表。"}
        route, secret, _ = _candidate(store, "模型列表", protocol, base_url, "model-discovery", key, provider_id)
        if protocol == "codex_subscription":
            import json
            from native_catalog import native_model_ids
            document = json.loads((store.root / "baseline-catalog.json").read_text(encoding="utf-8"))
            rows = document.get("models") if isinstance(document, dict) else None
            if not isinstance(rows, list):
                raise ValueError("本机订阅模型目录不可用。")
            seen, models = set(), []
            aliases = store.known_engine_aliases()
            native = (native_model_ids(root=store.root, excluded_aliases=aliases)
                      | store.enabled_subscription_models()) - aliases
            for row in rows:
                model = row.get("slug") if isinstance(row, dict) else None
                if (isinstance(model, str) and model in native
                        and row.get("visibility") != "hide" and model not in seen):
                    seen.add(model)
                    models.append({"id": model})
            return {"ok": True, "models": models, "source": "本机订阅模型目录"} if models else {
                "ok": False, "message": "本机订阅模型目录为空，可手动填写。"}
        endpoint = route["base_url"]
        if protocol == "system_one":
            if not endpoint.endswith("/systemone"):
                return {"ok": False, "message": "该专用接口未提供可识别的模型列表地址，可手动填写。"}
            endpoint = endpoint[:-len("/systemone")]
        from probe import fetch_model_ids
        result = (fetch_model_ids(endpoint, secret, protocol=protocol) if protocol in {"anthropic_messages", "gemini_native"}
                  else fetch_model_ids(endpoint, secret))
        if result.get("ok"):
            return {"ok": True, "models": result["models"], "source": "服务商模型列表",
                    "truncated": bool(result.get("truncated"))}
        status = result.get("http_status")
        if status in (401, 403):
            message = "模型列表认证失败，请检查密钥；也可手动填写。"
        elif status in (404, 405) or result.get("code") in {"model_list_empty", "model_list_unsupported"}:
            message = "服务商未提供可用的模型列表，可手动填写。"
        else:
            message = "无法获取模型列表，请检查线路或手动填写。"
        return {"ok": False, "message": message}
    except ValueError as error:
        return {"ok": False, "message": str(error)}
    except Exception:
        return {"ok": False, "message": "无法获取模型列表，请稍后重试或手动填写。"}


@server.tool(name="save_provider", title="保存模型连接", meta={"ui": {"visibility": ["app"]}})
def save_provider(name: str, protocol: str, base_url: str, model: str,
                  key: str = "", provider_id: str = "", context_window: int | None = None,
                  compact_limit: int | None = None, reasoning_levels: list[str] | None = None,
                  default_effort: str | None = None, models: list[str] | None = None,
                  per_model_overrides: dict[str, dict] | None = None) -> dict:
    """Test new models before committing; previously saved models are not retested."""
    try:
        started=time.monotonic()
        read_deadline=started+SAVE_READ_SECONDS
        commit_deadline=started+SAVE_COMMIT_SECONDS
        selected=validate_models(model,models,protocol)
        if len(selected)>1:
            return start_provider_save(name,protocol,base_url,model,client_job_id=str(uuid.uuid4()),models=selected,key=key,provider_id=provider_id,
                                       context_window=context_window,compact_limit=compact_limit,reasoning_levels=reasoning_levels,
                                       default_effort=default_effort,per_model_overrides=per_model_overrides)
        overrides={} if per_model_overrides is None else per_model_overrides
        fields={"context_window","compact_limit","reasoning_levels","default_effort"}
        if (not isinstance(overrides,dict) or any(m not in selected or not isinstance(v,dict) or set(v)-fields for m,v in overrides.items())):
            raise ValueError("逐模型设置仅允许所选模型及 context_window/compact_limit/reasoning_levels/default_effort。")
        store=ProfileStore()
        from engine_control import activation_lock
        with activation_lock(store.root):
            from probe import probe_snapshot,probe_jev,probe_official,discover_capabilities,official_capabilities,describe_probe_failure
            route,test_key,fingerprint=_candidate(store,name,protocol,base_url,model,key,provider_id)
            if protocol in MODEL_ADAPTERS:
                from engine_control import public_aliases
                public_aliases(store,provider_id or "00000000-0000-0000-0000-000000000000",[model])
            manual=validate_model_settings(model,context_window,compact_limit,reasoning_levels,default_effort)
            settings_by_model,detected_by_model={},{}
            configured={}
            previous=store.all_model_settings(provider_id)
            previous_models=set(store.model_ids(provider_id)) if provider_id else set()
            prior_profile=next((p for p in store.list() if p["id"]==provider_id),None) if provider_id else None
            credentials_changed=bool(prior_profile and protocol!="codex_subscription" and
                (route["protocol"]!=prior_profile["protocol"] or route["base_url"]!=prior_profile["base_url"] or key.strip()))
            tested_models=[]
            for current in selected:
                base=dict(manual) if current == model else dict(previous.get(current,store._settings(None)))
                base.update(overrides.get(current,{}))
                configured[current]=validate_model_settings(current,**base)
            with store.connection() as db:
                active_count=db.execute("SELECT count(*) FROM engine_bindings WHERE provider_id=?",(provider_id,)).fetchone()[0]
            if active_count>1:
                return start_provider_save(name,protocol,base_url,model,client_job_id=str(uuid.uuid4()),models=selected,key=key,provider_id=provider_id,
                                           context_window=context_window,compact_limit=compact_limit,reasoning_levels=reasoning_levels,
                                           default_effort=default_effort,per_model_overrides=per_model_overrides)
            for current in selected:
                if current in previous_models:
                    settings_by_model[current]=configured[current]
                    detected_by_model[current]={}
                    continue
                candidate=dict(route,model=current)
                if protocol == "system_one":
                    check=_bounded_read(lambda c=dict(candidate):probe_jev(c,test_key),read_deadline)
                elif protocol == "codex_subscription":
                    check=_bounded_read(lambda m=current:probe_official(m,store.root),read_deadline)
                else:
                    check=_bounded_read(lambda c=dict(candidate):probe_snapshot(c,test_key,None),read_deadline)
                if not check.get("ok"):
                    result={"ok":False,"code":"probe_failed","failed_model":current,"detail":check.get("code","probe_failed"),
                            "message":current+"："+describe_probe_failure(check)+"未保存或修改连接。"}
                    for field in ("http_status", "upstream_error_type", "request_id"):
                        if field in check: result[field]=check[field]
                    return result
                tested_models.append(current)
                detected=_bounded_read(lambda c=dict(candidate),m=current:(official_capabilities(store.root,m) if protocol == "codex_subscription" else
                          discover_capabilities(c,test_key) if protocol in MODEL_ADAPTERS else {}),read_deadline)
                # Only the first model receives the legacy form's advanced overrides.
                settings=dict(configured[current])
                settings_by_model[current]=merge_discovered_model_settings(current,settings,detected)
                detected_by_model[current]=detected
            expected=(provider_id,fingerprint) if provider_id else None
            # Active edits keep the same visible connection ID. Failure rolls back
            # both its SQLite version and all staged engine changes.
            with store.connection() as db:
                active=bool(provider_id and db.execute("SELECT 1 FROM engine_bindings WHERE provider_id=?",(provider_id,)).fetchone())
            target_id=provider_id or None
            if time.monotonic() >= read_deadline:
                raise ValueError("整组测试超时，未保存或修改连接。")
            with store.atomic():
                profile=store.save(name,protocol,base_url,model,key,target_id,
                                   models=selected,per_model_settings=settings_by_model,expected_existing=expected)
                for current in tested_models:
                    effort = None
                    store.record_capability_evidence(profile['id'], current, 'connection', 'passed', effort,
                                                     channel='connection')
                if active:
                    from engine_control import activate_provider,EngineManager,keyring as engine_keyring
                    from catalog import refresh
                    activated=activate_provider(store,profile["id"],EngineManager(store.root),
                                                lambda action,account,value:engine_keyring(action,account,value,store.root),
                                                publish=lambda:refresh(store.root,store=store),
                                                forward_deadline=min(time.monotonic()+20,started+40))
                    if not activated.get("ok"):
                        raise ValueError("整组启用失败（"+activated.get("code","activation_failed")+"），未保存修改。"+
                                         ("引擎回滚未能完全确认，请检查线路。" if not activated.get("rollback_ok",True) else ""))
                result={"ok":True,"profile":profile,"detected_models":detected_by_model,"tested_models":tested_models}
                if active: result["activation"]=activated
                if protocol == "codex_subscription":
                    from catalog import refresh
                    from catalog import transactional_publish
                    transactional_publish(store,lambda:refresh(store.root,store=store))
                    result["catalog_updated"]=True
                if time.monotonic() >= commit_deadline:
                    raise ValueError("保存时限已到，未提交连接修改。")
            if protocol in MODEL_ADAPTERS and set(tested_models)==set(selected):
                saved,_=store.resolve_profile(profile["id"])
                _remember_pretest(profile["id"],saved["_revision"])
            if protocol != "system_one":
                detected=detected_by_model[model]
                origin=("未查询" if model not in tested_models else
                        "本机现有模型目录" if protocol == "codex_subscription" else "服务商模型目录")
                result["detected"]={**detected,"source":origin}
                result["warnings"]=[]
                if tested_models and "context_window" not in detected: result["warnings"].append("未获取到上下文窗口；请以服务商信息为准并按需设置。")
                if tested_models and "reasoning_levels" not in detected: result["warnings"].append("未获取到思考档位；请以服务商信息为准并按需设置。")
                if credentials_changed and model in previous_models and model not in tested_models:
                    result["warnings"].append("地址或密钥已变更，已有模型未重新测试。")
            return result
    except ReadDeadline as error:
        return {"ok":False,"code":"save_deadline","failed_model":current,"message":current+"："+str(error)}
    except ValueError as error:
        return {"ok":False,"message":str(error)}
    except Exception:
        return {"ok":False,"message":"保存失败，请检查本机存储状态。"}


_save_jobs_manager = None
_save_jobs_manager_lock = threading.Lock()


def _save_jobs():
    global _save_jobs_manager
    with _save_jobs_manager_lock:
        if _save_jobs_manager is None:
            from save_jobs import SaveJobs
            _save_jobs_manager=SaveJobs(store_factory=lambda:ProfileStore(),candidate_fn=_candidate)
        return _save_jobs_manager


@server.tool(name="start_provider_save",title="开始整组测试，等待确认提交",meta={"ui":{"visibility":["app"]}})
def start_provider_save(name: str, protocol: str, base_url: str, model: str, client_job_id: str,
                        models: list[str] | None = None, key: str = "", provider_id: str = "",
                        context_window: int | None = None, compact_limit: int | None = None,
                        reasoning_levels: list[str] | None = None, default_effort: str | None = None,
                        per_model_overrides: dict[str,dict] | None = None, retest_existing: bool = False) -> dict:
    """Start a cancellable app-only job. Secrets never appear in job status."""
    try:
        return _save_jobs().start(client_job_id=client_job_id,name=name,protocol=protocol,base_url=base_url,model=model,models=models,
                                  key=key,provider_id=provider_id,context_window=context_window,compact_limit=compact_limit,
                                  reasoning_levels=reasoning_levels,default_effort=default_effort,per_model_overrides=per_model_overrides,
                                  retest_existing=retest_existing)
    except ValueError as error:
        return {"ok":False,"message":str(error),**({"code":"model_alias_invalid","failed_model":error.model} if hasattr(error,"model") else {})}
    except Exception: return {"ok":False,"message":"无法启动保存任务。"}


@server.tool(name="get_provider_save_job",title="查看整组保存进度",meta={"ui":{"visibility":["app"]}})
def get_provider_save_job(job_id: str) -> dict:
    try: return _save_jobs().status(job_id)
    except Exception: return {"ok":False,"message":"无法读取任务进度。"}


@server.tool(name="commit_provider_save_job",title="确认提交整组连接",meta={"ui":{"visibility":["app"]}})
def commit_provider_save_job(job_id: str) -> dict:
    """Authorize a ready job once. On a lost reply, query/retry the same UUID."""
    try: return _save_jobs().commit(job_id)
    except Exception: return {"ok":False,"message":"未能确认提交，请用同一任务 ID 查询状态，勿新建任务重试。"}


@server.tool(name="cancel_provider_save_job",title="取消整组保存",meta={"ui":{"visibility":["app"]}})
def cancel_provider_save_job(job_id: str) -> dict:
    try: return _save_jobs().cancel(job_id)
    except Exception: return {"ok":False,"message":"未能确认取消，请查询任务状态。"}


@server.tool(name="activate_connection", title="启用模型连接", meta={"ui": {"visibility": ["app"]}})
def activate_connection(provider_id: str) -> dict:
    """Activate one saved connection without returning credentials."""
    try:
        store = ProfileStore()
        profile = next((item for item in store.list() if item["id"] == provider_id), None)
        if not profile:
            return {"ok": False, "code": "profile_missing"}
        if len(profile.get("models",[profile["model"]]))>1:
            return start_provider_save(profile["name"],profile["protocol"],profile["base_url"],profile["model"],
                                       client_job_id=str(uuid.uuid4()),models=profile["models"],provider_id=provider_id,
                                       per_model_overrides=profile.get("model_settings"),retest_existing=True)
        if profile["protocol"] == "system_one":
            from engine_control import activation_lock
            with activation_lock(store.root):
                store.set_jev_profile(provider_id)
            return {"ok": True, "kind": "jev"}
        if profile["protocol"] == "codex_subscription":
            from engine_control import activation_lock
            try:
                with activation_lock(store.root),store.atomic():
                    store.enable_connection(provider_id)
                    from catalog import refresh
                    from catalog import transactional_publish
                    transactional_publish(store,lambda:refresh(store.root,store=store))
                return {"ok":True,"kind":"subscription","catalog_updated":True}
            except Exception:
                return {"ok":False,"kind":"subscription","catalog_updated":False,"code":"catalog_update_failed"}
        if profile["protocol"] in MODEL_ADAPTERS:
            from engine_control import activate_local_provider
            return activate_local_provider(provider_id, pretested_revision=_consume_pretest(provider_id))
        return {"ok": False, "code": "model_not_supported"}
    except Exception:
        return {"ok": False, "code": "activation_failed"}


@server.tool(name="disable_connection", title="停用模型连接", meta={"ui": {"visibility": ["app"]}})
def disable_connection(provider_id: str) -> dict:
    """Pause a connection without checking its upstream availability."""
    try:
        from engine_control import activation_lock, disable_provider, EngineManager
        store = ProfileStore()
        with activation_lock(store.root):
            result = disable_provider(store, provider_id, EngineManager(store.root))
            if result.get("ok"):
                try:
                    from catalog import refresh
                    refresh(store.root)
                    result["catalog_updated"] = True
                except Exception:
                    result["catalog_updated"] = False
            return result
    except ValueError as error:
        return {"ok": False, "message": str(error)}
    except Exception:
        return {"ok": False, "message": "停用未完成，请检查本机服务状态。"}


@server.tool(name="delete_provider", title="删除模型连接", meta={"ui": {"visibility": ["app"]}})
def delete_provider(provider_id: str, confirm_active: bool = False) -> dict:
    """Delete one connection after the app confirms any active-route impact."""
    try:
        from engine_control import activation_lock, delete_provider as delete_local, EngineManager, keyring
        store = ProfileStore()
        with activation_lock(store.root):
            result = delete_local(store, provider_id, confirm_active, EngineManager(store.root),
                                  lambda action, account, secret: keyring(action, account, secret, store.root))
            if result.get("ok") and (result.get("active_removed") or result.get("official_settings_removed")):
                try:
                    from catalog import refresh
                    refresh(store.root)
                    result["catalog_updated"] = True
                except Exception:
                    result["catalog_updated"] = False
            return result
    except ValueError as error:
        return {"ok": False, "message": str(error)}
    except Exception:
        return {"ok": False, "message": "删除未完成，请检查本机服务状态。"}


@server.tool(name="retry_credential_cleanup", title="重试凭据清理", meta={"ui": {"visibility": ["app"]}})
def retry_credential_cleanup() -> dict:
    try:
        from engine_control import activation_lock, cleanup_unpublished_providers as retry, EngineManager, keyring
        store = ProfileStore()
        with activation_lock(store.root):
            pending = retry(store, EngineManager(store.root),
                            lambda action, account, secret: keyring(action, account, secret, store.root))
        return {"ok": True, "pending_credential_cleanup": pending}
    except Exception:
        return {"ok": False, "message": "凭据清理未完成，请检查本机服务状态后重试。"}


@server.tool(name="enable_codex", title="接入 Codex", meta={"ui": {"visibility": ["app"]}})
def enable_codex() -> dict:
    """Apply a verified local connection, with an encrypted rollback snapshot."""
    try:
        from config_migration import apply_config
        result = apply_config()
        return {"ok": True, "changed": result["changed"], "jev_configured": result["jev_configured"]}
    except ValueError as error:
        return {"ok": False, "message": str(error)}
    except Exception:
        return {"ok": False, "message": "接入未完成，请检查本机服务和守卫状态。"}


if __name__ == "__main__":
    server.run(transport="stdio")
