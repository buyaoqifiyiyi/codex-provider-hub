"""Synthetic, opt-in probes. No persistence, scheduling, retries or protocol fallback.

transport(request, timeout=seconds) is an urllib-style context-manager opener.
media_inspector has gemini_media.inspect_media's signature and must honour the
supplied bounded transport. Injected code is trusted; do not pass a live store
to tests. Callers own binding/identity snapshots and atomic evidence recording.
"""
import base64
import hashlib
import io
import json
from pathlib import Path
import re
import secrets
import socket
import subprocess
import sys
import threading
from copy import deepcopy
import time
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener

import gemini_media

ASSETS = Path(__file__).resolve().parent / 'assets' / 'capability'
REQUEST_TIMEOUT = 25
MAX_RESPONSE_BYTES = 256 * 1024
MAX_ANSWER_BYTES = 8192
REASONS = frozenset({'sample_match', 'semantic_mismatch', 'invalid_argument',
    'route_unavailable', 'unsupported_path', 'asset_invalid',
    'timeout', 'authentication', 'rate_limit', 'server', 'transport', 'format',
    'response_too_large', 'incomplete', 'upstream_rejected', 'configuration', 'unknown'})
SAMPLES = {'tool': 'tool-roundtrip-v1', 'image': 'two-colour-v1',
           'video': 'video-visual-v1', 'speech': 'speech-transcription-v1',
           'audio': 'non-speech-tones-v1'}
MEDIA = {
    'video': ('video-sample.mp4',
        '仅看视频画面，忽略配音。只返回JSON，字段 red_circle_motion（left/right/still）、'
        'red_circle_final_relative_to_blue_square（left/right）、yellow_triangle_motion（left/right/still）。',
        {'red_circle_motion': 'right', 'red_circle_final_relative_to_blue_square': 'right',
         'yellow_triangle_motion': 'still'}),
    'speech': ('audio-sample.wav',
        '转写音频中的编号和代号。只返回JSON，number为整数字段，code为字符串字段。'
        '这仅是转写样例，不评估是否能直接听原音。',
        {'number': 417, 'code': '海风'}),
    'audio': ('tone-sample.wav',
        '听非语音音频，数独立提示音并判断音高趋势及有无人声。只返回JSON，'
        'tone_count为整数，pitch_trend为rising/falling/steady，speech为布尔值。'
        '无法听到音频时返回 {"unavailable":true}。',
        {'tone_count': 3, 'pitch_trend': 'rising', 'speech': False}),
}


class _ProbeError(Exception):
    def __init__(self, reason):
        self.reason = reason


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        raise _ProbeError('upstream_rejected')


def _http_reason(status):
    if status in (401, 403):
        return 'authentication'
    if status == 429:
        return 'rate_limit'
    return 'server' if status >= 500 else 'upstream_rejected'


def _remaining(deadline):
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise _ProbeError('timeout')
    return remaining


def _read_timeout(response, timeout):
    # urllib HTTPResponse -> BufferedReader -> SocketIO -> socket (also TLS).
    # BytesIO and injected transports need not expose a socket.
    fp = getattr(response, 'fp', None)
    raw = getattr(fp, 'raw', None)
    sock = getattr(raw, '_sock', None)
    if sock is not None:
        sock.settimeout(timeout)


def _read(response, deadline):
    chunks, size = [], 0
    read = getattr(response, 'read1', response.read)
    while True:
        _read_timeout(response, _remaining(deadline))
        chunk = read(min(8192, MAX_RESPONSE_BYTES - size + 1))
        if time.monotonic() >= deadline:
            raise _ProbeError('timeout')
        if not chunk:
            return b''.join(chunks)
        size += len(chunk)
        if size > MAX_RESPONSE_BYTES:
            raise _ProbeError('response_too_large')
        chunks.append(chunk)


