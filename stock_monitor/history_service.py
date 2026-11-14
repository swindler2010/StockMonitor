"""Separate provider series. Never join markets or currencies into a candle."""
from datetime import datetime, timezone
from urllib.parse import quote
import requests
import pandas as pd
import market

HEADERS={'User-Agent':'Mozilla/5.0 StockMonitor/0.15'}
ARCHIVE_AUDIT_VERSION='scoped-parent-and-gettex-chart-v2'

def _archive_recovery_binding(info,store,identity,base,recover_gettex=None):
    # Only proof validity for this exact listing matters here, not receipt or
    # next-request timestamps. The actual recovery resolves proof again.
    from eur_listings import stored_identity
    with store.connect() as db:confirmed=bool(stored_identity(db,info,identity,base))
    return dict(confirmed=confirmed,adapter_available=identity['source']!='gettex' or recover_gettex is not None)


def _archive_scopes(isin,identity,base,selected):
    from timeframes import MINUTES
    import data_quality as quality
    children=[tf for tf in ('1m','5m','15m','1H') if MINUTES[tf]<(1440 if base=='1D' else MINUTES[base])]
    scopes=['instrument:'+isin,'market:'+isin+':'+base,'setting:interval_evidence:'+isin,
            'setting:history_coverage:'+isin+':'+identity['symbol']+':'+base,
            'setting:'+parent_provenance_key(isin,identity,base),
            *('market:'+isin+':'+tf for tf in children)]
    if selected.get('listing_proof') and all(selected.get(k)==identity[k] for k in quality.IDENTITY):
        scopes.append('setting:history_identity:'+isin)
    return scopes


