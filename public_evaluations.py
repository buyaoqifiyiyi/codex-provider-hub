"""Anonymous public references, never authorization or route-quality evidence.

read_references(root, model_ids) -> (mapping keyed by requested name, pending bool)
automatically schedules first/expired entries. schedule_refresh(root, model_ids)
is also safe to call explicitly (no forced refresh of fresh/backoff entries).
Only pass names the caller has already established are enabled. The only source
configuration read is optional public-reference-sources.json under root. It does
not read provider addresses, credentials, or establish that a model is enabled.

All timestamps are UTC Unix seconds (or None); result.date is ISO calendar date.
checked_at is the last completed attempt, expires_at is its freshness/retry
boundary. Pending stale entries deliberately omit old scores. Poll pending with
a caller-owned bounded budget; unavailable/backoff does not request polling.
Public strings are untrusted display metadata, never instructions. verified=True
only reflects HF's flag; even that flag does not attest the caller's route.
Scores are raw, without normalization or ranking across benchmarks/conditions.
"""
import contextlib
import datetime
import hashlib
from html.parser import HTMLParser
import http.client
import ipaddress
import json
import math
from pathlib import Path
import re
import socket
import sqlite3
import ssl
import threading
import time
import urllib.parse
import urllib.request
import uuid

__all__ = ['read_references', 'schedule_refresh', 'cache_matches_source']
TTL_SECONDS = 7 * 86400
LEASE_SECONDS = 120
DB_FILENAME = 'public_evaluations.sqlite3'
_MAX_BYTES = 2 * 1024 * 1024
_MAX_CONFIG_BYTES = 64 * 1024
_CONFIG_FILENAME = 'public-reference-sources.json'
_NAME = re.compile(r'[A-Za-z0-9][A-Za-z0-9_.-]{0,159}(?:/[A-Za-z0-9][A-Za-z0-9_.-]{0,159})?\Z')
_TEXT = re.compile(r'[A-Za-z0-9 _./:(),+%=-]{1,160}\Z')
_HF_URL = re.compile(r'https://huggingface\.co(?:/[A-Za-z0-9_.-]+)*/?\Z')
_SECRET = re.compile(r'(?:^|[/._-])(?:sk|hf|api[_-]?key|access[_-]?token|token|secret|password|bearer)[-_][A-Za-z0-9_-]{8,}', re.I)
_lock = threading.Lock()
_queues = {}


def _looks_like_endpoint(name):
    parts = name.split('/')
    host = parts[0].casefold()
    try:
        ipaddress.ip_address(host)
        return True
    except ValueError:
        pass
    if (host == 'localhost' or host.endswith(('.localhost', '.local', '.internal', '.lan', '.home'))
            or host.startswith(('api.', 'gateway.', 'proxy.'))):
        return True
    return (len(parts) == 2 and '.' in host
            and bool(re.fullmatch(r'(?:v[0-9]+|api|chat|completions|responses)', parts[1], re.I)))


def _names(model_ids):
    if isinstance(model_ids, (str, bytes)):
        raise ValueError('model_ids must be a collection of enabled model names')
    try:
        iterator = iter(model_ids)
    except TypeError:
        raise ValueError('model_ids must be a collection of enabled model names') from None
    names = []
    for index, name in enumerate(iterator):
        if (index >= 256 or not isinstance(name, str) or len(name) > 200
                or not _NAME.fullmatch(name) or _SECRET.search(name) or _looks_like_endpoint(name)
                or any(p in ('.', '..') for p in name.split('/'))):
            raise ValueError('invalid model names (maximum 256, bounded identifiers only)')
        if name not in names:
            names.append(name)
    return names


@contextlib.contextmanager
def _db(root):
    root = Path(root)
    root.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(root / DB_FILENAME, timeout=0.05)
    db.row_factory = sqlite3.Row
    try:
        db.execute('CREATE TABLE IF NOT EXISTS public_references ('
                   'model TEXT PRIMARY KEY, payload TEXT, checked REAL, expires REAL DEFAULT 0, '
                   'failures INTEGER DEFAULT 0, owner TEXT, lease REAL DEFAULT 0)')
        yield db
        db.commit()
    finally:
        db.close()