def _open_direct(request, transport, *, streaming=False, deadline=None):
    """Synchronous urllib implementation, isolated in a worker in production.

    urllib's connect/header work (including OS DNS) is synchronous: its timeout
    is not a hard total-time cancellation. Once a response is available, reset
    its socket timeout before every read1. Injected transports must honour the
    timeout themselves; socket-less test responses remain supported.
    """
    # The loopback gateway must never go through an environment proxy.  Native
    # media uses the same system proxy settings as the actual media request.
    local = urlsplit(request.full_url).hostname in {'127.0.0.1', '::1', 'localhost'}
    opener = transport or (build_opener(ProxyHandler({}), _NoRedirect()).open if local
                           else build_opener(_NoRedirect()).open)
    request_deadline = time.monotonic() + REQUEST_TIMEOUT
    deadline = request_deadline if deadline is None else min(deadline, request_deadline)
    try:
        with opener(request, timeout=_remaining(deadline)) as response:
            status = getattr(response, 'status', 200)
            if status != 200:
                raise _ProbeError(_http_reason(status))
            if streaming:
                headers = getattr(response, 'headers', {})
                if headers.get('Content-Type', '').split(';')[0].strip().lower() != 'text/event-stream':
                    raise _ProbeError('format')
            return _read(response, deadline)
    except HTTPError as exc:
        reason = _http_reason(exc.code)
        exc.close()
        raise _ProbeError(reason) from None
    except (TimeoutError, socket.timeout):
        raise _ProbeError('timeout') from None
    except URLError as exc:
        raise _ProbeError('timeout' if isinstance(exc.reason, TimeoutError) else 'transport') from None


