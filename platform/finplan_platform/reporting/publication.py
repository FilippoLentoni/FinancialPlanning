"""Serialized, recoverable local report aliases. Immutable archives are authority."""
from __future__ import annotations
import contextlib
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import tempfile
from datetime import datetime, date
from .transport import decode_transport

LIMIT = 151 * 1024 * 1024
EXTENSIONS = ('html', 'csv', 'json')
class PublicationError(ValueError):
    pass

def _sha(raw):
    return 'sha256:' + hashlib.sha256(raw).hexdigest()

def _read(path, limit=LIMIT):
    if path.is_symlink() or path.stat().st_uid != os.getuid() or path.stat().st_size > limit:
        raise PublicationError('Unsafe or oversized report publication file')
    raw = path.read_bytes()
    if len(raw) > limit: raise PublicationError('Report publication size limit exceeded')
    path.chmod(0o600)
    return raw

def _sync(directory):
    fd = os.open(directory, os.O_RDONLY | os.O_DIRECTORY)
    try: os.fsync(fd)
    finally: os.close(fd)

def _atomic(path, raw):
    if path.is_symlink() or path.exists() and path.stat().st_uid != os.getuid():
        raise PublicationError('Unsafe report publication alias')
    name = None
    try:
        with tempfile.NamedTemporaryFile(dir=path.parent, prefix='.report-publish-', delete=False) as f:
            name = Path(f.name);os.fchmod(f.fileno(), 0o600);f.write(raw);f.flush();os.fsync(f.fileno())
        os.replace(name, path);_sync(path.parent)
    finally:
        if name is not None and name.exists(): name.unlink()

def _json(path):
    return json.loads(_read(path, 65536))

def _canonical(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=False).encode()

def _moment(value, *, allow_missing=False):
    if value is None and allow_missing:return float('-inf')
    if not isinstance(value,str):raise PublicationError('Invalid publication timestamp')
    at = datetime.fromisoformat(value.replace('Z', '+00:00'))
    if at.tzinfo is None: raise PublicationError('Publication timestamps require timezone')
    return at.timestamp()

def _head(target, archive, manifest, report):
    if any(p.is_symlink() for p in (archive,archive.parent,archive.parent.parent)):
        raise PublicationError('Symbolic-link report archive is unsafe')
    return {'protocol':'finplan-report-publication/1', 'archive':str(archive.relative_to(target)),
            'manifest_checksum':_sha(_read(archive/'manifest.json')), 'revision':manifest['revision'],
            'mode':report['mode'], 'period':report['period'], 'start_day':report['start_day'], 'end_day':report['end_day'],
            'generated_at':report['generated_at'], 'data_cutoff':report['data_cutoff']}

def _verify(target, head):
    relative = head.get('archive', '')
    if not re.fullmatch(r'archive/\d{4}-\d{2}-\d{2}(?:_\d{4}-\d{2}-\d{2})?/[a-f0-9]{20}', relative):
        raise PublicationError('Invalid immutable report publication archive')
    if head.get('protocol') != 'finplan-report-publication/1' or head.get('period') not in ('daily','weekly') or head.get('mode') not in ('forward','controlled_replay'):
        raise PublicationError('Invalid publication head identity')
    period = head['end_day'] if head['period']=='daily' else head['start_day']+'_'+head['end_day']
    if head['period']=='daily' and head['start_day']!=head['end_day'] or relative != 'archive/'+period+'/'+head['revision']:
        raise PublicationError('Publication head dates/type disagree with immutable archive')
    _moment(head['generated_at']);_moment(head['data_cutoff'],allow_missing=True)
    archive = target/relative
    if any(p.is_symlink() for p in [archive, archive.parent, archive.parent.parent]):
        raise PublicationError('Symbolic-link report archive is unsafe')
    raw = _read(archive/'manifest.json')
    if _sha(raw) != head['manifest_checksum']: raise PublicationError('Publication manifest checksum mismatch')
    manifest = json.loads(raw)
    for key in ('revision', 'generated_at', 'data_cutoff', 'mode'):
        if manifest[key] != head[key]: raise PublicationError('Publication head/manifest identity mismatch')
    payloads = {}
    for ext in EXTENSIONS:
        payload = _read(archive/('report.'+ext))
        expected = manifest['artifacts'][ext]
        if len(payload) != expected['size_bytes'] or _sha(payload) != expected['checksum']:
            raise PublicationError('Immutable report publication artifact integrity failure')
        payloads[ext] = payload
    return payloads

