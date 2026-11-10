"""Restartable history coverage, scoped to one provider/listing/interval."""
import json
import math
import pandas as pd

FIELDS=('open','high','low','close','volume')
IDENTITY=('source','venue','currency','symbol')
INTRADAY_RETENTION_DAYS={'5m':60,'15m':60,'1H':730}

def intraday_retention_start(base,now):
    """Existing Yahoo rolling archive policy, shared by backfill and repair."""
    days=INTRADAY_RETENTION_DAYS.get(base)
    return pd.Timestamp(now)-pd.Timedelta(days=days)+pd.Timedelta(minutes=1) if days else None

def validate(frame):
    if frame.empty:raise ValueError('Источник не вернул свечи')
    if not all(frame.attrs.get(k) for k in IDENTITY+('timezone',)):
        raise ValueError('Не подтверждены идентичность и timezone истории')
    if frame.index.has_duplicates or not frame.index.is_monotonic_increasing:
        raise ValueError('Некорректные timestamps истории')
    values=frame[list(FIELDS)]
    if not values.map(lambda x:math.isfinite(float(x))).all().all():
        raise ValueError('Неполные OHLCV')
    if (values[['open','high','low','close']]<=0).any().any() or (values.volume<0).any():
        raise ValueError('Некорректные цены или объём')
    if ((values.high<values[['open','close','low']].max(axis=1)) |
        (values.low>values[['open','close','high']].min(axis=1))).any():
        raise ValueError('Невозможные OHLC')

def key(info,symbol,base):
    return 'history_coverage:'+info['isin']+':'+symbol+':'+base

def display_coverage(store,info,source,timeframe,base):
    """Describe only the displayed source; proxy coverage is not GETTEX coverage."""
    if source=='gettex':
        return 'SOURCE_LIMITED • GETTEX: глубина архива ограничена; 20 лет не подтверждены',''
    if source!='proxy':
        return 'История источника не подтверждена',''
    identity=store.setting('history_identity:'+info['isin'],{})
    daily=store.setting(key(info,identity.get('symbol',''),'1D'),identity)
    status='история проверяется' if not identity else daily.get('status','INCOMPLETE')
    if timeframe not in ('1D','1W','1M'):
        intraday=store.setting(key(info,identity.get('symbol',''),base),{})
        status=intraday.get('status','INCOMPLETE')+' • '+intraday.get('limitation','intraday проверяется')
    coverage=(f" • 1D: {daily.get('count',0)} свечей, {daily.get('first','?')[:10]} — {daily.get('last','?')[:10]}" if identity else '')
    return status,coverage

def sync(info,store,symbol,base,download,now=None):
    import data_quality as quality
    previous_quality=store.setting(quality.key(info,symbol,base),{})
    clock=pd.Timestamp(now or pd.Timestamp.now(tz='UTC'))
    source_retry=store.setting('cp_source_retry:proxy',None)
    if source_retry and clock<pd.Timestamp(source_retry):raise quality.QualityError(previous_quality or {'state':'SOURCE_UNAVAILABLE','issues':['Proxy source cooldown'],'ranges':[]})
    fresh=_fresh_daily_context(info,store,symbol,base,previous_quality,clock)
    if fresh is not None:
        retry=store.setting(key(info,symbol,base),{}).get('fresh_tail',{}).get('retry_after')
        if retry and clock<pd.Timestamp(retry):raise quality.QualityError(previous_quality)
        try:return _sync(info,store,symbol,base,download,clock,fresh=fresh)
        except quality.QualityError as error:
            report=error.report
            if (report.get('state')=='INVALID' and report.get('repairable') and report.get('ranges') and
                    all((report.get('identity') or {}).get(k)==fresh['meta'].get(k) for k in IDENTITY+('timezone',))):
                with store.connect() as db:
                    db.execute('BEGIN IMMEDIATE')
                    # A valid prefix already committed its retry metadata. Do
                    # not reinterpret that as a second failed response.
                    if all((db.execute('SELECT revision FROM input_revisions WHERE scope=?',(scope,)).fetchone() or [0])[0]==revision for scope,revision in fresh['token'].items()):
                        row=db.execute('SELECT value FROM settings WHERE key=?',(key(info,symbol,base),)).fetchone()
                        state=json.loads(row[0]) if row else {};prior=state.get('fresh_tail',{}).get('rejected_response') or {}
                        repeated=all(prior.get(k)==report.get(k) for k in ('state','identity','base_tf','issues','ranges'))
                        state['fresh_tail']={**state.get('fresh_tail',{}),'rejected_response':report,
                            'retry_after':(clock+(pd.Timedelta(days=1) if repeated else pd.Timedelta(minutes=5))).isoformat()}
                        db.execute('INSERT OR REPLACE INTO settings VALUES (?,?)',(key(info,symbol,base),json.dumps(state)))
            raise
    if previous_quality.get('raw_retry_after') and clock<pd.Timestamp(previous_quality['raw_retry_after']):
        # Rejected old archive bytes have their own retry deadline. They must
        # not starve a provably disjoint, closed daily tail of the SAME listing.
        raise quality.QualityError(previous_quality)
    try:return _sync(info,store,symbol,base,download,now)
    except Exception as error:
        report=error.report if isinstance(error,quality.QualityError) else {'state':'SOURCE_UNAVAILABLE','issues':[str(error)],'ranges':[]}
        # Two matching invalid archive responses are evidence of a persistent
        # provider defect. Do not download the entire rejected archive every
        # five minutes; keep the existing listing/TF-scoped retry timestamp.
        signature=('state','identity','base_tf','issues','ranges')
        repeated_invalid=(report.get('state')=='INVALID'
            and bool(previous_quality.get('raw_retry_after'))
            and all(report.get(k)==previous_quality.get(k) for k in signature))
        delay=pd.Timedelta(days=1) if repeated_invalid else pd.Timedelta(minutes=5)
        report['raw_retry_after']=(clock+delay).isoformat()
        quality.persist(store,info,symbol,base,report)
        previous=store.setting(key(info,symbol,base),{})
        store.set_setting(key(info,symbol,base),{**previous,'status':'ERROR','last_error':str(error),
                          'last_attempt':pd.Timestamp.now(tz='UTC').isoformat()})
        raise

