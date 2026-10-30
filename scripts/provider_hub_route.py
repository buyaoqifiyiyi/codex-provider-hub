"""Internal route discovery. Never print configurations, capabilities or keys.

Hub intent is explicit: a static X-Provider-Hub-Token header on custom. Failure
on that path does not fall back to another service. Health checks use loopback only,
no environment proxy, no redirects, and bounded response sizes/timeouts. Check
both unauthenticated rejection and authenticated identity/readiness. Targets
add a read-only binding/engine-config proof. Health is
not evidence of a successful parent inference; check-parent-route still supplies
that separate gate. Binding checks never decrypt credentials.
"""
import importlib.util
from contextlib import closing
import json
import os
from pathlib import Path
import re
import sqlite3
import tomllib
from urllib.error import HTTPError
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener

CAPABILITY_HEADER = 'X-Provider-Hub-Token'


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, request, file_pointer, code, message, headers, new_url):
        return None


def custom_route():
    home = Path(os.environ.get('CODEX_HOME') or Path.home() / '.codex')
    config = tomllib.loads((home / 'config.toml').read_text(encoding='utf-8-sig'))
    if config.get('profile') or config.get('model_provider') != 'custom':
        return None
    route = config.get('model_providers', {}).get('custom')
    return route if isinstance(route, dict) else None


def local_base_url(value):
    try:
        if not isinstance(value, str) or any(c.isspace() or c == '\x00' for c in value):
            return False
        url = urlsplit(value)
        return (url.scheme == 'http' and url.hostname == '127.0.0.1'
                and url.port is not None and 0 < url.port <= 65535 and url.path.rstrip('/') == '/v1'
                and not any((url.username, url.password, url.query, url.fragment)))
    except (TypeError, ValueError):
        return False


def health_request(url, capability):
    """Return status plus JSON; errors and bodies never escape via exceptions."""
    headers = {'Accept': 'application/json'}
    if capability is not None:
        headers[CAPABILITY_HEADER] = capability
    try:
        request = Request(url, headers=headers, method='GET')
        with build_opener(ProxyHandler({}), NoRedirect()).open(request, timeout=0.2) as response:
            if response.status != 200:
                return response.status, None
            if response.headers.get('Content-Type', '').split(';', 1)[0].strip().lower() != 'application/json':
                return 200, None
            body = response.read(4097)
            if len(body) > 4096:
                return 200, None
            return 200, json.loads(body)
    except HTTPError as error:
        code = error.code
        error.close()
        return code, None
    except Exception:
        return None, None


def authenticated_hub(base_url, capability):
    if not local_base_url(base_url) or not isinstance(capability, str) or not re.fullmatch(r'[A-Za-z0-9_-]{32,128}', capability):
        return False
    url = base_url.rstrip('/') + '/_provider_hub/health'
    # An unprotected JSON endpoint is not the authenticated Hub contract.
    status, _ = health_request(url, None)
    if status not in (401, 403):
        return False
    status, payload = health_request(url, capability)
    return (status == 200 and isinstance(payload, dict)
            and payload.get('service') == 'codex-provider-hub'
            and type(payload.get('version')) is int and payload['version'] == 1)


def bound_engine_route(alias, revision):
    """Verify target binding and engine config without resolving any secret."""
    local = os.environ.get('LOCALAPPDATA')
    if not local or not re.fullmatch(r'[a-f0-9]{64}', revision or ''): return None
    root = Path(local)/'CodexProviderHub'
    spec = importlib.util.spec_from_file_location('parent_binding_metadata', Path(__file__).with_name('binding-kind.py'))
    module = importlib.util.module_from_spec(spec); spec.loader.exec_module(module)
    with closing(sqlite3.connect((root/'profiles.sqlite').resolve().as_uri()+'?mode=ro',uri=True,timeout=.4)) as db:
        db.execute('BEGIN')
        binding = module.active_bindings(db).get(alias)
        if not binding or binding['revision'] != revision: return None
        account, protocol, endpoint = db.execute('SELECT b.account,p.protocol,p.base_url FROM engine_bindings b JOIN providers p ON p.id=b.provider_id WHERE b.alias=?',(alias,)).fetchone()
        config = json.loads((root/'engine-state'/'config.json').read_text(encoding='utf-8'))
        provider = config.get('providers',{}).get(account.split('/',1)[0],{})
        if (provider.get('disabled') is True or provider.get('apiKey') != 'keychain:'+account
                or provider.get('adapter') != module.MODEL_ADAPTERS.get(protocol)
                or provider.get('baseUrl','').rstrip('/') != endpoint.rstrip('/')
                or provider.get('defaultModel') != binding['model'] or provider.get('models') != [binding['model']]):
            return None
        return binding


def active_source(alias=None, revision=None):
    """Resolve the connection center through its authenticated local health check."""
    route = custom_route()
    if route is None or not local_base_url(route.get('base_url')):
        return None
    base_url = route['base_url']
    headers = route.get('http_headers', {})
    env_headers = route.get('env_http_headers', {})
    if not isinstance(headers, dict) or not isinstance(env_headers, dict):
        return None
    matches = [value for key, value in headers.items() if key.lower() == CAPABILITY_HEADER.lower()]
    if any(key.lower() == CAPABILITY_HEADER.lower() for key in env_headers):
        return None  # The candidate only supports the explicit static-header contract.
    if matches:
        binding = bound_engine_route(alias, revision) if alias else None
        if alias and not binding: return None
        if len(matches) != 1 or not authenticated_hub(base_url, matches[0]):
            return None
        if alias:
            return {'kind':'hub','base_url':base_url,'capability':matches[0],'binding':binding}
        return {'kind': 'hub', 'base_url': base_url, 'capability': matches[0]}
    return None


def hub_provider(source):
    """Credential-based diagnostics are retired; callers must use binding proof."""
    raise ValueError('ROUTE_DIAGNOSTIC_REMOVED: use active_source(alias, revision) without supplier probes')