def inspect_saved_archive(info,store,now=None,recover_gettex=None,*,unit=None,local_only=False):
    """Audit one complete saved base series per feed, with durable revision progress.

    Historical coverage is distinct from current price freshness (still checked
    by runtime DQ). Never certify a batch read across changing dependencies.
    This inventory includes dormant listings, not only the current selection.
    """
    import json
    import data_quality as quality
    isin=info['isin'];key='history_archive_progress:'+isin
    progress=store.setting(key,{})
    with store.connect() as db:
        catalog=db.execute("SELECT DISTINCT source,venue,currency,symbol,base_tf FROM series WHERE isin=? UNION SELECT DISTINCT source,'GETTEX','EUR',isin,base_tf FROM candles WHERE isin=? AND source='gettex'",(isin,isin)).fetchall()
    pending=[]
    for source,venue,currency,symbol,base in catalog:
        if base not in ('5m','15m','1H','1D'):continue
        identity=dict(source=source,venue=venue,currency=currency,symbol=symbol,timezone=quality.TIMEZONES.get(quality.CALENDARS.get(venue)))
        from eur_listings import venue_allowed
        if not venue_allowed(info,identity):continue
        token_key=json.dumps([source,venue,currency,symbol,base])
        selected=store.setting('history_identity:'+isin,{})
        scopes=_archive_scopes(isin,identity,base,selected)
        def revisions(db):
            return {s:(db.execute('SELECT revision FROM input_revisions WHERE scope=?',(s,)).fetchone() or [0])[0] for s in scopes}
        with store.connect() as db:token=revisions(db)
        old=progress.get('series',{}).get(token_key,{})
        retry_due=False
        if not local_only and old.get('quality',{}).get('state') in ('GAP','INVALID','UNCONFIRMED') and old.get('quality',{}).get('repairable'):
            retry=store.setting(quality.key(info,symbol,base),{}).get('next_retry')
            due=not retry or pd.Timestamp(now or pd.Timestamp.now(tz='UTC'))>=pd.Timestamp(retry)
            unserved=not old.get('recovery_checked_at') or old.get('recovery_binding')!=_archive_recovery_binding(info,store,identity,base,recover_gettex)
            retry_due=bool(due and (retry or unserved))
        if old.get('audit_version')!=ARCHIVE_AUDIT_VERSION or old.get('input_revisions')!=token or retry_due:pending.append((bool(old),old.get('audit_order',0),token_key,identity,base,token,scopes))
    eligible=pending if unit is None else [p for p in pending if json.loads(p[2])==list(unit)]
    if not eligible:return {'state':'DONE' if catalog else 'NOT_LOADED','series':len(catalog),'pending':len(pending)}
    # Never-visited series first, then oldest successful audit. Persist the
    # order so frequent live updates cannot starve other changed listings/TF.
    _,_,token_key,identity,base,token,scopes=min(eligible,key=lambda x:(x[0],x[1],x[2]))
    frame=store.load_official(isin,base) if identity['source']=='gettex' else store.load_series(isin,base,identity['source'],identity['symbol'],identity['venue'],identity['currency'])
    frame.attrs.update(identity)
    quality.attach_interval_evidence(store,frame,isin)
    report=quality.audit(frame,base,identity,now,check_stale=False)
    # Audit is not a readiness override: rejected upstream archives stay intact.
    report.update(count=len(frame),first=str(frame.index.min()) if len(frame) else None,last=str(frame.index.max()) if len(frame) else None)
    order=max((x.get('audit_order',0) for x in progress.get('series',{}).values()),default=0)+1
    entry={'audit_order':order,'audit_version':ARCHIVE_AUDIT_VERSION,'input_revisions':token,'quality':report,'scope':'entire saved base series; runtime freshness remains mandatory'}
    with store.connect() as db:
        db.execute('BEGIN IMMEDIATE')
        if revisions(db)!=token:raise ValueError('Archive audit input changed; resume on next feed')
        row=db.execute('SELECT value FROM settings WHERE key=?',(key,)).fetchone()
        if (json.loads(row[0]) if row else {})!=progress:raise ValueError('Archive audit progress changed')
        progress.setdefault('series',{})[token_key]=entry
        progress.update(catalog_count=len(catalog),pending=len(pending)-1)
        db.execute('INSERT OR REPLACE INTO settings VALUES (?,?)',(key,json.dumps(progress)))
    result={'state':'AUDITED','series':len(catalog),'pending':len(pending)-1,'identity':identity,'base':base,'quality':report['state'],'count':len(frame)}
    if not local_only and report['state'] in ('GAP','INVALID','UNCONFIRMED') and report.get('repairable'):
        # Use existing bounded recovery, never a second recovery queue. Unknown
        # intervals need evidence; do not refetch them on every archive pass.
        binding=_archive_recovery_binding(info,store,identity,base,recover_gettex)
        callback=None;network_not_before=None
        if identity['source']=='gettex' and recover_gettex:
            callback=lambda symbol,interval,start,end=None:recover_gettex(symbol,start,end,base)
        elif identity['source']=='gettex_chart':
            from eur_listings import stored_identity
            from gettex_chart import recovery_download,recovery_wait
            with store.connect() as db:confirmed=stored_identity(db,info,identity,base)
            waiting=recovery_wait(store,info,identity,base,now)
            if confirmed:
                frame.attrs.update(confirmed)
                network_not_before=waiting
                callback=lambda symbol,interval,start,end=None:recovery_download(store,info,identity,base,start,end,now)
            elif waiting:
                result.update(recovery='WAITING_PROVIDER',next_retry=waiting)
                previous=store.setting(quality.key(info,identity['symbol'],base),{})
                quality.persist(store,info,identity['symbol'],base,dict(previous or report,next_retry=waiting))
        elif identity['source']=='proxy':
            from eur_listings import confirmed_listing
            saved=store.setting('history_identity:'+isin,{})
            if all(saved.get(k)==identity[k] for k in quality.IDENTITY):
                frame.attrs['listing_proof']=saved.get('listing_proof')
            if confirmed_listing(info,frame.attrs):
                callback=lambda symbol,interval,start,end=None:download(symbol,interval,start,end,store=store,expected=identity)
        if callback:
            repaired=quality.repair(info,store,identity['symbol'],base,frame.attrs,callback,now,max_ranges=1,network_not_before=network_not_before)
            result['recovery']=repaired['state']
            if repaired.get('recovery_wait')=='PROVIDER':
                result.update(recovery='WAITING_PROVIDER',next_retry=repaired['next_retry'])
        result.setdefault('recovery','NO_CONFIRMED_ADAPTER')
        # Audit and recovery are different stages. A local-only inspection must
        # not consume the normal writer's recovery opportunity. Record only a
        # completed bounded visit, and never overwrite a concurrent new audit.
        with store.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            current_row=db.execute('SELECT value FROM settings WHERE key=?',(key,)).fetchone()
            current=json.loads(current_row[0]) if current_row else {}
            if current.get('series',{}).get(token_key)==entry:
                current['series'][token_key].update(recovery_checked_at=pd.Timestamp(now or pd.Timestamp.now(tz='UTC')).isoformat(),
                    recovery_binding=binding,recovery_state=result['recovery'])
                db.execute('INSERT OR REPLACE INTO settings VALUES (?,?)',(key,json.dumps(current)))
            else:result['recovery_progress']='CHANGED_DURING_RECOVERY'
    return result


