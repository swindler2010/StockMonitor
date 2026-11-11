"""Validated, append-only import of owner-authorized Onvista table exports.

No HTTP/login automation, provider mixing, archive replacement or DQ override.
Uses existing series, revision triggers, calendar and DQ; unknown years stay raw.
"""
import csv
import hashlib
import json
import re
from pathlib import Path
from urllib.parse import urlparse

import pandas as pd
import data_quality as quality
from performance_cache import InputRevisionChanged

SOURCE = 'onvista'
HEADER = ['Datum', 'Eröffnungskurs', 'Höchststand', 'Tiefststand', 'Schlusskurs', 'Volumen']


def available_daily(store, info, now=None):
    """Read confirmed imported archives; no download, setting write or selection."""
    with store.connect() as db:
        keys = [r[0] for r in db.execute('SELECT key FROM settings WHERE key LIKE ?', ('onvista_archive:' + info['isin'] + ':%',))]
    frames = []
    for key in keys:
        receipt = store.setting(key, {})
        meta = receipt.get('identity', {}); proof = receipt.get('listing_proof', {})
        evidence = proof.get('evidence', {})
        url = urlparse(receipt.get('source_url', ''))
        if (receipt.get('isin') != info['isin'] or proof.get('isin') != info['isin'] or
            proof.get('status') != 'CONFIRMED' or receipt.get('base_tf') != '1D' or
            meta.get('source') != SOURCE or meta.get('venue') != 'GETTEX' or meta.get('currency') != 'EUR' or
            meta.get('timezone') != 'Europe/Berlin' or
            any(not meta.get(k) or proof.get(k) != meta[k] for k in quality.IDENTITY) or
            not re.fullmatch(r'[0-9a-f]{64}', receipt.get('input_file_sha256', '')) or
            evidence.get('sha256') != receipt.get('input_file_sha256') or
            url.scheme != 'https' or url.netloc != 'www.onvista.de' or
            not url.path.startswith('/aktien/historische-kurse/') or not url.path.endswith('-' + info['isin'])):
            continue
        frame = store.load_series(info['isin'], '1D', SOURCE, meta['symbol'], meta['venue'], meta['currency'])
        if frame.empty or any(frame.attrs.get(k) != meta[k] for k in quality.IDENTITY):continue
        report = quality.audit(frame, '1D', meta, now=now)
        persisted = store.setting(quality.key(info, meta['symbol'], '1D'), {})
        if persisted.get('identity') and persisted['identity'] != meta:continue
        if persisted.get('state') in ('INVALID', 'RECOVERING', 'SOURCE_UNAVAILABLE', 'SOURCE_GAP'):
            report = persisted
        frame.attrs.update(listing_proof=proof, data_quality=report,
                           import_provenance={k:receipt.get(k) for k in ('source_url', 'input_file_sha256', 'excluded')})
        frames.append(frame)
    return frames


def _read(store, isin, symbol):
    with store.connect() as db:
        db.execute('BEGIN')
        return _state(db, isin, symbol)


def _state(db, isin, symbol):
    scopes = ['instrument:' + isin, 'market:' + isin + ':1D']
    return {
        'instrument': db.execute('SELECT payload FROM instruments WHERE isin=?', (isin,)).fetchone(),
        'revisions': {s: (db.execute('SELECT revision FROM input_revisions WHERE scope=?', (s,)).fetchone() or [0])[0] for s in scopes},
        'rows': db.execute('SELECT ts,open,high,low,close,volume FROM series WHERE isin=? AND source=? AND venue=? AND currency=? AND symbol=? AND base_tf=? ORDER BY ts',
                           (isin, SOURCE, 'GETTEX', 'EUR', symbol, '1D')).fetchall(),
        'receipt': db.execute('SELECT value FROM settings WHERE key=?', ('onvista_archive:' + isin + ':' + symbol,)).fetchone(),
    }