def _open_bounded(request, transport, *, streaming=False, deadline=None):
    request_deadline = time.monotonic() + REQUEST_TIMEOUT
    deadline = request_deadline if deadline is None else min(deadline, request_deadline)
    _remaining(deadline)
    if transport is not None:
        return _open_direct(request, transport, streaming=streaming, deadline=deadline)
    # One worker per request: credentials travel only in private stdin, never argv.
    payload = json.dumps({'url': request.full_url, 'method': request.get_method(),
        'headers': dict(request.header_items()),
        'data': base64.b64encode(request.data or b'').decode('ascii'),
        'deadline': deadline, 'streaming': streaming}).encode('utf-8')
    # Windows venv launchers can spawn a second interpreter. Use the base
    # executable so kill/wait owns the actual worker, not just its launcher.
    worker = subprocess.Popen([getattr(sys, '_base_executable', sys.executable), '-B', str(Path(__file__).resolve()),
        '--bounded-request'], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL, creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
    stdin = worker.stdin
    # communicate(input) can block synchronously writing a full Windows pipe.
    # Only this writer owns stdin; communicate must not flush/close it under us.
    worker.stdin = None
    write_failed = []
    def write_input():
        try:
            stdin.write(payload)
            stdin.flush()
        except (OSError, ValueError):
            write_failed.append(True)
        finally:
            try:
                stdin.close()
            except OSError:
                pass
    writer = threading.Thread(target=write_input, name='capability-probe-stdin')
    try:
        writer.start()
        try:
            output, _ = worker.communicate(timeout=_remaining(deadline))
        except subprocess.TimeoutExpired:
            raise _ProbeError('timeout') from None
        _remaining(deadline)
        if write_failed or worker.returncode != 0 or len(output) > MAX_RESPONSE_BYTES * 2:
            raise _ProbeError('transport')
        value = json.loads(output)
        if value.get('reason') in REASONS:
            raise _ProbeError(value['reason'])
        raw = base64.b64decode(value['data'], validate=True)
        if len(raw) > MAX_RESPONSE_BYTES:
            raise _ProbeError('response_too_large')
        return raw
    finally:
        # Also clean up on cancellation, malformed IPC, or pre-communicate expiry.
        if worker.poll() is None:
            worker.kill()
        try:
            worker.communicate()  # wait/reap and drain Windows pipe reader threads
        finally:
            # Killing the sole child closes its read end, releasing a blocked
            # writer. Join before touching stdin's buffered-I/O lock.
            if writer.ident is not None:
                writer.join()
            else:
                stdin.close()
            worker.stdin = stdin
            worker.stdout.close()


def _request_worker():
    try:
        value = json.loads(sys.stdin.buffer.read())
        request = Request(value['url'], data=base64.b64decode(value['data'], validate=True),
                          headers=value['headers'], method=value['method'])
        raw = _open_direct(request, None, streaming=value['streaming'], deadline=value['deadline'])
        # An upstream can echo request credentials. Never carry them back over IPC.
        sensitive = [v for k, v in value['headers'].items()
                     if any(word in k.lower() for word in ('authorization', 'token', 'key')) and v]
        sensitive += [v.split(' ', 1)[1] for v in sensitive if v.lower().startswith('bearer ')]
        if any(secret.encode('utf-8') in raw for secret in sensitive):
            raise _ProbeError('upstream_rejected')
        result = {'data': base64.b64encode(raw).decode('ascii')}
    except _ProbeError as exc:
        result = {'reason': exc.reason}
    except Exception:
        result = {'reason': 'transport'}
    sys.stdout.buffer.write(json.dumps(result).encode('ascii'))
    sys.stdout.buffer.flush()


def _asset(name):
    try:
        data = (ASSETS / name).read_bytes()
        manifest = json.loads((ASSETS / 'manifest.json').read_text(encoding='utf-8'))
        if hashlib.sha256(data).hexdigest() != manifest[name]:
            raise ValueError()
        return data
    except (OSError, ValueError, KeyError, TypeError):
        raise _ProbeError('asset_invalid') from None


def _stream(request, transport, deadline):
    raw = _open_bounded(request, transport, streaming=True, deadline=deadline)
    completed = None
    decoded = raw.decode('utf-8').replace('\r\n', '\n')
    if not decoded.endswith('\n\n'):
        raise _ProbeError('format')
    # Only the terminal response object is evidence; deltas alone cannot pass.
    for block in decoded.split('\n\n'):
        lines = [line[5:].lstrip() for line in block.split('\n') if line.startswith('data:')]
        if not lines:
            continue
        data = '\n'.join(lines)
        if data == '[DONE]':
            continue
        event = json.loads(data)
        if not isinstance(event, dict):
            raise _ProbeError('format')
        if event.get('error') is not None:
            raise _ProbeError('incomplete')
        if event.get('type') in ('error', 'response.failed', 'response.incomplete'):
            raise _ProbeError('incomplete')
        if event.get('type') == 'response.completed':
            if completed is not None:
                raise _ProbeError('format')
            completed = event.get('response')
    if not isinstance(completed, dict):
        raise _ProbeError('format')
    if completed.get('status') != 'completed':
        raise _ProbeError('incomplete')
    if completed.get('error') is not None:
        raise _ProbeError('incomplete')
    output = completed.get('output')
    if not isinstance(output, list) or not all(isinstance(x, dict) for x in output):
        raise _ProbeError('format')
    if any(item.get('status', 'completed') != 'completed' for item in output):
        raise _ProbeError('incomplete')
    return output


def _text(output):
    texts = []
    for item in output:
        if item.get('type') == 'message':
            content = item.get('content')
            if not isinstance(content, list):
                raise _ProbeError('format')
            for part in content:
                if not isinstance(part, dict):
                    raise _ProbeError('format')
                if part.get('type') == 'output_text':
                    if not isinstance(part.get('text'), str):
                        raise _ProbeError('format')
                    texts.append(part['text'])
    answer = ''.join(texts)
    if not answer:
        raise _ProbeError('format')
    return answer


def _matches(answer, expected):
    if not isinstance(answer, str):
        raise _ProbeError('format')
    if len(answer.encode('utf-8')) > MAX_ANSWER_BYTES:
        raise _ProbeError('response_too_large')
    value = json.loads(answer)
    if not isinstance(value, dict):
        raise _ProbeError('format')
    # Explicit inability is a semantic failure of this sample only.
    if value == {'unavailable': True}:
        return False
    if set(value) != set(expected) or any(type(value[k]) is not type(v) for k, v in expected.items()):
        raise _ProbeError('format')
    return value == expected


def _gateway(store, alias, purpose, effort, transport, deadline, expected_binding):
    binding = store.get_engine_binding(alias)
    if (not binding or store.is_disabled(binding['id'])
            or expected_binding is not None and binding != expected_binding):
        raise _ProbeError('route_unavailable')
    binding = deepcopy(binding)
    if not all(isinstance(binding.get(k), str) and binding[k] for k in ('_revision', 'account')):
        raise _ProbeError('route_unavailable')
    from ocx_service import _state
    from service import _read_capability
    state = _state(store.root)
    if not state:
        raise _ProbeError('configuration')
    token = _read_capability(store.root)  # DPAPI; never in returned evidence/logs.
    url = 'http://127.0.0.1:%d/v1/responses' % state['port']

    def send(items, **extra):
        _remaining(deadline)
        if store.get_engine_binding(alias) != binding or store.is_disabled(binding['id']):
            raise _ProbeError('route_unavailable')
        # Older running gateways ignore the new expectation headers. Require an
        # authenticated capability handshake before EVERY potentially paid POST.
        health_request = Request(url.removesuffix('/responses') + '/_provider_hub/health',
                                 headers={'X-Provider-Hub-Token': token}, method='GET')
        try:
            health = json.loads(_open_bounded(health_request, transport, deadline=deadline))
        except _ProbeError as exc:
            if exc.reason == 'timeout':
                raise
            raise _ProbeError('configuration') from None
        except (ValueError, UnicodeError):
            raise _ProbeError('configuration') from None
        if (not isinstance(health, dict) or health.get('capability_binding_guard') is not True
                or health.get('capability_deadline_guard') is not True):
            raise _ProbeError('route_unavailable')
        _remaining(deadline)
        if store.get_engine_binding(alias) != binding or store.is_disabled(binding['id']):
            raise _ProbeError('route_unavailable')
        body = {'model': alias, 'input': items, 'stream': True, 'store': False,
                'max_output_tokens': 512, **extra}
        if effort is not None:
            body['reasoning'] = {'effort': effort}
        request = Request(url, data=json.dumps(body).encode(), headers={
            'Content-Type': 'application/json', 'Accept': 'text/event-stream',
            'X-Provider-Hub-Token': token,
            'X-Provider-Hub-Expected-Revision': binding['_revision'],
            'X-Provider-Hub-Expected-Account': binding['account'],
            'X-Provider-Hub-Deadline': repr(deadline)}, method='POST')
        return _stream(request, transport, deadline)

    if purpose == 'image':
        image = base64.b64encode(_asset('two-colour.png')).decode('ascii')
        output = send([{'role': 'user', 'content': [
            {'type': 'input_text', 'text': 'Identify the colour of each half of this image. '
             'Return only JSON with left and right fields; use lowercase English colour names.'},
            {'type': 'input_image', 'image_url': 'data:image/png;base64,' + image}]}])
        return _matches(_text(output), {'left': 'red', 'right': 'blue'})
    prompt = [{'role': 'user', 'content': 'Call capability_lookup with key="sample". '
               'After receiving its result return only JSON {"receipt":the returned receipt}.'}]
    tools = [{'type': 'function', 'name': 'capability_lookup', 'description': 'Get a synthetic receipt.',
              'parameters': {'type': 'object', 'properties': {'key': {'type': 'string'}},
                             'required': ['key'], 'additionalProperties': False}, 'strict': True}]
    output = send(prompt, tools=tools, tool_choice='required', parallel_tool_calls=False)
    calls = [item for item in output if item.get('type') == 'function_call']
    if not calls:
        _text(output)  # a valid plain answer is semantic noncompliance
        return False
    for call in calls:
        if (not isinstance(call.get('call_id'), str) or not call['call_id']
                or not isinstance(call.get('arguments'), str) or not isinstance(call.get('name'), str)):
            raise _ProbeError('format')
        if not isinstance(json.loads(call['arguments']), dict):
            raise _ProbeError('format')
    if len(calls) != 1:
        return False
    call = calls[0]
    args = json.loads(call['arguments'])
    if call.get('name') != 'capability_lookup' or args != {'key': 'sample'}:
        return False
    receipt = secrets.token_hex(12)  # only appears in the tool result, not the prompt
    final = send(prompt + output + [{'type': 'function_call_output', 'call_id': call['call_id'],
                  'output': json.dumps({'receipt': receipt})}], tools=tools, tool_choice='none')
    if any(item.get('type') == 'function_call' for item in final):
        return False
    return _matches(_text(final), {'receipt': receipt})


def _gemini_sample(store, profile, secret, model, purpose, deadline, transport, media_inspector):
    _remaining(deadline)
    if store.is_disabled(profile['id']):
        raise _ProbeError('route_unavailable')
    name, question, expected = MEDIA[purpose]
    _asset(name)

    def bounded_transport(request, timeout):
        _remaining(deadline)
        if store.is_disabled(profile['id']):
            raise _ProbeError('route_unavailable')
        return io.BytesIO(_open_bounded(request, transport, deadline=deadline))

    response = (media_inspector or gemini_media.inspect_media)(profile, secret, model,
        str(ASSETS / name), question, transport=bounded_transport)
    if not isinstance(response, dict):
        raise _ProbeError('format')
    if response.get('ok') is not True:
        status = response.get('http_status')
        if type(status) is int:
            raise _ProbeError(_http_reason(status))
        code = response.get('code')
        raise _ProbeError({'MEDIA_RESPONSE_INVALID': 'format',
            'MEDIA_RESPONSE_INCOMPLETE': 'incomplete',
            'MEDIA_REQUEST_FAILED': 'transport'}.get(code, 'unknown'))
    _remaining(deadline)
    return _matches(response.get('answer'), expected)


def probe_capability(store, alias, purpose, effort=None, *, transport=None, media_inspector=None,
                     provider_id=None, model=None, deadline=None, expected_binding=None):
    """Return bounded evidence for ONE synthetic sample; never write the store.

    purpose: tool/image => subagent_gateway; video/speech/audio => gemini_media.
    Native media never sends effort; returned effort is always None on that path.
    Speech proves transcription only, even if the provider used text internally.
    Non-native media is unsupported; there is no audio subagent gateway probe.
    deadline is an absolute time.monotonic() time; by default the whole probe
    shares REQUEST_TIMEOUT seconds, including both tool turns. Each request also
    has its own REQUEST_TIMEOUT ceiling. Socket read timeouts shrink each read.
    Default requests run in a killable, reaped process (including DNS/headers).
    Trusted injected transports run directly and must honour timeout themselves.
    expected_binding pins the full safe get_engine_binding dictionary; omitted
    means snapshot the first binding read. Neither turn may switch connection.
    A caller must preserve channel AND scope when storing/displaying evidence.
    """
    media = isinstance(purpose, str) and purpose in MEDIA
    result = {'status': 'error', 'reason': 'invalid_argument',
              'channel': 'gemini_media' if media or provider_id is not None else 'subagent_gateway',
              'effort': None if media else effort if isinstance(effort, str) else None,
              'sample_id': SAMPLES.get(purpose) if isinstance(purpose, str) else None,
              'scope': 'transcription_sample' if purpose == 'speech' else
                       'visual_sample' if purpose == 'video' else
                       'non_speech_sample' if purpose == 'audio' else 'sample'}
    if deadline is None:
        deadline = time.monotonic() + REQUEST_TIMEOUT
    try:
        _remaining(deadline)
        from storage import EFFORTS
        direct = provider_id is not None
        if ((not direct and (not isinstance(alias, str) or not re.fullmatch(r'[A-Za-z0-9_.:/~-]{1,160}', alias)))
                or (direct and (not media or not isinstance(provider_id, str) or not provider_id
                    or not isinstance(model, str) or not model))
                or not isinstance(purpose, str) or purpose not in SAMPLES
                or (effort is not None and (not isinstance(effort, str) or effort not in EFFORTS))):
            return result
        if media:
            route = next((p for p in store.list() if p['id'] == provider_id), None) if direct else store.get_route(alias)
            if not route or store.is_disabled(route['id']):
                raise _ProbeError('route_unavailable')
            if route.get('protocol') != 'gemini_native':
                raise _ProbeError('unsupported_path')
            if direct and model not in route.get('models', []):
                raise _ProbeError('route_unavailable')
            resolved = store.resolve_profile(provider_id) if direct else store.resolve_route(alias)
            if not resolved:
                raise _ProbeError('route_unavailable')
            profile, secret = resolved
            if profile.get('protocol') != 'gemini_native':
                raise _ProbeError('unsupported_path')
            if direct:
                if model not in profile.get('models', []):
                    raise _ProbeError('route_unavailable')
                profile = dict(profile, model=model)
            passed = _gemini_sample(store, profile, secret, profile['model'], purpose,
                                    deadline, transport, media_inspector)
        else:
            passed = _gateway(store, alias, purpose, effort, transport, deadline, expected_binding)
        result.update(status='passed' if passed else 'failed',
                      reason='sample_match' if passed else 'semantic_mismatch')
    except _ProbeError as exc:
        result['reason'] = exc.reason
    except (TimeoutError, socket.timeout):
        result['reason'] = 'timeout'
    except (ValueError, UnicodeError, KeyError, TypeError):
        result['reason'] = 'format'
    except (OSError, URLError):
        result['reason'] = 'transport'
    except Exception:
        result['reason'] = 'unknown'
    return result


def probe_gemini_media(store, provider_id, model, purpose, *, deadline=None, transport=None, media_inspector=None):
    """Direct provider/model selection, without any subagent alias or effort."""
    result = probe_capability(store, None, purpose, transport=transport,
                              media_inspector=media_inspector, provider_id=provider_id, model=model, deadline=deadline)
    result.update(channel='gemini_media', effort=None)
    return result


def probe_gemini_snapshot(store, profile, secret, model, purpose, *, deadline=None,
                          transport=None, media_inspector=None):
    """Probe only the caller's selected connection/key; never resolve the store.

    Callers own identity snapshots and evidence persistence. Only disabled state
    is read here. No credentials, questions or answers appear in the result.
    """
    media = isinstance(purpose, str) and purpose in MEDIA
    result = {'status': 'error', 'reason': 'invalid_argument', 'channel': 'gemini_media',
              'effort': None, 'sample_id': SAMPLES.get(purpose) if media else None,
              'scope': 'transcription_sample' if purpose == 'speech' else
                       'visual_sample' if purpose == 'video' else
                       'non_speech_sample' if purpose == 'audio' else 'sample'}
    if deadline is None:
        deadline = time.monotonic() + REQUEST_TIMEOUT
    try:
        _remaining(deadline)
        if (not media or not isinstance(profile, dict) or not profile.get('id')
                or not isinstance(model, str) or not model
                or not isinstance(secret, str) or not secret):
            return result
        if profile.get('protocol') != 'gemini_native':
            raise _ProbeError('unsupported_path')
        # Copy the wire fields once; later store edits cannot replace this input.
        profile = dict(profile, model=model)
        passed = _gemini_sample(store, profile, secret, model, purpose, deadline,
                                transport, media_inspector)
        result.update(status='passed' if passed else 'failed',
                      reason='sample_match' if passed else 'semantic_mismatch')
    except _ProbeError as exc:
        result['reason'] = exc.reason
    except (TimeoutError, socket.timeout):
        result['reason'] = 'timeout'
    except (ValueError, UnicodeError, KeyError, TypeError):
        result['reason'] = 'format'
    except (OSError, URLError):
        result['reason'] = 'transport'
    except Exception:
        result['reason'] = 'unknown'
    return result


if __name__ == '__main__' and sys.argv[1:] == ['--bounded-request']:
    _request_worker()
