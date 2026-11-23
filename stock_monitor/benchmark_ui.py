from i18n import tr
"""Benchmark display and market-context API; no Trading Engine decisions."""
import copy
import tkinter as tk
from tkinter import ttk
import pandas as pd
import benchmarks as bm
import data_quality as quality
from analytics import resample

def position_focus(app):
    """Scheduling hints from the existing drafts, without reading source policy.

    owner_history_info only overlays history_venue_lock; it never changes a
    position. Trade calculations still use user_state and their normal guards.
    """
    return [info['isin'] for info in app.watchlist
            if float(app.drafts.get(info['isin'],info).get('position',{}).get('quantity') or 0)!=0]

class BenchmarkMixin:
    def apply_benchmark_labels(self,info,archive):
        meta=bm.identity(info)
        source='Cboe' if meta['source']=='cboe' else 'Yahoo'
        unit=tr(info['unit'])
        self.ax_price.set_title(f"{info['name']} · {self.timeframe.get()} · {source} / {meta['venue']} / {meta['symbol']} · {unit}",color='#e2e8f0',fontsize=10)
        self.ax_price.set_ylabel(unit,color='#94a3b8')
        for key in ('price','ema10','ema30','support','resistance'):
            value=self.metrics[key].get()
            self.metrics[key].set(value.removesuffix(' '+meta['currency'])+' '+unit if value.endswith(' '+meta['currency']) else value)
        for key in ('stop','add','position'):self.metrics[key].set('—')
        self.decision.set(tr('ИНДЕКС'))
        detail=f"{source} · {meta['symbol']} · {meta['venue']} · {meta['timezone']}{tr('\nЕдиница: ')}{unit}{tr('; валюта источника: ')}{meta['currency']}{tr('\nDQ ряда: ')}{archive.get('quality', 'SOURCE_UNAVAILABLE')}{tr('\nАрхив: ')}{archive.get('status', 'INCOMPLETE')} / {archive.get('archive_quality') or archive.get('quality', 'SOURCE_UNAVAILABLE')}\n{archive.get('first', '—')} → {archive.get('last', '—')}"
        self.metrics['updated'].set(detail)
        self.explain.set(detail+tr('\nИндекс используется как рыночный контекст. Торговый объём не применим.'))
        self.canvas.draw_idle()

    def build_ui(self):
        super().build_ui()
        embedded=hasattr(self,'main_benchmark_host')
        row=self.main_benchmark_host if embedded else ttk.Frame(self.chart_outer)
        if not embedded:row.pack(fill='x',before=self.chart_body,padx=6,pady=2)
        ttk.Label(row,text=tr('Benchmark:')).pack(anchor='w') if embedded else ttk.Label(row,text=tr('Benchmark:')).pack(side='left')
        self.benchmark_choice=tk.StringVar()
        self.benchmark_box=ttk.Combobox(row,textvariable=self.benchmark_choice,state='readonly',width=25,
            values=['—']+[x['name'] for x in bm.CATALOG.values() if x['return_basis']!='volatility'])
        self.benchmark_box.pack(side='top' if embedded else 'left',padx=5)
        self.benchmark_box.bind('<<ComboboxSelected>>',self.change_benchmark)
        self.relative_status=ttk.Label(row,text=tr(''),wraplength=250 if embedded else 1050)
        self.relative_status.pack(side='top' if embedded else 'left',fill='x',expand=not embedded)

    def change_benchmark(self,_=None):
        info=self.instrument_cfg();draft=copy.deepcopy(self.user_state(info))
        draft['benchmark_id']=next((k for k,v in bm.CATALOG.items() if v['name']==self.benchmark_choice.get()),None)
        self.drafts[info['isin']]=draft;self.user_dirty=True;self.refresh_chart()

    def _run_feed(self,info):
        if info.get('kind')!='benchmark':return super()._run_feed(info)
        with self.feed_slots:
            state=bm.sync(self.store,info)
            error='; '.join(state.get('issues',[])) if state.get('quality')!='READY' else ''
            self.store.event('benchmark_feed_result',{'isin':info['isin'],'ok':not error,'errors':[error] if error else [],'state':state})
        if not self.closing:self.events.put((self.feed_done,(info,[error] if error else [])))

    def feed_done(self,info,errors):
        # Index candles are analytical context, never positions or trade signals.
        if info.get('kind')!='benchmark':return super().feed_done(info,errors)
        self.feed_active.discard(info['isin'])
        self.feed_errors[info['isin']]='; '.join(errors) if errors else None
        if self.instrument_cfg()['isin']==info['isin']:self.refresh_chart()

    def _frame_uncached(self,info,tf):
        if info.get('kind')!='benchmark':return super()._frame_uncached(info,tf)
        raw=bm.load(self.store,info)
        clock=getattr(self,'analysis_at',None)
        if getattr(self,'five_year_scope',False):
            from calculation_window import scope_raw
            raw=scope_raw(raw,'1D',clock)
        if tf not in ('1D','1W','1M'):
            raw=pd.DataFrame();raw.attrs.update(bm.identity(info),data_quality={'state':'SOURCE_UNAVAILABLE','issues':[tr('Этот прямой источник подключён только для 1D / 1W / 1M; intraday не подменяется')]})
            return raw
        report=quality.audit(raw,'1D',bm.identity(info),now=clock);raw.attrs['data_quality']=report
        if report['state']!='READY':return raw.iloc[:0].copy()
        return resample(raw,'1D',tf,now=clock)

    def market_context(self):
        info=self.instrument_cfg();state=self.user_state(info)
        benchmark_id=state.get('benchmark_id',bm.default_benchmark(info))
        if info.get('kind')=='benchmark':return {'state':'UNAVAILABLE','reason':tr('Самостоятельный индекс'),'benchmark_id':None}
        if benchmark_id not in bm.CATALOG:return {'state':'UNAVAILABLE','reason':tr('Выберите benchmark'),'benchmark_id':None}
        benchmark=bm.CATALOG[benchmark_id]
        result=bm.relative_performance(self.frame_for(info,'1D'),bm.load(self.store,benchmark),self.period.get())
        return {**result,'benchmark_id':benchmark_id,'benchmark_name':benchmark['name'],
                'benchmark_archive':self.store.setting('benchmark_history:'+benchmark_id,{})}

    def refresh_chart(self):
        info=self.instrument_cfg()
        if getattr(self,'_benchmark_opened_isin',None)!=info['isin']:
            # Select a real interval this listing actually supplies when first
            # opened. Later manual requests for unsupported intraday stay unavailable.
            if info.get('kind')=='benchmark' and self.timeframe.get() not in ('1D','1W','1M'):
                self.timeframe.set('1D')
            self._benchmark_opened_isin=info['isin']
        self.dispatcher.focus(info['isin'],position_focus(self))
        # Immutable UI capture: feed workers must never read Tk variables.
        focus=getattr(self,'_cp_focus',{})
        if focus.get('isin')!=info['isin'] or focus.get('timeframe')!=self.timeframe.get():
            self._cp_focus={'isin':info['isin'],'timeframe':self.timeframe.get()}
        from chart_worker import LatestRequest,prepare,capture
        if getattr(self,'closing',False):return
        if getattr(self,'bridge',None) is not None:
            selected={'isin':info['isin'],'timeframe':self.timeframe.get(),'period':self.period.get()}
            if self.bridge.state.get('analysis_snapshot'):
                from snapshot_worker import bind_view_selection
                # The historical all-list result survives viewport changes;
                # the current chart still clears and revalidates its own CORE.
                self.bridge.publish(bind_view_selection(self.bridge.state,selected))
            else:self.bridge.selection_changed(selected)
        if not hasattr(self,'_chart_jobs'):
            self._chart_jobs=LatestRequest(lambda work,done,failed:self.job(work,done,failed,lane='chart',key='chart'),self._apply_chart,self._chart_failed)
        values=capture(self);store=self.store
        import time
        self._chart_requested_at=time.monotonic()
        self.quality_signature=self.current_quality_signature()
        self.relative_status.configure(text=tr('Подготовка графика и Relative Performance…'))
        from trading_view import bind_engine
        self._accepted_trading_chart=None
        bind_engine(self,None)
        self.status.set(tr('Загрузка: {name} · {timeframe}',name=info['name'],timeframe=self.timeframe.get()))
        if self._chart_jobs.running and not self.dispatcher.cancel('chart'):self._supersede_chart_read()
        self._chart_jobs.request(lambda:(values,self._calculate_chart_read(values,'display')))

    def _calculate_chart_read(self,values,phase):
        import time
        active=(object(),phase,time.monotonic())
        self._active_chart_read=active
        try:return self.compute_chart.calculate('chart',{**values,'_chart_phase':phase})
        finally:
            if getattr(self,'_active_chart_read',None) is active:self._active_chart_read=None

    def _supersede_chart_read(self):
        # A short CORE tail often finishes sooner than restarting this readonly
        # process and importing the entire calculation stack again. LatestRequest
        # still discards its superseded result; no permission survives selection.
        import time
        active=getattr(self,'_active_chart_read',None)
        if active is None or active[1]!='engine':
            self.compute_chart.interrupt('chart');return
        pending=getattr(self,'_chart_read_cancel',None)
        if pending and pending[0] is active:return
        if pending:self.root.after_cancel(pending[1])
        delay=max(1,round((5-(time.monotonic()-active[2]))*1000))
        def cancel_old():
            pending=getattr(self,'_chart_read_cancel',None)
            if pending and pending[0] is active:self._chart_read_cancel=None
            if not self.closing and getattr(self,'_active_chart_read',None) is active:
                self.compute_chart.interrupt('chart')
        self._chart_read_cancel=(active,self.root.after(delay,cancel_old))
        emit=getattr(self,'_performance_emit',None)
        if emit:emit('chart_read_handoff',phase='engine',grace_seconds=delay/1000)

    def _chart_failed(self,error):
        if self.closing:return
        from trading_view import bind_engine
        self._accepted_trading_chart=None
        bind_engine(self,None)
        self.relative_status.configure(text=tr('Расчёт недоступен: {error}',error=str(error)))
        self.status.set(tr('Ошибка подготовки графика: {error}',error=str(error)))
        if hasattr(self,'main_screen'):
            self.main_screen.sync_text.set(tr('Синхронизация: ошибка'))
            self.main_screen.sync_label.configure(style='DeskSyncBad.TLabel')
        if 'InputRevisionChanged:' in str(error):
            from chart_worker import capture
            self._retry_current_chart(capture(self))

    def _retry_current_chart(self,values):
        """Reject stale output, then request the current view on the same lane."""
        from chart_worker import capture
        def view(v):return (v['watchlist'][0]['isin'],v['timeframe'],v['period'])
        if self.closing or view(capture(self))!=view(values) or getattr(self,'_chart_retry_pending',False):return
        self._chart_retry_pending=True
        def retry():
            self._chart_retry_pending=False
            if not self.closing and view(capture(self))==view(values):self.refresh_chart()
        self.root.after(500,retry)

    def _apply_chart(self,payload):
        from chart_worker import capture
        values,prepared=payload
        if self.closing:return
        try:current=capture(self)
        except StopIteration:return
        if values!=current:
            self._retry_current_chart(values);return
        token=prepared.get('_input_revisions')
        if token is None or not self.store.read_cache.current(token):
            self._retry_current_chart(values);return
        from partial_chart import current as files_current
        if prepared.get('_chart_engine'):
            accepted=getattr(self,'_accepted_trading_chart',None)
            if not accepted or accepted[0]!=values:return
            display=accepted[1]
            if not self.store.read_cache.current(display.get('_input_revisions',{})):
                self._retry_current_chart(values);return
            if not files_current(display['frame'].attrs.get('partial_chart',{}).get('files',[])):
                self._retry_current_chart(values);return
            merged={**display,**{k:prepared[k] for k in ('trading_engine','context')},
                '_core_pending':False,'_input_revisions':{**display['_input_revisions'],**token}}
            self._accepted_trading_chart=(values,merged)
            from trading_view import bind_engine
            bind_engine(self,merged['trading_engine'])
            self._apply_benchmark_context(merged['context'])
            self._chart_timing('core',prepared)
            return
        if not files_current(prepared['frame'].attrs.get('partial_chart',{}).get('files',[])):
            self._retry_current_chart(values);return
        self._accepted_trading_chart=(values,prepared)
        self._cp_focus={'isin':values['watchlist'][0]['isin'],'timeframe':values['timeframe'],
                        'source':prepared['frame'].attrs.get('source')}
        self._cp_chart_inputs={'isin':values['watchlist'][0]['isin'],'timeframe':values['timeframe'],
            'scopes':[s for s in token if s.startswith('market:'+values['watchlist'][0]['isin']+':')]}
        self._prepared_chart=prepared
        try:self._render_current_display(prepared)
        finally:self._prepared_chart=None
        if hasattr(self,'main_screen'):self.main_screen.bind_chart(prepared)
        self._chart_timing('display',prepared)
        if prepared.get('_core_pending'):
            self._chart_jobs.request(lambda:(values,self._calculate_chart_read(values,'engine')))

    def _render_current_display(self,prepared):
        # Called only after selection, live revisions and receipt files passed
        # _apply_chart. Retain existing artists for unchanged pixel inputs;
        # CORE is cleared/recomputed/bound independently as before.
        revision=prepared.get('_visual_revision')
        mode=getattr(self,'chart_mode',None)
        key=(revision,mode.get() if mode is not None else None)
        if revision and getattr(self,'_display_revision',None)==key:
            self.status.set(self._display_status)
            emit=getattr(self.root,'_performance_emit',None)
            if emit:emit('chart_reused',timeframe=self.timeframe.get())
            return
        self._display_revision=None
        self._render_prepared_chart()
        if revision:
            self._display_revision=key;self._display_status=self.status.get()

    def _chart_timing(self,stage,prepared):
        import time
        emit=getattr(self.root,'_performance_emit',None)
        if emit:emit('chart_switch',stage=stage,timeframe=self.timeframe.get(),
            seconds=round(time.monotonic()-getattr(self,'_chart_requested_at',time.monotonic()),4),
            compute_seconds=prepared.get('_calculation_timing',{}).get('compute_seconds'))

    def frame_for(self,info,tf):
        prepared=getattr(self,'_prepared_chart',None)
        if prepared is not None:return prepared['frame']
        return super().frame_for(info,tf)

    def analyse_cached(self,info,frame,calculation):
        prepared=getattr(self,'_prepared_chart',None)
        if prepared is not None:return prepared['analysis']
        return super().analyse_cached(info,frame,calculation)

    def level_zones(self,info,frame):
        prepared=getattr(self,'_prepared_chart',None)
        if prepared is not None:return prepared['zones']
        return super().level_zones(info,frame)

    def close(self,*args,**kwargs):
        result=super().close(*args,**kwargs)
        if self.closing:
            self.dispatcher.close()
            if hasattr(self,'_chart_jobs'):self._chart_jobs.close()
            self.compute_chart.close();self.compute_snapshot.close()
            for request in getattr(self,'_feed_analysis_jobs',{}).values():request.close()
            self.compute_feed.close();self.compute_feed_update.close()
            if hasattr(self,'compute_eligibility'):self.compute_eligibility.close()
            if hasattr(self,'compute_trading'):self.compute_trading.close()
        return result

    def _render_prepared_chart(self):
        super().refresh_chart()
        self._apply_benchmark_context(self._prepared_chart['context'])

    def _apply_benchmark_context(self,context):
        if not hasattr(self,'relative_status'):return
        info=self.instrument_cfg();benchmark_id=context.get('benchmark_id')
        self.benchmark_choice.set(bm.CATALOG[benchmark_id]['name'] if benchmark_id else '—')
        if info.get('kind')=='benchmark':
            meta=bm.identity(info);archive=self.store.setting('benchmark_history:'+info['isin'],{})
            self.source_choice.set(tr('Cboe — официальный VIX') if meta['source']=='cboe' else tr('Yahoo — прямой индекс'))
            intraday_note=tr(' · intraday SOURCE_UNAVAILABLE; direct daily only') if self.timeframe.get() not in ('1D','1W','1M') else ''
            self.source_status.set(tr('{p0} · {p1} · {p2} · {p3} · {p4} · DQ {p5} · archive {p6} · {p7} → {p8} · {p9}{p10}',p0=meta['symbol'],p1=meta['venue'],p2=meta['currency'],p3=meta['timezone'],p4=tr(info['unit']),p5=archive.get('quality', 'SOURCE_UNAVAILABLE'),p6=archive.get('status', 'INCOMPLETE'),p7=archive.get('first', '—'),p8=archive.get('last', '—'),p9=tr(info['return_basis']),p10=intraday_note))
            self.relative_status.configure(text=tr('Индекс: объём не является торговым объёмом. ')+(tr('VIX — контекст волатильности.') if info['return_basis']=='volatility' else tr('История 1D / 1W / 1M.')))
            self.apply_benchmark_labels(info,archive)
        else:
            from benchmark_display import relative_text
            meta=context.get('benchmark_identity') or (bm.identity(bm.CATALOG[benchmark_id]) if benchmark_id in bm.CATALOG else {})
            self.relative_status.configure(text=relative_text(context,meta))

    def snapshot(self):
        result=super().snapshot();result['market_context']=self.market_context()
        result['benchmark_catalog']=list(bm.CATALOG.values())
        return result