def inspect_changed_official_archive(store,now=None):
    """One changed registered series, using the existing durable audit progress.

    Bulk cache imports affect other ISINs too. Recheck the oldest changed unit
    at a history-worker boundary rather than waiting for its network rotation.
    No HTTP/recovery, candle writes, trading grants or second queue here.
    """
    import json
    import data_quality as quality
    from eur_listings import OWNER_HISTORY_POLICY,owner_history_info,venue_allowed
    from paper_account import instrument_allowed
    policy=store.setting(OWNER_HISTORY_POLICY,{})
    if not (policy.get('approved') is True and policy.get('version')==1 and
            policy.get('scope')=='registered_stocks' and policy.get('exclusive_venue')=='GETTEX'):
        return {'state':'NOT_ENABLED'}
    infos={i['isin']:owner_history_info(i,policy) for i in store.instruments() if instrument_allowed(i)}
    with store.connect() as db:
        catalog=db.execute("SELECT DISTINCT isin,source,'GETTEX','EUR',isin,base_tf FROM candles WHERE source='gettex' AND base_tf IN ('5m','15m','1H','1D') "
            "UNION SELECT DISTINCT isin,source,venue,currency,symbol,base_tf FROM series WHERE source='gettex_chart' AND venue='GETTEX' AND currency='EUR' AND base_tf IN ('5m','15m','1H','1D')").fetchall()
        # Read only progress metadata, not all historical reports/evidence into
        # Python. The chosen inspector rechecks its full revision token at commit.
        previous=db.execute("SELECT s.key,j.key,json_extract(j.value,'$.input_revisions'),json_extract(j.value,'$.quality.checked_at'),json_extract(j.value,'$.audit_version') "
            "FROM settings s,json_each(s.value,'$.series') j WHERE s.key LIKE 'history_archive_progress:%' "
            "AND json_extract(j.key,'$[0]') IN ('gettex','gettex_chart')").fetchall()
        revisions=dict(db.execute("SELECT scope,revision FROM input_revisions WHERE scope LIKE 'market:%' OR scope LIKE 'instrument:%' "
            "OR scope LIKE 'setting:interval_evidence:%' OR scope LIKE 'setting:history_coverage:%' OR scope LIKE 'setting:history_identity:%' OR scope LIKE 'setting:parent_provenance:%'"))
    prior={(key.split(':',1)[1],unit):(json.loads(token) if token else {},checked or '',version) for key,unit,token,checked,version in previous}
    pending=[]
    for isin,source,venue,currency,symbol,base in catalog:
        if isin not in infos:continue
        identity=dict(source=source,venue=venue,currency=currency,symbol=symbol,timezone='Europe/Berlin')
        if not venue_allowed(infos[isin],identity):continue
        unit=[source,venue,currency,symbol,base]
        old,checked,version=prior.get((isin,json.dumps(unit)),({},'',None))
        scopes=_archive_scopes(isin,identity,base,store.setting('history_identity:'+isin,{}))
        token={s:revisions.get(s,0) for s in scopes}
        if old!=token or version!=ARCHIVE_AUDIT_VERSION:pending.append((checked,isin,base,unit))
    if not pending:return {'state':'IDLE','pending':0}
    _,isin,base,unit=min(pending)
    result=inspect_saved_archive(infos[isin],store,now,unit=unit,local_only=True)
    summary=dict(state=result['state'],isin=isin,base=base,quality=result.get('quality'),count=result.get('count'),
                 pending=max(0,len(pending)-(result['state']=='AUDITED')),checked_at=pd.Timestamp(now or pd.Timestamp.now(tz='UTC')).isoformat())
    if result['state']=='AUDITED':
        progress=store.setting('gettex_cached_history',{})
        progress['last_dq_refresh']=summary
        store.set_setting('gettex_cached_history',progress)
    return summary

def normalize_daily(frame):
    """Exclude only closed-day carry-forward quotes, with explicit provenance."""
    from data_quality import expected_index
    if frame.empty or frame.index.has_duplicates or not frame.index.is_monotonic_increasing:return frame
    try:grid=expected_index(frame.attrs,'1D',frame.index.min(),frame.index.max(),pd.Timestamp.now(tz='UTC')+pd.Timedelta(days=3))
    except (ValueError,KeyError):return frame  # Unknown calendars stay blocked by DQ.
    prices=frame[['open','high','low','close']]
    markers=(~frame.index.isin(grid)) & (frame.volume==0) & prices.notna().all(axis=1) & (prices>0).all(axis=1) & (prices.nunique(axis=1)==1) & (frame.close==frame.close.shift())
    if not markers.any():return frame
    result=frame.loc[~markers].copy();result.attrs=frame.attrs.copy()
    result.attrs['daily_normalization']={'excluded_closed_day_carry_forward':[str(x) for x in frame.index[markers]],'count':int(markers.sum())}
    return result
# Explicit instrument identities, never inferred from a similar company name.
KNOWN={
 'US0378331005':('APC.DE','AAPL'), 'US88160R1014':('TL0.DE','TSLA'),
 'US02079K1079':('ABEC.DE','GOOG'), 'US02079K3059':('ABEA.DE','GOOGL'),
 'US11135F1012':('1YD.DE','AVGO'), 'DE0006231004':('IFX.DE','IFX.DE'),
 'US68389X1054':('ORC.DE','ORCL')}

