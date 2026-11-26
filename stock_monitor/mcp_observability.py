"""Readonly, compact observability. No fetching, grants or strategy evaluation."""
import json,time,hashlib
from collections import Counter

READ_DIAGNOSTIC_REVISION="cp-reconciliation-20261125-r6"


def read(db,key,default=None):
    row=db.execute('SELECT value FROM settings WHERE key=?',(key,)).fetchone()
    return json.loads(row[0]) if row else default


def exact_reconciliation(db,info,reports,now,deadline,observer=None,_read_memo=None):
    """Match native problem IDs and independently validated exact TV intervals.

    Remaining saved DQ is suspicion, never automatically a proven candle error.
    Accepted scopes do not appear in the user-facing problem list.
    """
    import pandas as pd
    from cp_controls import effective_accepted,digest,IDENTITY,TV_ACTOR
    import cp_reconciliation as rv
    if _read_memo is not None:
        from cp_native_resolution import _view_memo
        if not db.in_transaction or not db.execute('PRAGMA query_only').fetchone()[0]:raise ValueError('READ_MEMO_REQUIRES_READONLY_VIEW')
        _view_memo(db,_read_memo)
    proof_cache=(_read_memo.setdefault('approval_proofs',{}) if _read_memo is not None else {}) if db.in_transaction and db.execute('PRAGMA query_only').fetchone()[0]==1 else None
    # A Bridge-only recycle may still share an earlier imported CP validator.
    # Keep its strict default path compatible; never convert a signature mismatch
    # into unavailable diagnostics for the whole catalog.
    memo_args={'_read_memo':proof_cache} if '_read_memo' in (getattr(effective_accepted,'__kwdefaults__',None) or {}) else {}
    accepted=effective_accepted(db,info['isin'],read(db,'cp_manual_risk:'+info['isin'],{}),**memo_args)
    approved_scopes={}
    for grant in accepted.values():
        p=grant.get('problem',{});meta={k:p.get('identity',{}).get(k) for k in IDENTITY}
        span=p.get('range')
        if p.get('isin')!=info['isin'] or not isinstance(span,list) or len(span)!=2 or not all(meta.values()):continue
        if p.get('state') not in ('GAP','SOURCE_GAP','UNCONFIRMED'):continue
        approved_scopes[(p['isin'],json.dumps(meta,sort_keys=True),p.get('tf'),json.dumps(span,sort_keys=True))]=grant
    if _read_memo is not None:
        from cp_native_resolution import _view_memo
        if not db.in_transaction or not db.execute('PRAGMA query_only').fetchone()[0]:raise ValueError('READ_MEMO_REQUIRES_READONLY_VIEW')
        _view_memo(db,_read_memo)
    native_memo={} if _read_memo is None else _read_memo.setdefault('native_resolution',{})
    jobs={};observed={};prefix=rv.PREFIX+info['isin']+':'
    for payload, in db.execute("SELECT value FROM settings WHERE key>=? AND key<? AND json_type(value,'$.observation')='object'",(prefix,prefix+'\uffff')):
        job=json.loads(payload);key=(json.dumps(job.get('identity'),sort_keys=True),job.get('tf'),job.get('first'),job.get('end'))
        jobs.setdefault(key,[]).append(job)
        observed.setdefault(key[:2],[]).append(job)
    owner=set();unverified=set();excluded=set();native=set();budget_exceeded=False
    classifications={} if _read_memo is None else _read_memo.setdefault(('exact_scopes',str(now),digest(reports)),{})
    def classify(key,kind):
        {'owner_risk':owner,'unverified':unverified,'excluded':excluded,'official_confirmed':native}[kind].add(key)
        classifications[key]=kind
        if observer:observer(key,kind)
    def limited():
        return dict(evidence_status='BUDGET_EXCEEDED',unresolved_errors=None,owner_accepted_risk=None,unverified_saved_scopes=None)
    for report_key,q in reports.items():
        if q.get('state')=='READY':continue
        tf=q.get('base_tf',report_key.rsplit(':',1)[-1]);meta={k:q.get('identity',{}).get(k) for k in IDENTITY}
        if not meta['timezone']:
            import data_quality as dq
            meta['timezone']=dq.TIMEZONES.get(dq.CALENDARS.get(meta['venue']))
        meta_key=json.dumps(meta,sort_keys=True)
        from timeframes import MINUTES
        step=pd.Timedelta(minutes=MINUTES[tf]) if tf in MINUTES and jobs else None
        for span in q.get('ranges') or [None]:
            item=dict(isin=info['isin'],kind='archive',identity=meta,tf=tf,state=q.get('state','UNKNOWN'),reasons=q.get('issues',[]),range=span,report_key=report_key)
            pid=digest({k:v for k,v in item.items() if k!='reasons' or not span}) if accepted else None
            canonical=(info['isin'],meta_key,tf,json.dumps(span or q.get('issues',[]),sort_keys=True))
            if not all(meta.values()):canonical=(*canonical,report_key) # Unknown feeds are not proven identical entities.
            if canonical in classifications:
                classify(canonical,classifications[canonical]);continue
            grant=(accepted.get(pid) or approved_scopes.get(canonical)) if q.get('state') in ('GAP','SOURCE_GAP','UNCONFIRMED') else None
            if grant:
                kind='excluded' if grant.get('actor')==TV_ACTOR else 'owner_risk'
                classify(canonical,kind)
                continue
            if (span and len(span)==2 and tf in ('5m','15m') and meta.get('source')=='gettex_chart'
                    and meta.get('venue')=='GETTEX' and meta.get('currency')=='EUR'
                    and q.get('state') in ('GAP','SOURCE_GAP','UNCONFIRMED')):
                if time.monotonic()>deadline:return limited()
                else:
                    from cp_native_resolution import native_source_resolution
                    overlaps=[j for j in observed.get((meta_key,tf),[]) if j['first']<=span[1] and j['last']>=span[0]]
                    if native_source_resolution(db,item,overlaps,native_memo,now=now):
                        classify(canonical,'official_confirmed')
                        continue
            covered=False
            if jobs and span and len(span)==2 and q.get('state') in ('GAP','SOURCE_GAP','UNCONFIRMED'):
                if step is not None:
                    try:key=(meta_key,tf,str(pd.Timestamp(span[0])),str(pd.Timestamp(span[1])+step))
                    except (ValueError,TypeError):
                        unverified.add(canonical)
                        if observer:observer(canonical,'unverified')
                        continue
                    for job in jobs.get(key,[]):
                        if time.monotonic()>deadline:return limited()
                        obs=job.get('observation',{})
                        if obs.get('identity_confirmed') is not True or obs.get('result')!='ABSENT':continue
                        try:covered=rv._observation_current(job,rv._input(db,job,_memo=proof_cache))
                        except (ValueError,KeyError,TypeError):covered=False
                        if covered:break
            classify(canonical,'excluded' if covered else 'unverified')
    return dict(owner_accepted_risk=len(owner),unresolved_errors=None,
        unverified_saved_scopes=len(unverified),evidence_status='BUDGET_EXCEEDED' if budget_exceeded else 'EXACT_SCOPES_RECONCILED',
        error_unit='Independent unresolved candle errors; unknown until confirmed. Saved DQ scopes are not errors.',
        # Audit-only count; no TV column or accepted interval in user report.
        internal_excluded_scope_count=len(excluded),official_confirmed_scopes=len(native))


