from localized_widgets import CanonicalCombobox
from i18n import tr, rendered_message
import copy
from datetime import datetime, timezone
import hashlib
import json
import math
from pathlib import Path
import queue
import re
import threading
import time
import tkinter as tk
from tkinter import ttk, messagebox, filedialog
from tkinter.scrolledtext import ScrolledText
import webbrowser
import pandas as pd

from ai_service import AIService, KeyVault, DEFAULTS, validate_config
from analytics import analyse, diagnostics, resample
from paths import DATA_DIR
import market

def text_area(parent):
    style=ttk.Style(parent);bg=style.lookup('TFrame','background') or '#111827';fg=style.lookup('TLabel','foreground') or '#e5e7eb'
    widget=ScrolledText(parent,wrap='word',font=('Segoe UI',10),background=bg,foreground=fg,insertbackground=fg,padx=12,pady=12)
    widget.pack(fill='both',expand=True); return widget

def set_text(widget,text):
    if widget.winfo_exists():
        # Avoid layout/repaint and losing selection/scroll on every status poll.
        # Read the widget itself so an external edit cannot make a cache stale.
        if widget.get('1.0','end-1c')==text:
            if str(widget.cget('state'))!='disabled':widget.configure(state='disabled')
            return
        widget.configure(state='normal');widget.delete('1.0','end');widget.insert('1.0',text);widget.configure(state='disabled')