def download(symbol, interval='1d', start=None, end=None, store=None,expected=None):
    # Stock OHLCV needs confirmed EUR identity before HTTP. Direct indices
    # remain separate native-unit diagnostics, never substitute stock prices.
    fields=('source','symbol','currency','venue','timezone')
    if expected is None:
        from benchmarks import CATALOG,identity as index_identity
        direct=[index_identity(i) for i in CATALOG.values() if i['history_source']=='proxy' and i['history_symbol']==symbol]
        if len(direct)!=1:raise ValueError('EUR_HISTORY_IDENTITY_REQUIRED: no confirmed stock listing before request')
        expected=direct[0]
    elif expected.get('currency')!='EUR':
        raise ValueError('EUR_HISTORY_ONLY: non-EUR stock history request rejected')
    if (expected.get('source')!='proxy' or expected.get('symbol')!=symbol or
        not all(expected.get(k) for k in fields)):
        raise ValueError('EUR_HISTORY_IDENTITY_REQUIRED: incomplete or mismatched listing')
    params={'interval':interval,'includePrePost':'false'}
    intraday_base={'1m':'1m','5m':'5m','15m':'15m','60m':'1H','1h':'1H'}.get(interval)
    clock=pd.Timestamp.now(tz='UTC')
    if start is None and interval=='1d':start=pd.Timestamp('1900-01-01',tz='UTC')
    if interval=='1m':
        # Conservative request budget, not a claim about the provider's full
        # retention: only a named recent range, at most one session per attempt.
        if start is None or end is None or pd.Timestamp(start).tzinfo is None or pd.Timestamp(end).tzinfo is None:
            raise ValueError('AUX_1M_EXPLICIT_RANGE_REQUIRED')
        if pd.Timestamp(start)<clock-pd.Timedelta(days=1) or pd.Timestamp(end)-pd.Timestamp(start)>pd.Timedelta(days=1):
            raise ValueError('AUX_1M_OUTSIDE_BOUNDED_RECENT_WINDOW')
    if start is None and intraday_base:
        start=clock-pd.Timedelta(days=730 if intraday_base=='1H' else 60)+pd.Timedelta(minutes=1)
    if start is None:raise ValueError('Unsupported history interval without explicit start')
    else:
        ts=pd.Timestamp(start)
        if ts.tzinfo is None:
            if intraday_base:raise ValueError('Intraday request start требует timezone')
            ts=ts.tz_localize('Europe/Berlin')
        params.update(period1=int(ts.timestamp()),period2=int(pd.Timestamp(end).timestamp()) if end is not None else int(clock.timestamp()))
    if intraday_base and end is not None and pd.Timestamp(end).tzinfo is None:raise ValueError('Intraday request end требует timezone')
    if store is not None:
        retry=store.setting('cp_source_retry:proxy',None)
        if retry and clock<pd.Timestamp(retry):raise ValueError('Proxy source cooldown')
    r=requests.get('https://query1.finance.yahoo.com/v8/finance/chart/'+quote(symbol,safe=''),params=params,headers=HEADERS,timeout=30)
    response_received_at=datetime.now(timezone.utc).isoformat()
    try:r.raise_for_status()
    except Exception as error:
        if store is not None and r.status_code==429:
            import candle_schedule as scheduler
            scheduler.finish(store,'source:proxy',{'source':'proxy'},intraday_base or '1D',{'key':'cp_source_status:proxy'},error)
        raise
    chart=r.json()['chart']
    if chart.get('error') or not chart.get('result'):raise ValueError('Источник не вернул историю '+symbol)
    data=chart['result'][0];meta=data['meta'];currency=meta.get('currency');venue=meta.get('exchangeName')
    if meta.get('symbol','').upper()!=symbol.upper():
        from data_quality import QualityError
        raise QualityError({'state':'INVALID','issues':['Источник вернул другой инструмент'],'ranges':[]})
    if not currency or not venue:raise ValueError('Источник не указал валюту/биржу')
    returned=dict(source='proxy',symbol=meta.get('symbol'),currency=currency,venue=venue,timezone=meta.get('exchangeTimezoneName'))
    if any(returned.get(k)!=expected.get(k) for k in fields):
        raise ValueError('HISTORY_RESPONSE_IDENTITY_MISMATCH: currency/venue/symbol/timezone changed')
    stamps=data.get('timestamp') or []
    if not stamps:return pd.DataFrame()
    if intraday_base and not meta.get('exchangeTimezoneName'):raise ValueError('Источник не подтвердил intraday timezone')
    index=pd.to_datetime(stamps,unit='s',utc=True).tz_convert(meta.get('exchangeTimezoneName','Europe/Berlin'))
    if not intraday_base:index=index.tz_localize(None)
    if interval=='1d':index=index.normalize()
    q=data['indicators']['quote'][0]
    f=pd.DataFrame({k:q.get(k,[]) for k in ('open','high','low','close','volume')},index=index)
    # Keep malformed rows, duplicates and order for the quality gate to diagnose.
    f=f.apply(pd.to_numeric,errors='coerce')
    f['source']='proxy';f['venue']=venue;f['currency']=currency;f['symbol']=symbol
    f.attrs={'source':'proxy','venue':venue,'currency':currency,'symbol':symbol,'timezone':meta.get('exchangeTimezoneName'),'fetched_at':datetime.now(timezone.utc).isoformat(),'first_trade_date':meta.get('firstTradeDate')}
    f.attrs['application_received_at']=response_received_at
    # Provider-reported quote time is not the bar's open/close or permission.
    market_time=meta.get('regularMarketTime')
    try:
        f.attrs['provider_reported_market_at']=datetime.fromtimestamp(float(market_time),timezone.utc).isoformat() if market_time is not None and not isinstance(market_time,bool) else None
    except (ValueError,TypeError,OverflowError,OSError):
        f.attrs['provider_reported_market_at']=None
    regular=meta.get('currentTradingPeriod',{}).get('regular',{})
    if regular.get('start') and meta.get('exchangeTimezoneName'):
        opening=pd.Timestamp(regular['start'],unit='s',tz='UTC').tz_convert(meta['exchangeTimezoneName'])
        f.attrs['session_open']=opening.strftime('%H:%M')
        if regular.get('end'):
            closing=pd.Timestamp(regular['end'],unit='s',tz='UTC').tz_convert(meta['exchangeTimezoneName'])
            f.attrs['session_close']=closing.strftime('%H:%M')
        f.attrs['session_policy']='provider current regular-session anchor; historical holidays/early closes not verified'
    if intraday_base:
        from intraday import normalize
        f=normalize(f,intraday_base,now=clock,start=start,end=end or clock)
    return normalize_daily(f) if interval=='1d' else f