def _empty(model, status='pending'):
    return dict(status=status, model_id=model, repository=None, source_url=None,
                checked_at=None, expires_at=None, stale=False, results=[])


def _text(value):
    return value if isinstance(value, str) and _TEXT.fullmatch(value) else None


def _url(value):
    if not isinstance(value, str) or len(value) > 512 or not _HF_URL.fullmatch(value):
        return None
    if any(p in ('.', '..') for p in urllib.parse.urlsplit(value).path.split('/')):
        return None
    return value


def _public_hostname(host):
    if not isinstance(host, str) or not host.isascii() or len(host) > 253:
        return None
    host = host.lower()
    labels = host.split('.')
    if (len(labels) < 2 or labels[-1] in {'localhost', 'local', 'internal', 'lan', 'home'}
            or not re.fullmatch(r'(?:[a-z]{2,63}|xn--[a-z0-9-]{1,59})', labels[-1])
            or any(not re.fullmatch(r'[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?', part) for part in labels)):
        return None
    try:
        ipaddress.ip_address(host)
        return None
    except ValueError:
        return host


def _report_target(url, allowed_hosts):
    if (not isinstance(url, str) or len(url) > 512 or not url.startswith('https://')
            or any(not '!' <= char <= '~' for char in url) or '?' in url or '#' in url
            or _SECRET.search(url)):
        raise ValueError('invalid public report URL')
    parsed = urllib.parse.urlsplit(url)
    host = _public_hostname(parsed.hostname)
    if (parsed.scheme != 'https' or not host or parsed.netloc.lower() != host
            or host not in allowed_hosts or parsed.query or parsed.fragment
            or not re.fullmatch(r'/[A-Za-z0-9._~/-]*', parsed.path or '/')
            or '//' in parsed.path or any(part in {'.', '..'} for part in parsed.path.split('/'))):
        raise ValueError('invalid public report URL')
    return host, parsed.path or '/'


