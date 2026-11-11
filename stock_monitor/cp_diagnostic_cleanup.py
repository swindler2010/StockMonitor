"""Bounded identity repair for duplicate legacy diagnostics; never repairs OHLCV."""
import json
from datetime import datetime,timezone


def turn(store,info):
    try:return _turn(store,info)
    except (ValueError,TypeError,KeyError,AttributeError):
        # The connection context rolls back before this optional maintenance
        # returns. Corrupt legacy metadata must not interrupt the normal feed.
        return {'state':'UNKNOWN','reason':'Malformed legacy diagnostic metadata'}


def _turn(store,info):
    isin=info['isin'];prefix='data_quality:'+isin+':'
    with store.connect() as db:
        db.execute('BEGIN IMMEDIATE')
        row=db.execute('SELECT value FROM settings WHERE key=?',('history_archive_progress:'+isin,)).fetchone()
        if not row:return {'state':'NO_MATCH'}
        archive=json.loads(row[0]).get('series',{})
        candidates=db.execute("SELECT key,value FROM settings WHERE key LIKE ? AND CASE WHEN json_valid(value) THEN json_extract(value,'$.identity') IS NULL AND json_extract(value,'$.state')='INVALID' ELSE 0 END ORDER BY key LIMIT 8",(prefix+'%',)).fetchall()
        for key,raw in candidates:
            old=json.loads(raw)
            if len(raw)>32768 or not old.get('issues'):continue
            symbol,base=key[len(prefix):].rsplit(':',1)
            # A legacy symbol alone is not a listing proof. Require one exact
            # stored series AND the same archive identity. No venue guessing.
            tuples=db.execute('SELECT DISTINCT source,venue,currency,symbol,base_tf FROM series WHERE isin=? AND symbol=? AND base_tf=?',(isin,symbol,base)).fetchall()
            if symbol==isin:
                tuples+=db.execute("SELECT DISTINCT source,'GETTEX','EUR',isin,base_tf FROM candles WHERE isin=? AND source='gettex' AND base_tf=?",(isin,base)).fetchall()
            tuples=set(tuples)
            if len(tuples)!=1:continue
            identity_tuple=next(iter(tuples))
            if not all(identity_tuple):continue
            matches=[v for k,v in archive.items() if tuple(json.loads(k))==identity_tuple]
            if len(matches)!=1:continue
            report=matches[0].get('quality',{});meta=report.get('identity',{})
            fields=('source','venue','currency','symbol')
            if tuple(meta.get(k) for k in fields)!=identity_tuple[:4] or report.get('base_tf')!=base:continue
            if report.get('state')!='INVALID' or not set(old['issues']).issubset(report.get('issues',[])):continue
            try:
                checked=datetime.fromisoformat(report['checked_at'])
                if checked.tzinfo is None or checked>datetime.now(timezone.utc):continue
                if old.get('checked_at') and datetime.fromisoformat(old['checked_at'])>checked:continue
            except (KeyError,ValueError,TypeError):continue
            # Preserve the original failure and its age. The archive may be
            # stale: it is used only to disambiguate identity, never as a new
            # quality result, freshness check or trading permission.
            updated=dict(old,identity=meta,base_tf=base)
            receipt_key='cp_diagnostic_cleanup:'+isin
            prior=db.execute('SELECT value FROM settings WHERE key=?',(receipt_key,)).fetchone()
            history=json.loads(prior[0]) if prior else {}
            if len(history)>=32:return {'state':'RECEIPT_LIMIT'}
            history[key]=dict(before=old,after_identity=meta,normalized_at=datetime.now(timezone.utc).isoformat(),
                reason='Duplicate unaddressed INVALID; same error retained under unique stored identity')
            changed=db.execute('UPDATE settings SET value=? WHERE key=? AND value=?',
                (json.dumps(updated,ensure_ascii=False),key,raw)).rowcount
            if changed!=1:raise ValueError('Legacy diagnostic changed; no cleanup')
            db.execute('INSERT OR REPLACE INTO settings VALUES (?,?)',(receipt_key,json.dumps(history,ensure_ascii=False)))
            return dict(state='NORMALIZED',key=key,quality='INVALID',market_data_changed=False)
    return {'state':'NO_MATCH'}
