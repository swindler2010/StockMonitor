"""Explicit, offline recovery bundles. Never imports Store or starts application services.

Run in a worker/maintenance process, never a Tk callback. Input files must be
reviewed for secrets; filenames alone cannot establish that content is public.
Bundles are private plaintext and belong on an owner-protected volume.
"""
import hashlib
import json
import os
from pathlib import Path, PureWindowsPath
import shutil
import sqlite3
import time
from contextlib import closing


class RecoveryError(ValueError):
    pass


def digest(path):
    h = hashlib.sha256()
    with Path(path).open('rb') as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b''):
            h.update(block)
    return h.hexdigest()


def safe_name(name):
    if not isinstance(name, str) or not name or '\\' in name:
        raise RecoveryError('Invalid relative path')
    parts = name.split('/')
    if any(p in ('', '.', '..') or any(c in p for c in ':*?"<>|') or any(ord(c)<32 for c in p) or p.endswith((' ', '.'))
           or PureWindowsPath(p).is_reserved() for p in parts):
        raise RecoveryError('Unsafe recovery path')
    return name


def regular(path):
    path = Path(path).absolute()
    for parent in (path, *path.parents):
        if parent.is_symlink() or (hasattr(parent, 'is_junction') and parent.is_junction()):
            raise RecoveryError('Links and junctions are not recovery inputs')
    if not path.is_file():
        raise RecoveryError('Missing regular recovery file')
    return path


def db_check(path):
    with closing(sqlite3.connect(Path(path).resolve().as_uri() + '?mode=ro', uri=True)) as db:
        if db.execute('PRAGMA integrity_check').fetchall() != [('ok',)]:
            raise RecoveryError('SQLite integrity check failed')
        return db.execute('PRAGMA user_version').fetchone()[0]


def snapshot(source, target, timeout=60):
    deadline = time.monotonic() + timeout
    def progress(*_):
        if time.monotonic() > deadline:
            raise RecoveryError('SQLite snapshot timeout; incomplete bundle retained')
    with closing(sqlite3.connect(source.resolve().as_uri() + '?mode=ro', uri=True)) as src:
        with closing(sqlite3.connect(target)) as dst:
            src.backup(dst, pages=256, progress=progress, sleep=0.05)
            # Backup inherits source WAL mode. Make this isolated snapshot
            # self-contained before subsequent read-only validation.
            dst.execute('PRAGMA journal_mode=DELETE')
    return db_check(target)


def create(destination, entries, *, version, excluded, content_reviewed=False):
    """entries: [{source, path, category, kind: file|sqlite}]. No recursive collection.

    Completion is published by manifest.json, written last. Any failure leaves
    evidence in the new directory; it does not clean up, overwrite or retry.
    Code/external history must be quiescent for a coordinated full-system image.
    SQLite alone uses the online backup API, including committed WAL data.
    """
    if not content_reviewed:
        raise RecoveryError('Explicit content review is required; secrets stay separate')
    prepared, seen = [], set()
    for entry in entries:
        name = safe_name(entry['path'])
        if name.casefold() in seen or name.casefold() == 'manifest.json':
            raise RecoveryError('Duplicate or reserved destination')
        seen.add(name.casefold())
        kind = entry.get('kind', 'file')
        if kind not in ('file', 'sqlite'):
            raise RecoveryError('Unsupported input kind')
        source = regular(entry['source'])
        if source.name.lower().endswith(('-wal', '-shm', '.pem', '.key')) or source.name.lower() in ('.env', 'credentials.json', 'token.json'):
            raise RecoveryError('Secret or SQLite sidecar must not be bundled')
        if kind == 'file':
            with source.open('rb') as stream:
                if stream.read(16) == b'SQLite format 3\x00':
                    raise RecoveryError('SQLite input requires snapshot kind')
        prepared.append((entry, name, source, kind))
    if not prepared:
        raise RecoveryError('Empty recovery selection')
    destination = Path(destination).absolute()
    for parent in (destination, *destination.parents):
        if parent.is_symlink() or (hasattr(parent, 'is_junction') and parent.is_junction()):
            raise RecoveryError('Backup destination contains a link')
    destination.mkdir(parents=False, exist_ok=False)
    records = []
    for entry, name, source, kind in prepared:
        target = destination / name
        target.parent.mkdir(parents=True, exist_ok=True)
        record = dict(path=name, category=entry['category'], kind=kind)
        if kind == 'sqlite':
            record['schema_version'] = snapshot(source, target)
        else:
            before = digest(source)
            with source.open('rb') as src, target.open('xb') as dst:
                shutil.copyfileobj(src, dst, 1024 * 1024)
            if digest(target) != before or digest(source) != before:
                raise RecoveryError('Input changed during backup; no complete manifest')
        record.update(size=target.stat().st_size, sha256=digest(target))
        records.append(record)
    manifest = dict(format=1, version=version, files=records, excluded=excluded,
                    private=True, secrets='Separate owner-managed recovery required',
                    consistency='Per-file; external writers must be quiescent for coordinated restore')
    with (destination / 'manifest.json').open('x', encoding='utf-8') as stream:
        json.dump(manifest, stream, ensure_ascii=False, indent=2)
        stream.flush()
        os.fsync(stream.fileno())
    verify(destination)
    return manifest


