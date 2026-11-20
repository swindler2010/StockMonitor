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
        db.execute('CREATE TABLE IF NOT EXISTS confirmed_latest(identity_key TEXT PRIMARY KEY, market_at TEXT NOT NULL, payload TEXT NOT NULL)')
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
        if confirmed_event(row):
            previous=db.execute('SELECT market_at FROM confirmed_latest WHERE identity_key=?',(key,)).fetchone()
            # A cached reply, historical correction/backfill or restart never
            # renews the exchange-event clock. Only a later executed trade does.
            if previous is None or row['confirmed_market_update_at']>previous[0]:
                db.execute('INSERT OR REPLACE INTO confirmed_latest VALUES (?,?,?)',
                    (key,row['confirmed_market_update_at'],json.dumps(row,ensure_ascii=False,allow_nan=False)))
    return row


def confirmed_event(row):
    """Strict supported producer contract, not a provider/bar/receipt clock."""
    proof=row.get('market_time_evidence',{})
    if not isinstance(proof,dict):return False
    hashes=proof.get('sha256',{}) if isinstance(proof,dict) else {}
    return (row.get('source')=='gettex' and row.get('venue')=='GETTEX'
        and row.get('currency')=='EUR' and row.get('symbol')==row.get('isin')
        and row.get('base_tf')=='trades' and row.get('outcome')=='RECEIVED'
        and proof.get('contract')=='validated_munc_mund_pair_v1'
        and set(hashes)=={'munc','mund'}
        and all(isinstance(h,str) and len(h)==64 and all(c in '0123456789abcdef' for c in h) for h in hashes.values())
        and bool(row.get('tail_sha256'))
        and valid_time(row.get('confirmed_market_update_at'),row.get('checked_at')) is not None)


def official_trades(directory,isin,frame,hashes,anchor,checked_at):
    """After the existing atomic validated pair writer committed, diagnostics only.

    Positive-quantity source trades carry exact UTC-anchored event timestamps.
    This certifies that event time, never current market/HTTP publication time.
    Zero-quantity observations and absent rows cannot supply this clock.
    """
    try:
        import pandas as pd
        from candle_schedule import gettex_meta
        positive=frame.loc[frame.volume>0].sort_index()
        if positive.empty:return {'status':'NO_EXECUTED_TRADE'}
        at=pd.Timestamp(positive.index[-1])
        if at.tzinfo is None:at=at.tz_localize('Europe/Berlin',ambiguous='raise',nonexistent='raise')
        at=at.tz_convert('UTC');end=pd.Timestamp(anchor)
        if end.tzinfo is None or not end-pd.Timedelta(minutes=15)<=at<end:
            raise ValueError('OFFICIAL_EVENT_OUTSIDE_PAIR')
        positive.attrs=gettex_meta(isin)
        row=event(isin,'trades',positive,checked_at=checked_at,receipt_kind='VALIDATED_OFFICIAL_PAIR')
        row.update(confirmed_market_update_at=at.isoformat(),
            market_time_evidence={'contract':'validated_munc_mund_pair_v1','sha256':hashes,'pair_end':end.isoformat()},
            reason='EXACT_EXECUTED_TRADE_TIME_NOT_RECEIPT_OR_BAR_CLOSE')
        if not confirmed_event(row):raise ValueError('UNSUPPORTED_MARKET_TIME_EVIDENCE')
        return append(directory,row)
    except Exception as error:
        logging.getLogger(__name__).warning('DELIVERY_DIAGNOSTICS_FAILED %s',type(error).__name__)
        return {'status':'DIAGNOSTIC_ERROR','error_type':type(error).__name__}


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
