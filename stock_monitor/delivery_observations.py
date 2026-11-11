"""Receipt diagnostics only. Never feeds DQ, analysis or trading permissions.

Called on the existing feed lane. Uses a separate SQLite file with a 20ms
busy timeout; failures are visible in logging, never fail the market writer.
No source payloads, URLs, tokens, or full frames are persisted.
"""
import hashlib
import json
import logging
import math
import sqlite3
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path

FIELDS = ('source', 'venue', 'currency', 'symbol')


def stamp():
    return datetime.now(timezone.utc).isoformat()


def valid_time(value, checked):
    try:
        parsed = datetime.fromisoformat(value.replace('Z', '+00:00'))
        now = datetime.fromisoformat(checked.replace('Z', '+00:00'))
        if parsed.tzinfo is None or now.tzinfo is None or parsed > now:
            return None
        return parsed.astimezone(timezone.utc).isoformat()
    except (AttributeError, ValueError, TypeError, OverflowError):
        return None


def event(isin, base, frame, received_at=None, receipt_kind='UNKNOWN',
          reported_market_at=None, checked_at=None, transport_returned_at=None):
    checked = checked_at or stamp()
    identity = {k: frame.attrs.get(k) for k in FIELDS}
    if not isin or not all(identity.values()):
        raise ValueError('DELIVERY_IDENTITY_MISSING')
    row = {'schema':1, 'isin':isin, 'base_tf':base, **identity,
           'checked_at':checked, 'receipt_kind':receipt_kind,
           'application_received_at':valid_time(received_at, checked),
           'transport_returned_at':valid_time(transport_returned_at, checked),
           'provider_reported_market_at':valid_time(reported_market_at, checked),
           'confirmed_market_update_at':None, 'provider_published_at':None,
           'provider_latency_seconds':None, 'freshness_status':'UNKNOWN',
           'reason':'RECEIPT_IS_NOT_CONFIRMED_MARKET_UPDATE',
           'rows_received':len(frame), 'tail_at':None, 'tail_sha256':None}
    row['outcome']='EMPTY' if frame.empty else 'RECEIVED'
    if frame.empty:row['reason']='EMPTY_RESPONSE_NOT_NO_TRADES'
    if not frame.empty:
        # Do not sort/copy entire frames or infer a close/publication timestamp.
        if not frame.index.is_monotonic_increasing or frame.index.has_duplicates:
            row['reason']='UNORDERED_OR_DUPLICATE_INPUT';row['outcome']='INVALID'
            return row
        last=frame.iloc[-1]
        names = ('open','high','low','close','volume') if 'open' in frame else ('price','volume')
        values=[float(last[k]) for k in names]
        if not all(math.isfinite(v) for v in values):
            row['reason']='INVALID_TAIL_NUMBERS';row['outcome']='INVALID'
            return row
        row['tail_at']=str(frame.index[-1])
        row['tail_sha256']=hashlib.sha256(json.dumps([row['tail_at'],values],allow_nan=False).encode()).hexdigest()
    return row


def append(directory, row):
    path=Path(directory)/'delivery_observations.sqlite3'
    key=hashlib.sha256(json.dumps([row[k] for k in ('isin',)+FIELDS+('base_tf',)]).encode()).hexdigest()
    with closing(sqlite3.connect(path,timeout=.02)) as db, db:
        db.execute('CREATE TABLE IF NOT EXISTS receipts(id INTEGER PRIMARY KEY, identity_key TEXT, checked_at TEXT, payload TEXT)')
        db.execute('CREATE TABLE IF NOT EXISTS tails(identity_key TEXT, digest TEXT, first_seen_at TEXT, PRIMARY KEY(identity_key,digest))')
        db.execute('CREATE TABLE IF NOT EXISTS latest(identity_key TEXT PRIMARY KEY, digest TEXT, first_seen_at TEXT)')
        # All state changes and receipt must commit together, including restart.
        db.execute('BEGIN IMMEDIATE')
        prior=db.execute('SELECT digest,first_seen_at FROM latest WHERE identity_key=?',(key,)).fetchone()
        digest=row['tail_sha256']
        seen=db.execute('SELECT first_seen_at FROM tails WHERE identity_key=? AND digest=?',(key,digest)).fetchone() if digest else None
        row=dict(row)
        row['tail_observation']='NO_VALID_TAIL' if not digest else 'UNCHANGED' if prior and prior[0]==digest else 'REPLAY' if seen else 'FIRST_OBSERVATION' if not prior else 'CHANGED_TAIL'
        first=seen[0] if seen else row['checked_at'] if digest else None
        row['tail_first_observed_at']=first
        if digest:
            db.execute('INSERT OR IGNORE INTO tails VALUES (?,?,?)',(key,digest,first))
            db.execute('INSERT OR REPLACE INTO latest VALUES (?,?,?)',(key,digest,first))
        db.execute('INSERT INTO receipts(identity_key,checked_at,payload) VALUES (?,?,?)',
                   (key,row['checked_at'],json.dumps(row,ensure_ascii=False,allow_nan=False)))
    return row


def observe(directory, isin, base, frame, **kwargs):
    try:
        return append(directory,event(isin,base,frame,**kwargs))
    except Exception as error:
        # Only diagnostic failure is isolated. Do not log network payload/error
        # text which could contain tokens. Caller retains its normal errors.
        logging.getLogger(__name__).warning('DELIVERY_DIAGNOSTICS_FAILED %s',type(error).__name__)
        return {'status':'DIAGNOSTIC_ERROR','error_type':type(error).__name__}


def outcome(directory,isin,base,identity,kind,error=None):
    """Record a failed/empty attempt, never reset any last valid tail clock.

Identity is the requested listing, NOT a newly confirmed provider response.
Unknown fields stay null. Never persist exception text, URLs or headers.
"""
    try:
        if kind not in ('ERROR','EMPTY','IDENTITY_MISMATCH'):raise ValueError('INVALID_OUTCOME')
        response=getattr(error,'response',None)
        code=getattr(response,'status_code',None) if response is not None else getattr(error,'code',None)
        code=code if type(code) is int and 100<=code<=599 else None
        reason='EMPTY_RESPONSE_NOT_NO_TRADES' if kind=='EMPTY' else 'IDENTITY_MISMATCH' if kind=='IDENTITY_MISMATCH' else 'HTTP_RATE_LIMIT' if code==429 else 'HTTP_ERROR' if code else 'TIMEOUT' if 'Timeout' in type(error).__name__ else 'SOURCE_ERROR'
        row={'schema':1,'isin':isin,'base_tf':base,**{k:identity.get(k) for k in FIELDS},
             'identity_basis':'REQUESTED_LISTING_NOT_RESPONSE_CONFIRMATION',
             'checked_at':stamp(),'receipt_kind':'ATTEMPT_OUTCOME','outcome':kind,
             'reason':reason,'http_status':code,'error_type':type(error).__name__ if error else None,
             'application_received_at':None,'transport_returned_at':None,'provider_reported_market_at':None,
             'confirmed_market_update_at':None,'provider_published_at':None,'provider_latency_seconds':None,
             'freshness_status':'UNKNOWN','rows_received':0 if kind=='EMPTY' else None,'tail_at':None,'tail_sha256':None}
        return append(directory,row)
    except Exception as failure:
        logging.getLogger(__name__).warning('DELIVERY_DIAGNOSTICS_FAILED %s',type(failure).__name__)
        return {'status':'DIAGNOSTIC_ERROR','error_type':type(failure).__name__}
