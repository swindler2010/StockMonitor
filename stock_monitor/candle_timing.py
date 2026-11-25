"""Observed storage timings, never inferred provider publication/receipt times.

Triggers cover the existing ingestion and repair writers in their transactions.
No history backfill, OHLCV changes, or trading dependency is introduced here.
"""
import pandas as pd

KEYS=('isin','source','venue','currency','symbol','base_tf','ts')
VALUES=('open','high','low','close','volume')


def install(db):
    db.execute('''CREATE TABLE IF NOT EXISTS candle_observations (
        isin TEXT, source TEXT, venue TEXT, currency TEXT, symbol TEXT,
        base_tf TEXT, ts TEXT, first_insert_observed_at TEXT,
        last_change_observed_at TEXT NOT NULL, observations INTEGER NOT NULL,
        open REAL, high REAL, low REAL, close REAL, volume REAL,
        PRIMARY KEY(isin,source,venue,currency,symbol,base_tf,ts))''')
    for table in ('series','candles'):
        columns=[x[1] for x in db.execute('PRAGMA table_info('+table+')')]
        primary=[x[1] for x in db.execute('PRAGMA table_info('+table+')') if x[5]]
        same=' AND '.join('NEW.'+k+' IS '+k for k in columns)
        existing=' AND '.join('NEW.'+k+' IS '+k for k in primary)
        changed=' OR '.join('NEW.'+k+' IS NOT OLD.'+k for k in columns)
        # Only official GETTEX candles have a verified fixed identity in this
        # legacy table. CSV/unknown venues must not acquire invented metadata.
        supported="NEW.source='gettex'" if table=='candles' else '1'
        expr={k:'NEW.'+k for k in KEYS+VALUES}
        if table=='candles':expr.update(venue="'GETTEX'",currency="'EUR'",symbol='NEW.isin')
        stamp="strftime('%Y-%m-%dT%H:%M:%fZ','now')"
        for event in ('insert','update'):
            first=(f'CASE WHEN EXISTS(SELECT 1 FROM {table} WHERE {existing}) THEN NULL ELSE {stamp} END'
                   if event=='insert' else 'NULL')
            values=','.join([*(expr[k] for k in KEYS),first,stamp,'1',*(expr[k] for k in VALUES)])
            updates=','.join(k+'=excluded.'+k for k in ('last_change_observed_at',)+VALUES)
            body=(f'INSERT INTO candle_observations VALUES ({values}) ON CONFLICT({",".join(KEYS)}) '
                  f'DO UPDATE SET {updates},observations=candle_observations.observations+1;')
            when=f'{supported} AND NOT EXISTS(SELECT 1 FROM {table} WHERE {same})' if event=='insert' else f'{supported} AND ({changed})'
            phase='BEFORE INSERT' if event=='insert' else 'AFTER UPDATE'
            db.execute(f'CREATE TRIGGER IF NOT EXISTS candle_timing_{table}_{event} {phase} ON {table} WHEN {when} BEGIN {body} END')


def annotate_page(store,isin,tf,identity,page,calculated_at):
    """Bounded exact-base read; derived candles need constituent timing support.

    A storage observation occurs inside the write transaction, not at commit,
    HTTP receipt or source publication. Missing old timings remain unknown.
    """
    if not page:return 0
    for bar in page:
        bar['delivery_timing']={'status':'NOT_MEASURED','provider_published_at':None,
            'application_received_at':None,'analysis_observed_at':calculated_at,
            'note':'Storage observation is not source delivery time or commit time.'}
    if tf not in ('5m','15m','1H','1D'):
        for bar in page:bar['delivery_timing']['status']='NOT_MEASURED_DERIVED'
        return 0
    if not all(identity.get(k) for k in ('source','venue','currency','symbol','timezone')):return 0
    stamps=[str(pd.Timestamp(b['open_at']).tz_convert(identity['timezone']).tz_localize(None)) for b in page]
    with store.connect() as db:
        if not db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='candle_observations'").fetchone():return 0
        rows=db.execute('SELECT ts,first_insert_observed_at,last_change_observed_at,observations,open,high,low,close,volume '
            'FROM candle_observations WHERE isin=? AND source=? AND venue=? AND currency=? AND symbol=? AND base_tf=? '
            'AND ts IN ('+','.join('?' for _ in stamps)+')',
            (isin,*(identity[k] for k in ('source','venue','currency','symbol')),tf,*stamps)).fetchall()
    observed={r[0]:r[1:] for r in rows};measured=0
    for stamp,bar in zip(stamps,page):
        row=observed.get(stamp)
        # A concurrent changed series must not lend its timing to older OHLCV.
        if row is None or any(bar.get(k)!=v for k,v in zip(VALUES,row[3:])):continue
        close=pd.Timestamp(bar['close_at']);written=pd.Timestamp(row[1])
        delta=(written-close).total_seconds()
        bar['delivery_timing'].update(status='PARTIAL_MEASURED',first_insert_observed_at=row[0],
            storage_write_observed_at=row[1],observations=row[2],
            bar_close_to_storage_seconds=delta if bar.get('closed') and delta>=0 else None,
            interpretation='May include historical backfill; not provider latency.')
        measured+=1
    return measured


def record_recovery(db,isin,meta,base,original,window):
    """Called ONLY by validated recovery inside its candle write transaction.
    Count missing interior bars or proven corrupt old OHLCV, never new tails.
    """
    import json,math
    db.execute("CREATE TABLE IF NOT EXISTS candle_recoveries(isin TEXT,source TEXT,venue TEXT,currency TEXT,symbol TEXT,base_tf TEXT,ts TEXT,observed_at TEXT,reason TEXT,old_ohlcv TEXT,new_ohlcv TEXT,PRIMARY KEY(isin,source,venue,currency,symbol,base_tf,ts))")
    for ts,row in window.iterrows():
        old=original.loc[ts] if ts in original.index else None
        if old is None:
            if original.empty or not original.index.min()<ts<original.index.max():continue
            reason='MISSING_INTERIOR';old_values=None
        else:
            old_values=[float(old[k]) for k in VALUES]
            o,h,l,c,v=old_values
            invalid=not all(math.isfinite(x) for x in old_values) or min(o,h,l,c)<=0 or v<0 or h<max(o,l,c) or l>min(o,h,c)
            if not invalid:continue
            reason='INVALID_OHLCV'
        values=[float(row[k]) for k in VALUES]
        db.execute("INSERT OR IGNORE INTO candle_recoveries VALUES (?,?,?,?,?,?,?,strftime('%Y-%m-%dT%H:%M:%fZ','now'),?,?,?)",(isin,*(meta[k] for k in ('source','venue','currency','symbol')),base,str(ts),reason,json.dumps(old_values),json.dumps(values)))
