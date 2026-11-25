"""Shared read-only native evidence resolution, with no UI or data writes."""
import json,sqlite3

KEYS=('source','venue','currency','symbol','timezone')


def native_empty_resolution(db,problem,jobs):
    """Reuse the live official-empty validator, never a terminal job label alone.

    Called only inside the existing readonly view transaction. The revision
    map is captured from that transaction for the validator; it is not an
    analytical dependency proof or a permission to execute a paper order.
    """
    if (problem.get('kind')!='archive' or problem.get('tf') not in ('5m','15m')
            or problem.get('state') not in ('GAP','SOURCE_GAP','UNCONFIRMED')
            or not problem.get('range') or len(problem['range'])!=2
            or (problem['tf']=='15m' and problem['range'][0]!=problem['range'][1])):return None
    for job in jobs:
        if (not same_problem(problem,job) or job.get('status')!='RESOLVED_NATIVE'
                or (job.get('finding') or {}).get('state')!='NO_TRADES'
                or (job.get('official_timebase_repair') or {}).get('state')!='APPLIED'):continue
        if ((job.get('observation') or {}).get('result') in ('PRESENT','ZERO_VOLUME','DIFFERENT')
                and (job.get('source_comparison') or {}).get('observation_current') is True):
            continue  # Keep a current conflicting TV fact visible for review.
        from cp_reconciliation import _spec
        from cp_controls import _official_empty_problem_current
        from revision_watcher import RETIME_PREFIX,RETIME_POLICY
        try:
            spec=_spec(problem['isin'],problem['identity'],problem['tf'],*problem['range'])
            receipt=RETIME_PREFIX+problem['isin']+':'+spec['id']
            scopes=['setting:'+receipt,'setting:'+RETIME_POLICY,'setting:interval_evidence:'+problem['isin']]
            if problem['tf']=='5m':scopes.append('setting:cp_gettex_source_policy')
            revisions={k:r for k,r in db.execute('SELECT scope,revision FROM input_revisions WHERE scope IN ('+','.join('?' for _ in scopes)+')',scopes)}
            if not _official_empty_problem_current(db,problem,dict(revisions=revisions)):continue
            return dict(state='CONFIRMED_NO_TRADES',current=True,isin=problem['isin'],identity=problem['identity'],
                        tf=problem['tf'],range=problem['range'],job_id=spec['id'],receipt_key=receipt,
                        checked_at=job.get('checked_at'),revisions=revisions)
        except (ValueError,KeyError,TypeError,sqlite3.OperationalError):continue
    return None


def native_source_resolution(db,problem,jobs,memo,now=None):
    """A current complete primary-source window supersedes a stale gap label.

    Validate actual native rows on both TF, not a terminal job status,
    inferred TV absence or a trading grant. Bound the view to eight receipts.
    """
    import pandas as pd
    from revision_watcher import RETIME_PREFIX,retime_current
    from cp_reconciliation import _input,_observation_current
    meta=problem.get('identity') or {};span=problem.get('range');tf=problem.get('tf')
    if (problem.get('kind')!='archive' or tf not in ('5m','15m') or not span or len(span)!=2
            or problem.get('state') not in ('GAP','SOURCE_GAP','UNCONFIRMED')):return None
    try:
        a,b=map(pd.Timestamp,span);end=b+pd.Timedelta(minutes=5 if tf=='5m' else 15)
        if a.tzinfo or b.tzinfo or not pd.Timedelta(0)<end-a<=pd.Timedelta(minutes=45):return None
        prefix=RETIME_PREFIX+problem['isin']+':'
        memo=_view_memo(db,memo)
        clock=pd.Timestamp(now) if now is not None else memo.setdefault('_native_clock',pd.Timestamp.now(tz='UTC'))
        receipts=_source_receipts(db,problem['isin'],meta,str(a),str(end),memo)
        for key,raw in receipts:
            receipt=json.loads(raw)
            if key not in memo:memo[key]=retime_current(db,receipt,now=clock,**({'_memo':memo} if memo.get('_native_view') else {}))
            if not memo[key]:continue
            conflict=False
            for job in jobs:
                result=job.get('observation',{}).get('result')
                if result not in ('ABSENT','DIFFERENT','PRESENT','ZERO_VOLUME'):continue
                rows=[r for r in receipt['source']['rows'][tf] if pd.Timestamp(job['first'])<=pd.Timestamp(r[0])<pd.Timestamp(job['end'])]
                disputed=(result=='DIFFERENT' or result=='ABSENT' and bool(rows)
                    or result=='PRESENT' and not rows or result=='ZERO_VOLUME' and any(r[5]>0 for r in rows))
                if not disputed:continue
                binding=('review',job['id'])
                if binding not in memo:
                    # A saved 'current' flag predates the repair itself. When
                    # the visual fingerprint is available, revalidate it now;
                    # retain only unbound legacy disputes conservatively.
                    memo[binding]=(_observation_current(job,_input(db,job)) if job.get('observation',{}).get('input_fingerprint')
                        else job.get('source_comparison',{}).get('observation_current') is True)
                if memo[binding]:conflict=True;break
            if conflict:continue
            return dict(state='CONFIRMED_NATIVE_REPAIR',current=True,isin=problem['isin'],identity=meta,
                tf=tf,range=span,job_id=receipt['job_id'],receipt_key=key,checked_at=receipt['created_at'],
                basis='Complete paired official source and current exact native5m/15m window; no DQ or trading grant rewrite')
    except sqlite3.OperationalError:
        # A read-budget interruption is unavailable evidence, not a negative
        # source finding. Let the bounded caller resume without caching it.
        raise
    except (ValueError,KeyError,TypeError):return None
    return None