def verify(bundle):
    bundle = Path(bundle).absolute()
    manifest_path = regular(bundle / 'manifest.json')
    if manifest_path.stat().st_size > 16 * 1024 * 1024:
        raise RecoveryError('Manifest too large')
    manifest = json.loads(manifest_path.read_text(encoding='utf-8'))
    if manifest.get('format') != 1 or not manifest.get('files'):
        raise RecoveryError('Unsupported or empty manifest')
    seen = set()
    for record in manifest['files']:
        name = safe_name(record['path'])
        if name.casefold() in seen or name.casefold() == 'manifest.json':
            raise RecoveryError('Duplicate or reserved path')
        seen.add(name.casefold())
        target = regular(bundle / name)
        if target.stat().st_size != record['size'] or digest(target) != record['sha256']:
            raise RecoveryError('File integrity mismatch: ' + name)
        if record['kind'] == 'sqlite':
            if db_check(target) != record['schema_version']:
                raise RecoveryError('SQLite schema mismatch')
        elif record['kind'] != 'file':
            raise RecoveryError('Unsupported kind')
    # Unlisted files are never restored, but report their presence as invalid.
    actual = {p.relative_to(bundle).as_posix().casefold() for p in bundle.rglob('*') if p.is_file()}
    if actual != seen | {'manifest.json'}:
        raise RecoveryError('Unlisted files in recovery bundle')
    return manifest


def restore(bundle, destination, *, expected_version):
    """Validate before creating a NEW target. Does not launch app or migrate DB."""
    bundle = Path(bundle).absolute()
    manifest = verify(bundle)
    if manifest['version'] != expected_version:
        raise RecoveryError('Version mismatch; select compatible application explicitly')
    destination = Path(destination).absolute()
    # Validate ancestors before writing, including Windows junctions.
    for parent in (destination, *destination.parents):
        if parent.is_symlink() or (hasattr(parent, 'is_junction') and parent.is_junction()):
            raise RecoveryError('Restore destination contains a link')
    destination.mkdir(parents=False, exist_ok=False)
    for record in manifest['files']:
        target = destination / record['path']
        target.parent.mkdir(parents=True, exist_ok=True)
        with regular(bundle / record['path']).open('rb') as src, target.open('xb') as dst:
            shutil.copyfileobj(src, dst, 1024 * 1024)
        if digest(target) != record['sha256']:
            raise RecoveryError('Restore readback mismatch')
        if record['kind'] == 'sqlite':
            db_check(target)
    return manifest


def main():
    import argparse
    parser = argparse.ArgumentParser(description='Offline Stock Monitor recovery; never starts trading/services')
    sub = parser.add_subparsers(dest='command', required=True)
    make = sub.add_parser('create')
    make.add_argument('plan', help='Reviewed JSON: entries, version, excluded, content_reviewed')
    make.add_argument('destination', help='New directory; parent must exist')
    check = sub.add_parser('verify'); check.add_argument('bundle')
    recover = sub.add_parser('restore'); recover.add_argument('bundle'); recover.add_argument('destination')
    recover.add_argument('--expected-version', required=True)
    args = parser.parse_args()
    try:
        if args.command == 'create':
            plan = json.loads(Path(args.plan).read_text(encoding='utf-8'))
            result = create(args.destination, plan['entries'], version=plan['version'],
                            excluded=plan['excluded'], content_reviewed=plan.get('content_reviewed') is True)
        elif args.command == 'verify': result = verify(args.bundle)
        else: result = restore(args.bundle, args.destination, expected_version=args.expected_version)
        print(json.dumps(dict(status='PASS', files=len(result['files']), version=result['version'])))
    except (OSError, ValueError, KeyError, TypeError, sqlite3.Error) as error:
        parser.exit(2, 'Recovery failed: ' + str(error) + '\n')


if __name__ == '__main__': main()
