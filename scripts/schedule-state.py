"""Synchronous JSON persistence under a process-owned SQLite transaction.

No provider, chat, credential, network, or third-party imports. All payloads use
stdin/stdout; diagnostics are fixed codes. The lock DB contains no task data.
"""
from contextlib import closing
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
import stat
import sys
import tempfile

TTL = 300000
MAX_RECORDS = 128
MAX_BYTES = 1024
FIELDS = ['schemaVersion','parentThreadId','turnId','callId','model','effort','createdAt','expiresAt']
EFFORTS = {'none','minimal','low','medium','high','xhigh','max','ultra'}
UUID = re.compile(r'[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}', re.I)
MODEL = re.compile(r'[A-Za-z0-9_.:/~-]{1,160}')

class StateError(Exception):
    pass

def compact(value):
    return json.dumps(value, ensure_ascii=False, separators=(',', ':'))

def digest(value):
    return hashlib.sha256(compact(value).encode('utf8')).hexdigest()

def key(row):
    return digest([row['parentThreadId'].lower(), row['turnId'].lower(), row['callId']]) + '.json'

def checksum(row):
    return digest([row[k] for k in FIELDS])

def valid_model(value):
    return isinstance(value,str) and MODEL.fullmatch(value) and value != 'auto' and not value.startswith('auto:')

def preferences(value):
    fields = ['model','effort','fallback_model','fallback_effort']
    if not isinstance(value,dict) or set(value) != {'bounded','boundary','critical'}:
        raise StateError('SCHEDULING_STATE_INVALID')
    for row in value.values():
        if not isinstance(row,dict) or set(row) != set(fields) or any(not isinstance(row[k],str) for k in fields):
            raise StateError('SCHEDULING_STATE_INVALID')
        for model,effort in [(row['model'],row['effort']), (row['fallback_model'],row['fallback_effort'])]:
            if not MODEL.fullmatch(model) or effort not in EFFORTS:
                raise StateError('SCHEDULING_STATE_INVALID')
    return value

def atomic_write(dest, value, prefix):
    fd, temporary = tempfile.mkstemp(prefix=prefix, dir=dest.parent)
    try:
        with os.fdopen(fd, 'wb') as stream:
            stream.write((compact(value)+'\n').encode('utf8'))
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary,dest)
    finally:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass

def schedule(request):
    root = Path(request['root'])
    dest = root / 'scheduling-mode.json'
    if request['action'] == 'read':
        try:
            value=json.loads(dest.read_text(encoding='utf8'))
        except FileNotFoundError:
            value={'mode':'auto'}
        if not isinstance(value,dict) or value.get('mode') not in {'auto','manual'}:
            raise StateError('SCHEDULING_STATE_INVALID')
        if 'preferences' in value:
            return {'mode':value['mode'],'preferences':preferences(value['preferences']),'saved':True}
        try:
            legacy=json.loads((root/'scheduling.json').read_text(encoding='utf8'))
        except FileNotFoundError:
            # Without a saved preference there is nothing to execute manually.
            return {'mode':'auto','preferences':None,'saved':False}
        return {'mode':value['mode'],'preferences':preferences(legacy),'saved':True}
    if request['action'] == 'mode':
        try:
            value = json.loads(dest.read_text(encoding='utf8'))
        except FileNotFoundError:
            value = {'mode':'auto'}
        if not isinstance(value,dict) or value.get('mode') not in {'auto','manual'}:
            raise StateError('SCHEDULING_STATE_INVALID')
        if 'preferences' in value:
            preferences(value['preferences'])
        if request.get('mode') not in {'auto','manual'}:
            raise StateError('SCHEDULING_STATE_INVALID')
        if request['mode'] == 'manual' and 'preferences' not in value:
            try:
                preferences(json.loads((root/'scheduling.json').read_text(encoding='utf8')))
            except FileNotFoundError:
                raise StateError('SCHEDULING_PREFERENCES_REQUIRED') from None
        value['mode'] = request['mode']
        atomic_write(dest,value,'.scheduling-')
        return request['mode']
    value = preferences(request['preferences'])
    if request['action'] == 'manual':
        atomic_write(dest,{'mode':'manual','preferences':value},'.scheduling-')
    elif request['action'] == 'preferences':
        try:
            current=json.loads(dest.read_text(encoding='utf8'))
        except FileNotFoundError:
            current={'mode':'auto'}
        if not isinstance(current,dict) or current.get('mode') not in {'auto','manual'}:
            raise StateError('SCHEDULING_STATE_INVALID')
        atomic_write(dest,{'mode':current['mode'],'preferences':value},'.scheduling-')
        return {'mode':current['mode'],'preferences':value}
    else:
        atomic_write(root/'scheduling.json',value,'.scheduling-')
    return value

def inventory(directory):
    names=[]
    with os.scandir(directory) as entries:
        for ent in entries:
            # Legacy mkdir lock is inert. Never delete a potentially old writer's lock.
            if ent.name == '.lock':
                continue
            if len(names) >= MAX_RECORDS+1 or not ent.is_file(follow_symlinks=False) or not re.fullmatch(r'(?:[a-f0-9]{64}\.json|pending-[a-f0-9]{24})',ent.name):
                raise StateError('AUTO_SELECTION_STORE_INVALID')
            names.append(ent.name)
    return names