def _fresh_daily_context(info,store,symbol,base,previous,now):
    """Narrow existing sync to a disjoint tail; never waive archive INVALID."""
    import data_quality as quality
    from eur_listings import confirmed_listing,stored_identity
    from history_service import parent_provenance_key
    if base!='1D' or previous.get('state')!='INVALID' or not previous.get('repairable') or not previous.get('ranges'):
        return None
    scopes=['instrument:'+info['isin'],'market:'+info['isin']+':1D',
            'setting:'+quality.key(info,symbol,base),'setting:'+key(info,symbol,base),
            'setting:history_identity:'+info['isin'],'setting:cp_source_retry:proxy']
    meta=previous.get('identity') or {}
    if any(not meta.get(k) for k in IDENTITY+('timezone',)):return None
    lineage_key=parent_provenance_key(info['isin'],meta,base)
    scopes += ['setting:'+lineage_key,'setting:interval_evidence:'+info['isin'],
               *('market:'+info['isin']+':'+tf for tf in ('1m','5m','15m','1H')),
               *('setting:'+quality.key(info,symbol,tf) for tf in ('1m','5m','15m','1H'))]
    token=store.read_cache.token_for(scopes)
    if store.setting(quality.key(info,symbol,base),{})!=previous:return None
    retry=store.setting('cp_source_retry:proxy',None)
    if retry and now<pd.Timestamp(retry):return None
    selected=store.setting('history_identity:'+info['isin'],{})
    with store.connect() as db:
        saved=db.execute('SELECT payload FROM instruments WHERE isin=?',(info['isin'],)).fetchone()
        actual=json.loads(saved[0]) if saved else info
        proven=stored_identity(db,actual,meta,base)
    if not proven:return None
    meta={**meta,**proven}
    # Existing automatic EUR candidates may be separate from the UI selection.
    # A user pin without automatic listing selection remains binding.
    if saved:
        reserve=(actual.get('eur_history_auto') and selected.get('symbol')!=symbol and
                 all(not actual.get('history_'+k) or actual['history_'+k]==selected.get(k) for k in ('symbol','venue','currency')))
        if not reserve and any(actual.get('history_'+k) and actual['history_'+k]!=meta.get(k) for k in ('symbol','venue','currency')):
            return None
    if (meta.get('source')!='proxy' or meta.get('symbol')!=symbol or meta.get('currency')!='EUR'
            or not all(meta.get(k) for k in IDENTITY+('timezone',)) or not confirmed_listing(info,meta)):
        return None
    if any(info.get('history_'+k) and info['history_'+k]!=meta.get(k) for k in ('symbol','venue','currency')):
        return None
    if any((previous.get('identity') or {}).get(k)!=meta.get(k) for k in IDENTITY+('timezone',)):
        return None
    cached=store.load_series(info['isin'],base,'proxy',symbol,meta['venue'],meta['currency'])
    if cached.empty or not cached.index.is_unique or not cached.index.is_monotonic_increasing:return None
    start=pd.Timestamp(cached.index[-1]).normalize()+pd.Timedelta(days=1)
    try:
        if any(pd.Timestamp(a)>pd.Timestamp(b) for a,b in previous['ranges']):return None
        # Do not refetch a known defective overlapping day before its existing
        # deadline. After that deadline, validate only the incremental response.
        if (previous.get('raw_retry_after') and now<pd.Timestamp(previous['raw_retry_after'])
                and any(pd.Timestamp(end)>=start for _,end in previous['ranges'])):return None
    except (TypeError,ValueError):return None
    if store.read_cache.token_for(scopes)!=token:return None
    return dict(meta=meta,start=start.tz_localize(meta['timezone']),token=token,quality=previous,lineage_key=lineage_key)