class Features:
    def register_confirmed_instrument(self,item):
        # Add is an explicit durable registration. Persist only this instrument,
        # not unrelated unsaved drawings/settings, before the CP worker starts.
        from eur_listings import confirmed_listing
        meta=dict(source='proxy',symbol=item.get('history_symbol'),venue=item.get('history_venue'),
                  currency=item.get('history_currency'),timezone=item.get('timezone','Europe/Berlin'))
        if not confirmed_listing(item,meta):raise ValueError('NEW_INSTRUMENT_IDENTITY_UNCONFIRMED')
        if any(i['isin']==item['isin'] for i in self.watchlist):raise ValueError('Instrument already exists')
        self.store.save_instrument(item)
        self.watchlist.append(item)
        if hasattr(self,'persisted'):self.persisted.append(copy.deepcopy(item))
        self.start_feed(item)

    def init_features(self):
        from compute_process import Client
        self.compute_chart=Client(self.store.path);self.compute_snapshot=Client(self.store.path)
        self.compute_feed=Client(self.store.path)
        self.compute_eligibility=Client(self.store.path)
        self.compute_trading=Client(self.store.path)
        self.compute_feed_update=Client(self.store.path,feed_updates=True)
        self.events=queue.Queue(); self.closing=False; self.feed_active=set(); self.feed_slots=threading.Semaphore(2)
        from performance_dispatcher import Dispatcher
        self.dispatcher=Dispatcher(lambda callback,value:self.events.put((callback,(value,))),self.store.directory/'performance-queue.json')
        self.root._performance_dispatcher=self.dispatcher
        self.user_dirty=False
        self.feed_errors={}; self.ai_busy=False; self.card_jobs=set(); self.ai_window=None
        self.ai_config={**DEFAULTS,**self.store.setting('ai_config',{})}
        self.ai=AIService(self.store,KeyVault(DATA_DIR/'secrets'))
        self.ai_status=tk.StringVar(value='ИИ: локальная диагностика')
        self.last_ai_time=0; self.last_ai_digest=None; self.alerts=set()
        self.root.protocol('WM_DELETE_WINDOW',self.close)
        self.root.after(100,self.pump)
        self.root.after(1000,self.ai_tick)
        from bridge import Bridge
        self.bridge=Bridge(DATA_DIR,self.events,self.bridge_dispatch,lambda:self.store.setting('mcp_allow_edits',False))
        self.snapshot_after=self.root.after(1500,self.publish_bridge)
        self.root.after(30000,lambda:self.check_updates(automatic=True))
        from news_runtime import NewsRuntime
        self.news_runtime=NewsRuntime(self);self.news_runtime.start()
        from paper_hourly import Runtime as PaperRuntime
        self.paper_runtime=PaperRuntime(self);self.paper_runtime.start()
        from paper_strategy_shadow import Runtime as ShadowRuntime
        self.paper_shadow=ShadowRuntime(self);self.paper_shadow.start()
        from cp_assessment import Runtime as AssessmentRuntime
        self.cp_assessment=AssessmentRuntime(self);self.cp_assessment.start()
        from watchlist_order import Runtime as WatchlistOrder
        self.stock_order=WatchlistOrder(self);self.stock_order.start()
        if self.ai_config['open_on_start']:self.root.after(750,self.show_ai)

    def close(self):
        if self.user_dirty:
            answer=messagebox.askyesnocancel(tr('Несохранённые изменения'),tr('Сохранить пользовательские линии и настройки перед выходом?'))
            if answer is None:return
            if answer:self.save_user_changes()
        self.closing=True
        self.bridge.close()
        self.store.set_setting('selected',{'symbol':self.symbol.get(),'timeframe':self.timeframe.get(),'period':self.period.get()})
        self.root.destroy()

    def save_user_changes(self):
        for item in self.watchlist:self.store.save_instrument(item)
        self.store.set_setting('indicators',self.store.setting('indicators',{'ema10':True,'ema30':True,'sma20':False,'bollinger':False,'vwap':False,'volume':True}))
        self.user_dirty=False;self.status.set(tr('Пользовательские настройки сохранены.'))

    def show_settings(self):
        win=tk.Toplevel(self.root);win.title(tr('Настройки'));win.geometry('600x650')
        notebook=ttk.Notebook(win);notebook.pack(fill='both',expand=True,padx=12,pady=12)
        pages={}
        for name in ('Вид и график','Данные','Уведомления','ChatGPT / MCP'):
            pages[name]=ttk.Frame(notebook,padding=18);notebook.add(pages[name],text=tr(name))
        body=pages['Вид и график']
        from i18n import language_settings
        language_settings(body,self.store)
        for label,command in [('Оформление',self.show_theme),('Индикаторы выбранной акции',self.show_indicators),('Рисование',self.show_drawing)]:
            ttk.Button(body,text=tr(label),command=command).pack(fill='x',pady=5)
        ttk.Label(body,text=tr('Параметры индикаторов и рисунки сохраняются для выбранной акции кнопкой «Сохранить». Тема сохраняется сразу.'),wraplength=480).pack(pady=12)
        actions=[('Связь с Codex',self.show_connection),('Обновить программу',lambda:self.check_updates(False)),('ИИ и диагностика',self.show_ai),('Импорт базы',self.import_legacy_data),('Создать резервную копию',self.backup_data)]
        for label,command in actions:
            target=pages['ChatGPT / MCP'] if label in ('Связь с Codex','ИИ и диагностика') else pages['Данные']
            ttk.Button(target,text=tr(label),command=command).pack(fill='x',pady=4)
        body=pages['Уведомления']
        alerts=tk.BooleanVar(value=self.store.setting('alerts_enabled',True))
        def alerts_changed():self.store.set_setting('alerts_enabled',alerts.get())
        ttk.Checkbutton(body,text=tr('Уведомления Windows'),variable=alerts,command=alerts_changed).pack(anchor='w',pady=12)
        body=pages['ChatGPT / MCP']
        edits=tk.BooleanVar(value=self.store.setting('mcp_allow_edits',False))
        def edits_changed():
            if edits.get() and not messagebox.askyesno(tr('Управление через MCP'),tr('Разрешить подключённому MCP менять список акций, позиции и журнал рекомендаций? Это не даёт доступа к сделкам у брокера.'),parent=win):
                edits.set(False)
            self.store.set_setting('mcp_allow_edits',edits.get())
        ttk.Checkbutton(body,text=tr('Разрешить MCP сохранять изменения в программе'),variable=edits,command=edits_changed).pack(anchor='w',pady=8)
        def configure_tunnel():
            folder=filedialog.askdirectory(title=tr('Папка настроенного туннеля (с tunnel-client и profiles)'),parent=win)
            if not folder:return
            profile=Path(folder)/'profiles'/'stock-monitor-managed.yaml'
            try:
                data=json.loads(profile.read_text(encoding='utf-8-sig'))
                if not data.get('control_plane',{}).get('api_key','').startswith('file:'):raise ValueError('Нужен профиль с локальным защищённым файлом ключа')
                if not (Path(folder)/'tunnel-client'/'tunnel-client.exe').is_file():raise ValueError('Не найден официальный tunnel-client.exe')
            except (OSError,ValueError) as exc:
                messagebox.showerror(tr('Подключение'),rendered_message(str(exc)),parent=win);return
            if not messagebox.askyesno(tr('Автоматическое подключение'),tr('Использовать этот ранее настроенный туннель при запуске Stock Monitor? Доступ будет соответствовать его существующему профилю.'),parent=win):return
            config={'enabled':True,'directory':folder,'profile':'stock-monitor-managed','alias':'stock-monitor'}
            self.store.set_setting('chatgpt_tunnel',config)
            from tunnel_runtime import TunnelRuntime
            self.tunnel_runtime=TunnelRuntime(config);self.tunnel_runtime.poll()
        ttk.Button(body,text=tr('Настроить автозапуск туннеля ChatGPT'),command=configure_tunnel).pack(fill='x',pady=4)
        body=pages['Данные']
        ttk.Label(body,text=tr('Источник выбирается явно на графике. При ошибках или короткой истории приложение не переключает валюту и площадку автоматически.'),wraplength=470).pack(anchor='w',pady=4)
        win.geometry('640x600')
        ttk.Label(body,text=tr('Рыночная история сохраняется автоматически. Кнопка «Сохранить» относится к пользовательской разметке и настройкам.'),wraplength=470).pack(anchor='w')

    def show_indicators(self):
        current=self.store.setting('indicators',{'ema10':True,'ema30':True,'sma20':False,'bollinger':False,'vwap':False,'volume':True})
        win=tk.Toplevel(self.root);win.title(tr('Индикаторы'));win.geometry('410x360');body=ttk.Frame(win,padding=18);body.pack(fill='both',expand=True)
        labels={'ema10':'EMA 10','ema30':'EMA 30','sma20':'SMA 20','bollinger':'Полосы Боллинджера (20, 2)','vwap':'VWAP','volume':'Объём'}
        values={k:tk.BooleanVar(value=current.get(k,False)) for k in labels}
        for key,label in labels.items():ttk.Checkbutton(body,text=tr(label),variable=values[key]).pack(anchor='w',pady=5)
        ttk.Label(body,text=tr('Несколько индикаторов можно включать одновременно. Настройки сохраняются локально.'),wraplength=360).pack(anchor='w',pady=10)
        def apply():
            self.store.set_setting('indicators',{k:v.get() for k,v in values.items()});self.refresh_chart();win.destroy()
        ttk.Button(body,text=tr('Применить и сохранить'),command=apply).pack(anchor='e',pady=8)

    def show_drawing(self):
        item=self.instrument_cfg();win=tk.Toplevel(self.root);win.title(tr('Рисование • {name}',name=item['name']));win.geometry('470x390')
        body=ttk.Frame(win,padding=18);body.pack(fill='both',expand=True)
        ttk.Label(body,text=tr('Пользовательская разметка'),font=('Segoe UI',14,'bold')).pack(anchor='w')
        kind=tk.StringVar(value='Горизонтальный уровень');value=tk.StringVar()
        CanonicalCombobox(body,textvariable=kind,state='readonly',values=['Горизонтальный уровень','Вертикальная линия'],width=30).pack(fill='x',pady=(14,5))
        ttk.Label(body,text=tr('Цена или дата/время (ГГГГ-ММ-ДД ЧЧ:ММ)')).pack(anchor='w');ttk.Entry(body,textvariable=value).pack(fill='x',pady=5)
        saved=tk.StringVar(value=rendered_message(f"Объектов: {len(item.get('drawings',[]))}"));ttk.Label(body,textvariable=saved).pack(anchor='w',pady=8)
        def add():
            raw=value.get().strip()
            try:
                if kind.get().startswith('Горизонтальный'):obj={'type':'hline','price':float(raw.replace(',','.'))}
                else:obj={'type':'vline','at':str(pd.Timestamp(raw))}
                item.setdefault('drawings',[]).append(obj);self.user_dirty=True;saved.set(rendered_message(f"Объектов: {len(item['drawings'])} • нажмите «Сохранить»"));value.set('');self.refresh_chart()
            except Exception:messagebox.showerror(tr('Разметка'),tr('Укажите корректную положительную цену или дату.'),parent=win)
        def clear():
            if not item.get('drawings'):return
            item['drawings']=[];self.user_dirty=True;saved.set(rendered_message('Объектов: 0 • нажмите «Сохранить»'));self.refresh_chart()
        row=ttk.Frame(body);row.pack(fill='x',pady=8);ttk.Button(row,text=tr('Добавить'),command=add).pack(side='left');ttk.Button(row,text=tr('Удалить все'),command=clear).pack(side='right')
        ttk.Label(body,text=tr('Автоматические уровни показаны полупрозрачно. Пользовательские линии — ярким голубым пунктиром.'),wraplength=420).pack(anchor='w',pady=10)

    def show_news(self):
        from news_panel import show
        return show(self)

    def legacy_show_news(self):
        info=copy.deepcopy(self.instrument_cfg());win=tk.Toplevel(self.root);win.title(tr('Новости • {name}',name=info['name']));win.geometry('900x620')
        top=ttk.Frame(win,padding=10);top.pack(fill='x');status=tk.StringVar(value='Сохранённые новости');ttk.Label(top,textvariable=status).pack(side='left')
        box=text_area(win);links=[]
        def render(card):
            links.clear();lines=[]
            from news_view import articles
            for article in articles(card,info.get('profile_symbol') or info.get('history_symbol')):
                links.append(article['url']);lines.append(f"{len(links)}. {article['published']} • {article['provider']}\n{article['title']}\n{article['relevance']}\n{article['url'] or 'Оригинал недоступен'}")
            set_text(box,'\n\n'.join(lines) if lines else tr('Свежие новости пока не получены.'))
        def update():
            symbol=info.get('profile_symbol') or info.get('history_symbol')
            if not symbol:messagebox.showinfo(tr('Новости'),tr('Сначала укажите тикер карточки в настройках инструмента.'),parent=win);return
            status.set(tr('Загрузка свежих новостей…'))
            def done(card):self.store.set_setting('card:'+info['isin'],card);status.set(tr('Новости обновлены'));render(card)
            self.job(lambda:market.company_card(symbol,DATA_DIR/'provider_cache'),done,lambda msg:status.set(tr('Ошибка: ') + msg[:130]))
        def open_original():
            from tkinter import simpledialog
            if not links:return
            n=simpledialog.askinteger(tr('Открыть оригинал'),tr('Номер новости (1–{p0}):',p0=len(links)),parent=win,minvalue=1,maxvalue=len(links))
            if n and links[n-1]:webbrowser.open(links[n-1])
        ttk.Button(top,text=tr('Обновить'),command=update).pack(side='right');ttk.Button(top,text=tr('Открыть оригинал'),command=open_original).pack(side='right',padx=6)
        render(self.store.setting('card:'+info['isin'],{}))
        win.after(100,update)

    def publish_bridge(self):
        if self.closing:return
        # Direct command requests replace the periodic timer, never multiply it.
        if getattr(self,'snapshot_after',None) is not None:
            self.root.after_cancel(self.snapshot_after)
            self.snapshot_after=None
        from snapshot_worker import inputs,AnalysisCycle,analysis_inputs,run_cycle
        if not hasattr(self,'_analysis_cycle'):self._analysis_cycle=AnalysisCycle()
        if self._analysis_cycle.due(time.time()):
            values=inputs(self);transport=self.tunnel_runtime.snapshot();self.snapshot_busy=True
            started=time.monotonic()
            self.bridge.diagnostic_event('RUNNING',started_at=time.time(),duration_seconds=None,cause=None,
                                         instrument_count=len(values['watchlist']),generation_mode='input_revisions')
            def done(state):
                self.snapshot_busy=False;self._analysis_cycle.finished()
                current=inputs(self)
                changed=[key for key in analysis_inputs(values) if values[key]!=current.get(key)]
                accepted=self.accept_background_snapshot(state,values)
                self.bridge.diagnostic_event('ACCEPTED' if accepted else 'REJECTED',duration_seconds=round(time.monotonic()-started,3),
                    cause=None if accepted else 'INPUT_CHANGED',changed_inputs=changed,
                    dependency_count=len(state.get('_input_revisions',{})),inputs_current=accepted,
                    calculation_timing=state.get('_calculation_timing',{}))
            def failed(message):
                self.snapshot_busy=False;self._analysis_cycle.finished();self._write_log('Bridge snapshot: '+message)
                # Only a classified cause, never arbitrary exception text/tokens.
                self.bridge.diagnostic_event('FAILED',duration_seconds=round(time.monotonic()-started,3),
                    cause='RELEVANT_INPUT_CHANGED' if any(marker in message for marker in (
                        'Stale Trading Engine input dependency','Snapshot inputs keep changing',
                        'Conflicting snapshot input revisions')) else 'WORKER_ERROR',
                    changed_inputs=[],generation_now=self.store.read_cache.version())
            last_client=self.bridge.last_client
            self.job(lambda:run_cycle(self.compute_snapshot,self.store,{**values,'tunnel_state':transport},last_client),done,failed,lane='snapshot',key='snapshot')
        self.snapshot_after=self.root.after(5000,self.publish_bridge)

    def accept_background_snapshot(self,state,values):
        from snapshot_worker import inputs,diagnostic_detail,analysis_inputs
        from analytics import diagnostics
        # Preserve the captured analytical context when only the viewport moved.
        # Drafts/identity/strategy changes still reject, as does shutdown.
        current=inputs(self)
        if self.closing or analysis_inputs(values)!=analysis_inputs(current):return False
        # Feed error text is presentation telemetry, not a CORE/DQ input.
        # Refresh both views from the same current copy, without recalculation.
        for stock in state.get('stocks',[]):stock['error']=current['feed_errors'].get(stock['isin'])
        timing=state.setdefault('_calculation_timing',{})
        now=time.time()
        if timing.get('finished_at') is not None:timing['publication_wait_seconds']=round(max(0,now-timing['finished_at']),3)
        if state.get('at'):timing['snapshot_age_at_publication_seconds']=round(max(0,now-datetime.fromisoformat(state['at']).timestamp()),3)
        timing['instrument_count']=len(state.get('stocks',[]))
        token=state.get('_input_revisions')
        if token is None:
            self.bridge.revision_rejected(state,'INPUT_REVISION_CHANGED')
            return False
        current_token=self.store.read_cache.token_for(token)
        if current_token!=token:
            self.bridge.revision_rejected(state,'INPUT_REVISION_CHANGED')
            from snapshot_worker import retain_current_publication
            filter_started=time.monotonic()
            if not retain_current_publication(state,current_token):return False
            timing['publication_invalidation_seconds']=round(time.monotonic()-filter_started,6)
            if not self.store.read_cache.current(state['_input_revisions']):return False
        state['diagnostics']=diagnostics(state)
        state['diagnostic_detail']=[diagnostic_detail(stock) for stock in state.get('stocks',[])]
        if state.get('analysis_snapshot'):
            state['analysis_selection']=dict(state.get('selected',{}))
            selected=next((i for i in current['watchlist'] if i['name']==current['symbol']),None)
            if selected is None:return False
            state['selected']={'isin':selected['isin'],'timeframe':current['timeframe'],'period':current['period']}
            if state['selected']!=state['analysis_selection']:
                state['analysis_market_context']=state.get('market_context')
                state['market_context']={'state':'UNAVAILABLE','reason':'Current chart differs from the completed analysis context'}
            state['analysis_snapshot']['published_at']=datetime.now(timezone.utc).isoformat()
        self.bridge.publish(state)
        self._latest_snapshot=state;self._latest_snapshot_inputs=values
        self.local_report='\n\n'.join(rendered_message(v) for v in state.get('diagnostics',[]))
        self.update_ai_view()
        return True

    def bridge_dispatch(self,action,args):
        if action=='paper_trade':
            from paper_orders import owner_route, submit
            if owner_route(self.store.setting('paper_auto_1h',{}),args.get('isin')):
                return submit(self,args,'CHATGPT')
            from analytical_read import capture
            from paper_account import execute, instrument_allowed
            from concurrent.futures import Future
            import sqlite3
            if not instrument_allowed(next((i for i in self.watchlist if i['isin']==args.get('isin')),None)):
                raise ValueError('PAPER_INSTRUMENT_BLOCKED')
            if self.user_dirty:raise ValueError('Save local drafts before paper orders')
            if getattr(self,'_paper_busy',False):raise ValueError('PAPER_BUSY')
            # At 5m two full GETTEX sessions exceed one 500-row page. A trailing
            # 24h range contains at most 289 timestamps, including a boundary.
            request={'isin':args['isin'],'timeframe':args.get('timeframe','15m'),'limit':500,'from':(pd.Timestamp.now(tz='UTC')-pd.Timedelta(days=1)).isoformat()}
            values=capture(self,request);future=Future();self._paper_busy=True
            def done(result):
                self._paper_busy=False
                try:
                    if self.closing or not self.store.setting('mcp_allow_edits',False) or capture(self,request)!=values:raise ValueError('PAPER_STALE_INPUTS')
                    info=values['watchlist'][0]
                    with sqlite3.connect(self.store.path,timeout=.05) as db:
                        db.execute('BEGIN IMMEDIATE')
                        answer=execute(db,info,result,args)
                    future.set_result(answer)
                except Exception as error:
                    code=str(error)
                    if code.startswith('PAPER_DATA_BLOCKED ('):
                        future.set_result({'status':'BLOCKED','code':'PAPER_DATA_BLOCKED','detail':code})
                    else:
                        future.set_result({'status':'BLOCKED','code':code if code.startswith('PAPER_') else 'PAPER_EXECUTION_UNAVAILABLE'})
            def failed(message):
                self._paper_busy=False;future.set_result({'status':'BLOCKED','code':'PAPER_DATA_UNAVAILABLE'})
            self.job(lambda:self.compute_chart.calculate('read_analytics',values),done,failed,lane='chart',key='paper_quote')
            return future
        if action=='read_analytics':
            from analytical_read import capture
            from concurrent.futures import Future
            # Use the existing serial chart lane instead of failing a second
            # request while the first is still completing after HTTP's wait.
            if getattr(self,'_analytical_reads_pending',0)>=4:
                return {'status':'BLOCKED','code':'READ_QUEUE_FULL','retryable':True}
            args=dict(args);deadline=args.pop('_deadline',time.time()+110)
            values=capture(self,args);future=Future()
            self._analytical_reads_pending=getattr(self,'_analytical_reads_pending',0)+1
            def work():
                for attempt in range(2):
                    if time.time()>=deadline:raise ValueError('READ_EXPIRED')
                    try:return self.compute_chart.calculate('read_analytics',values)
                    except RuntimeError as error:
                        # Fresh worker scope, same captured identity/settings.
                        # Never retry arbitrary errors or bypass final guards.
                        if attempt or not str(error).startswith('InputRevisionChanged:'):raise
            def done(result):
                self._analytical_reads_pending-=1
                try:
                    if self.closing or capture(self,args)!=values or not self.store.read_cache.current(result.get('_input_revisions',{})):raise ValueError('STALE_INPUTS')
                    result.setdefault('timestamps',{})['application_result_received_at']=pd.Timestamp.now(tz='UTC').isoformat()
                    future.set_result(result)
                except Exception:future.set_result({'status':'BLOCKED','code':'STALE_INPUTS'})
            def failed(message):
                self._analytical_reads_pending-=1
                codes=('IDENTITY_MISMATCH','INVALID_PAGE','STALE_PAGE','RANGE_REQUIRES_TIMEZONE','READ_EXPIRED','REVISION_REQUIRED','EXACT_STORED_IDENTITY_REQUIRED','ARCHIVE_RANGE_REQUIRES_CALCULATED_VIEW','ARCHIVE_CLOSED_STATUS_UNASSESSED')
                code=next((code for code in codes if code in message),None)
                if code is None and 'InputRevisionChanged:' in message:code='INPUTS_CHANGED'
                if code is None and 'Superseded read-only calculation' in message:code='READ_SUPERSEDED'
                if code is None:
                    self._write_log('Analytical read: '+message)
                    code='READ_FAILED'
                future.set_result({'status':'BLOCKED','code':code,'root_cause':'UNCONFIRMED' if code=='READ_FAILED' else code})
            if self.job(work,done,failed,lane='chart',key=('read_analytics',id(future))) is False:
                failed('READ_EXPIRED')
            return future
        if action=='refresh':
            self.refresh_all_async();return {'status':'refresh requested'}
        if action=='select':
            from chart_periods import MONTHS
            isin=args.get('isin',self.instrument_cfg()['isin'])
            item=next((i for i in self.watchlist if i['isin']==isin),None)
            if not item:raise ValueError('Unknown ISIN')
            tf=args.get('timeframe',self.timeframe.get());period=args.get('period',self.period.get())
            from timeframes import LABELS
            if tf not in LABELS or period not in ['Последние',*MONTHS]:raise ValueError('Invalid chart selection')
            self.symbol.set(item['name']);self.period.set(period);self.timeframe.set(tf);self.refresh_chart()
            return {'status':'selected','isin':isin,'timeframe':tf,'period':period}
        if action=='set_levels':
            item=next((i for i in self.watchlist if i['isin']==args.get('isin')),None)
            if not item:raise ValueError('Unknown ISIN')
            levels={k:args[k] for k in ('support','resistance','stop') if k in args}
            if not levels or any(isinstance(v,bool) or not isinstance(v,(int,float)) or not math.isfinite(v) or v<=0 for v in levels.values()):raise ValueError('Invalid levels')
            # Prompt asynchronously so the bridge request cannot expire while waiting for the user.
            def approve():
                current=next((i for i in self.watchlist if i['isin']==item['isin']),None)
                if current and messagebox.askyesno(tr('Предложение Codex'),tr(item['name']+'\n'+str(levels)+'\nСохранить эти уровни?')):
                    current.setdefault('levels',{}).update(levels);self.store.save_instrument(current)
                    self.store.event('bridge_levels',{'isin':item['isin'],'levels':levels});self.refresh_chart()
            self.root.after(0,approve)
            return {'status':'awaiting approval in Stock Monitor; not saved yet'}
        raise ValueError('Unknown action')

    def show_connection(self):
        from paths import APP_DIR
        win=tk.Toplevel(self.root);win.title(tr('Связь с Codex'));win.geometry('780x420')
        box=text_area(win)
        exe=APP_DIR/'StockMonitorBridge.exe'
        if (APP_DIR.parent/'StockMonitorBridge.exe').is_file():exe=APP_DIR.parent/'StockMonitorBridge.exe'
        recent=time.time()-self.bridge.last_client<120
        text=(tr('Локальный мост работает. ')+(tr('Codex обращался к программе недавно.') if recent else tr('Подключение Codex ещё не подтверждено.'))+
              tr('\n\nВ Codex: Настройки → Серверы MCP → Добавить сервер → STDIO.\nИмя: stock-monitor\nКоманда: ')+str(exe)+
              tr('\nАргументы: пусто. Сохраните и перезапустите подключение.\n\nПосле подключения продолжайте общаться в своей задаче Codex. Доступны состояние программы, свечи с источниками, диагностика, обновление данных, выбор графика и предложение уровней. Уровни сохраняются после подтверждения в Stock Monitor.\n\nЭто подключение к работающей программе. Оно не переносит сюда личность или историю ChatGPT, не выполняет сделки и не устанавливает произвольный код. Для ChatGPT на другом устройстве требуется отдельно настроенный защищённый сервер; локальный мост туда автоматически не доступен.'))
        set_text(box,text)
        def copy_command():self.root.clipboard_clear();self.root.clipboard_append(str(exe))
        ttk.Button(win,text=tr('Копировать команду подключения'),command=copy_command).pack(pady=8)

    def check_updates(self,automatic=False):
        from tkinter import simpledialog
        import updater
        from app import VERSION
        repo=self.store.setting('update_repository','swindler2010/StockMonitor')
        if automatic and not repo:return
        if not repo:
            value=simpledialog.askstring(tr('Источник обновлений'),tr('GitHub: владелец/репозиторий с релизами Stock Monitor.\nУкажите только свой доверенный репозиторий:'))
            if not value:return
            try:repo=updater.repository(value)
            except ValueError as exc:messagebox.showerror(tr('Обновления'),rendered_message(str(exc)));return
            self.store.set_setting('update_repository',repo)
        self.status.set(tr('Проверка обновлений программы…'))
        def found(release):
            if not release:self.status.set(tr('Новых стабильных версий нет.'));return
            if not messagebox.askyesno(tr('Обновление'),tr('Доступна {p0} из {p1}.\nСкачать и запустить установку? Данные и настройки сохранятся.',p0=release['version'],p1=repo)):return
            self.status.set(tr('Загрузка и проверка установщика…'))
            def ready(path):
                import subprocess
                self.store.backup(DATA_DIR/'before-update.sqlite3')
                subprocess.Popen([str(path)],close_fds=True)
                self.close()
            self.job(lambda:updater.download(release,DATA_DIR/'updates'),ready)
        self.job(lambda:updater.latest(repo,VERSION),found)

    def pump(self):
        if self.closing:return
        deadline=time.monotonic()+.012
        for _ in range(30):
            try:callback,args=self.events.get_nowait()
            except queue.Empty:break
            started=time.monotonic()
            try:callback(*args)
            except Exception as e:
                self._write_log('UI callback: '+repr(e)); self.status.set(tr('Ошибка обработки: ') + str(e)[:160])
            elapsed=time.monotonic()-started
            if elapsed>=.25 and hasattr(self.root,'_performance_emit'):
                self.root._performance_emit('ui_result',name=getattr(callback,'__qualname__',type(callback).__name__),seconds=round(elapsed,4))
            if time.monotonic()>=deadline:break
        self.root.after(10 if not self.events.empty() else 50,self.pump)

    def job(self,work,done,failed=None,**options):
        return self.dispatcher.submit(work,done,failed or (lambda msg:self.status.set(tr(msg))),**options)

    def snapshot(self):
        stocks=[]; tf=self.timeframe.get()
        for info in self.watchlist:
            stock={'name':info['name'],'isin':info['isin'],'currency':info.get('currency','EUR'),'timeframe':tf,
                   'error':self.feed_errors.get(info['isin']),'candles':[]}
            frame=self.frame_for(info,tf)
            if not frame.empty:
                enriched,result=analyse(frame,info)
                stock.update(result);stock['count']=len(frame)
                stock['age_minutes']=max(0,(pd.Timestamp.now(tz='Europe/Berlin').tz_localize(None)-frame.index[-1]).total_seconds()/60)
                stock['candles']=[{'at':str(ts),**market.clean(r.to_dict())} for ts,r in enriched.tail(30).iterrows()]
                stock['position']=copy.deepcopy(info.get('position',{}))
            stocks.append(stock)
        return {'schema_version':1,'at':datetime.now(timezone.utc).isoformat(),'timezone':'Europe/Berlin',
                'stocks':stocks,'database':'local SQLite','execution':'read-only analysis'}

    def ai_tick(self):
        if self.closing:return
        from snapshot_worker import inputs
        snapshot=getattr(self,'_latest_snapshot',None)
        if snapshot is None or getattr(self,'_latest_snapshot_inputs',None)!=inputs(self):
            self.root.after(60000,self.ai_tick);return
        self.update_ai_view()
        if self.ai_config['automatic'] and self.ai_config['provider']!='local' and not self.ai_busy:
            # Candle timestamps define change; elapsed wall-time alone must not trigger a paid call.
            digest=hashlib.sha256(json.dumps([(s['isin'],s.get('decision'),s['candles'][-1]['at'] if s['candles'] else '') for s in snapshot['stocks']]).encode()).hexdigest()
            interval=self.ai_config['interval_minutes']*60
            if any(s['candles'] for s in snapshot['stocks']) and digest!=self.last_ai_digest and time.monotonic()-self.last_ai_time>=interval:
                self.last_ai_digest=digest;self.run_ai()
        self.root.after(60000,self.ai_tick)

    def show_ai(self):
        if self.ai_window and self.ai_window.winfo_exists():self.ai_window.lift();return
        win=self.ai_window=tk.Toplevel(self.root);win.title(tr('Stock Monitor • ИИ и диагностика'));win.geometry('850x690')
        bar=ttk.Frame(win,padding=10);bar.pack(fill='x')
        ttk.Label(bar,textvariable=self.ai_status).pack(side='left')
        ttk.Button(bar,text=tr('Настройки ИИ'),command=self.ai_settings).pack(side='right')
        ttk.Button(bar,text=tr('Экспорт снимка'),command=self.export_snapshot).pack(side='right',padx=5)
        ttk.Button(bar,text=tr('Запросить анализ'),command=self.run_ai).pack(side='right',padx=8)
        self.ai_text=text_area(win)
        self.update_ai_view()

    def update_ai_view(self):
        if not self.ai_window or not self.ai_window.winfo_exists():return
        records=self.store.events('ai_response',1)
        tail='Модель пока не подключена. Локальные проверки работают без API.'
        if records:
            r=records[0];tail=rendered_message(f"Последний сохранённый ответ • {r['at']} UTC • {r['provider']} / {r['model']}\nСнимок данных: {r['snapshot_at']}\n\n{r['text']}")
        elif self.ai_config['provider']!='local':tail='Сервис выбран. Нажмите «Запросить анализ» или включите автоматический запуск в настройках.'
        set_text(self.ai_text,tr('ЛОКАЛЬНАЯ ДИАГНОСТИКА (проверки по правилам)\n\n') + getattr(self, 'local_report', tr('Ожидание данных')) + '\n\n' + tail)

    def export_snapshot(self):
        path=filedialog.asksaveasfilename(title=tr('Локальный снимок для диагностики и будущего клиента'),defaultextension='.json',initialfile='stock-monitor-snapshot.json')
        if path:Path(path).write_text(json.dumps(self.snapshot(),ensure_ascii=False,indent=2,allow_nan=False),encoding='utf-8')

    def run_ai(self):
        if self.ai_busy:return
        if self.ai_config['provider']=='local':self.ai_settings();return
        self.ai_busy=True;self.last_ai_time=time.monotonic();self.ai_status.set(tr('ИИ: запрос к сервису…'))
        snapshot=self.snapshot();config=copy.deepcopy(self.ai_config)
        def done(result):
            self.ai_busy=False;self.ai_status.set(tr('ИИ: ответ получен • ') + result['model']);self.update_ai_view()
        def failed(msg):
            self.ai_busy=False;self.ai_status.set(tr('ИИ: ') + rendered_message(msg)[:140]);self.store.event('ai_error',{'message':msg[:200]})
        self.job(lambda:self.ai.request(snapshot,config),done,failed)

    def ai_settings(self):
        win=tk.Toplevel(self.root);win.title(tr('Подключение ИИ'));win.geometry('780x590')
        body=ttk.Frame(win,padding=18);body.pack(fill='both',expand=True)
        ttk.Label(body,text=tr('Локальная диагностика запускается вместе с программой.\nOpenAI или совместимый сервис требуют доступной модели. Запросы могут быть платными.'),wraplength=720).pack(anchor='w',pady=8)
        values={}
        for key,label in [('provider','Режим: local / openai / compatible'),('model','ID модели'),('endpoint','Адрес compatible API (полный путь /v1/chat/completions)'),('interval_minutes','Интервал автоанализа, минут (минимум 5)'),('daily_limit','Максимум запросов за сутки UTC')]:
            ttk.Label(body,text=tr(label)).pack(anchor='w'); var=tk.StringVar(value=str(self.ai_config[key]));values[key]=var
            widget=ttk.Combobox(body,textvariable=var,values=['local','openai','compatible'],state='readonly') if key=='provider' else ttk.Entry(body,textvariable=var)
            widget.pack(fill='x',pady=(0,6))
        ttk.Label(body,text=tr('API-ключ (пусто = сохранить существующий; хранится с защитой Windows)')).pack(anchor='w')
        secret=tk.StringVar();ttk.Entry(body,textvariable=secret,show='•').pack(fill='x',pady=5)
        flags={}
        for key,label in [('automatic','Запрашивать ИИ при запуске и изменении свечей (в пределах лимита)'),('share_positions','Включать количество и цену покупки в отправляемые данные'),('open_on_start','Открывать панель ИИ при запуске')]:
            flags[key]=tk.BooleanVar(value=self.ai_config[key]);ttk.Checkbutton(body,text=tr(label),variable=flags[key]).pack(anchor='w',pady=3)
        ttk.Label(body,text=tr('В сервис отправляются выбранные свечи, источники и индикаторы списка акций. Ответ сохраняется локально. ИИ не выполняет сделки и команды.'),wraplength=720).pack(anchor='w',pady=8)
        def save():
            if self.ai_busy:messagebox.showinfo(tr('ИИ'),tr('Дождитесь текущего запроса.'),parent=win);return
            try:
                config=validate_config({**{k:v.get() for k,v in values.items()},**{k:v.get() for k,v in flags.items()}})
                if secret.get().strip():self.ai.vault.save(secret.get().strip(),config['provider'],config['endpoint'])
                self.store.set_setting('ai_config',config);self.ai_config=config;self.last_ai_digest=None;self.last_ai_time=0
                self.ai_status.set(tr('ИИ: локальная диагностика') if config['provider'] == 'local' else tr('ИИ: сервис настроен'))
                secret.set('');win.destroy();self.update_ai_view()
            except Exception as e:messagebox.showerror(tr('ИИ'),rendered_message(str(e)),parent=win)
        row=ttk.Frame(body);row.pack(fill='x',pady=8)
        ttk.Button(row,text=tr('Сохранить'),command=save).pack(side='right')
        def forget():
            self.ai.vault.forget(values['provider'].get(),values['endpoint'].get());secret.set('')
            messagebox.showinfo(tr('Ключ'),tr('Сохранённый ключ удалён. Ключ из переменной OPENAI_API_KEY, если задан, остаётся доступен.'),parent=win)
        ttk.Button(row,text=tr('Удалить сохранённый ключ'),command=forget).pack(side='left')

    def remove_instrument(self):
        if len(self.watchlist)<=1:messagebox.showinfo(tr('Список'),tr('Оставьте хотя бы одну акцию.'));return
        info=self.instrument_cfg()
        if not messagebox.askyesno(tr('Удалить'),tr('Убрать {p0} из списка? История останется в локальной базе.',p0=info['name'])):return
        self.watchlist=[i for i in self.watchlist if i['isin']!=info['isin']];self.drafts.pop(info['isin'],None);self.user_dirty=True;self.symbol_box['values']=[i['name'] for i in self.watchlist]
        self.symbol.set(self.watchlist[0]['name']);self.refresh_chart()

    def add_stock_dialog(self):
        win=tk.Toplevel(self.root);win.title(tr('Поиск и добавление акции'));win.geometry('770x560')
        body=ttk.Frame(win,padding=16);body.pack(fill='both',expand=True)
        ttk.Label(body,text=tr('Поиск по названию, тикеру или ISIN. Для GETTEX нужен ISIN; EUR proxy указывается отдельно.')).pack(anchor='w')
        query=tk.StringVar();ttk.Entry(body,textvariable=query).pack(fill='x',pady=8)
        results=ttk.Treeview(body,height=6,show='tree',selectmode='browse');results.pack(fill='x'); candidates=[]
        fields={};selection_generation=0;confirmed_identity={}
        for key,label in [('name','Название'),('isin','ISIN'),('symbol','Тикер / обозначение'),('venue','Биржа'),('currency','Валюта'),('history_symbol','Тикер истории'),('profile_symbol','Тикер карточки')]:
            row=ttk.Frame(body);row.pack(fill='x',pady=4);ttk.Label(row,text=tr(label),width=47).pack(side='left');fields[key]=tk.StringVar();ttk.Entry(row,textvariable=fields[key]).pack(side='left',fill='x',expand=True)
        state=tk.StringVar();ttk.Label(body,textvariable=state,wraplength=720).pack(anchor='w',pady=8)
        def selected(_):
            nonlocal selection_generation
            if not results.selection():return
            selection_generation+=1;generation=selection_generation
            confirmed_identity.clear()
            for value in fields.values():value.set('')
            add_button.configure(state='disabled')
            item=copy.deepcopy(candidates[int(results.selection()[0])]);symbol=item['symbol']
            fields['name'].set(item.get('longname') or item.get('shortname') or symbol)
            fields['profile_symbol'].set(symbol)
            requested_isin=query.get().strip().upper()
            state.set(tr('Identity LOADING: подтверждаю ISIN, symbol, venue и currency…'))
            def resolved(details):
                if not win.winfo_exists() or selection_generation!=generation:return
                if details.get('status')!='CONFIRMED':
                    state.set(rendered_message(details.get('error') or 'Identity BLOCKED: источник не подтвердил инструмент'));return
                if re.fullmatch(r'[A-Z]{2}[A-Z0-9]{9}\d',requested_isin) and requested_isin!=details['isin']:
                    state.set(tr('Identity BLOCKED: ISIN результата не совпадает с запросом'));return
                confirmed_identity.update(details)
                if details.get('name'):fields['name'].set(details['name'])
                for key in ('isin','symbol','venue','currency'):fields[key].set(details[key])
                fields['history_symbol'].set(details['symbol']);fields['profile_symbol'].set(details['symbol'])
                add_button.configure(state='normal')
                state.set(tr('Identity CONFIRMED: ISIN / symbol / venue / currency. Можно добавить инструмент.'))
            def resolve_failed(msg):
                if not win.winfo_exists() or selection_generation!=generation:return
                state.set('Identity BLOCKED: '+rendered_message(msg)[:160])
            from eur_listings import resolve_for_add
            self.job(lambda:resolve_for_add(symbol,DATA_DIR/'provider_cache',item,requested_isin),resolved,resolve_failed)
        results.bind('<<TreeviewSelect>>',selected)
        def found(items):
            if not win.winfo_exists():return
            candidates[:]=items
            for item in results.get_children():results.delete(item)
            for index,q in enumerate(items):
                item=results.insert('','end',iid=str(index),text=tr('{p0} • {p1} • {p2}',p0=q['symbol'],p1=q.get('longname') or q.get('shortname', ''),p2=q.get('exchDisp', '')))
                self.attach_logo(results,item,q)
            state.set(rendered_message(f'Результатов: {len(items)}. Выберите строку для подтверждения identity.'))
        def find():
            nonlocal selection_generation
            q=query.get().strip()
            if not q:return
            selection_generation+=1;generation=selection_generation
            confirmed_identity.clear();add_button.configure(state='disabled')
            for value in fields.values():value.set('')
            for row in results.get_children():results.delete(row)
            def current_found(items):
                if selection_generation==generation:found(items)
            def current_failed(msg):
                if win.winfo_exists() and selection_generation==generation:state.set(tr('Identity BLOCKED: Search недоступен: ')+rendered_message(msg)[:100])
            state.set(tr('Поиск…'));self.job(lambda:market.search(q),current_found,current_failed)
        def add():
            item={k:v.get().strip() for k,v in fields.items()};item['isin']=item['isin'].upper()
            if not confirmed_identity or any(item[k]!=confirmed_identity[k] for k in ('isin','symbol','venue','currency')) or any(item[k]!=confirmed_identity['symbol'] for k in ('history_symbol','profile_symbol')):
                messagebox.showerror(tr('Identity BLOCKED'),tr('Выберите результат Search и дождитесь подтверждения identity. Изменённые поля нужно подтвердить заново.'),parent=win);return
            if not item['name']:item['name']=item['symbol'] or item['isin']
            if any(i['name']==item['name'] or i['isin']==item['isin'] for i in self.watchlist):messagebox.showerror(tr('Список'),tr('Такая акция или название уже есть.'),parent=win);return
            item.update(history_currency=confirmed_identity['currency'],history_venue=confirmed_identity['venue'],identity_evidence=confirmed_identity['evidence'],eur_listings=confirmed_identity.get('eur_listings',[]),eur_history_auto=True,position={},levels={},drawings=[])
            self.register_confirmed_instrument(item);self.symbol_box['values']=[i['name'] for i in self.watchlist]
            self.symbol.set(item['name']);win.destroy()
            # The same background feed used at startup/Refresh prepares every
            # base interval (and therefore all supported derived TF) for this item.
            self.refresh_chart()
            self.source_status.set(tr('Данные: LOADING • история нового инструмента загружается'))
        ttk.Button(body,text=tr('Найти'),command=find).pack(side='left')
        add_button=ttk.Button(body,text=tr('Добавить'),command=add,state='disabled');add_button.pack(side='right')

    def position_dialog(self):
        item=copy.deepcopy(self.instrument_cfg());win=tk.Toplevel(self.root);win.title(tr('Позиция, уровни и источники'));win.geometry('650x470')
        body=ttk.Frame(win,padding=18);body.pack(fill='both',expand=True);fields={}
        specs=[('position','quantity','Количество акций'),('position','average_price','Средняя цена покупки EUR'),('levels','support','Поддержка EUR (пусто = авто)'),('levels','resistance','Сопротивление EUR (пусто = авто)'),('levels','stop','Стоп EUR (пусто = сохранённый автостоп)')]
        for group,key,label in specs:
            ttk.Label(body,text=tr(label)).pack(anchor='w');v=tk.StringVar(value=str(item.get(group,{}).get(key) or ''));fields[(group,key)]=v;ttk.Entry(body,textvariable=v).pack(fill='x',pady=(0,5))
        extras={}
        for key,label in [('history_symbol','EUR proxy тикер'),('profile_symbol','Тикер карточки Yahoo')]:
            ttk.Label(body,text=tr(label)).pack(anchor='w');extras[key]=tk.StringVar(value=item.get(key) or '');ttk.Entry(body,textvariable=extras[key]).pack(fill='x')
        reset=tk.BooleanVar();ttk.Checkbutton(body,text=tr('Пересчитать и сохранить автоматический стоп'),variable=reset).pack(anchor='w',pady=5)
        def save():
            try:
                for (group,key),v in fields.items():
                    n=float(v.get().replace(',','.')) if v.get().strip() else None
                    if n is not None and (not math.isfinite(n) or n<0 or (key!='quantity' and n==0)):raise ValueError('Цена должна быть положительной, количество — неотрицательным.')
                    item.setdefault(group,{})[key]=n
                item.update({k:v.get().strip() for k,v in extras.items()})
                if reset.get():item.pop('auto_stop',None)
                self.store.save_instrument(item);self.watchlist=self.store.instruments();self.refresh_chart();win.destroy()
            except ValueError as e:messagebox.showerror(tr('Значения'),rendered_message(str(e)),parent=win)
        ttk.Button(body,text=tr('Сохранить'),command=save).pack(anchor='e',pady=10)

    def backup_data(self):
        path=filedialog.asksaveasfilename(title=tr('Резервная копия базы'),defaultextension='.sqlite3',initialfile='StockMonitor-backup.sqlite3')
        if path:
            if Path(path).resolve()==self.store.path.resolve():messagebox.showerror(tr('Копия'),tr('Выберите другой файл.'));return
            self.store.backup(path);self.status.set(tr('Резервная копия базы сохранена.'))

    def import_legacy_data(self):
        path=filedialog.askopenfilename(title=tr('База v0.10: user_data/stock_monitor.sqlite3'),filetypes=[('SQLite','*.sqlite3'),('Все файлы','*.*')])
        if not path:return
        if Path(path).resolve()==self.store.path.resolve():return
        if not messagebox.askyesno(tr('Импорт базы'),tr('Добавить историю из этой базы и перенести из неё позиции и уровни? Текущая база будет сохранена в резервную копию.')):return
        try:
            dest=DATA_DIR/('before_import_'+datetime.now().strftime('%Y%m%d_%H%M%S')+'.sqlite3');self.store.backup(dest)
            count=self.store.import_legacy(path);self.watchlist=self.store.instruments();self._load_cached()
            self.symbol_box['values']=[i['name'] for i in self.watchlist]
            if self.symbol.get() not in self.symbol_box['values']:self.symbol.set(self.watchlist[0]['name'])
            self.refresh_chart();self.status.set(tr('Импортировано инструментов: {p0}. Резервная копия: {p1}',p0=count,p1=dest.name))
        except Exception as e:messagebox.showerror(tr('Импорт базы'),rendered_message(str(e)))

    def notify_signal(self,info,decision,stamp):
        key=(info['isin'],str(stamp),decision)
        if decision!='STOP' or key in self.alerts:return
        self.alerts.add(key)
        if not self.store.setting('alerts_enabled',True):return
        self.root.bell()
        def send():
            from plyer import notification
            notification.notify(title=tr('Stock Monitor • STOP'),message=tr('{p0}: цена достигла сохранённого стопа. Проверьте карточку и свежесть данных.',p0=info['name']),app_name='Stock Monitor',timeout=10)
        self.job(send,lambda _:None,lambda msg:self._write_log('Windows alert: '+msg))

    def frame_for(self,info,tf):
        raw=self.data.get(info['name'])
        base=self.data_meta.get(info['name'],{}).get('base_tf','15m')
        if raw is None:return pd.DataFrame()
        frame=resample(raw,base,tf)
        if tf in ('1D','1W'):
            daily,_=self.store.load_frame(info['isin'],'1D')
            if not daily.empty:
                # Historical daily proxy fills dates before intraday archive coverage.
                intraday=resample(raw,base,'1D') if base not in ('1W',) else pd.DataFrame()
                if not intraday.empty:
                    daily=pd.concat([daily[daily.index<intraday.index.min()],intraday]).sort_index()
                frame=resample(daily,'1D',tf)
        return frame

    def show_card(self):
        info=copy.deepcopy(self.user_state(self.instrument_cfg()));win=tk.Toplevel(self.root);win.title(tr('Карточка • {name}',name=info['name']));win.geometry('990x720')
        top=ttk.Frame(win,padding=10);top.pack(fill='x');status=tk.StringVar(value=tr('Сохранённые данные'))
        ttk.Label(top,textvariable=status).pack(side='left')
        from fundamentals_panel import show as show_statements
        ttk.Button(top,text=tr('Отчётность: KPI и графики'),command=lambda:show_statements(self,info)).pack(side='right')
        book=ttk.Notebook(win);book.pack(fill='both',expand=True,padx=10,pady=10)
        widgets={}
        for name in ('Обзор','Фундаментал','Отчёты','Новости / события','Техника','Позиция','Scalable'):
            page=ttk.Frame(book);book.add(page,text=tr(name));widgets[name]=text_area(page)
        def render(card):
            if not win.winfo_exists():return
            from card_view import fundamentals,reports as report_text,events as event_text
            expected=info.get('profile_symbol') or info.get('history_symbol')
            if expected and card.get('symbol') and card['symbol']!=expected:
                card={'errors':{'identity':'Тикер сохранённой карточки отличается от выбранного. Обновите данные карточки.'}}
            overview=card.get('overview') or {};src=card.get('source',tr('Не загружено'));at=card.get('fetched_at','—')
            suffix=tr('\n\nИсточник: {provider}\nПолучено: {at}\nОшибки источника: {errors}',provider=src,at=at,errors=card.get('errors') or tr('нет'))
            display_symbol=info.get('symbol') or info.get('profile_symbol') or info.get('history_symbol') or '—'
            set_text(widgets['Обзор'],f"{info['name']} • {display_symbol} • {info['isin']}\n{info.get('venue','—')} / {info.get('currency','—')} • {overview.get('country',tr('страна не указана'))}\n\n{overview.get('longBusinessSummary',tr('Описание эмитента пока не получено.'))}"+suffix)
            labels={'marketCap':'Капитализация','totalRevenue':'Выручка','netIncomeToCommon':'Чистая прибыль','trailingEps':'EPS TTM','forwardEps':'Ожидаемый EPS','trailingPE':'P/E TTM','forwardPE':'Forward P/E','priceToSalesTrailing12Months':'P/S','priceToBook':'P/B','ebitda':'EBITDA','ebitdaMargins':'Маржа EBITDA','profitMargins':'Чистая маржа','grossMargins':'Валовая маржа','returnOnEquity':'ROE','freeCashflow':'Свободный денежный поток','totalDebt':'Долг','totalCash':'Денежные средства','revenueGrowth':'Рост выручки','earningsGrowth':'Рост прибыли','dividendYield':'Дивидендная доходность','exDividendDate':'Дата без дивиденда','earningsTimestamp':'Дата отчётности','fiftyTwoWeekLow':'Минимум за 52 недели','fiftyTwoWeekHigh':'Максимум за 52 недели','sector':'Сектор','industry':'Отрасль','country':'Страна','financialCurrency':'Валюта отчётности','currency':'Валюта котировки карточки'}
            set_text(widgets['Фундаментал'],fundamentals(overview,labels)+suffix)
            set_text(widgets['Отчёты'],report_text(card)+suffix)
            news=[]
            from news_view import articles
            for article in articles(card,info.get('profile_symbol') or info.get('history_symbol')):
                news.append(f"{article['published']} • {article['provider']}\n{article['title']}\n{article['relevance']}\n{article['url']}")
            set_text(widgets['Новости / события'],'\n\n'.join(news or [tr('Новости ещё не получены.')]) + tr('\n\nСобытия:\n') + event_text(card) + suffix)
            from trading_view import accepted_chart,format_engine
            prepared=accepted_chart(self,info['isin']) or {}
            frame=prepared.get('frame',pd.DataFrame())
            engine_title,engine_text=format_engine(prepared.get('trading_engine'))
            if not frame.empty and prepared.get('analysis'):
                calc=copy.deepcopy(info)
                currency=frame.attrs.get('currency','?')
                if currency!=info.get('levels_currency',info.get('currency','EUR')) or info.get('levels_source',self.source_mode(info))!=frame.attrs.get('source'):calc['levels']={}
                x,result=prepared['analysis'];r=x.iloc[-1];qty=float(info.get('position',{}).get('quantity') or 0);avg=float(info.get('position',{}).get('average_price') or 0)
                def value(v):
                    try:return f'{float(v):.3f}' if math.isfinite(float(v)) else tr('недостаточно истории')
                    except (TypeError,ValueError):return tr('нет данных')
                technical=f"{self.timeframe.get()} • свечей: {len(frame)}, закрытых: {result['closed_bar_count']}\nПоследняя: {frame.index[-1]} • {frame.attrs.get('timezone') or tr('часовой пояс неизвестен')}\nИсточник: {frame.attrs.get('source','unknown')} / {frame.attrs.get('venue','?')} • {currency}\n\nЧто происходит сейчас\n{engine_title}\n{engine_text}\n\nТехнические уровни\nПоддержка: {value(result['support'])}\nСопротивление: {value(result['resistance'])}\nСтоп сценария: {value(result['stop'])}\n\nИндикаторы последней свечи (она может быть открыта)\nEMA10 {value(r.ema10)} • EMA30 {value(r.ema30)}\nRSI14 {value(r.rsi)} • ATR14 {value(r.atr)}\nMACD {value(r.macd)} / Signal {value(r.macd_signal)}\nОбъём {value(r.volume)} / средний 20 {value(r.volume_mean20)}\nСценарий использует только закрытые свечи."
                set_text(widgets['Техника'],rendered_message(technical))
                risk=max(0,qty*(r.close-result['stop'])) if result['stop'] else None
                position_currency=info.get('position',{}).get('currency',info.get('currency','EUR'))
                pnl=f'{qty*(r.close-avg):+.2f} {currency}' if qty and avg and currency==position_currency else tr('не рассчитан: нет позиции или разные валюты')
                risk_text=f'{risk:.2f} {currency}' if risk is not None and qty and currency==position_currency else tr('не рассчитан')
                set_text(widgets['Позиция'],f"{tr('Количество: ')}{qty:g}{tr('\nСредняя цена: ')}{avg:g} {position_currency}{tr('\nЦена данных: ')}{r.close:.2f} {currency}{tr('\nИсточник: ')}{frame.attrs.get('source', '?')} / {frame.attrs.get('venue', '?')}\nP/L: {pnl}{tr('\nРиск до стопа: ')}{risk_text}{tr('\nКомиссии, налоги и проскальзывание не учтены.')}")
            else:
                position=info.get('position',{})
                set_text(widgets['Техника'],engine_title+'\n'+engine_text);set_text(widgets['Позиция'],f"{tr('Количество: ')}{position.get('quantity') or 0}{tr('\nСредняя цена: ')}{position.get('average_price') or tr('не указана')} {position.get('currency', info.get('currency', ''))}{tr('\nP/L и риск: нет котировок для расчёта.')}")
            set_text(widgets['Scalable'],tr('Официальный Scalable MCP: https://mcp.scalable.capital/mcp\nОфициальный CLI: https://github.com/ScalableCapital/scalable-cli\n\nДля чтения через CLI выберите его исполняемый файл в настройках подключения. Выполните вход самостоятельно в официальном сервисе. Документированные готовые CLI-сборки — macOS/Linux; Windows CLI здесь не прилагается.\n\nStock Monitor разрешает адаптеру только чтение обзора, позиций, возможностей и новостей.\n\n') + json.dumps(self.store.setting('scalable_last', {}), ensure_ascii=False, indent=2))
        def update():
            symbol=info.get('profile_symbol') or info.get('history_symbol')
            if not symbol:messagebox.showinfo(tr('Карточка'),tr('Укажите Yahoo тикер карточки в «Позиция и уровни».'),parent=win);return
            if info['isin'] in self.card_jobs:return
            self.card_jobs.add(info['isin']);status.set(tr('Загрузка карточки…'))
            def done(card):
                self.card_jobs.discard(info['isin']);self.store.set_setting('card:'+info['isin'],card)
                if win.winfo_exists():status.set(tr('Карточка сохранена'));render(card)
            def fail(msg):
                self.card_jobs.discard(info['isin'])
                if win.winfo_exists():status.set(tr('Ошибка карточки: ') + msg[:130])
            if not self.job(lambda:market.company_card(symbol,DATA_DIR/'provider_cache'),done,fail):
                fail(tr('Не удалось запустить обновление. Повторите позже.'))
        ttk.Button(top,text=tr('Получить данные карточки'),command=update).pack(side='right')
        ttk.Button(top,text=tr('Scalable: чтение'),command=self.scalable_read).pack(side='right',padx=8)
        render(self.store.setting('card:'+info['isin'],{}))
        saved=self.store.setting('card:'+info['isin'],{})
        try:stale=(datetime.now(timezone.utc)-datetime.fromisoformat(saved['fetched_at'])).total_seconds()>3600
        except (KeyError,TypeError,ValueError):stale=True
        if stale:win.after(100,update)

    def scalable_read(self):
        from broker import ScalableReadOnly
        executable=self.store.setting('scalable_executable','')
        if not executable:
            executable=filedialog.askopenfilename(title=tr('Официальный Scalable CLI (не системный Windows sc.exe)'))
            if not executable:return
        try:adapter=ScalableReadOnly(executable)
        except Exception as e:messagebox.showerror(tr('Scalable'),rendered_message(str(e)));return
        self.store.set_setting('scalable_executable',executable);self.status.set(tr('Scalable: чтение позиций…'))
        def done(result):
            self.store.set_setting('scalable_last',{'at':datetime.now(timezone.utc).isoformat(),'holdings':result})
            self.status.set(tr('Scalable: позиции получены. Откройте карточку / Scalable.'))
        self.job(lambda:adapter.read('holdings'),done)

