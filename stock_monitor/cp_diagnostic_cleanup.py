"""Bounded identity repair for duplicate legacy diagnostics; never repairs OHLCV."""
import json
import hashlib
from datetime import datetime,timezone
RETIRE_RECEIPT_LIMIT=4096


def turn(store,info):
    try:
        result=_turn(store,info)
        return _retire_orphan(store) if result['state']=='NO_MATCH' else result
    except (ValueError,TypeError,KeyError,AttributeError):
        # The connection context rolls back before this optional maintenance
        # returns. Corrupt legacy metadata must not interrupt the normal feed.
        return {'state':'UNKNOWN','reason':'Malformed legacy diagnostic metadata'}


def _retire_orphan(store):
    """Retire one obsolete diagnostic, preserving exact bytes and all OHLCV."""
    prefix='data_quality:';receipts='cp_retired_diagnostic:'
    with store.connect() as db:
        db.execute('BEGIN IMMEDIATE')
        rows=db.execute("SELECT s.key,s.value FROM settings s LEFT JOIN instruments i "
            "ON i.isin=substr(s.key,14,instr(substr(s.key,14),':')-1) "
            "WHERE s.key LIKE 'data_quality:%' AND i.isin IS NULL AND length(s.value)<=65536 "
            "AND length(s.key)-length(replace(s.key,':',''))=3 AND s.key NOT LIKE '%::%' AND substr(s.key,-1)<>':' "
            "ORDER BY s.key LIMIT 8").fetchall()
        for key,raw in rows:
            parts=key[len(prefix):].split(':')
            if len(parts)!=3 or not all(parts):continue
            isin=parts[0]
            # Recheck inside the write transaction; a registered stock must
            # never lose its diagnostic merely because an older snapshot did.
            if db.execute('SELECT 1 FROM instruments WHERE isin=?',(isin,)).fetchone():continue
            receipt_key=receipts+hashlib.sha256((key+'\0'+raw).encode()).hexdigest()
            prior=db.execute('SELECT value FROM settings WHERE key=?',(receipt_key,)).fetchone()
            if prior:
                saved=json.loads(prior[0])
                if saved.get('key')!=key or saved.get('before_raw')!=raw:raise ValueError('Retired receipt mismatch')
            else:
                count=db.execute('SELECT count(*) FROM settings WHERE key LIKE ?',(receipts+'%',)).fetchone()[0]
                if count>=RETIRE_RECEIPT_LIMIT:return {'state':'RECEIPT_LIMIT','reason':'Original diagnostic retained'}
                saved=dict(key=key,isin=isin,before_raw=raw,retired_at=datetime.now(timezone.utc).isoformat(),
                    reason='REMOVED_INSTRUMENT',quality_repaired=False,market_data_changed=False)
                db.execute('INSERT INTO settings VALUES (?,?)',(receipt_key,json.dumps(saved,ensure_ascii=False)))
            changed=db.execute('DELETE FROM settings WHERE key=? AND value=? AND NOT EXISTS (SELECT 1 FROM instruments WHERE isin=?)',
                (key,raw,isin)).rowcount
            if changed!=1:raise ValueError('Diagnostic changed during retirement')
            return dict(state='RETIRED',key=key,receipt_key=receipt_key,reason='REMOVED_INSTRUMENT',quality_repaired=False,market_data_changed=False)
    return {'state':'NO_MATCH'}


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