def confirmed_native_repair(problem):
    proof=problem.get('native_resolution') or {}
    return bool(proof.get('state')=='CONFIRMED_NATIVE_REPAIR' and proof.get('current') is True
        and proof.get('isin')==problem.get('isin') and proof.get('identity')==problem.get('identity')
        and proof.get('tf')==problem.get('tf') and problem.get('tf') in ('5m','15m')
        and proof.get('range')==problem.get('range') and proof.get('receipt_key') and proof.get('job_id'))


def confirmed_native_resolution(problem):
    return confirmed_native_absence(problem) or confirmed_native_repair(problem)


def confirmed_native_absence(problem):
    proof=problem.get('native_resolution') or {}
    return bool(proof.get('current') is True and proof.get('state')=='CONFIRMED_NO_TRADES'
                and proof.get('isin')==problem.get('isin') and proof.get('identity')==problem.get('identity')
                and proof.get('tf')==problem.get('tf') and problem.get('tf') in ('5m','15m') and proof.get('range')==problem.get('range')
                and problem.get('kind')=='archive' and problem.get('state') in ('GAP','SOURCE_GAP','UNCONFIRMED')
                and proof.get('receipt_key') and proof.get('job_id'))


def same_problem(problem,job):
    if problem.get('isin')!=job.get('isin') or problem.get('tf')!=job.get('tf'):return False
    a,b=problem.get('identity') or {},job.get('identity') or {}
    if any(not a.get(k) or a.get(k)!=b.get(k) for k in KEYS):return False
    span=problem.get('range')
    if not isinstance(span,(list,tuple)) or len(span)!=2:return False
    import pandas as pd
    try:return [pd.Timestamp(v) for v in span]==[pd.Timestamp(job[k]) for k in ('first','last')]
    except (KeyError,ValueError,TypeError):return False


def _view_memo(db,memo):
    # Cross-scope reuse is allowed only inside one immutable readonly SQL view.
    if not db.in_transaction or not db.execute('PRAGMA query_only').fetchone()[0]:return {}
    token=(db,db.execute('PRAGMA data_version').fetchone()[0],db.total_changes)
    if memo.setdefault('_native_view',token)!=token:raise ValueError('NATIVE_RESOLUTION_VIEW_CHANGED')
    return memo


def _source_receipts(db,isin,identity,first,end,memo):
    from revision_watcher import RETIME_PREFIX
    key=('native_receipt_index',isin)
    if key not in memo:
        prefix=RETIME_PREFIX+isin+':'
        memo[key]=[(k,json.loads(v)) for k,v in db.execute("""SELECT key,json_extract(value,'$.first','$.end','$.identity')
          FROM settings WHERE key>=? AND key<? AND json_extract(value,'$.state')='APPLIED'
          AND json_extract(value,'$.mode') IN ('VERIFIED_NATIVE_EMPTY','VERIFIED_NATIVE_TRADE_PARTITION','VERIFIED_NATIVE_SESSION_TAIL','VERIFIED_NATIVE_POSITIVE_PRICE_EXTREME','VERIFIED_PRIMARY_WINDOW')
          ORDER BY key""",(prefix,prefix+'\uffff'))]
    selected=[k for k,(a,b,meta) in memo[key] if isinstance(a,str) and isinstance(b,str) and a<=first and b>=end and meta==identity][:8]
    rows=[]
    for k in selected:
        cache=('native_receipt',k)
        if cache not in memo:
            row=db.execute('SELECT value FROM settings WHERE key=?',(k,)).fetchone()
            memo[cache]=row[0] if row else None
        if memo[cache] is not None:rows.append((k,memo[cache]))
    return rows