def _legacy(target, name):
    paths = [target/(name+'.'+ext) for ext in EXTENSIONS]
    if not any(p.exists() or p.is_symlink() for p in paths): return None
    if not all(p.exists() for p in paths): raise PublicationError('Incomplete legacy report aliases require repair')
    payloads = {ext:_read(target/(name+'.'+ext)) for ext in EXTENSIONS}
    report = decode_transport(json.loads(payloads['json']))
    if report.get('mode') not in ('forward','controlled_replay') or report.get('period') not in ('daily','weekly'):
        raise PublicationError('Invalid legacy report period/mode')
    for field in ('start_day','end_day'):
        value=report.get(field)
        if not isinstance(value,str) or not re.fullmatch(r'\d{4}-\d{2}-\d{2}',value):
            raise PublicationError('Invalid legacy report date')
        try:date.fromisoformat(value)
        except ValueError as error:raise PublicationError('Invalid legacy report date') from error
    duration=(date.fromisoformat(report['end_day'])-date.fromisoformat(report['start_day'])).days
    if not 0<=duration<=6 or report['period']=='daily' and duration!=0:
        raise PublicationError('Invalid legacy report window')
    revision = hashlib.sha256(payloads['json']+payloads['html']).hexdigest()[:20]
    period = report['end_day'] if report['period']=='daily' else report['start_day']+'_'+report['end_day']
    archive = target/'archive'/period/revision
    manifest = json.loads(_read(archive/'manifest.json'))
    head = _head(target, archive, manifest, report)
    if _verify(target, head) != payloads: raise PublicationError('Legacy alias exports disagree with immutable revision')
    return head

def _eligible(new, old, latest):
    if old is None: return True
    if new['mode'] != old['mode'] or new['period'] != old['period']:
        raise PublicationError('Publication period/mode mismatch')
    new_period = (new['end_day'], new['start_day'])
    old_period = (old['end_day'], old['start_day'])
    if new_period != old_period:
        if not latest: raise PublicationError('Dated publication head has wrong period')
        return new_period > old_period
    return _moment(new['data_cutoff'],allow_missing=True) >= _moment(old['data_cutoff'],allow_missing=True) and _moment(new['generated_at']) >= _moment(old['generated_at'])

def _finish(target, heads, pending):
    # A durable intent is written before the first alias. On restart every export
    # is replayed from the same verified archive before comparing another writer.
    for name, head in pending['updates'].items():
        expected = head['end_day'] if head['period']=='daily' else head['start_day']+'_'+head['end_day']
        if name not in ('latest',expected):raise PublicationError('Publication journal alias/period mismatch')
        current_path=heads/(name+'.json')
        if current_path.exists():
            current=_json(current_path);_verify(target,current)
            if not _eligible(head,current,name=='latest'):head=current
        payloads = _verify(target, head)
        for ext in EXTENSIONS: _atomic(target/(name+'.'+ext), payloads[ext])
        _atomic(heads/(name+'.json'), _canonical(head))
    (target/'.report-publication.pending.json').unlink();_sync(target)

def publish(target: Path, archive: Path, manifest: dict, report: dict, period_name: str, *, update_latest=True):
    lock = target/'.report-publication.lock'
    fd = os.open(lock, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    try:
        if os.fstat(fd).st_uid != os.getuid(): raise PublicationError('Publication lock is not owned')
        os.fchmod(fd,0o600);fcntl.flock(fd,fcntl.LOCK_EX)
        heads = target/'.report-heads'
        if heads.is_symlink(): raise PublicationError('Unsafe publication head directory')
        heads.mkdir(mode=0o700, exist_ok=True)
        if heads.stat().st_uid != os.getuid(): raise PublicationError('Publication heads are not owned')
        heads.chmod(0o700)
        journal = target/'.report-publication.pending.json'
        recovered=False
        if journal.exists():
            pending = _json(journal)
            if pending.get('protocol') != 'finplan-report-publication/1' or not isinstance(pending.get('updates'),dict) or not 1<=len(pending['updates'])<=2:
                raise PublicationError('Invalid report publication recovery journal')
            for name in pending['updates']:
                if name!='latest' and not re.fullmatch(r'\d{4}-\d{2}-\d{2}(?:_\d{4}-\d{2}-\d{2})?',name):raise PublicationError('Unsafe report publication alias name')
            _finish(target, heads, pending);recovered=True
        incoming = _head(target,archive,manifest,report);_verify(target,incoming)
        updates={}
        for name in [period_name]+(['latest'] if update_latest else []):
            path=heads/(name+'.json')
            old=_json(path) if path.exists() else _legacy(target,name)
            if old is not None:
                payloads=_verify(target,old)
                # Repair any externally interrupted/changed alias from its head.
                for ext in EXTENSIONS:
                    alias=target/(name+'.'+ext)
                    if not alias.exists() or _sha(_read(alias)) != _sha(payloads[ext]):_atomic(alias,payloads[ext])
                if not path.exists():_atomic(path,_canonical(old))
            if _eligible(incoming,old,name=='latest'):updates[name]=incoming
        if updates:
            pending={'protocol':'finplan-report-publication/1','updates':updates}
            _atomic(journal,_canonical(pending));_finish(target,heads,pending)
        return {'aliases_published':list(updates), 'publication_recovered':recovered}
    finally:
        fcntl.flock(fd,fcntl.LOCK_UN);os.close(fd)