def prepare(store, info, csv_path, manifest, now=None):
    """Freeze dependencies before validation; no writes or provider requests."""
    clock = pd.Timestamp(now or pd.Timestamp.now(tz='UTC'))
    if clock.tz is None: raise ValueError('Aware validation time required')
    meta = {k: manifest.get(k) for k in quality.IDENTITY}
    url = urlparse(manifest.get('source_url', ''))
    if (manifest.get('status') != 'CONFIRMED' or manifest.get('isin') != info['isin'] or
        meta != dict(source=SOURCE, venue='GETTEX', currency='EUR', symbol=manifest.get('symbol'), timezone='Europe/Berlin') or
        not re.fullmatch(r'\d+', str(meta['symbol'])) or manifest.get('base_tf') != '1D' or
        url.scheme != 'https' or url.netloc != 'www.onvista.de' or
        not url.path.startswith('/aktien/historische-kurse/') or not url.path.endswith('-' + info['isin']) or
        manifest.get('market_label') != 'gettex (EUR, Echtzeit)'):
        raise ValueError('ONVISTA_IDENTITY_UNCONFIRMED')
    frozen = _read(store, info['isin'], meta['symbol'])
    if not frozen['instrument'] or json.loads(frozen['instrument'][0]) != info:
        raise InputRevisionChanged('ONVISTA_INSTRUMENT_CHANGED')
    data = Path(csv_path).read_bytes()
    if hashlib.sha256(data).hexdigest() != manifest.get('sha256'):
        raise ValueError('ONVISTA_EXPORT_HASH_MISMATCH')
    rows = list(csv.reader(data.decode('utf-8-sig').splitlines(), delimiter=';'))
    if not rows or rows.pop(0) != HEADER or len(rows) != manifest.get('observed_rows'):
        raise ValueError('ONVISTA_EXPORT_FORMAT')
    def number(value, suffix):
        if not value.endswith(suffix): raise ValueError('ONVISTA_UNITS_MISMATCH')
        text = value[:-len(suffix)]
        if not re.fullmatch(r'\d{1,3}(?:\.\d{3})*(?:,\d+)?|\d+(?:,\d+)?', text):
            raise ValueError('ONVISTA_NUMBER_FORMAT')
        return float(text.replace('.', '').replace(',', '.'))
    if any(len(r) != 6 for r in rows): raise ValueError('ONVISTA_COLUMN_COUNT')
    frame = pd.DataFrame([[*(number(x, ' EUR') for x in r[1:5]), number(r[5], ' Stk.')] for r in rows],
                         index=pd.to_datetime([r[0] for r in rows], format='%d.%m.%Y'), columns=quality.FIELDS)
    frame.attrs = meta.copy()
    if frame.empty or frame.index.has_duplicates or not frame.index.is_monotonic_increasing:
        raise ValueError('ONVISTA_DUPLICATE_OR_ORDER')
    if (str(frame.index.min().date()) != manifest.get('observed_first') or
        str(frame.index.max().date()) != manifest.get('observed_last') or
        frame.index.min() < pd.Timestamp(manifest['requested_start']) or
        frame.index.max() >= pd.Timestamp(manifest['requested_end'])):
        raise ValueError('ONVISTA_RANGE_MISMATCH')
    # Partition by the existing calendar's actual coverage; never invent years.
    admitted = []; excluded = []
    for year in frame.index.year.unique():
        part = frame.loc[frame.index.year == year].copy()
        try:
            grid = quality.expected_index(meta, '1D', part.index.min(), part.index.max(), clock)
        except ValueError as error:
            excluded.append(dict(first=str(part.index.min()), last=str(part.index.max()), count=len(part),
                                 reason='CALENDAR_UNCONFIRMED', detail=str(error)))
            continue
        if len(part.index.difference(grid)):
            raise ValueError('ONVISTA_NOT_CONFIRMED_CLOSED_SESSION')
        report = quality.audit(part, '1D', meta, now=clock, check_stale=False)
        if report['state'] == 'INVALID': raise quality.QualityError(report)
        admitted.append(part)
    if not admitted: raise ValueError('ONVISTA_NO_VERIFIED_CALENDAR_ROWS')
    frame = pd.concat(admitted); frame.attrs = meta.copy()
    existing = {r[0]: tuple(r[1:]) for r in frozen['rows']}
    insert = []
    for stamp, row in frame.iterrows():
        values = tuple(float(row[k]) for k in quality.FIELDS); key = str(stamp)
        if key in existing and existing[key] != values:
            raise ValueError('ONVISTA_EXISTING_CANDLE_CONFLICT:' + key)
        if key not in existing: insert.append((info['isin'], SOURCE, 'GETTEX', 'EUR', meta['symbol'], '1D', key, *values))
    merged = {**existing, **{str(t): tuple(float(r[k]) for k in quality.FIELDS) for t, r in frame.iterrows()}}
    all_frame = pd.DataFrame([merged[t] for t in sorted(merged)], index=pd.to_datetime(sorted(merged)), columns=quality.FIELDS)
    all_frame.attrs = meta.copy()
    report = quality.audit(all_frame, '1D', meta, now=clock)
    if report['state'] == 'INVALID': raise quality.QualityError(report)
    receipt = dict(identity=meta, isin=info['isin'], base_tf='1D', quality=report,
                   input_file_sha256=manifest['sha256'], source_url=manifest['source_url'],
                   listing_proof={**meta, 'isin': info['isin'], 'status': 'CONFIRMED', 'evidence': manifest},
                   imported_calendar_rows=len(frame), raw_rows=len(rows), excluded=excluded,
                   export_method=manifest['export_method'], raw_file=str(Path(csv_path).resolve()),
                   provider_publication_delay='UNKNOWN', first=str(all_frame.index.min()), last=str(all_frame.index.max()))
    return dict(isin=info['isin'], symbol=meta['symbol'], frozen=frozen, rows=insert,
                frame=all_frame, receipt=receipt, manifest=dict(manifest), csv_path=str(csv_path))


def commit(store, plan):
    """Short coherent transaction, exact CAS, INSERT only; reprepare on revision race."""
    if hashlib.sha256(Path(plan['csv_path']).read_bytes()).hexdigest() != plan['manifest']['sha256']:
        raise ValueError('ONVISTA_EXPORT_CHANGED')
    with store.connect() as db:
        db.execute('BEGIN IMMEDIATE')
        if _state(db, plan['isin'], plan['symbol']) != plan['frozen']:
            raise InputRevisionChanged('ONVISTA_INPUT_CHANGED')
        # An unchanged repeated export has no side effects, including revisions.
        if not plan['rows'] and plan['frozen']['receipt']:
            return dict(state='UNCHANGED', inserted=0, quality=plan['receipt']['quality']['state'])
        db.executemany('INSERT INTO series VALUES (?,?,?,?,?,?,?,?,?,?,?,?)', plan['rows'])
        db.execute('INSERT OR REPLACE INTO settings VALUES (?,?)',
                   ('onvista_archive:' + plan['isin'] + ':' + plan['symbol'], json.dumps(plan['receipt'], ensure_ascii=False)))
        report = plan['receipt']['quality']
        db.execute('INSERT OR REPLACE INTO settings VALUES (?,?)',
                   (quality.key({'isin': plan['isin']}, plan['symbol'], '1D'), json.dumps(report, ensure_ascii=False)))
    return dict(state='SAVED', inserted=len(plan['rows']), quality=report['state'], excluded=plan['receipt']['excluded'])
