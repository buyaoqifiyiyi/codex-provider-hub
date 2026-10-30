"""Run a small capability sample once per target and reuse its outcome."""
import time
import uuid
from datetime import datetime, timezone


MAX_AGE = {'passed': 30 * 86400, 'failed': 7 * 86400, 'error': 300}
LEASE_SECONDS = 300
ERRORS = {'timeout', 'authentication', 'rate_limit', 'server', 'transport', 'format', 'unknown'}


def public_evidence(store, provider_id, model):
    """Expose bounded outcome metadata without credential-derived identity."""
    result=[]
    for item in store.capability_evidence(provider_id, model):
        tested=item.get('tested_at');status=item.get('status')
        if status in MAX_AGE and type(tested) is int and not 0 <= int(time.time())-tested < MAX_AGE[status]:
            status='stale'
        try:
            tested=datetime.fromtimestamp(tested,timezone.utc).isoformat() if type(tested) is int else None
        except (ValueError,OverflowError,OSError):
            tested=None
        result.append({key:item.get(key) for key in ('purpose','channel','effort','source','error_code')}
                      | {'status':status,'tested_at':tested})
    return result


def _matching(store, provider_id, model, purpose, channel, effort, now, expected_identity=None):
    rows = [row for row in store.capability_evidence(provider_id, model)
            if row.get('purpose') == purpose and row.get('channel') == channel
            and row.get('effort') == effort and row.get('status') in MAX_AGE
            and (expected_identity is None or row.get('identity') == expected_identity['identity'])]
    rows.sort(key=lambda row: row['tested_at'], reverse=True)
    if rows and 0 <= now - rows[0]['tested_at'] < MAX_AGE[rows[0]['status']]:
        return rows[0]
    return None


def _safe(row, cached=False):
    return {key: row.get(key) for key in ('purpose', 'channel', 'status', 'effort', 'tested_at', 'error_code')} | {'cached': cached}


def ensure_capability(store, provider_id, model, purpose, channel, effort, runner, *, alias=None,
                      expected_identity=None):
    """Run runner only if this exact target has no fresh sample.

    A SQLite lease prevents duplicate paid checks in separate MCP processes.
    The runner returns bounded metadata only; bodies and secrets are never saved.
    """
    now = int(time.time())
    if store.is_disabled(provider_id):
        return {'status': 'error', 'error_code': 'unknown', 'reason': 'connection_disabled'}
    identity = store.capability_identity(provider_id, model, effort, channel=channel,
                                         expected_identity=expected_identity)
    binding = store.get_engine_binding(alias) if alias else None
    if alias and (not binding or binding['id'] != provider_id or binding['model'] != model):
        return {'status': 'error', 'error_code': 'unknown', 'reason': 'route_unavailable'}
    def reuse(row):
        if store.is_disabled(provider_id):
            return {'status': 'error', 'error_code': 'unknown', 'reason': 'connection_disabled'}
        try:
            store.capability_identity(provider_id, model, effort, channel=channel, expected_identity=identity)
        except ValueError:
            return {'status': 'error', 'error_code': 'unknown', 'reason': 'route_changed'}
        if alias:
            current = store.get_engine_binding(alias)
            if not current or any(current.get(k) != binding.get(k) for k in ('id', 'model', 'account', '_revision')):
                return {'status': 'error', 'error_code': 'unknown', 'reason': 'route_changed'}
        return _safe(row, True)
    cached = _matching(store, provider_id, model, purpose, channel, effort, now, identity)
    if cached:
        return reuse(cached)
    owner = uuid.uuid4().hex
    with store.atomic() as db:
        db.execute('CREATE TABLE IF NOT EXISTS capability_sample_leases ('
                   'identity TEXT, purpose TEXT, channel TEXT, effort TEXT, owner TEXT, expires_at INTEGER, '
                   'PRIMARY KEY(identity,purpose,channel,effort))')
        values = (identity['identity'], purpose, channel, effort or '', owner, now + LEASE_SECONDS)
        db.execute('INSERT INTO capability_sample_leases VALUES (?,?,?,?,?,?) '
                   'ON CONFLICT(identity,purpose,channel,effort) DO UPDATE SET owner=excluded.owner, '
                   'expires_at=excluded.expires_at WHERE capability_sample_leases.expires_at<=?', values + (now,))
        held = db.execute('SELECT owner FROM capability_sample_leases WHERE identity=? AND purpose=? '
                          'AND channel=? AND effort=?', values[:4]).fetchone()
        if not held or held[0] != owner:
            return {'status': 'testing', 'purpose': purpose, 'channel': channel, 'effort': effort}
    try:
        # Another caller may have finished between the first read and the lease.
        cached = _matching(store, provider_id, model, purpose, channel, effort, int(time.time()), identity)
        if cached:
            return reuse(cached)
        if store.is_disabled(provider_id):
            return {'status': 'error', 'error_code': 'unknown', 'reason': 'connection_disabled'}
        store.capability_identity(provider_id, model, effort, channel=channel, expected_identity=identity)
        result = runner()
        if not isinstance(result, dict) or result.get('status') not in MAX_AGE:
            result = {'status': 'error', 'error_code': 'format'}
        status = result['status']
        code = result.get('error_code', result.get('reason')) if status == 'error' else None
        if code not in ERRORS:
            code = 'unknown' if status == 'error' else None
        # Route account is checked around the actual sample but is deliberately
        # not part of long-lived identity: normal account rotation is not proof
        # that the provider or the model changed.
        if store.is_disabled(provider_id):
            return {'status': 'error', 'error_code': 'unknown', 'reason': 'connection_disabled'}
        if alias:
            after = store.get_engine_binding(alias)
            if not after or any(after.get(k) != binding.get(k) for k in ('id', 'model', 'account', '_revision')):
                return {'status': 'error', 'error_code': 'unknown', 'reason': 'route_changed'}
        store.record_capability_evidence(provider_id, model, purpose, status, effort,
                                         expected_identity=identity, channel=channel, error_code=code)
        return _safe(_matching(store, provider_id, model, purpose, channel, effort, int(time.time()), identity))
    except Exception:
        # Exceptions may contain credentials or raw provider responses.
        return {'status': 'error', 'error_code': 'unknown', 'reason': 'sample_not_recorded'}
    finally:
        with store.atomic() as db:
            db.execute('DELETE FROM capability_sample_leases WHERE identity=? AND purpose=? AND channel=? '
                       'AND effort=? AND owner=?', (identity['identity'], purpose, channel, effort or '', owner))
