from performance_cache import InputRevisionChanged
import copy
import json
import time
import pandas as pd
from types import SimpleNamespace
from desktop014 import Desktop014, ALIASES
from benchmark_ui import BenchmarkMixin
import benchmarks
from analytics import diagnostics
import analytics
from trading_engine import profile

class Value:
    def __init__(self,value):self.value=value
    def get(self):return self.value

class SnapshotContext:
    def set_analysis_scope(self,now):
        self.analysis_at=pd.Timestamp(now);self.five_year_scope=True
        if not hasattr(self,'closed_specs'):self.closed_specs={}
    def closed_frame(self,info,frame,tf):
        if not hasattr(self,'analysis_at') or frame.empty:return frame
        # Read-only execution diagnostics share an as-of clock without changing
        # their scope to five years. They still require closed-input tracking.
        if not hasattr(self,'closed_specs'):self.closed_specs={}
        data=frame.copy(deep=False);data.attrs={**frame.attrs,'timeframe':tf}
        ends=analytics.bar_ends(data);mask=ends<=self.analysis_at
        data=data.loc[mask].copy()
        data.attrs={**frame.attrs,'timeframe':tf,'bar_ends':{str(stamp):end.isoformat() for stamp,end in zip(frame.index[mask],ends[mask])}}
        if getattr(self,'five_year_scope',False):
            from calculation_window import exclude_verified_leading_partial
            data=exclude_verified_leading_partial(data,tf,self.analysis_at)
            if tf=='1M':
                from gettex_chart import monthly_warmup
                if getattr(self,'approved_demo_warmup',False):data.attrs['approved_demo_warmup']=True
                data=monthly_warmup(self.store,info,data,self.analysis_at)
            if getattr(self,'approved_demo_warmup',False) and tf in ('1W','1M'):
                from gettex_chart import verified_demo_warmup
                data=verified_demo_warmup(self.store,info,data,self.analysis_at)
        from performance_cache import frame_dependency
        spec=frame_dependency(info['isin'],data,tf,self.analysis_at)
        for spec in (spec,*data.attrs.get('warmup_dependencies',[])):
            if not spec:continue
            key=json.dumps(spec,sort_keys=True);self.closed_specs[key]=spec
            if hasattr(self,'used_closed_keys'):self.used_closed_keys.add(key)
        return data

    def pinned_frame(self,info,tf,identity):
        """Same exact prepared series across this calculation's TF candidates.

        Return independent copies and replay every captured dependency through
        ReadCache. closed_frame still registers each caller's actual proof.
        """
        state=json.dumps(self.user_state(info),sort_keys=True,default=str)
        key=('pinned_frame_v1',info['isin'],tf,tuple(sorted(identity.items())),state,
            str(getattr(self,'analysis_at','')),bool(getattr(self,'five_year_scope',False)),
            bool(getattr(self,'closed_parent_inputs',False)),
            int(time.time()//60) if not hasattr(self,'analysis_at') else None)
        return self.store.read_cache.get(key,
            lambda:Desktop014._frame_uncached(self,info,tf,required_identity=identity),
            scoped=True,inputs=self.store.read_cache.token_for(['instrument:'+info['isin']]))

    def frame_for(self,info,tf):
        from cp_controls import reviewed_entry_identity
        required=reviewed_entry_identity(self.store,info,tf)
        if required:
            return self.closed_frame(info,self.pinned_frame(info,tf,required),tf)
        return self.closed_frame(info,Desktop014.frame_for(self,info,tf),tf)
    def _frame_uncached(self,info,tf):
        if info.get('kind')=='benchmark':return BenchmarkMixin._frame_uncached(self,info,tf)
        return Desktop014._frame_uncached(self,info,tf)
    market_context=BenchmarkMixin.market_context
    def analyse_cached(self,info,frame,calculation):
        if hasattr(self,'analysis_at'):return analytics.analyse(frame,calculation,now=self.analysis_at)
        return Desktop014.analyse_cached(self,info,frame,calculation)
    user_state=Desktop014.user_state
    source_mode=Desktop014.source_mode
    def instrument_cfg(self):return next(i for i in self.watchlist if i['name']==self.symbol.get())

    def trading_analysis(self,info,analysis_only=False):
        with self.store.read_cache.track_inputs() as token:
            self.store.read_cache.depend('instrument:'+info['isin'])
            scoped=copy.copy(self)
            scoped.analysis_only=bool(analysis_only)
            scoped.set_analysis_scope(getattr(self,'analysis_at',pd.Timestamp.now(tz='UTC')))
            result=scoped._trading_analysis(info)
            self.store.read_cache.assert_current(token)
            return result

    def _trading_analysis(self,info):
        """Existing worker context, cached per generation/inputs/closed candles.

        Reads only existing series. Never starts a feed or touches Tk. The
        engine payload is separate from legacy UI presentation for stage 2B.
        """
        if info.get('kind')=='benchmark':return None
        now=getattr(self,'analysis_at',pd.Timestamp.now(tz='UTC'));tf=self.timeframe.get()
        required=getattr(self,'trading_required_tf',None)
        if required is not None:
            from trading_context import plan
            required=tuple(plan(tf,required)['required_tf'])
        else:required=profile(tf)
        state=copy.deepcopy(self.user_state(info));frames={}
        from cp_controls import demo_initialization_policy,initialization_policy_for
        policy_key='cp_manual_risk:'+info['isin']
        self.store.read_cache.depend('setting:'+policy_key)
        policy=initialization_policy_for(self.store,info)
        from cp_controls import reviewed_entry_identity
        reviewed_identity=reviewed_entry_identity(self.store,info,tf)
        self.approved_demo_warmup=bool(policy and policy.get('older_verified_warmup'))
        # Explicit analysis-only context. Keep chart/archive selection and the
        # existing 5m/15m execution path unchanged; align all CORE dependencies
        # to the owner's verified provider instead of silently joining series.
        approved_analysis=(demo_initialization_policy(policy,info['isin'])
            and tf in policy.get('analysis_timeframes',[])
            and (tf not in policy['entry_timeframes'] or getattr(self,'analysis_only',False)))
        for interval in dict.fromkeys((tf,*required,'1D')):
            try:
                if approved_analysis:
                    frames[interval]=self.closed_frame(info,self.pinned_frame(info,interval,policy['identity']),interval)
                else:
                    frames[interval]=self.frame_for_entry(info,interval,frames[tf]) if interval!=tf and tf in frames else self.frame_for(info,interval)
            except ValueError as error:
                frames[interval]=pd.DataFrame()
                frames[interval].attrs['data_quality']={'state':'INVALID','issues':[str(error)]}
        benchmark_id=state.get('benchmark_id',benchmarks.default_benchmark(info))
        benchmark=benchmarks.CATALOG.get(benchmark_id)
        benchmark_frame=benchmarks.load(self.store,benchmark) if benchmark else pd.DataFrame()
        if benchmark and getattr(self,'five_year_scope',False):
            from calculation_window import scope_raw
            benchmark_frame=scope_raw(benchmark_frame,'1D',now)
        if benchmark:benchmark_frame=self.closed_frame(benchmark,benchmark_frame,'1D')
        generation=tuple(sorted(self.store.read_cache.dependencies(local=True).items()))
        def closed_token(frame,interval):
            if frame.empty:return b''
            data=frame.copy(deep=False);data.attrs={**frame.attrs,'timeframe':interval}
            try:return (analytics.bar_ends(data)<=now).tobytes()
            except (ValueError,TypeError,KeyError):return b'invalid'
        key=('trading_engine',generation,info['isin'],tf,self.period.get(),tuple(required),
             json.dumps(state,sort_keys=True,default=str),str(now.date()),
             tuple((interval,tuple(frame.attrs.get(k) for k in ('source','symbol','venue','currency','timezone')),closed_token(frame,interval),json.dumps({k:frame.attrs.get('data_quality',{}).get(k) for k in ('state','issues')},sort_keys=True,default=str)) for interval,frame in frames.items()),
             closed_token(benchmark_frame,'1D'))
        def calculate_engine():
            if benchmark:
                market=benchmarks.relative_performance(frames['1D'],benchmark_frame,self.period.get(),now=now)
                market.update(benchmark_id=benchmark_id,benchmark_name=benchmark['name'],
                    benchmark_archive=self.store.setting('benchmark_history:'+benchmark_id,{}))
            else:market={'state':'UNAVAILABLE','benchmark_id':benchmark_id,'reason':'Benchmark РЅРµ РІС‹Р±СЂР°РЅ'}
            mode=self.source_mode(info)
            state['expected_identity']=({'source':'gettex','symbol':info['isin'],'venue':'GETTEX','currency':'EUR'} if mode=='gettex' else
                {k:v for k,v in dict(source='proxy',symbol=state.get('history_symbol'),venue=state.get('history_venue'),currency=state.get('history_currency')).items() if v})
            if info.get('eur_history_auto') or frames[tf].attrs.get('source_selection',{}).get('policy')=='proxy_extended_eur_v1':
                state['expected_identity']={k:frames[tf].attrs.get(k) for k in ('source','symbol','venue','currency')}
            if frames[tf].attrs.get('source_selection',{}).get('policy')=='proxy_extended_eur_v1':
                if any(state.get('levels_'+k)!=frames[tf].attrs.get(k) for k in ('source','symbol','venue','currency')):
                    state['levels']={} # Existing levels from another listing are not transferable.
            if approved_analysis:
                # This separately approved calculation explicitly pinned all
                # dependencies above. Legacy Proxy display preferences are not
                # its identity; every actual frame still must match this pin.
                state['expected_identity']=dict(policy['identity'])
            elif reviewed_identity:
                # frame_for already pins the current owner-reviewed entry to
                # this provider. Legacy Proxy preferences are not its inputs.
                # Every dependent frame still has to match this exact identity.
                state['expected_identity']=dict(reviewed_identity)
            result=analytics.analyse_multiframe({interval:frames[interval] for interval in dict.fromkeys((tf,*required))},tf,state,market,now=now,required_tf=required)
            result['analysis_scope']=frames[tf].attrs.get('analysis_scope')
            return result
        result=self.store.read_cache.get(key,calculate_engine,scoped=True,inputs=dict(generation),category='analytics')
        if approved_analysis:
            result=copy.deepcopy(result)
            result['analysis_identity']=policy['identity']
            result['analysis_only']=True
            result['warnings'].append('Отдельный разрешённый демоанализ: GETTEX / '+policy['identity']['symbol']+' / EUR. Исполнение проверяется отдельно на '+', '.join(policy['entry_timeframes'])+'.')
        return result

    def daily_for_entry(self,info,entry):
        return self.frame_for_entry(info,'1D',entry)

    def frame_for_entry(self,info,tf,entry):
        from paper_account import instrument_allowed
        from data_quality import IDENTITY
        identity={k:entry.attrs.get(k) for k in IDENTITY}
        auto=entry.attrs.get('source_selection',{}).get('selected') in ('GETTEX_EUR','GETTEX_CHART_EUR')
        if (not auto and (not instrument_allowed(info) or not info.get('eur_history_auto'))) or identity.get('source') not in ('proxy','gettex','gettex_chart') or identity.get('currency')!='EUR':
            return self.frame_for(info,tf)
        # Calculation-local selection. No preference changes or cross-venue joins;
        # absent/bad daily history remains blocked by the existing CORE gates.
        key=('entry_aligned',info['isin'],tf,tuple(sorted(identity.items())),str(getattr(self,'analysis_at','')),bool(getattr(self,'five_year_scope',False)))
        frame=self.store.read_cache.get(key,
            lambda:Desktop014._frame_uncached(self,info,tf,required_identity=identity),
            scoped=True,inputs=self.store.read_cache.token_for(['instrument:'+info['isin']]))
        return self.closed_frame(info,frame,tf)

def inputs(app):
    return dict(watchlist=copy.deepcopy(app.watchlist),drafts=copy.deepcopy(app.drafts),feed_errors=copy.deepcopy(app.feed_errors),symbol=app.symbol.get(),timeframe=app.timeframe.get(),period=ALIASES.get(app.period.get(),app.period.get()),ai_config=copy.deepcopy(app.ai_config),instrument_revisions=app.store.read_cache.token_for('instrument:'+i['isin'] for i in app.watchlist))


def analysis_inputs(values):
    # Viewport choice is captured/as-of, not a mutable dependency of a running
    # all-list cycle. Strategy parameters, drafts and instrument identity remain.
    return {k:v for k,v in values.items() if k not in ('feed_errors','symbol','timeframe','period')}


def bind_view_selection(state,selected):
    """Preserve an explicitly historical result without relabelling its scope."""
    result=dict(state)
    result.setdefault('analysis_selection',dict(state.get('selected',{})))
    result['selected']=dict(selected)
    if result['selected']!=result['analysis_selection']:
        result.setdefault('analysis_market_context',result.get('market_context'))
        result['market_context']={'state':'UNAVAILABLE','reason':'Current chart differs from the completed analysis context'}
    elif 'analysis_market_context' in result:result['market_context']=result['analysis_market_context']
    return result


class AnalysisCycle:
    """Ten-minute start cadence, one active cycle and one coalesced overdue turn."""
    interval=600
    def __init__(self):self.busy=False;self.next_due=0.;self.pending=False
    def due(self,now):
        if self.busy:
            self.pending |= now>=self.next_due
            return False
        if now<self.next_due and not self.pending:return False
        self.busy=True;self.pending=False;self.next_due=now+self.interval
        return True
    def finished(self):self.busy=False


def run_cycle(client,store,values,last_client):
    """Run in the existing dispatcher lane; persist progress outside Tk.

    Restart starts one fresh coherent cycle. An interrupted calculation is never
    resumed against changed rows, and completed OHLCV are never rewritten here.
    """
    started=time.time();key='analysis_cycle'
    prior=store.setting(key,{})
    record=dict(sequence=int(prior.get('sequence',0))+1,started_at=started,next_due=started+600,
                state='RUNNING',restart_policy='one fresh closed snapshot; no queued replay')
    store.set_setting(key,record)
    try:
        result=client.calculate('snapshot',values,last_client)
        record.update(state='COMPUTED',completed_at=time.time())
        result.setdefault('analysis_snapshot',{})['cycle']=dict(record)
        from revision_diagnostics import safe_observe
        safe_observe(store.path,result,started)
        return result
    except Exception:
        record.update(state='FAILED',completed_at=time.time());raise
    finally:store.set_setting(key,record)

def diagnostic_detail(stock):
    """Evidence from already computed results only: no IO or second engine."""
    engine=stock.get('trading_engine') or {}
    quality=stock.get('data_quality') or {}
    candles=stock.get('candles') or []
    last=candles[-1] if candles else {}
    causes=[]
    def cause(code,evidence,next_check):
        causes.append({'code':code,'certainty':'observed','evidence':evidence,'next_check':next_check})
    if not candles:
        cause('NO_PUBLISHED_CANDLES',{'count':stock.get('count'),'quality':quality.get('state')},
              'Compare stored coverage, selected listing and DQ; absent published candles do not prove an empty database.')
    if quality.get('state') not in (None,'READY'):
        cause('DATA_QUALITY_GATE',quality,'Inspect reported ranges and identity before any recovery; do not change venue/currency silently.')
    mtf=engine.get('timeframe_context') or {}
    for tf,context in mtf.items():
        dq=context.get('data_quality') or {}
        if dq.get('state')!='READY' or not context.get('closed_bar_count'):
            cause('REQUIRED_TF_BLOCKED',{'timeframe':tf,**context},
                  'Check this TF coverage, closed candles and listing identity; do not infer a trading signal from another TF.')
    if engine.get('blockers'):
        cause('ENGINE_BLOCKED',engine['blockers'],'Resolve the cited input gates; changing score thresholds cannot fix missing/invalid inputs.')
    elif engine.get('signal')=='WAIT' and engine.get('entry_blockers'):
        cause('ENTRY_NOT_CONFIRMED',engine['entry_blockers'],'These are unmet strategy conditions, not necessarily a software or data error.')
    if stock.get('error'):
        cause('FEED_REPORTED_ERROR',stock['error'],'Correlate with DQ and coverage; the upstream root cause is unconfirmed without provider evidence.')
    benchmark=engine.get('benchmark_context') or {}
    if benchmark.get('state') in ('UNAVAILABLE','INVALID','GAP','STALE'):
        cause('BENCHMARK_UNAVAILABLE',benchmark.get('reason'), 'Inspect benchmark identity, archive quality and common closed dates; no substitute index.')
    indicators={k:last.get(k) for k in ('ema10','ema30','rsi','macd','macd_signal','atr','volume_mean20')}
    return {'schema_version':1,'isin':stock.get('isin'),'timeframe':stock.get('timeframe'),
            'quote':{k:last.get(k) for k in ('at','close','volume','source','symbol','venue','currency')},
            'history':{k:stock.get(k) for k in ('count','coverage','first','last','completion_status','limitation','closed_bar_count','excluded_open_bars')},
            'indicators':{'latest_published_bar':indicators,'basis':'May include forming bar; confirmed signal uses engine timeframe_context.signal_at',
                          'missing':[k for k,v in indicators.items() if v is None]},
            'signal':{k:engine.get(k) for k in ('signal','status','score','confidence','timestamp','rules_version','bullish_reasons','bearish_reasons','blockers','entry_blockers')},
            'required_timeframes':mtf,'benchmark':benchmark,'causes':causes,
            'upstream_root_cause':'UNCONFIRMED' if causes else 'No blocking cause established by this snapshot'}

def calculate(store,values,last_client):
    # Reuse the same bounded pure-calculation cache as CP assessment. Exact
    # OHLCV, evidence and as-of clocks remain part of every calculation key;
    # revision tracking and final snapshot validation still run below.
    from calculation_reuse import using
    with using(store.read_cache):
        return _calculate(store,values,last_client)


def _calculate(store,values,last_client):
    context=SnapshotContext()
    for key,value in values.items():setattr(context,key,Value(value) if key in ('symbol','timeframe','period') else value)
    context.store=store;context.bridge=SimpleNamespace(last_client=last_client)
    if values.get('analysis_at'):
        context.set_analysis_scope(values['analysis_at'])
    context.tunnel_runtime=SimpleNamespace(snapshot=lambda:values.get('tunnel_state',{}))
    started=time.monotonic();selected_engine=None
    timing={'engine_by_instrument':{}};engine_seconds=0.0
    def consume_stock(stock,info):
        nonlocal selected_engine,engine_seconds
        if info.get('kind')!='benchmark':
            engine_started=time.monotonic()
            stock['trading_engine']=context.trading_analysis(info)
            elapsed=time.monotonic()-engine_started;engine_seconds+=elapsed
            timing['engine_by_instrument'][info['isin']]=round(elapsed,3)
            if info['name']==context.symbol.get():selected_engine=stock['trading_engine']
    # Consume each instrument while its prepared series still occupy the bounded
    # cache. Keep minute/DQ expiry and all worker/publication revision guards.
    records={}
    def build_stock(info):
        # Temporary per-instrument read scopes must not poison the outer token.
        # The complete union is recorded only after every retained record agrees.
        for read_attempt in range(2):
            try:
                with store.read_cache.track_inputs(isolated=True) as token:
                    if hasattr(context,'analysis_at'):context.used_closed_keys=set()
                    store.read_cache.depend('instrument:'+info['isin'])
                    stock=Desktop014.snapshot_stock(context,info)
                    consume_stock(stock,info)
                    store.read_cache.assert_current(token)
                    if hasattr(context,'analysis_at'):
                        from performance_cache import capture_closed_inputs
                        with store.connect() as db:
                            db.execute('BEGIN')
                            actual={k:v for k,v in token.items() if not k.startswith(('setting:benchmark_history:','setting:gettex_file_errors:'))}
                            stock['_closed_inputs']=capture_closed_inputs(db,[context.closed_specs[k] for k in context.used_closed_keys],actual)
                records[info['isin']]=(stock,dict(token))
                return stock
            except InputRevisionChanged as error:
                if read_attempt:
                    raise InputRevisionChanged(f'instrument_read_retry_exhausted {info["isin"]}: {error}') from error
                timing['instrument_read_retries']=timing.get('instrument_read_retries',0)+1
    state=Desktop014.snapshot(context,stock_builder=build_stock)
    repaired=0
    for attempt in range(3):
        scopes={k for _,token in records.values() for k in token}
        current=store.read_cache.token_for(scopes)
        changed=[info for info in context.watchlist if any(current[k]!=v for k,v in records[info['isin']][1].items())]
        if not changed:break
        if attempt==2:
            changes=[f'{info["isin"]}/{key} {old}->{current[key]}' for info in changed
                for key,old in records[info['isin']][1].items() if current[key]!=old]
            raise RuntimeError('Snapshot inputs keep changing after two targeted repair passes: '+'; '.join(changes[:12]))
        for info in changed:build_stock(info);repaired+=1
    state['stocks']=[records[info['isin']][0] for info in context.watchlist]
    union={}
    for _,token in records.values():
        for key,value in token.items():
            if key in union and union[key]!=value:raise RuntimeError('Conflicting snapshot input revisions: '+key)
            union[key]=value
    store.read_cache.assert_current(union)
    store.read_cache._record(union)
    timing['repaired_instruments']=repaired

    timing['legacy_snapshot_seconds']=round(max(0,time.monotonic()-started-engine_seconds),3)
    state['market_context']=selected_engine['benchmark_context'] if selected_engine is not None else context.market_context()
    state['benchmark_catalog']=list(benchmarks.CATALOG.values())
    state['diagnostics']=diagnostics(state)
    state['diagnostic_detail']=[diagnostic_detail(stock) for stock in state['stocks']]
    if hasattr(context,'analysis_at'):
        from performance_cache import capture_closed_inputs
        with store.connect() as db:
            db.execute('BEGIN')
            token={k:v for k,v in union.items() if not k.startswith(('setting:benchmark_history:','setting:gettex_file_errors:'))}
            state['_closed_inputs']=capture_closed_inputs(db,list(context.closed_specs.values()),token)
        state['analysis_snapshot']={'basis':'coherent closed-input snapshot; historical analysis, not execution permission',
            'captured_at':context.analysis_at.isoformat(),'completed_at':pd.Timestamp.now(tz='UTC').isoformat(),
            'as_of':list(context.closed_specs.values()),'execution_permission':False}
    state['_calculation_timing']=timing
    return state


def validate_snapshot(store,state):
    """Retain valid instruments; a changed instrument cannot keep old signals."""
    from performance_cache import validate_closed_inputs,InputRevisionChanged,_digest
    union={};guards=[];invalidated={};memo={}
    with store.connect() as db:
        db.execute('BEGIN')
        for index,stock in enumerate(state.get('stocks',[])):
            guard=stock.get('_closed_inputs')
            try:
                current=validate_closed_inputs(db,guard,memo)
                union.update(current);guards.append(guard)
            except InputRevisionChanged as error:
                invalidated[stock['isin']]=str(error)
                state['stocks'][index]={k:stock[k] for k in ('isin','name','timeframe','source_mode') if k in stock}
                state['stocks'][index].update(candles=[],count=0,input_state='INVALIDATED',
                    data_quality={'state':'STALE','issues':['Closed calculation inputs changed; re-evaluation required'],
                                  'basis':'calculation validity; saved archive DQ not modified'},
                    trading_engine={'status':'DATA BLOCKED','signal':'WAIT','confidence':0,'score':0,
                                    'blockers':['CLOSED_INPUT_CHANGED'],'input_change':str(error)})
    merged=dict(version=1,revisions={},strict={},settings={},settings_start={},series=[]);series={}
    for guard in guards:
        for field in ('revisions','strict','settings','settings_start'):merged[field].update(guard.get(field,{}))
        for item in guard['series']:series[_digest(item['spec'])]=item
    merged['series']=list(series.values())
    state['_closed_inputs']=merged;state['_input_revisions']=union
    state['analysis_snapshot'].update(invalidated_inputs=invalidated,validated_instruments=len(guards))
    state['_calculation_timing']['invalidated_instruments']=len(invalidated)
    if state.get('selected',{}).get('isin') in invalidated:
        state['market_context']={'state':'UNAVAILABLE','reason':'Selected closed inputs changed'}
    state['diagnostics']=diagnostics(state)
    state['diagnostic_detail']=[diagnostic_detail(s) for s in state['stocks']]
    return state