def candidates(info):
    known=KNOWN.get(info['isin']);out=[]
    if known:out.extend(known)
    out.extend([info.get('history_symbol'),info.get('profile_symbol')])
    if not any(out):
        raise ValueError('Нужен подтверждённый history_symbol для этого ISIN; автоматический выбор похожего листинга запрещён')
    return list(dict.fromkeys(s for s in out if s))

def recover(info,store):
    if info.get('history_venue_lock')=='GETTEX':
        # GETTEX has its existing chart/post-trade turns. Keep reserve archives,
        # but do not refresh/discover another venue for this instrument.
        return {'state':'VENUE_EXCLUDED','venue':'GETTEX'},[]
    if info.get('eur_history_auto'):
        from eur_listings import TARGETS,depth,rank_reports
        from analytics import resample
        from timeframes import base_for
        import data_quality
        selection=store.setting('eur_history_selection:'+info['isin'],{})
        listings=info.get('eur_listings',[])
        if selection.get('symbol') and selection.get('checked_date')==str(pd.Timestamp.now(tz='UTC').date()):
            listings=[v for v in listings if v.get('symbol')==selection['symbol']]
        choices=[];failures=[]
        for listing in listings:
            if listing.get('isin')!=info['isin'] or listing.get('currency')!='EUR' or listing.get('status')!='CONFIRMED':continue
            candidate={**info,'history_symbol':listing['symbol'],'history_currency':'EUR','history_venue':listing['venue']}
            try:
                meta,errors=_recover(candidate,store)
                reports={}
                for tf in TARGETS:
                    base=base_for(tf)
                    raw=store.load_series(info['isin'],base,'proxy',listing['symbol'],listing['venue'],'EUR')
                    if not raw.empty:
                        raw.attrs.update({k:listing[k] for k in ('source','symbol','venue','currency','timezone')})
                        raw.attrs['data_quality']=data_quality.audit(raw,base)
                        persisted=store.setting(data_quality.key(info,listing['symbol'],base),{})
                        if persisted.get('state') in ('INVALID','RECOVERING','SOURCE_UNAVAILABLE','SOURCE_GAP') and raw.attrs['data_quality']['state']!='INVALID':
                            raw.attrs['data_quality'].update(persisted)
                        raw=resample(raw,base,tf)
                    report=depth(raw,tf);report['quality']=raw.attrs.get('data_quality',{}).get('state','SOURCE_UNAVAILABLE')
                    reports[tf]=report
                score=rank_reports(reports)
                choices.append((score,meta,errors,reports))
                if score[0]>=len(TARGETS):break
            except Exception as error:failures.append(str(error))
        if not choices:raise ValueError('EUR history blocked: '+'; '.join(failures))
        _,meta,errors,reports=max(choices,key=lambda c:c[0])
        store.set_setting('history_identity:'+info['isin'],meta)
        store.set_setting('eur_history_selection:'+info['isin'],dict(symbol=meta['symbol'],reports=reports,checked_date=str(pd.Timestamp.now(tz='UTC').date())))
        return meta,errors+failures
    return _recover(info,store)