def _no_duplicate_keys(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError('duplicate public source field')
        result[key] = value
    return result


def _load_sources(root):
    """Read only the optional explicit public sources, never provider settings.

    Schema 1 has allowedReportHosts and models. Each exact model name maps to
    either {type: huggingface, repository: owner/name} or
    {type: report, url: HTTPS_URL, column: exact_column_header}. Reports use
    one table headed "Evaluation benchmark" and percentage values.
    """
    file = Path(root) / _CONFIG_FILENAME
    try:
        with file.open('rb') as stream:
            data = stream.read(_MAX_CONFIG_BYTES + 1)
    except FileNotFoundError:
        if file.is_symlink():
            raise ValueError('unreadable public source configuration') from None
        return {'allowedReportHosts': [], 'models': {}}
    if len(data) > _MAX_CONFIG_BYTES:
        raise ValueError('public source configuration too large')
    value = json.loads(data.decode('utf-8-sig'), object_pairs_hook=_no_duplicate_keys)
    if (not isinstance(value, dict) or set(value) != {'schemaVersion', 'allowedReportHosts', 'models'}
            or type(value['schemaVersion']) is not int or value['schemaVersion'] != 1
            or not isinstance(value['allowedReportHosts'], list) or len(value['allowedReportHosts']) > 32
            or not isinstance(value['models'], dict) or len(value['models']) > 256):
        raise ValueError('invalid public source configuration')
    hosts = []
    for item in value['allowedReportHosts']:
        host = _public_hostname(item)
        if not host or host in hosts or _SECRET.search(host):
            raise ValueError('invalid public report host')
        hosts.append(host)
    models = {}
    for name, source in value['models'].items():
        _names([name])
        key = name.casefold()
        if key in models or not isinstance(source, dict):
            raise ValueError('invalid public source mapping')
        if source.get('type') == 'huggingface' and set(source) == {'type', 'repository'}:
            repo = source['repository']
            _names([repo])
            if '/' not in repo or not _url('https://huggingface.co/' + repo):
                raise ValueError('invalid public repository')
        elif source.get('type') == 'report' and set(source) == {'type', 'url', 'column'}:
            _report_target(source['url'], hosts)
            if not _text(source['column']) or _SECRET.search(source['column']):
                raise ValueError('invalid public report column')
        else:
            raise ValueError('invalid public source mapping')
        models[key] = source
    return {'allowedReportHosts': hosts, 'models': models}


def _source_signature(sources, model):
    source = sources['models'].get(model.casefold())
    hosts = sorted(sources['allowedReportHosts']) if source and source['type'] == 'report' else []
    payload = json.dumps([source, hosts], sort_keys=True, separators=(',', ':'))
    return hashlib.sha256(payload.encode('utf-8')).hexdigest()


def cache_matches_source(root, model, payload):
    """Check source identity for a direct cache reader, without scheduling HTTP.

    The caller must independently check expiry. Invalid configuration or legacy
    payloads without a source signature return False.
    """
    try:
        _names([model])
        return (isinstance(payload, dict) and payload.get('model_id') == model
                and payload.get('_source_signature') == _source_signature(_load_sources(root), model))
    except (OSError, ValueError, TypeError, RecursionError):
        return False


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise ValueError('public API redirects are not followed')


def _http_json(url):
    # No SDK token lookup, cookies, netrc, provider headers, or environment proxy
    # credentials. No public URL is ever used as a request target.
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), _NoRedirect())
    req = urllib.request.Request(url, headers={'Accept': 'application/json'})
    deadline = time.monotonic() + 20
    with opener.open(req, timeout=8) as response:
        if response.status != 200:
            raise ValueError('unexpected public API status')
        chunks, size = [], 0
        while True:
            if time.monotonic() > deadline:
                raise TimeoutError('public API deadline')
            chunk = response.read1(min(65536, _MAX_BYTES + 1 - size))
            if not chunk:
                break
            chunks.append(chunk)
            size += len(chunk)
            if size > _MAX_BYTES:
                raise ValueError('public metadata too large')
    return json.loads(b''.join(chunks))


class _PinnedReportConnection(http.client.HTTPSConnection):
    def __init__(self, host, address):
        super().__init__(host, port=443, timeout=8, context=ssl.create_default_context())
        self._address = address

    def connect(self):
        family, kind, protocol, _, destination = self._address
        connection = socket.socket(family, kind, protocol)
        try:
            connection.settimeout(self.timeout)
            connection.connect(destination)
            self.sock = self._context.wrap_socket(connection, server_hostname=self.host)
        except Exception:
            connection.close()
            raise


def _http_html(url, allowed_hosts):
    # Resolve once, reject any non-public answer, then connect to that exact IP.
    # TLS still verifies the configured hostname. No proxy, cookies, auth, or
    # redirect handling is involved, and no model/provider data is sent.
    host, target = _report_target(url, allowed_hosts)
    addresses = socket.getaddrinfo(host, 443, family=socket.AF_UNSPEC, type=socket.SOCK_STREAM)
    if not addresses or len(addresses) > 32:
        raise ValueError('invalid public report DNS response')
    for family, kind, _, _, destination in addresses:
        if (family not in {socket.AF_INET, socket.AF_INET6} or kind != socket.SOCK_STREAM
                or len(destination) < 2 or destination[1] != 443 or '%' in destination[0]
                or (family == socket.AF_INET6 and len(destination) > 3 and destination[3])):
            raise ValueError('invalid public report DNS response')
        address = ipaddress.ip_address(destination[0])
        if (address.version != (4 if family == socket.AF_INET else 6) or not address.is_global
                or address.is_multicast or address.is_reserved or address.is_unspecified):
            raise ValueError('non-public report address')
    connection = _PinnedReportConnection(host, addresses[0])
    deadline = time.monotonic() + 20
    try:
        connection.request('GET', target, headers={'Accept':'text/html'})
        response = connection.getresponse()
        if response.status != 200:
            raise ValueError('unexpected public report status')
        content_type = response.getheader('Content-Type', '').split(';', 1)[0].strip().lower()
        if content_type != 'text/html':
            raise ValueError('invalid public report format')
        length = response.getheader('Content-Length')
        if length is not None and (not length.isdecimal() or int(length) > _MAX_BYTES):
            raise ValueError('invalid public report size')
        chunks, size = [], 0
        while True:
            if time.monotonic() > deadline:
                raise TimeoutError('public report deadline')
            chunk = response.read1(min(65536, _MAX_BYTES + 1 - size))
            if not chunk:
                break
            chunks.append(chunk); size += len(chunk)
            if size > _MAX_BYTES:
                raise ValueError('public report too large')
        return b''.join(chunks).decode('utf-8')
    finally:
        connection.close()