def read_record(directory,name):
    file=directory/name
    info=file.lstat()
    if not stat.S_ISREG(info.st_mode) or info.st_size > MAX_BYTES:
        raise StateError('AUTO_SELECTION_CORRUPT')
    with file.open('rb') as stream:
        data=stream.read(MAX_BYTES+1)
    try:
        if len(data)>MAX_BYTES:
            raise ValueError()
        r=json.loads(data)
        if isinstance(r,dict):
            for field in ['schemaVersion','createdAt','expiresAt']:
                if isinstance(r.get(field),float) and r[field].is_integer():
                    r[field]=int(r[field])
        valid = (isinstance(r,dict) and set(r)==set(FIELDS+['checksum'])
            and type(r['schemaVersion']) is int and r['schemaVersion']==1
            and valid_model(r['model']) and r['effort'] in EFFORTS
            and type(r['createdAt']) is int and 0<=r['createdAt']<=9007199254740991
            and type(r['expiresAt']) is int and r['expiresAt']==int(float(r['createdAt']+TTL))
            and all(isinstance(r[k],str) and UUID.fullmatch(r[k]) for k in ['parentThreadId','turnId'])
            and isinstance(r['callId'],str) and re.fullmatch(r'[A-Za-z0-9_.:-]{1,160}',r['callId'])
            and r['checksum']==checksum(r) and key(r)==name)
        if not valid:
            raise ValueError()
        return r
    except (ValueError,TypeError,KeyError):
        raise StateError('AUTO_SELECTION_CORRUPT') from None

def selection(request):
    directory=Path(request['root'])/'auto-selections'
    if request['action']=='lookup' and not directory.exists():
        return None
    directory.mkdir(mode=0o700,parents=True,exist_ok=True)
    if not stat.S_ISDIR(directory.lstat().st_mode) or directory.is_symlink():
        raise StateError('AUTO_SELECTION_STORE_INVALID')
    names=inventory(directory)
    row=request['record']
    name=key(row)
    now=request['now']
    if request['action']=='lookup':
        if name not in names:
            return None
        r=read_record(directory,name)
        return r if r['model']==row['model'] and r['createdAt']<=now<r['expiresAt'] else None
    if name in names:
        r=read_record(directory,name)
        if r['model']!=row['model'] or r['effort']!=row['effort']:
            raise StateError('AUTO_SELECTION_CONFLICT')
        if r['createdAt']>now or r['expiresAt']<=now:
            raise StateError('AUTO_SELECTION_EXPIRED')
        return r
    count=0
    for existing in names:
        if existing.startswith('pending-'):
            (directory/existing).unlink()
            continue
        r=read_record(directory,existing)
        if r['expiresAt']<=now:
            (directory/existing).unlink()
        else:
            count+=1
    if count>=MAX_RECORDS:
        raise StateError('AUTO_SELECTION_CAPACITY')
    row={'schemaVersion':1,**row,'createdAt':now,'expiresAt':int(float(now+TTL))}
    row['checksum']=checksum(row)
    if len(compact(row).encode('utf8'))>MAX_BYTES:
        raise StateError('AUTO_SELECTION_TOO_LARGE')
    # Same bounded pending-name format as the original store.
    temporary=directory/('pending-'+os.urandom(12).hex())
    try:
        with temporary.open('xb') as stream:
            stream.write(compact(row).encode('utf8'));stream.flush();os.fsync(stream.fileno())
        os.replace(temporary,directory/name)
    finally:
        temporary.unlink(missing_ok=True)
    return row

def run(request):
    action=request['action']
    if action not in {'read','mode','manual','preferences','legacy','record','lookup'}:
        raise StateError('STATE_INVALID')
    root=Path(request['root'])
    if action=='lookup' and not (root/'auto-selections').exists():
        return None
    root.mkdir(mode=0o700,parents=True,exist_ok=True)
    # A fixed-size, payload-free SQLite database outside the bounded JSON store.
    database=root/('auto-selections-lock.sqlite3' if action in {'record','lookup'} else 'scheduling-lock.sqlite3')
    if database.is_symlink():
        raise StateError('STATE_UNAVAILABLE')
    with closing(sqlite3.connect(database,timeout=.5 if action=='lookup' else 5,isolation_level=None)) as db:
        db.execute('BEGIN IMMEDIATE')
        try:
            value=selection(request) if action in {'record','lookup'} else schedule(request)
        finally:
            # JSON replacement is the commit point; SQLite is only the mutex.
            db.rollback()
    return value

if __name__=='__main__':
    try:
        request=json.loads(sys.stdin.buffer.read(256*1024+1))
        response={'ok':True,'value':run(request)}
    except StateError as error:
        response={'ok':False,'code':str(error)}
    except sqlite3.OperationalError as error:
        response={'ok':False,'code':'STATE_BUSY' if getattr(error,'sqlite_errorcode',None) in {5,6} or str(error) in {'database is locked','database table is locked'} else 'STATE_UNAVAILABLE'}
    except Exception:
        response={'ok':False,'code':'STATE_UNAVAILABLE'}
    print(compact(response))