def _recover(info,store):
    from history_backfill import sync
    errors=[]
    saved=store.setting('history_identity:'+info['isin'],{})
    # A persisted user/candidate pin outranks a previous runtime cache choice.
    # Its actual venue/currency still have to be confirmed by the provider and
    # Resolver; never carry the previous listing's identity into this request.
    if info.get('history_symbol') and saved.get('symbol')!=info['history_symbol']:saved={}
    symbol=saved.get('symbol') or info.get('history_symbol')
    if not symbol:raise ValueError('Нужен явно заданный history_symbol; автоматический выбор листинга запрещён')
    identity={k:saved[k] for k in ('source','symbol','currency','venue','timezone') if saved.get(k)}
    identity.update(symbol=symbol,source='proxy')
    if info.get('history_currency'):identity.setdefault('currency',info['history_currency'])
    if info.get('history_venue'):identity.setdefault('venue',info['history_venue'])
    daily=None;successful=False
    def checked_download(symbol,interval,start,**kwargs):
        from delivery_observations import outcome
        base={'1d':'1D','60m':'1H'}.get(interval,interval)
        try:frame=download(symbol,interval,start,store=store,expected=identity,**kwargs)
        except Exception as error:
            outcome(store.directory,info['isin'],base,identity,'ERROR',error)
            raise
        if frame.empty:
            outcome(store.directory,info['isin'],base,identity,'EMPTY')
            return frame
        for field in ('source','symbol','currency','venue','timezone'):
            actual=frame.attrs.get(field)
            if identity.get(field) and identity[field]!=actual:
                outcome(store.directory,info['isin'],base,identity,'IDENTITY_MISMATCH')
                raise ValueError('Изменилась идентичность истории: '+field)
            if actual:identity[field]=actual
        from delivery_observations import observe
        observe(store.directory,info['isin'],{'1d':'1D','60m':'1H'}.get(interval,interval),frame,
                received_at=frame.attrs.get('application_received_at'),receipt_kind='HTTP_RESPONSE',
                reported_market_at=frame.attrs.get('provider_reported_market_at'))
        return frame
    import candle_schedule as scheduler
    from data_quality import CALENDARS,TIMEZONES
    identity.setdefault('timezone',TIMEZONES.get(CALENDARS.get(identity.get('venue'))))
    from eur_listings import stored_identity
    with store.connect() as db:confirmed=stored_identity(db,info,identity,'1D')
    if not confirmed:raise ValueError('EUR_HISTORY_IDENTITY_REQUIRED: stock history blocked before download')
    identity.update(confirmed)
    successful=bool(saved)
    for base in ('1D','5m','15m','1H'):
        plan=scheduler.decision(store,info['isin'],identity,base)
        if not plan['due']:continue
        plan['started_at']=pd.Timestamp.now(tz='UTC').isoformat()
        try:
            result=sync(info,store,symbol,base,checked_download)
            scheduler.finish(store,info['isin'],identity,base,plan)
            successful=True
            if base!='1D' and result.get('quality_state') not in (None,'READY'):
                errors.append(base+' '+result['quality_state']+': '+'; '.join(result.get('quality_issues',[])))
            if base=='1D':daily=result
        except Exception as error:
            scheduler.finish(store,info['isin'],identity,base,plan,error)
            errors.append(base+': '+str(error))
    if not successful:raise ValueError('; '.join(errors))
    meta=dict(daily or saved or identity)
    if saved.get('listing_proof') and all(meta.get(k)==saved.get(k) for k in ('source','symbol','venue','currency','timezone')):
        meta['listing_proof']=saved['listing_proof']
    store.set_setting('history_identity:'+info['isin'],meta)
    if daily and not daily.get('backfill_complete'):errors.append('Дневная история неполна: '+daily['first'])
    return meta,errors


# Same recovery writer and queue; these helpers never create a second pipeline.
def parent_provenance_key(isin,meta,base):
    import hashlib,json
    return 'parent_provenance:'+isin+':'+base+':'+hashlib.sha256(json.dumps([meta.get(k) for k in ('source','symbol','venue','currency','timezone')]).encode()).hexdigest()[:20]


def _parent_absence_valid(proof,meta):
    """Revalidate only the empty child slots used by this recovered parent.

    A refreshed publication manifest is not a change to historical children.
    Legacy receipts keep their conservative digest gate until ordinary repair
    creates a scoped proof; no read-time migration or permission is written.
    """
    from performance_cache import _digest
    import data_quality as quality
    if 'absence_scope' not in proof:
        return proof.get('absence_digest') is None or proof['absence_digest']==_digest(meta.get('absence_evidence'))
    try:
        scope=proof['absence_scope'];spec=proof['inputs'][0]
        if scope.get('version')!=1 or scope.get('base')!=spec['base_tf']:return False
        if any(spec['identity'].get(k)!=meta.get(k) for k in quality.IDENTITY):return False
        stamps=pd.DatetimeIndex(scope['stamps'])
        if stamps.hasnans or stamps.has_duplicates or stamps.tz is not None:return False
        start=pd.Timestamp(spec['from']);end=pd.Timestamp(spec['through']).tz_convert(meta['timezone']).tz_localize(None)
        if any(t<start or t>=end for t in stamps):return False
        if stamps.empty:return True
        covered=quality.covered_empty(meta,stamps,scope['base'])
        return stamps.isin(covered).all()
    except (KeyError,TypeError,ValueError,IndexError):return False


def attach_parent_provenance(store,frame,isin,base):
    """Read-time lineage gate. Child corrections revoke parents without deleting OHLCV."""
    if frame.empty:return frame
    import data_quality as quality
    from performance_cache import _closed_rows,_digest
    records=store.setting(parent_provenance_key(isin,frame.attrs,base),{})
    inputs=[];invalid=[];retimed={};origins=[];empty=[]
    for stamp,proof in records.items():
        if pd.Timestamp(stamp) not in frame.index:continue
        if proof.get('official_timebase'):
            from revision_watcher import retime_current,RETIME_POLICY
            key=proof['official_timebase']
            if key not in retimed:
                receipt=store.setting(key,{})
                store.read_cache.depend('setting:'+RETIME_POLICY)
                store.read_cache.depend('setting:interval_evidence:'+isin)
                for tf in ('5m','15m'):store.read_cache.depend('market:'+isin+':'+tf)
                with store.connect() as db:valid=bool(receipt) and retime_current(db,receipt)
                retimed[key]=(receipt,valid)
                if valid:
                    origins.append(dict(receipt_key=key,first=receipt['first'],end=receipt['end'],source='gettex',original_provider=frame.attrs['source']))
                    current=store.setting('interval_evidence:'+isin,{})
                    authority={**current['identity'],'absence_evidence':{**receipt['source']['proof'],'checked_at':current['checked_at']}}
                    empty.append(dict(first=receipt['first'],end=receipt['end'],authority=authority))
            receipt,valid=retimed[key]
            if not valid:invalid.append(stamp)
            else:
                # source identifies the retained archive; row origin records
                # the explicit correction without claiming it came from LSEG.
                frame.loc[pd.Timestamp(stamp),'origin_source']='gettex'
                inputs.append(dict(parent_at=stamp,inputs=[dict(isin=isin,identity=receipt['source']['proof']['identity'],base_tf=tf,timeframe=tf,
                    **{'from':receipt['first'],'through':pd.Timestamp(receipt['end']).tz_localize('Europe/Berlin').tz_convert('UTC').isoformat()}) for tf in ('5m','15m')]))
            continue
        for spec in proof['inputs']:
            store.read_cache.depend('market:'+isin+':'+spec['base_tf'])
        with store.connect() as db:
            current=[_digest(_closed_rows(db,s)) for s in proof['inputs']]
        observed=[float(frame.loc[pd.Timestamp(stamp),k]) for k in quality.FIELDS]
        if (current!=proof['digests'] or observed!=proof['ohlcv'] or
            not _parent_absence_valid(proof,frame.attrs)):
            invalid.append(stamp)
        inputs.append(dict(parent_at=stamp,inputs=proof['inputs']))
    frame.attrs['parent_recovery_inputs']=inputs
    frame.attrs['invalid_parent_provenance']=invalid
    if origins:
        frame.attrs['source_corrections']=origins
        frame.attrs['official_retiming_empty']=empty
    return frame


