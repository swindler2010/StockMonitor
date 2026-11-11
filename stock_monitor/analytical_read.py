"""Bounded read projection using the existing chart/indicator calculation path."""
import copy,json,hashlib
import pandas as pd
from snapshot_worker import inputs,SnapshotContext,Value
from desktop014 import DEFAULT_IND
from analytics import indicators,bar_ends

def read_time():return pd.Timestamp.now(tz='UTC')

def capture(app,args):
    values=inputs(app)
    info=next((i for i in values['watchlist'] if i['isin']==args['isin']),None)
    if info is None:raise ValueError('UNKNOWN_INSTRUMENT')
    values.update(watchlist=[info],symbol=info['name'],timeframe=args['timeframe'],period='Max',feed_errors={},request=copy.deepcopy(args))
    values['drafts']={k:v for k,v in values['drafts'].items() if k==info['isin']}
    values['instrument_revisions']={k:v for k,v in values['instrument_revisions'].items() if k=='instrument:'+info['isin']}
    return values

def calculate(store,values):
    # A reset invalidates prepared paper requests, not candle history.
    store.read_cache.depend('setting:paper_epoch')
    if values['request'].get('view')=='stored_archive':
        from mcp_projection import archive
        args=values['request']
        store.read_cache.depend('market:'+args['isin']+':'+args['timeframe'])
        store.read_cache.depend('instrument:'+args['isin'])
        return archive(store.path,args)
    context=SnapshotContext();context.store=store
    for key,value in values.items():setattr(context,key,Value(value) if key in ('symbol','timeframe','period') else value)
    args=values['request'];info=context.instrument_cfg();state=context.user_state(info)
    frame=context.frame_for(info,args['timeframe'])
    from partial_chart import select_display
    frame=select_display({**info,'history_venue_lock':state.get('history_venue_lock')},args['timeframe'],frame,store)
    settings={**DEFAULT_IND,**store.setting('global_indicators',{})}
    identity={k:frame.attrs.get(k) for k in ('source','symbol','venue','currency','timezone')}
    for key in ('source','symbol','venue','currency'):
        if key in args and args[key]!=identity[key]:raise ValueError('IDENTITY_MISMATCH')
    now=read_time()
    context.analysis_at=now
    execution_context=None
    analysis_context=None
    from paper_account import instrument_allowed
    if instrument_allowed(info) and args['timeframe'] in ('5m','15m'):
        from calculation_window import build
        execution_context=build(context,info,args['timeframe'],state,now)
    if instrument_allowed(info):
        from cp_controls import demo_initialization_policy,initialization_policy_for
        key='cp_manual_risk:'+info['isin'];store.read_cache.depend('setting:'+key)
        policy=initialization_policy_for(store,info)
        if demo_initialization_policy(policy,info['isin']) and args['timeframe'] in policy.get('analysis_timeframes',[]):
            context.analysis_at=now
            engine=context.trading_analysis(info,analysis_only=True)
            analysis_context={'analysis_ready':engine.get('status')=='READY',
                'execution_permission':False,'calculated_at':now.isoformat(),
                'identity':engine.get('analysis_identity'),'trading_engine':engine,
                'basis':'Separate owner-approved demo analysis; page OHLCV/indicators keep their own stated source'}
    token=store.read_cache.dependencies()
    # Bind actual closed-bar flags instead of an arbitrary wall-clock minute.
    # The existing engine still assesses DQ/admission on every read; this is
    # only the OHLCV/indicator-page identity, never a trade-readiness lease.
    ends=bar_ends(frame) if not frame.empty else pd.DatetimeIndex([],tz='UTC')
    closed=(ends<=now) & (frame['bar_complete'].fillna(False).astype(bool).to_numpy() if 'bar_complete' in frame else True)
    temporal=hashlib.sha256(closed.tobytes()).hexdigest()
    query={k:v for k,v in args.items() if k not in ('offset','limit','expected_revision','_deadline')}
    revision=hashlib.sha256(json.dumps([token,query,state,settings,temporal],sort_keys=True,default=str).encode()).hexdigest()
    offset=args.get('offset',0);limit=args.get('limit',200)
    if not 1<=limit<=500 or offset<0:raise ValueError('INVALID_PAGE')
    if args.get('expected_revision') and args['expected_revision']!=revision:raise ValueError('STALE_PAGE')
    if offset and args.get('expected_revision')!=revision:raise ValueError('STALE_PAGE')
    ends=bar_ends(frame) if not frame.empty else pd.DatetimeIndex([],tz='UTC')
    from partial_chart import bind
    display_only=bool(frame.attrs.get('partial_chart'))
    enriched=bind({},frame)['analysis'][0] if display_only else indicators(frame) if not frame.empty else frame
    chart_series={};chart_profile={'status':'NOT_REQUESTED'}
    if args.get('include_chart_series') and not frame.empty and not display_only:
        from mcp_chart_export import export
        chart_series,chart_profile=export(frame,settings,args['timeframe'])
    openings=pd.DatetimeIndex(enriched.index)
    if openings.tz is None:openings=openings.tz_localize(identity['timezone'] or 'Europe/Berlin',ambiguous='NaT',nonexistent='NaT')
    openings=openings.tz_convert('UTC')
    mask=~openings.isna()
    for key in ('from','to'):
        if args.get(key):
            boundary=pd.Timestamp(args[key])
            if boundary.tzinfo is None:raise ValueError('RANGE_REQUIRES_TIMEZONE')
            boundary=boundary.tz_convert('UTC')
            mask=mask & (openings>=boundary if key=='from' else openings<=boundary)
    closed=(ends<=now) & (enriched['bar_complete'].fillna(False).astype(bool).to_numpy() if 'bar_complete' in enriched else True)
    if args.get('closed_only'):mask=mask & closed
    matches=[n for n,keep in enumerate(mask) if keep]
    matched_total=len(matches);rows=[]
    for n in matches[offset:offset+limit]:
        opening=openings[n];row=enriched.iloc[n]
        record={k:float(row[k]) if pd.notna(row[k]) else None for k in ('open','high','low','close','volume','ema10','ema30','rsi','atr','macd','macd_signal','volume_mean20') if k in row}
        record.update(open_at=opening.isoformat(),close_at=ends[n].isoformat(),closed=bool(closed[n]))
        if args.get('include_chart_series'):record['chart_values']={key:values[n] for key,values in chart_series.items()}
        record['missing_values']=[k for k,v in record.items() if v is None]
        rows.append(record)
    page=rows
    from candle_timing import annotate_page
    measured=annotate_page(store,info['isin'],args['timeframe'],identity,page,now.isoformat())
    from market import clean
    return {'status':'AVAILABLE' if not frame.empty else 'NO_DATA','isin':info['isin'],'timeframe':args['timeframe'],
        'identity':identity,'data_quality':frame.attrs.get('data_quality',{}),'provenance':clean(copy.deepcopy(frame.attrs)),'history':{k:frame.attrs.get(k) for k in ('completion_status','limitation','history_depth')},
        'stored_frame_count':len(frame),'first':str(frame.index[0]) if len(frame) else None,'last':str(frame.index[-1]) if len(frame) else None,
        'as_of':now.isoformat(),'revision':revision,'offset':offset,'total_count':matched_total,'next_offset':offset+len(page) if offset+len(page)<matched_total else None,
        'revision_scope':'Inputs/settings/query/closed-bar flags; diagnostics and admission reassessed per request, never pinned for trading',
        'bars':page,'execution_context':execution_context,'analysis_context':analysis_context,
        'page_diagnostics':{'unaddressable_timestamps':int(openings.isna().sum()),'closed_only':bool(args.get('closed_only')),
            'empty_selection_reason':None if matched_total else 'NO_MATCHING_ADDRESSABLE_CLOSED_BARS' if args.get('closed_only') else 'NO_MATCHING_ADDRESSABLE_BARS'},
        'timestamps':{'calculated_at':now.isoformat(),'application_received_at':None,'mcp_published_at':None,
                      'provider_latency':'UNCONFIRMED','note':'Bar close_at is not receipt or publication time'},
        'chart_indicators':settings,'chart_volume_profile':chart_profile,'drawings':copy.deepcopy(state.get('drawings',[])),'levels':copy.deepcopy(state.get('levels',{})),
        'indicator_basis':{'series':'existing analytics.indicators; fixed CORE parameters, includes forming bars for inspection, not a confirmed signal',
            'display_only':display_only,'display_only_reason':'Partial diagnostic chart; indicators not computed across excluded intervals' if display_only else None,
            'parameters':{'EMA':[10,30],'RSI':14,'ATR':14,'MACD':[12,26,9],'average_volume':20},
            'chart_configurable_series':'EXPORTED_ENABLED_SETTINGS_USING_EXISTING_DESKTOP_METHOD' if args.get('include_chart_series') else 'NOT_REQUESTED; set include_chart_series=true',
            'missing_reason':'NaN means insufficient warmup or unavailable input; use data_quality for trustworthiness'},
        'delivery_latency':{'status':'PARTIAL_MEASURED' if measured else 'UNCONFIRMED','storage_observed_bars':measured,
            'provider_latency':'UNCONFIRMED','reason':'Storage observations are available only for tracked new/changed base bars; HTTP receipt and provider publication remain unknown'},
        'execution':'read only; no download, source switch or trade'}