def _sync(info,store,symbol,base,download,now=None,fresh=None):
    import data_quality as quality
    now=pd.Timestamp(now or pd.Timestamp.now(tz='UTC'))
    interval={'1D':'1d','1H':'60m','15m':'15m','5m':'5m'}[base]
    requested=now-pd.DateOffset(years=20)
    days=INTRADAY_RETENTION_DAYS.get(base)
    # Yahoo retains only a rolling intraday window. Do not fabricate older bars.
    step=pd.Timedelta(hours=1) if base=='1H' else pd.Timedelta(minutes=5 if base=='5m' else 15)
    target=pd.Timestamp('1900-01-01',tz='UTC') if base=='1D' else intraday_retention_start(base,now)
    state=store.setting(key(info,symbol,base),{})
    if fresh:state={**fresh['meta'],**state}
    cached=store.load_series(info['isin'],base,'proxy',symbol,
                             state.get('venue'),state.get('currency'))
    start=target
    if fresh:
        start=fresh['start']
        from candle_schedule import boundary
        eligible=boundary(fresh['meta'],base,now).get('close_at')
        if not eligible or start.date()>pd.Timestamp(eligible).tz_convert(fresh['meta']['timezone']).date():
            return {**state,'quality_state':'INVALID','fresh_tail_wait':'NO_NEW_CLOSED_SESSION'}
    elif state.get('backfill_complete') and not cached.empty:
        # Refresh only the final potentially forming bar, never seven closed days.
        start=pd.Timestamp(cached.index[-1]).tz_localize(state['timezone'],ambiguous='raise',nonexistent='raise')
        if base=='1D' and start.date()<now.tz_convert(state['timezone']).date():
            start+=pd.Timedelta(days=1)
        elif base!='1D' and start+step<=now:
            start+=step
    frame=download(symbol,interval,start)
    if fresh and frame.empty:
        # An empty supplier response proves neither completion nor NO_TRADES.
        raise quality.QualityError({'state':'SOURCE_UNAVAILABLE','issues':['No published closed daily tail'],'ranges':[]})
    if base!='1D' and not frame.empty:
        from intraday import normalize
        frame=normalize(frame,base,now=now,start=start,end=now)
    if frame.empty and state.get('backfill_complete') and not cached.empty:
        if state.get('status')=='ERROR':
            state={k:v for k,v in state.items() if k!='last_error'}
            state['status']=state.get('completion_status','SOURCE_LIMITED' if base!='1D' else 'COMPLETE')
            store.set_setting(key(info,symbol,base),state)
        import data_quality as quality
        checked=quality.repair(info,store,symbol,base,state,download,now)
        if base=='1D':return state
        return {**state,'quality_state':checked['state'],'quality_issues':checked.get('issues',[])+([checked['recovery_error']] if checked.get('recovery_error') else [])}
    meta=frame.attrs
    import data_quality as quality
    expected=state or {'symbol':symbol,'source':'proxy'}
    if info.get('history_currency'):expected={**expected,'currency':info['history_currency']}
    if info.get('history_venue'):expected={**expected,'venue':info['history_venue']}
    report=quality.audit(frame,base,expected,now,check_stale=False)
    if frame.index.tz is not None and str(frame.index.tz)==meta.get('timezone'):
        frame.index=frame.index.tz_localize(None)
    # An incremental response is not the whole cached history. Keep its DQ and
    # recovery cooldown until the merged cache is audited below.
    if not fresh and (base=='1D' or cached.empty):quality.persist(store,info,symbol,base,report)
    if fresh:
        rejected=None
        local_proofs={}
        if report['state']=='INVALID' and report.get('repairable') and 0<len(report.get('ranges',[]))<=8:
            from history_service import recover_from_children
            # Only complete genuine children of this exact listing. This does
            # not write anything; proofs and rows commit together below.
            repaired=frame.copy()
            for a,b in report['ranges']:
                if pd.Timestamp(a)<start.tz_localize(None):continue
                replacement=recover_from_children(info,store,meta,base,a,b,now)
                if replacement is None:continue
                values=replacement.reindex(columns=frame.columns).copy()
                for field in IDENTITY:
                    if field in values:values[field]=meta[field]
                repaired=pd.concat([repaired.loc[(repaired.index<pd.Timestamp(a))|(repaired.index>pd.Timestamp(b))],values]).sort_index()
                repaired.attrs=meta.copy()
                local_proofs.update(replacement.attrs.get('parent_recovery_proofs',{}))
            repaired_report=quality.audit(repaired,base,expected,now,check_stale=False)
            if local_proofs:frame,report=repaired,repaired_report
        if report['state']=='INVALID' and report.get('repairable') and report.get('ranges'):
            # Keep only a contiguous, independently audited prefix before the
            # FIRST known defect. Never skip across a bad/missing bar or forge
            # a replacement. The full rejected response diagnosis is retained.
            rejected=report
            first_bad=min(pd.Timestamp(a) for a,_ in report['ranges'])
            prefix=frame.loc[frame.index<first_bad].copy()
            checked=quality.audit(prefix,base,expected,now,check_stale=False)
            if checked['state']=='READY':frame,report=prefix,checked
        if report['state']!='READY':raise quality.QualityError(report)
        from timeframes import endpoints
        frame=frame.copy();frame.attrs={**meta,'timeframe':'1D'}
        frame=frame.loc[(frame.index>=start.tz_localize(None)) & (endpoints(frame)<=now)].copy()
        if frame.empty:return {**state,'quality_state':'INVALID','fresh_tail_wait':'NO_NEW_CLOSED_SESSION'}
        meta=frame.attrs
        local_proofs={stamp:proof for stamp,proof in local_proofs.items() if pd.Timestamp(stamp) in frame.index}
    if report['state']=='INVALID':
        try:frame=quality.repair_incoming(info,store,symbol,base,frame,download,report,now)
        except quality.QualityError:raise
        except Exception as error:
            report['recovery_error']=str(error)
            raise quality.QualityError(report) from error
        meta=frame.attrs
    if report['state']=='SOURCE_UNAVAILABLE':raise quality.QualityError(report)
    if base!='1D' and state.get('intraday_span') and meta.get('intraday_span'):
        meta={**meta,'intraday_span':[
            str(min(pd.Timestamp(state['intraday_span'][0]),pd.Timestamp(meta['intraday_span'][0]))),
            str(max(pd.Timestamp(state['intraday_span'][1]),pd.Timestamp(meta['intraday_span'][1])))]}
        frame.attrs=meta
    if all(state.get(k) for k in IDENTITY) and any(state.get(k)!=meta.get(k) for k in IDENTITY):
        raise ValueError('Изменились валюта/биржа/идентичность истории; автоматическое смешивание запрещено')
    first_trade=meta.get('first_trade_date')
    available=max(target,pd.Timestamp(first_trade,unit='s',tz='UTC')) if first_trade else target
    first=frame.index[0] if cached.empty else min(cached.index[0],frame.index[0])
    last=frame.index[-1] if cached.empty else max(cached.index[-1],frame.index[-1])
    # A later first bar without listing metadata is a partial archive, not a ready instrument.
    first_utc=pd.Timestamp(first).tz_localize(meta['timezone']).tz_convert('UTC')
    complete=bool(state.get('backfill_complete') or first_utc<=available+pd.Timedelta(days=7))
    from_inception=bool(first_trade and pd.Timestamp(first_trade,unit='s',tz='UTC')>requested)
    status=('SOURCE_LIMITED' if base!='1D' else 'COMPLETE_FROM_INCEPTION' if from_inception else 'COMPLETE') if complete else 'INCOMPLETE'
    result={**meta,'requested_from':target.isoformat(),'available_from':available.isoformat(),
            'first':str(first),'last':str(last),'backfill_complete':complete,
            'status':status,'completion_status':status,'requested_years':20,
            'limitation':f'Yahoo {base}: доступен только текущий архив до {days} дней' if base!='1D' else None,
            'retention_limited':base!='1D','base_tf':base,'depth_policy':'provider_maximum'}
    if fresh:
        # Coverage remains incomplete/error; the original rejected archive DQ
        # and its cooldown remain untouched. Only verified new rows are added.
        result={**state,**{k:meta[k] for k in IDENTITY+('timezone',)},
                'first':str(first),'last':str(last),'backfill_complete':False,
                'fresh_tail':{'first':str(frame.index[0]),'last':str(frame.index[-1]),
                              'checked_at':now.isoformat(),'count':len(frame),'quality':report,
                              'rejected_response':rejected},
                'quality_state':fresh['quality']['state']}
        if rejected:
            prior=state.get('fresh_tail',{}).get('rejected_response') or {}
            repeated=all(prior.get(k)==rejected.get(k) for k in ('state','identity','base_tf','issues','ranges'))
            result['fresh_tail']['retry_after']=(now+(pd.Timedelta(days=1) if repeated else pd.Timedelta(minutes=5))).isoformat()
    rows=((info['isin'],*[meta[k] for k in IDENTITY],base,str(ts),
           *[float(row[k]) for k in FIELDS]) for ts,row in frame.iterrows())
    # Candles and coverage commit together. A failed transaction cannot certify missing data.
    with store.connect() as db:
        if fresh:
            from performance_cache import InputRevisionChanged
            db.execute('BEGIN IMMEDIATE')
            if any((db.execute('SELECT revision FROM input_revisions WHERE scope=?',(scope,)).fetchone() or [0])[0]!=revision
                   for scope,revision in fresh['token'].items()):
                raise InputRevisionChanged('Fresh daily tail dependencies changed before commit')
            from performance_cache import _closed_rows,_digest
            for proof in local_proofs.values():
                if [_digest(_closed_rows(db,s)) for s in proof['inputs']]!=proof['digests']:
                    raise InputRevisionChanged('Fresh daily child lineage changed before commit')
        batch=[]
        for row in rows:
            batch.append(row)
            if len(batch)==2000:
                db.executemany('INSERT OR REPLACE INTO series VALUES (?,?,?,?,?,?,?,?,?,?,?,?)',batch);batch=[]
        if batch:db.executemany('INSERT OR REPLACE INTO series VALUES (?,?,?,?,?,?,?,?,?,?,?,?)',batch)
        if fresh and local_proofs:
            old=db.execute('SELECT value FROM settings WHERE key=?',(fresh['lineage_key'],)).fetchone()
            proofs=json.loads(old[0]) if old else {}
            for stamp,proof in local_proofs.items():
                proof['input_revisions']={s:(db.execute('SELECT revision FROM input_revisions WHERE scope=?',(s,)).fetchone() or [0])[0] for s in fresh['token']}
                proofs[stamp]=proof
            db.execute('INSERT OR REPLACE INTO settings VALUES (?,?)',(fresh['lineage_key'],json.dumps(proofs)))
        if fresh:
            # Remove only localized rejected ranges now covered by this READY
            # response. Other archival defects and the original diagnosis stay.
            old_quality=fresh['quality'];remaining=[];resolved=[]
            for a,b in old_quality['ranges']:
                grid=quality.expected_index(meta,base,a,b,now)
                covered=(pd.Timestamp(a)>=frame.index.min() and pd.Timestamp(b)<=frame.index.max()
                         and len(grid)>0 and grid.isin(frame.index).all())
                (resolved if covered else remaining).append([a,b])
            if resolved:
                updated={**old_quality,'ranges':remaining}
                if not remaining:
                    merged=pd.concat([cached.loc[~cached.index.isin(frame.index)],frame]).sort_index()
                    merged.attrs=meta.copy()
                    updated=quality.audit(merged,base,meta,now)
                updated.update(fresh_tail_resolved_ranges=resolved,
                    original_rejected_ranges=old_quality.get('original_rejected_ranges',old_quality['ranges']),
                    fresh_tail_checked_at=now.isoformat())
                result['quality_state']=updated['state']
                db.execute('INSERT OR REPLACE INTO settings VALUES (?,?)',(quality.key(info,symbol,base),json.dumps(updated)))
        count=db.execute('SELECT COUNT(*) FROM series WHERE isin=? AND source=? AND venue=? AND currency=? AND symbol=? AND base_tf=?',
                         (info['isin'],*[meta[k] for k in IDENTITY],base)).fetchone()[0]
        result['count']=count
        db.execute('INSERT OR REPLACE INTO settings VALUES (?,?)',
                   (key(info,symbol,base),json.dumps(result,allow_nan=False)))
    if fresh:
        # Let the existing scheduler retain its error retry, not certify the
        # missing end as fetched. The verified prefix was committed atomically.
        if rejected:raise quality.QualityError(rejected)
        return result
    checked=quality.repair(info,store,symbol,base,result,download,now)
    if base=='1D':return result
    return {**result,'quality_state':checked['state'],'quality_issues':checked.get('issues',[])+([checked['recovery_error']] if checked.get('recovery_error') else [])}