def recover_from_children(info,store,meta,base,first,last,now,allow_download=False,observations=None):
    """Return ONLY a complete validated replacement range, no writes here.

    Existing calendar aggregation is used verbatim. All-empty buckets remain
    absent. A valid published parent is never replaced by a disagreeing aggregate.
    """
    import data_quality as quality
    from analytics import resample
    from timeframes import MINUTES,endpoints
    from performance_cache import _closed_rows,_digest
    if base not in ('5m','15m','1H','1D'):return None
    # A queued job can outlive a resolver/identity update. Use the saved current
    # listing proof; the caller also checks its instrument revision at commit.
    import json
    with store.connect() as db:current=db.execute('SELECT payload FROM instruments WHERE isin=?',(info['isin'],)).fetchone()
    if current:info=json.loads(current[0])
    from eur_listings import stored_identity
    # The saved UI fallback can be another listing. Resolve this exact
    # parent's current database proof, not a caller-supplied/selected proof.
    with store.connect() as db:current_meta=stored_identity(db,info,meta,base)
    if not current_meta:return None
    target=1440 if base=='1D' else MINUTES[base]
    starts=quality.expected_index(meta,base,first,last,now)
    if starts.empty:return None
    probe=pd.DataFrame(index=starts);probe.attrs={**meta,'timeframe':base}
    ends=endpoints(probe)
    if ends.isna().any() or (ends>now).any():return None
    begin=starts[0];end=ends[-1].tz_convert(meta['timezone']).tz_localize(None)
    # Bound CPU and optional network work in the existing recovery batch.
    if end-begin>pd.Timedelta(days=8):return None
    store.read_cache.depend('market:'+info['isin']+':'+base)
    parent=None
    from cp_experience import child_order
    from time import perf_counter
    children=tuple(child for child in child_order(store,info,meta,base,('1H','15m','5m','1m'),now)
                   if MINUTES[child]<target and (base=='1D' or not target%MINUTES[child]))
    # Indexed existence checks avoid materializing whole unrelated archives.
    # Availability is not DQ or NO_TRADES. Do not persist negative results:
    # new children are visible next call; commit still checks all revisions.
    available={}
    with store.connect() as db:
        for child in children:
            store.read_cache.depend('market:'+info['isin']+':'+child)
            if meta['source']=='gettex':
                available[child]=db.execute("SELECT 1 FROM candles WHERE isin=? AND source='gettex' AND base_tf=? AND ts>=? AND ts<? LIMIT 1",
                    (info['isin'],child,str(begin),str(end))).fetchone()
            else:
                available[child]=db.execute('SELECT 1 FROM series WHERE isin=? AND source=? AND venue=? AND currency=? AND symbol=? AND base_tf=? AND ts>=? AND ts<? LIMIT 1',
                    (info['isin'],meta['source'],meta['venue'],meta['currency'],meta['symbol'],child,str(begin),str(end))).fetchone()
    for child in children:
        if not available[child] and not (child=='1m' and allow_download and meta['source']=='proxy'):continue
        started=perf_counter();outcome='REJECTED'
        try:
            raw=store.load_official(info['isin'],child) if meta['source']=='gettex' else store.load_series(info['isin'],child,meta['source'],meta['symbol'],meta['venue'],meta['currency'])
            auxiliary=None
            if child=='1m' and allow_download and meta['source']=='proxy':
                start_at=begin.tz_localize(meta['timezone']);end_at=end.tz_localize(meta['timezone'])
                if start_at>=now-pd.Timedelta(days=1) and end_at-start_at<=pd.Timedelta(days=1):
                    previous=raw
                    from eur_listings import confirmed_listing
                    if not confirmed_listing(info,meta):continue
                    raw=download(meta['symbol'],'1m',start_at,end_at,store=store,expected=meta)
                    if not previous.empty:
                        for stamp in previous.index.intersection(raw.index):
                            old=previous.loc[[stamp]].copy();old.attrs={**meta,'timeframe':'1m'}
                            if quality.audit(old,'1m',meta,now,check_stale=False)['state']=='READY' and any(float(old.iloc[0][k])!=float(raw.loc[stamp,k]) for k in quality.FIELDS):
                                raise ValueError('AUXILIARY_CHILD_CONFLICT: use existing Revision Watcher before replacing valid data')
                    auxiliary=raw
            if raw.empty:continue
            if any(raw.attrs.get(k)!=meta[k] for k in quality.IDENTITY if k!='timezone'):continue
            if raw.attrs.get('timezone') not in (None,meta['timezone']):continue
            raw=raw.loc[(raw.index>=begin)&(raw.index<end)].copy()
            raw.attrs.update(meta,timeframe=child)
            # Restrict metadata only when ranges are known; global problems stay.
            raw.attrs['intraday_span']=[str(begin),str(end-pd.Timedelta(nanoseconds=1))]
            if raw.empty:continue
            quality.attach_interval_evidence(store,raw,info['isin'])
            expected=quality.expected_index(meta,child,begin,end-pd.Timedelta(nanoseconds=1),now)
            missing=expected.difference(raw.index)
            if len(missing.difference(quality.covered_empty(raw.attrs,missing,child,now))):continue
            if len(raw.index.difference(expected)):continue
            report=quality.audit(raw,child,meta,now,check_stale=False)
            if report['state']!='READY':continue
            if auxiliary is not None:auxiliary=raw.copy()
            prior=store.setting(quality.key(info,meta['symbol'],child),{})
            if prior.get('state') in ('INVALID','SOURCE_UNAVAILABLE','RECOVERING','SOURCE_GAP'):
                if not prior.get('ranges') or any(pd.Timestamp(a)<end and pd.Timestamp(b)>=begin for a,b in prior['ranges']):continue
            raw.attrs['data_quality']=report
            # analytics.RANK/UI list deliberately do not expose auxiliary 1m.
            if child=='1m':
                from timeframes import aggregate
                if base=='1D':continue  # recover a native intraday base first
                aggregated=aggregate(raw,child,base,now)
            else:aggregated=resample(raw,child,base,now)
            if aggregated.empty or not starts.isin(aggregated.index).all():continue
            window=aggregated.loc[starts].copy()
            if 'bar_complete' not in window or not window.bar_complete.all():continue
            if quality.audit(window,base,meta,now,check_stale=False)['state']!='READY':continue
            if parent is None:
                parent=store.load_official(info['isin'],base) if meta['source']=='gettex' else store.load_series(info['isin'],base,meta['source'],meta['symbol'],meta['venue'],meta['currency'])
            proofs={}
            for stamp,closing in zip(starts,ends):
                row=window.loc[stamp];old=parent.loc[[stamp]].copy() if stamp in parent.index else None
                if old is not None and str(stamp) not in parent.attrs.get('invalid_parent_provenance',[]):
                    old.attrs={**meta,'timeframe':base}
                    if quality.audit(old,base,meta,now,check_stale=False)['state']=='READY' and any(float(old.iloc[0][k])!=float(row[k]) for k in quality.FIELDS):
                        raise ValueError('PARENT_CHILD_CONFLICT: valid published parent differs at '+str(stamp))
                spec=dict(isin=info['isin'],identity={k:meta[k] for k in quality.IDENTITY},base_tf=child,
                          timeframe=child,**{'from':str(stamp),'through':closing.isoformat()})
                # Capture nested lineage too, never treat a recovered child as raw proof.
                nested=[s for item in raw.attrs.get('parent_recovery_inputs',[]) if stamp<=pd.Timestamp(item['parent_at'])<closing.tz_convert(meta['timezone']).tz_localize(None) for s in item['inputs']]
                specs=[spec,*nested]
                with store.connect() as db:digests=[_digest(_closed_rows(db,s)) for s in specs]
                if auxiliary is not None:
                    part=raw.loc[(raw.index>=stamp)&(raw.index<closing.tz_convert(meta['timezone']).tz_localize(None))]
                    digests[0]=_digest([(str(t),*[float(r[k]) for k in quality.FIELDS]) for t,r in part.iterrows()])
                proofs[str(stamp)]=dict(inputs=specs,digests=digests,ohlcv=[float(row[k]) for k in quality.FIELDS],
                    absence_digest=_digest(raw.attrs.get('absence_evidence')) if len(missing) else None,
                    absence_scope=dict(version=1,base=child,stamps=[str(t) for t in missing
                        if stamp<=t<closing.tz_convert(meta['timezone']).tz_localize(None)]),
                    detected_at=now.isoformat(),basis='complete verified real child coverage; existing calendar aggregation')
            window.attrs.update(parent_recovery_proofs=proofs,auxiliary_children=auxiliary,auxiliary_base=child if auxiliary is not None else None,recovery_method='children:'+child)
            outcome='CANDIDATE'
            return window
        except Exception:
            outcome='ERROR'
            raise
        finally:
            if observations is not None:
                observations.append(dict(tf=child,elapsed_ms=round((perf_counter()-started)*1000,3),parents=len(starts),result=outcome))
    return None
