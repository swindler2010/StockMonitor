"""Prepare a chart without touching Tk; rendering stays on the main thread."""
import copy
from snapshot_worker import SnapshotContext,Value
from extras016 import Extras016

def capture(app):
    from snapshot_worker import ALIASES
    symbol=app.symbol.get();selected=next(i for i in app.watchlist if i['name']==symbol)
    return dict(watchlist=[copy.deepcopy(selected)],
        drafts={selected['isin']:copy.deepcopy(app.drafts[selected['isin']])} if selected['isin'] in app.drafts else {},
        feed_errors={},symbol=symbol,timeframe=app.timeframe.get(),
        period=ALIASES.get(app.period.get(),app.period.get()),ai_config=copy.deepcopy(app.ai_config),
        instrument_revisions=app.store.read_cache.token_for(['instrument:'+selected['isin']]))

def display_epoch():
    import pandas as pd
    return pd.Timestamp.now(tz='UTC').floor('min').isoformat()

def prepare(store,values):
    # Same worker/queue, two serial UI stages. A visual cache never contains a
    # signal permission. CORE is calculated separately with its current clock.
    phase=values.get('_chart_phase')
    if phase=='display':
        import json
        # Period is a viewport, not an input to this full-depth visual frame.
        key=('chart_display',json.dumps({k:v for k,v in values.items() if k!='period'},sort_keys=True,default=str),display_epoch())
        return store.read_cache.get(key,lambda:_prepare(store,values),scoped=True,
            inputs=values.get('instrument_revisions',{}))
    return _prepare(store,values)

def _prepare(store,values):
    phase=values.get('_chart_phase')
    context=SnapshotContext()
    for key,value in values.items():
        if key=='_chart_phase':continue
        setattr(context,key,Value(value) if key in ('symbol','timeframe','period') else value)
    context.store=store
    info=context.instrument_cfg();state=context.user_state(info)
    if phase=='engine':
        engine=context.trading_analysis(info,analysis_only=True)
        return dict(_chart_engine=True,trading_engine=engine,
            context=(engine or {}).get('benchmark_context') or {'state':'UNAVAILABLE','benchmark_id':None})
    frame=context.frame_for(info,context.timeframe.get())
    from partial_chart import select_display,bind
    frame=select_display({**info,'history_venue_lock':state.get('history_venue_lock')},context.timeframe.get(),frame,store)
    if frame.attrs.get('partial_chart'):
        # Display-only evidence must not depend on a successful signal/engine
        # calculation, nor become a replacement input to that calculation.
        prepared=bind({'frame':frame,'analysis':None,'zones':[],
                       'context':None,'trading_engine':None},frame,store.setting('global_indicators',{}))
        if phase=='display':
            from cp_controls import initialization_policy_for,reviewed_entry_identity
            policy=initialization_policy_for(store,info);tf=context.timeframe.get()
            if (policy and tf in policy.get('analysis_timeframes',[])) or reviewed_entry_identity(store,info,tf):
                prepared.update(_core_pending=True,trading_engine=None)
            return prepared
        # A display-only archive is never a CORE input. An explicitly approved
        # context can nevertheless calculate independently from verified rows.
        # Keep the original chart DQ and segment-local indicators unchanged.
        from performance_cache import InputRevisionChanged
        try:
            from cp_controls import initialization_policy_for,reviewed_entry_identity
            policy=initialization_policy_for(store,info)
            tf=context.timeframe.get()
            if (policy and tf in policy.get('analysis_timeframes',[])) or reviewed_entry_identity(store,info,tf):
                engine=context.trading_analysis(info,analysis_only=True)
                if isinstance(engine,dict) and engine.get('status') in ('READY','DATA BLOCKED'):
                    prepared['trading_engine']=engine
                    prepared['context']=engine.get('benchmark_context',prepared['context'])
                    prepared['analysis'][1]['reason']=str(frame.attrs.get('data_quality',{}).get('state','UNKNOWN'))+' / PARTIAL: качество архива сохранено; CORE проверяется в отдельном расчётном контексте'
        except InputRevisionChanged:
            raise
        except Exception as error:
            # A failed independent analysis must not hide valid chart segments
            # or turn them into permission. bind() already supplied WAIT/BLOCKED.
            prepared['engine_error']=str(error)
        return prepared
    analysis=None;zones=[]
    if not frame.empty:
        # The live display row must not change the confirmed analytical price,
        # levels or indicator warmup. Return it only in the rendered frame.
        live=frame.attrs.get('live_candle',{}).get('timestamp')
        confirmed=frame.drop(index=live) if live is not None else frame
        calculation=copy.deepcopy(state);mode=context.source_mode(info)
        if frame.attrs.get('currency')!=state.get('levels_currency',state.get('currency','EUR')) or state.get('levels_source',mode)!=frame.attrs.get('source'):
            calculation['levels']={}
        analysis=context.analyse_cached(info,confirmed,calculation)
        if live is not None:
            enriched=analysis[0].reindex(frame.index)
            for k in ('open','high','low','close','volume','bar_complete'):
                if k in frame:enriched[k]=frame[k]
            # Inspect the evolving indicators separately. Confirmed decision,
            # levels and execution above/below still use closed data only.
            from analytics import indicators
            preview=indicators(frame)
            for k in ('ema10','ema30','rsi','atr','macd','macd_signal','volume_mean20'):
                enriched.loc[live,k]=preview.loc[live,k]
            enriched.attrs=frame.attrs.copy();analysis=(enriched,analysis[1])
        if analysis[1].get('ohlcv_valid',False):zones=Extras016.level_zones(context,info,confirmed)
    engine=None if phase=='display' else context.trading_analysis(info,analysis_only=True)
    pending=phase=='display' and info.get('kind')!='benchmark'
    market=({'state':'UNAVAILABLE','benchmark_id':None,'reason':'CORE рассчитывается в фоне'} if pending else
            engine['benchmark_context'] if engine is not None else context.market_context())
    prepared={'frame':frame,'analysis':analysis,'zones':zones,'context':market,'trading_engine':engine}
    if pending:prepared['_core_pending']=True
    return prepared

def prepare_feed(store,values):
    """Existing post-feed analysis, isolated from Tk's Python interpreter."""
    from analytics import analyse
    context=SnapshotContext();context.store=store
    for key,value in values.items():
        setattr(context,key,Value(value) if key in ('symbol','timeframe','period') else value)
    info=context.watchlist[0];state=context.user_state(info)
    official=store.load_official(info['isin']);notification=None
    if context.source_mode(info)=='gettex':
        frame=context.frame_for(info,context.timeframe.get())
        if len(frame)>30:
            _,signal=analyse(frame.iloc[:-1],state)
            notification=(signal['decision'],frame.index[-2])
    return {'official':official,'notification':notification}

class LatestRequest:
    """One active calculation, one replaceable pending request; no stale apply."""
    def __init__(self,submit,apply,failed):
        self.submit=submit;self.apply=apply;self.failed=failed
        self.serial=0;self.running=False;self.pending=None;self.closed=False

    def request(self,work):
        if self.closed:return
        self.serial+=1;self.pending=(self.serial,work)
        if not self.running:self._start()

    def _start(self):
        if self.closed or self.pending is None:return
        serial,work=self.pending;self.pending=None;self.running=True
        def finish(result,error=False):
            self.running=False
            if not self.closed and serial==self.serial:
                (self.failed if error else self.apply)(result)
            self._start()
        self.submit(work,lambda result:finish(result),lambda error:finish(error,True))

    def close(self):
        self.closed=True;self.pending=None;self.serial+=1