class _ReportTables(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.tables, self.table, self.row, self.cell = [], None, None, None
        self.cells, self.cell_size = 0, 0

    def handle_starttag(self, tag, attrs):
        if tag == 'table' and self.table is None:
            if len(self.tables) >= 32:
                raise ValueError('too many public report tables')
            self.table = []
        elif self.table is not None and tag == 'tr':
            self.row = []
        elif self.row is not None and tag in {'td','th'}:
            self.cells += 1
            if self.cells > 4096:
                raise ValueError('too many public report cells')
            self.cell_size = 0
            self.cell = []

    def handle_data(self, data):
        if self.cell is not None:
            self.cell_size += len(data)
            if self.cell_size > 2048:
                raise ValueError('public report cell too large')
            self.cell.append(data)

    def handle_endtag(self, tag):
        if tag in {'td','th'} and self.cell is not None and self.row is not None:
            self.row.append(' '.join(''.join(self.cell).split())); self.cell = None
        elif tag == 'tr' and self.row is not None and self.table is not None:
            if len(self.table) >= 500:
                raise ValueError('too many public report rows')
            self.table.append(self.row); self.row = None
        elif tag == 'table' and self.table is not None:
            self.tables.append(self.table); self.table = self.row = self.cell = None


def _fetch_report(model, url, column, allowed_hosts):
    host, _ = _report_target(url, allowed_hosts)
    parser = _ReportTables()
    parser.feed(_http_html(url, allowed_hosts))
    result = _empty(model)
    tables = [table for table in parser.tables if table and table[0]
              and table[0][0].casefold() == 'evaluation benchmark']
    if len(tables) != 1:
        raise ValueError('public report table missing or ambiguous')
    table = tables[0]
    indices = [i for i, name in enumerate(table[0]) if name.casefold() == column.casefold()]
    if len(indices) != 1:
        raise ValueError('public report model column is ambiguous')
    index = indices[0]
    rows = []
    for row in table[1:]:
        if len(row) <= index or not _text(row[0]):
            continue
        score = re.fullmatch(r'(\d+(?:\.\d+)?)\s*%', row[index])
        if not score:
            continue
        value = float(score.group(1))
        if not 0 <= value <= 100:
            continue
        rows.append(dict(benchmark=row[0],task_id=None,score=value,unit='%',date=None,
                         verified=None,source_name=host,source_url=url))
        if len(rows) == 100:
            break
    result.update(status='available' if rows else 'missing',source_url=url,
                  source_kind='report',results=rows)
    return result


def _fetch(model, source=None, allowed_hosts=()):
    _names([model])
    result = _empty(model)
    if source and source['type'] == 'report':
        return _fetch_report(model, source['url'], source['column'], allowed_hosts)
    repo = source['repository'] if source and source['type'] == 'huggingface' else None
    if not repo:
        search = 'https://huggingface.co/api/models?' + urllib.parse.urlencode(
            {'search': model, 'limit': 100, 'full': 'true'})
        candidates = _http_json(search)
        if not isinstance(candidates, list) or len(candidates) > 100:
            raise ValueError('invalid search response')
        matches = set()
        for item in candidates:
            candidate = item.get('id') if isinstance(item, dict) else None
            if not isinstance(candidate, str) or len(candidate) > 200 or not _NAME.fullmatch(candidate) or '/' not in candidate:
                raise ValueError('invalid repository identifier')
            name = candidate if '/' in model else candidate.split('/')[1]
            if name.casefold() == model.casefold():
                matches.add(candidate)
        # A full unqualified search page cannot establish global uniqueness.
        if len(matches) > 1 or (len(candidates) == 100 and '/' not in model):
            result['status'] = 'ambiguous'
            return result
        if not matches:
            result['status'] = 'missing'
            return result
        repo = matches.pop()
    url = 'https://huggingface.co/' + repo
    if not _url(url):
        raise ValueError('invalid repository URL')
    detail = _http_json('https://huggingface.co/api/models/' + repo + '?expand%5B%5D=evalResults')
    if not isinstance(detail, dict) or detail.get('id') != repo:
        raise ValueError('repository response mismatch')
    entries = detail.get('evalResults', [])
    if not isinstance(entries, list):
        raise ValueError('invalid evaluation response')
    results = []
    for entry in entries[:100]:
        if not isinstance(entry, dict) or not isinstance(entry.get('data'), dict):
            continue
        data = entry['data']
        dataset = data.get('dataset')
        if not isinstance(dataset, dict):
            continue
        benchmark, task = _text(dataset.get('id')), _text(dataset.get('task_id'))
        score = data.get('value')
        if not benchmark or type(score) not in (int, float):
            continue
        try:
            if not math.isfinite(score) or abs(score) > 1e15:
                continue
        except OverflowError:
            continue
        date = data.get('date')
        try:
            if not isinstance(date, str) or not re.fullmatch(r'\d{4}-\d{2}-\d{2}', date):
                raise ValueError('invalid date')
            datetime.date.fromisoformat(date)
        except ValueError:
            date = None
        source = data.get('source')
        source = source if isinstance(source, dict) else {}
        row=dict(benchmark=benchmark, task_id=task, score=score, date=date,
                 verified=entry.get('verified') if type(entry.get('verified')) is bool else None,
                 source_name=_text(source.get('name')), source_url=_url(source.get('url')))
        # Keep only an equality fingerprint of the untrusted test conditions;
        # do not display or persist their free-form text as instructions.
        notes=data.get('notes')
        if isinstance(notes,str) and 0 < len(notes.strip()) <= 512:
            normalized=' '.join(notes.casefold().split())
            row['conditions_fingerprint']=hashlib.sha256(normalized.encode('utf-8')).hexdigest()
        if data.get('unit') == '%': row['unit']='%'
        results.append(row)
    result.update(status='available' if results else ('unavailable' if entries else 'missing'),
                  repository=repo, source_url=url, results=results)
    return result


def _refresh_one(root, model):
    owner, now = uuid.uuid4().hex, time.time()
    with _db(root) as db:
        db.execute('INSERT OR IGNORE INTO public_references(model) VALUES (?)', (model,))
        taken = db.execute('UPDATE public_references SET owner=?, lease=? WHERE model=? '
                           'AND expires<=? AND lease<=?',
                           (owner, now + LEASE_SECONDS, model, now, now)).rowcount
    if not taken:
        return
    try:
        sources = _load_sources(root)
        signature = _source_signature(sources, model)
    except (OSError, ValueError, TypeError, RecursionError):
        with _db(root) as db:
            db.execute('UPDATE public_references SET payload=NULL, expires=0, owner=NULL, lease=0 '
                       'WHERE model=? AND owner=?', (model, owner))
        return
    try:
        source = sources['models'].get(model.casefold())
        result = (_fetch(model, source, sources['allowedReportHosts']) if source else _fetch(model))
    except Exception:
        # Never persist/log exception bodies (they may contain arbitrary content).
        result = _empty(model, 'unavailable')
    try:
        current_signature = _source_signature(_load_sources(root), model)
    except (OSError, ValueError, TypeError, RecursionError):
        current_signature = None
    now = time.time()
    with _db(root) as db:
        row = db.execute('SELECT failures FROM public_references WHERE model=? AND owner=?',
                         (model, owner)).fetchone()
        if not row:
            return  # An expired lease was claimed by a newer worker.
        if current_signature != signature:
            db.execute('UPDATE public_references SET payload=NULL, expires=0, owner=NULL, lease=0 '
                       'WHERE model=? AND owner=?', (model, owner))
            return
        failures = min(row['failures'] + 1, 10) if result['status'] == 'unavailable' else 0
        lifetime = min(300 * 2 ** (failures - 1), 21600) if failures else TTL_SECONDS
        result.update(checked_at=now, expires_at=now + lifetime)
        result['_source_signature'] = signature
        db.execute('UPDATE public_references SET payload=?, checked=?, expires=?, failures=?, '
                   'owner=NULL, lease=0 WHERE model=? AND owner=?',
                   (json.dumps(result, allow_nan=False), now, now + lifetime, failures, model, owner))


def _worker(root):
    while True:
        with _lock:
            if not _queues[root]:
                del _queues[root]
                return
            model = _queues[root].pop()
        try:
            _refresh_one(root, model)
        except (OSError, sqlite3.Error):
            # Cache is optional; never propagate failures to saving/scheduling.
            pass


def schedule_refresh(root, model_ids):
    """Queue enabled names on a daemon worker; return None. Never performs HTTP here.

    SQLite leases coalesce processes; an abandoned lease is reclaimable after
    120s. Cached/backoff entries are skipped. No periodic background crawling.
    """
    names = _names(model_ids)
    if not names:
        return
    root = str(Path(root).resolve())
    with _lock:
        if root in _queues:
            _queues[root].update(names)
            return
        _queues[root] = set(names)
        try:
            threading.Thread(target=_worker, args=(root,), daemon=True,
                             name='public-evaluations').start()
        except RuntimeError:
            del _queues[root]


def read_references(root, model_ids):
    """Read bounded public cache and automatically queue missing/expired names.

    Return (dict[str, reference], bool). Invalid input raises ValueError before
    any I/O. Cache I/O errors return unavailable with pending=False. Neither
    function reads enabled-model configuration or performs model inference.
    """
    names = _names(model_ids)
    if not names:
        return {}, False
    try:
        sources = _load_sources(root)
    except (OSError, ValueError, TypeError, RecursionError):
        return {model: _empty(model, 'unavailable') for model in names}, False
    refs, due, now = {}, [], time.time()
    try:
        with _db(root) as db:
            for model in names:
                row = db.execute('SELECT * FROM public_references WHERE model=?', (model,)).fetchone()
                value = json.loads(row['payload']) if row and row['payload'] and row['expires'] > now else None
                if value is not None and not isinstance(value, dict):
                    raise ValueError('invalid cached reference')
                if (value is not None and value.get('model_id') == model
                        and value.pop('_source_signature', None) == _source_signature(sources, model)):
                    refs[model] = value
                else:
                    if value is not None:
                        # Other cache readers must not continue to use a stale source.
                        db.execute('UPDATE public_references SET expires=0 WHERE model=?', (model,))
                    value = _empty(model)
                    if row:
                        value.update(checked_at=row['checked'], expires_at=row['expires'] or None,
                                     stale=row['checked'] is not None)
                    refs[model] = value
                    due.append(model)
    except (OSError, sqlite3.Error, ValueError, TypeError):
        return {model: _empty(model, 'unavailable') for model in names}, False
    if due:
        schedule_refresh(root, due)
    return refs, bool(due)