def enrich(db,infos,rows,state,now,assessments=None):
    from calculation_window import eligibility_state,candidate_state
    import pandas as pd
    clock=pd.Timestamp(now);deadline=time.monotonic()+30
    day=clock.tz_convert('Europe/Berlin').normalize();start=day.tz_convert('UTC').isoformat();end=(day+pd.DateOffset(days=1)).tz_convert('UTC').isoformat()
    tables={r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    journal={};recoveries={}
    if 'candle_revision_history' in tables:
        columns="isin,json_extract(identity,'$.source'),json_extract(identity,'$.venue'),json_extract(identity,'$.currency'),json_extract(identity,'$.symbol'),base_tf,ts"
        journal=dict(db.execute('SELECT isin,count(*) FROM (SELECT '+columns+' FROM candle_revision_history WHERE julianday(detected_at)>=julianday(?) AND julianday(detected_at)<julianday(?) GROUP BY '+columns+') GROUP BY isin',(start,end)).fetchall())
    if 'candle_recoveries' in tables:
        recoveries=dict(db.execute('SELECT isin,count(*) FROM candle_recoveries WHERE julianday(observed_at)>=julianday(?) AND julianday(observed_at)<julianday(?) GROUP BY isin',(start,end)).fetchall())
    for row,(isin,info) in zip(rows,infos):
        prefix='data_quality:'+isin+':'
        reports={k:json.loads(v) for k,v in db.execute('SELECT key,value FROM settings WHERE key>=? AND key<?',(prefix,prefix+'\uffff'))}
        try:row.update(exact_reconciliation(db,info,reports,clock,deadline))
        except (ValueError,KeyError,TypeError):row.update(evidence_status='RECONCILIATION_UNAVAILABLE',unresolved_errors=None,owner_accepted_risk=None)
        assessment=assessments[isin] if assessments is not None else read(db,'cp_eligibility:'+isin,{})
        validity=eligibility_state(db,assessment,clock);memo={};tf_rows={}
        for c in assessment.get('candidates',[]):
            valid=candidate_state(db,assessment,c,clock,memo) if time.monotonic()<=deadline else 'NOT_REVALIDATED_BUDGET'
            engine=c.get('result') or {}
            tf_rows[c['timeframe']]=dict(validity=valid,analysis_ready=bool(valid=='CURRENT' and c.get('analysis_ready')),
                signal_ready=bool(valid=='CURRENT' and c.get('signal_ready')),saved_analysis_ready=c.get('analysis_ready'),
                execution_permission=False,signal=engine.get('signal'),checked_at=assessment.get('checked_at'),expires_at=c.get('expires_at'),
                blockers=c.get('blockers',[])[:12],trade_blockers=c.get('trade_blockers',[])[:12])
        from analytics import TIMEFRAMES
        for tf in TIMEFRAMES:tf_rows.setdefault(tf,dict(validity='UNKNOWN',analysis_ready=False,signal_ready=False,execution_permission=False,reason='NO_SAVED_ASSESSMENT'))
        row['saved_all_tf']=dict(validity=validity,timeframes=tf_rows,scope='Saved proof checked at report time; independent selected/read analytics reported separately')
        if (state.get('selected') or {}).get('isin')==isin and state.get('selected_analysis'):
            engine=state['selected_analysis']
            row['independent_analysis']=dict(timeframe=state['selected'].get('timeframe'),validity=state.get('selected_analysis_status'),
                status=engine.get('status'),signal=engine.get('signal'),completed_at=state.get('selected_analysis_completed_at'),execution_permission=False)
        prefix='cp_reconciliation_job:'+isin+':'
        row['queue']={'reconciliation':dict(db.execute("SELECT coalesce(json_extract(value,'$.status'),'UNKNOWN'),count(*) FROM settings WHERE key>=? AND key<? GROUP BY 1",(prefix,prefix+'\uffff')).fetchall()),
            'assessment_requested':bool(read(db,'cp_assessment_request:'+isin)),
            'repair_states':dict(Counter(v.get('state','UNKNOWN') for v in read(db,'cp_repair_requests:'+isin,{}).values() if isinstance(v,dict)))}
        success=read(db,'gettex_last_success:'+isin)
        if isinstance(success,dict):success={k:success[k] for k in ('at','updated_at','time','last_success','source','status','count') if k in success}
        row['source_health']={'last_saved_gettex_success':success,'basis':'Saved observation; no live-source or execution claim'}
        progress=read(db,'history_archive_progress:'+isin,{})
        row['loader']={k:progress[k] for k in ('state','status','updated_at','started_at','completed_at','total_series','completed_series') if k in progress}
        row['journal_today']=dict(start=start,end=end,
            changed_unique_physical_keys=journal.get(isin,0) if 'candle_revision_history' in tables else None,
            unique_recovery_receipts=recoveries.get(isin,0) if 'candle_recoveries' in tables else None,
            semantics='Journal-backed physical source/venue/currency/symbol/baseTF/timestamp keys; categories may overlap, never sum as unique new candles; no manual/CP attribution inferred.')
        semantic={k:row.get(k) for k in ('isin','stored_rows_by_table','unresolved_errors','owner_accepted_risk','unverified_saved_scopes','queue')}
        semantic['timeframes']={tf:{k:v.get(k) for k in ('validity','analysis_ready','signal_ready','signal','blockers')} for tf,v in tf_rows.items()}
        semantic['independent_analysis']={k:row.get('independent_analysis',{}).get(k) for k in ('timeframe','validity','status','signal')}
        row['fingerprint']=hashlib.sha256(json.dumps(semantic,sort_keys=True,default=str).encode()).hexdigest()
    return rows


def short_report(summary,previous=None):
    """At most ten significant stock changes, exact comparable scopes only."""
    current={r['isin']:r for r in summary['by_stock'] if r.get('kind')!='benchmark'}
    old={r['isin']:r for r in (previous or {}).get('by_stock',[]) if r.get('kind')!='benchmark'}
    scope=sorted((r['isin'],r.get('kind','stock')) for r in summary['by_stock'])
    comparable=bool(old and old.keys()==current.keys() and (previous or {}).get('catalog_scope')==scope)
    metrics={}
    for key,value in [('stocks',summary['totals']['stocks']),('stored_physical_rows',sum(summary['totals']['stored_rows_by_table'].values())),('unresolved_errors',summary['totals']['unresolved_errors']),('owner_accepted_risk',summary['totals']['owner_accepted_risk'])]:
        before=(previous or {}).get('metrics',{}).get(key) if comparable else None
        metrics[key]=dict(before=before,after=value,delta=value-before if isinstance(value,(int,float)) and isinstance(before,(int,float)) else None)
    changed=[]
    for isin,row in current.items():
        prior=old.get(isin)
        if prior and prior.get('fingerprint')!=row.get('fingerprint'):
            changed.append(dict(isin=isin,name=row.get('name'),unresolved_errors=row.get('unresolved_errors'),owner_accepted_risk=row.get('owner_accepted_risk'),
                stored_physical_rows=sum(row['stored_rows_by_table'].values()),change='Изменились сохранённые данные или состояние проверки; это не утверждение о новой ошибке свечей'))
    changed.sort(key=lambda r:(-(r['unresolved_errors'] or 0),r.get('name') or ''))
    fmt=lambda v:'не удалось определить' if v is None else str(v)
    lines=[f"СМ · {summary['generated_at']} · Bridge running · revision {summary['revision'][:12]}"]
    labels={'stocks':'Акции','stored_physical_rows':'Физические OHLCV-записи активного каталога','unresolved_errors':'Доказанные нерешённые ошибки','owner_accepted_risk':'Принятый исторический риск (точные области)'}
    for key,value in metrics.items():lines.append(labels[key]+': '+fmt(value['before'])+' → '+fmt(value['after'])+' → Δ '+fmt(value['delta'])+'.')
    lines.append('Нет сопоставимого исходного среза.' if not comparable else 'Сравнение одного состава активных акций и одинаковых определений счётчиков.')
    for row in changed[:10]:lines.append(' '.join(str(row['name']).split()[:12])+': сохранено '+str(row['stored_physical_rows'])+' записей; подтверждённых ошибок '+fmt(row['unresolved_errors'])+'.')
    lines.append(f"Остальные {len(current)-min(10,len(changed))} акций сгруппированы. Сырые DQ не считаются ошибками. Исполнение не разрешается этой сводкой.")
    return dict(text='\n'.join(lines),metrics=metrics,changed_stocks=changed[:10],other_stock_count=len(current)-min(10,len(changed)),baseline_comparable=comparable,
        baseline_at=(previous or {}).get('generated_at'),baseline_semantics='Previous completed observation in this Bridge instance; restart resets baseline, never invent a daily delta.')


def baseline(summary):
    return dict(generated_at=summary['generated_at'],catalog_scope=sorted((r['isin'],r.get('kind','stock')) for r in summary['by_stock']),
        by_stock=[{k:r.get(k) for k in ('isin','kind','fingerprint')} for r in summary['by_stock']],
        metrics={k:v['after'] for k,v in summary['short_report']['metrics'].items()})
