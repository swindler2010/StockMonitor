from localized_widgets import CanonicalCombobox
from i18n import tr
from chart_locale import draw_localized
"""0.14 UI and data orchestration, with explicit source identity and draft state."""
import copy
import math
import os
import time
import threading
import tkinter as tk
from tkinter import ttk, messagebox, simpledialog
import numpy as np
import pandas as pd
from matplotlib.figure import Figure
from matplotlib.backends.backend_tkagg import FigureCanvasTkAgg
from matplotlib.collections import LineCollection, PolyCollection
from matplotlib.ticker import FuncFormatter
from matplotlib.patches import Rectangle
from analytics import analyse, resample
from chart_indicators import macd_series, volume_profile
from features import text_area,set_text
from paths import DATA_DIR,DISTRIBUTION
import history_service
import market
from themes import THEMES

BG='#0b1220';TEXT='#e5e7eb';MUTED='#94a3b8';BLUE='#60a5fa'
PERIODS={'день':None,'неделя':None,'месяц':1,'6 месяцев':6,'год':12,'2 года':24,'5 лет':60,'Max':None}
ALIASES={'Последние':'неделя','1 месяц':'месяц','1 год':'год'}
from timeframes import LABELS as TF, base_for
DEFAULT_IND={'EMA':True,'SMA':False,'Bollinger':False,'VWAP':False,'RSI':False,'MACD':False,'ATR':False,'Объём':True,'Средний объём':False,'Относительный объём':False,'ema_fast':10,'ema_slow':30,'sma':20,'rsi':14,'atr':14,'bb':20,'bb_dev':2,'volume':20}
DEFAULT_IND.update(macd_fast=12,macd_slow=26,macd_signal=9)
DEFAULT_IND['Volume Profile']=False
DEFAULT_IND['Уровни анализа']=True

def number(value):
    try:return f'{float(value):.2f}' if math.isfinite(float(value)) else 'недостаточно истории'
    except (TypeError,ValueError):return 'недостаточно истории'

class Desktop014:
    def _styles(self):
        global BG,TEXT,MUTED,BLUE
        palette=THEMES.get(self.store.setting('theme','Тёмная'),THEMES['Тёмная'])
        BG,TEXT,MUTED,BLUE=(palette[k] for k in ('bg','text','muted','accent'))
        style=ttk.Style(self.root);style.theme_use('clam')
        style.configure('.',background=BG,foreground=TEXT,font=('Segoe UI',10))
        for name in ('TFrame','TLabel','TCheckbutton','TRadiobutton','TNotebook'):
            style.configure(name,background=BG,foreground=TEXT)
        for name in ('Panel.TFrame','Panel.TLabel','TButton','TNotebook.Tab'):
            style.configure(name,background=palette['panel'],foreground=TEXT)
        for name in ('TButton','TCheckbutton','TRadiobutton','TNotebook.Tab'):
            style.map(name,background=[('active',palette['panel']),('selected',palette['panel'])],foreground=[('disabled',MUTED),('active',TEXT)])
        for name in ('TEntry','TCombobox','Treeview'):
            style.configure(name,fieldbackground=palette['panel'],background=palette['panel'],foreground=TEXT,insertcolor=TEXT)
            style.map(name,fieldbackground=[('readonly',palette['panel'])],foreground=[('readonly',TEXT)],background=[('selected',BLUE)])
        for name in ('Muted.TLabel','PanelMuted.TLabel'):style.configure(name,foreground=MUTED)
        for name in ('Title.TLabel','Price.TLabel','Decision.TLabel'):style.configure(name,foreground=TEXT,font=('Segoe UI',16,'bold'))
        self.root.option_add('*TCombobox*Listbox.background',palette['panel']);self.root.option_add('*TCombobox*Listbox.foreground',TEXT)
        self.root.option_add('*Text.background',BG);self.root.option_add('*Text.foreground',TEXT)
        def recolor(widget):
            if isinstance(widget,(tk.Tk,tk.Toplevel,tk.Canvas,tk.Text,tk.Listbox,tk.Menu)):
                try:
                    widget.configure(background=BG)
                    if isinstance(widget,(tk.Text,tk.Listbox,tk.Menu)):widget.configure(foreground=TEXT)
                    if isinstance(widget,tk.Text):widget.configure(insertbackground=TEXT)
                except tk.TclError:pass
            for child in widget.winfo_children():recolor(child)
        recolor(self.root)
        if hasattr(self,'fig'):self.fig.set_facecolor(BG)

    def set_theme(self,name):
        if name not in THEMES:raise ValueError('Неизвестная тема')
        self.store.set_setting('theme',name);self._styles();self.refresh_chart()

    def show_theme(self):
        win=tk.Toplevel(self.root);win.title(tr('Оформление'));body=ttk.Frame(win,padding=16);body.pack(fill='both',expand=True)
        choice=tk.StringVar(value=self.store.setting('theme','Тёмная'))
        for name in THEMES:ttk.Radiobutton(body,text=tr(name),value=name,variable=choice,command=lambda:self.set_theme(choice.get())).pack(anchor='w',pady=6)

    def init_features(self):
        self.period.set(ALIASES.get(self.period.get(),self.period.get()))
        self.persisted=copy.deepcopy(self.watchlist);self.drafts={};self.render_key=None;self.drag=None;self.cursor=[];self.panels=[];self.drawing_start=None;self.drawing_mode=None
        self.history_meta={};self.session_source={};self.session_ind={}
        for item in self.watchlist:
            if 'indicators' not in item:
                old=self.indicators
                item['indicators']={**DEFAULT_IND,'EMA':old.get('ema10',True) or old.get('ema30',True),'SMA':old.get('sma20',False),'Bollinger':old.get('bollinger',False),'VWAP':old.get('vwap',False),'Объём':old.get('volume',True)}
        super().init_features()
        from tunnel_runtime import TunnelRuntime, discover_existing
        tunnel_config=self.store.setting('chatgpt_tunnel',{})  # Explicit TEST profile only; never discover the Store route.
        if DISTRIBUTION=='msix-store':
            from store_release import prepare_runtime
            from paths import APP_DIR
            tunnel_config=prepare_runtime(self.store,APP_DIR,allow_discovery=not bool(os.environ.get('STOCK_MONITOR_DATA_DIR')))
        if tunnel_config is None and not os.environ.get('STOCK_MONITOR_DATA_DIR') and DISTRIBUTION=='exe':
            tunnel_config=discover_existing()
            if tunnel_config:self.store.set_setting('chatgpt_tunnel',tunnel_config)
        self.tunnel_runtime=TunnelRuntime(tunnel_config or {},submit=lambda work:self.job(work,lambda _:None,key='connection-poll',lane='misc'))
        self.tunnel_runtime.poll()
        self.root.after(2000,self.connection_tick)
        self.quality_signature=None
        self.root.after(1000,self.quality_tick)

    def current_quality_signature(self):
        from data_quality import key as quality_key
        info=self.instrument_cfg();identity=self.store.setting('history_identity:'+info['isin'],{})
        base=base_for(self.timeframe.get(),self.source_mode(info))
        report=self.store.setting(quality_key(info,identity.get('symbol','') if self.source_mode(info)=='proxy' else info['isin'],base),{})
        # Checking the same report again must not restart chart/engine work.
        # These two timestamps are transport telemetry, already excluded by
        # input_revisions; all quality states, issues and ranges stay significant.
        report={k:v for k,v in report.items() if k not in ('checked_at','raw_retry_after')}
        accepted=getattr(self,'_cp_chart_inputs',{})
        dependencies={}
        if accepted.get('isin')==info['isin'] and accepted.get('timeframe')==self.timeframe.get():
            dependencies=self.store.read_cache.token_for(accepted['scopes'])
        return (info['isin'],base,str(report),int(time.time()//60),tuple(sorted(dependencies.items())))

    def quality_tick(self):
        if self.closing:return
        signature=self.current_quality_signature()
        if signature!=self.quality_signature:
            self.quality_signature=signature
            self.render_key=None;self.refresh_chart()
        self.root.after(1000,self.quality_tick)

    def _load_cached(self):
        # Legacy mixed archive is retained on disk, but never used implicitly.
        for info in self.watchlist:
            f=self.store.load_official(info['isin'])
            self.data[info['name']]=f;self.data_meta[info['name']]={'base_tf':'15m','source':'GETTEX','gettex':True}

    def build_ui(self):
        from text_menu import install_text_menus
        install_text_menus(self.root)
        self._styles();style=ttk.Style();style.configure('TButton',padding=(5,3));style.configure('Title.TLabel',font=('Segoe UI',16,'bold'))
        from freshness_panel import show as show_freshness, label as freshness_label
        menu=tk.Menu(self.root)
        for title,entries in [
            ('Файл',[('Добавить инструмент',self.add_stock_dialog),('Копия базы',self.backup_data),('Импорт базы',self.import_legacy_data),('Выход',self.close)]),
            ('Правка',[('Позиция и уровни',self.position_dialog),('Удалить инструмент',self.remove_instrument),('Сохранить',self.save_user_changes)]),
            ('Вид',[('Портфель',self.show_portfolio),('Журнал решений',self.show_decision_journal),('Карточка',self.show_card),('Новости',self.show_news),('Индикаторы',self.show_indicators),('Рисование',self.show_drawing)]),
            ('Настройки',[('Параметры',self.show_settings),('Оформление',self.show_theme)]),
            ('Справка',[(freshness_label('title'),lambda:show_freshness(self)),('Связь с ChatGPT / MCP',self.show_connection),('Диагностика',self.show_ai),('Обновить программу',lambda:self.check_updates(False))])]:
            sub=tk.Menu(menu,tearoff=False)
            for label,command in entries:sub.add_command(label=tr(label),command=command)
            menu.add_cascade(label=tr(title),menu=sub)
        self.root.configure(menu=menu)
        self.root.minsize(1000,680)
        outer=ttk.Frame(self.root,padding=10);outer.pack(fill='both',expand=True)
        top=ttk.Frame(outer);top.pack(fill='x')
        ttk.Label(top,text=tr('Stock Monitor 0.16 DEV'),style='Title.TLabel').pack(side='left')
        self.symbol_box=ttk.Combobox(top,textvariable=self.symbol,values=[i['name'] for i in self.watchlist],state='readonly',width=23);self.symbol_box.pack(side='left',padx=10);self.symbol_box.bind('<<ComboboxSelected>>',lambda _:self.refresh_chart())
        ttk.Button(top,text=tr('Акции ▾'),command=self.show_watchlist).pack(side='left')
        for label,command in [('＋',self.add_stock_dialog),('Индикаторы',self.show_indicators),('Рисование',self.show_drawing),('Новости',self.show_news),('ⓘ',self.show_card),('Портфель',self.show_portfolio),('Настройки',self.show_settings),('Сохранить',self.save_user_changes)]:
            b=ttk.Button(top,text=tr(label),command=command);b.pack(side='left',padx=2)
            if label=='ⓘ':self.tooltip(b,'Информация о компании')
        bar=ttk.Frame(outer);bar.pack(fill='x',pady=6)
        self.cp_label=ttk.Label(bar,text=tr('● CandlePilot (CP) — UNKNOWN'),foreground='#ef4444')
        self.cp_label.pack(side='right',padx=6)
        self.cp_label.bind('<Button-1>',lambda _:__import__('candlepilot_health').show(self))
        self.tooltip(self.cp_label,'CandlePilot Health — нажмите для диагностики')
        self.root.after(500,self._update_candlepilot)
        self.timeframe_box=ttk.Combobox(bar,textvariable=self.timeframe,values=list(TF),state='readonly',width=5)
        self.timeframe_box.pack(side='left',padx=3)
        self.timeframe_box.bind('<<ComboboxSelected>>',lambda _:self.refresh_chart())
        self.period_box=CanonicalCombobox(bar,textvariable=self.period,values=list(PERIODS),state='readonly',width=12)
        self.period_box.pack(side='left',padx=3)
        self.period_box.bind('<<ComboboxSelected>>',lambda _:self.change_period())
        ttk.Button(bar,text=tr('Линейка'),command=self.start_measurement).pack(side='left',padx=3)
        from chart_modes import MODES
        self.chart_mode=tk.StringVar(value=self.store.setting('chart_mode','Candles'))
        if self.chart_mode.get() not in MODES:self.chart_mode.set('Candles')
        mode_box=CanonicalCombobox(bar,textvariable=self.chart_mode,values=MODES,state='readonly',width=21)
        mode_box.pack(side='left',padx=3);mode_box.bind('<<ComboboxSelected>>',self.change_chart_mode)
        ttk.Button(bar,text=tr('⛶'),width=3,command=self.toggle_chart_fullscreen).pack(side='left',padx=3)
        self.root.bind('<Escape>',self.escape_chart,add='+')
        row=ttk.Frame(outer);row.pack(fill='x',pady=(0,5))
        self.source_choice=tk.StringVar(value=tr('Proxy — автоматически · EUR'));self.source_box=ttk.Label(row,textvariable=self.source_choice,width=38);self.source_box.pack(side='left')
        ttk.Button(row,text=tr('Обновить данные'),command=self.refresh_all_async).pack(side='left',padx=5)
        ttk.Button(row,text=tr('Сбросить масштаб'),command=self.reset_view).pack(side='left')
        ttk.Label(row,textvariable=self.source_status,style='Muted.TLabel').pack(side='left',padx=8)
        body=ttk.Panedwindow(outer,orient='horizontal');body.pack(fill='both',expand=True)
        chart=ttk.Frame(body);side=ttk.Frame(body,padding=8);body.add(chart,weight=5);body.add(side,weight=1)
        self.chart_outer=outer;self.chart_body=body;self.chart_side=side
        self.fig=Figure(figsize=(10,7),dpi=100,facecolor=BG);self.ax_price=self.fig.add_axes([.09,.3,.88,.65]);self.ax_vol=self.fig.add_axes([.09,.1,.88,.15],sharex=self.ax_price)
        self.canvas=FigureCanvasTkAgg(self.fig,master=chart);self.canvas.get_tk_widget().pack(fill='both',expand=True)
        for event,callback in [('motion_notify_event',self.on_motion),('figure_leave_event',self.on_leave),('button_press_event',self.on_press),('button_release_event',self.on_release),('scroll_event',self.on_scroll)]:self.canvas.mpl_connect(event,callback)
        self.instrument=tk.StringVar();ttk.Label(side,textvariable=self.instrument,wraplength=215).pack(anchor='w')
        self.decision=tk.StringVar();ttk.Label(side,textvariable=self.decision,font=('Segoe UI',17,'bold'),wraplength=215).pack(anchor='w',pady=10)
        self.metrics={k:tk.StringVar(value='—') for k in ['price','ema10','ema30','volume','support','resistance','stop','add','updated','rsi','atr','position']}
        for key,label in [('price','Цена'),('ema10','EMA10'),('ema30','EMA30'),('support','Поддержка'),('resistance','Сопротивление'),('stop','Стоп'),('add','Добавление'),('position','Позиция / результат')]:
            ttk.Label(side,text=tr(label),style='Muted.TLabel').pack(anchor='w');ttk.Label(side,textvariable=self.metrics[key],wraplength=215).pack(anchor='w',pady=(0,5))
        from tkinter.scrolledtext import ScrolledText
        self.explain=tk.StringVar()
        explanation=ScrolledText(side,height=9,width=27,wrap='word',font=('Segoe UI',9),background=BG,foreground=TEXT)
        explanation.pack(fill='x',pady=8);explanation.configure(state='disabled')
        self.explain.trace_add('write',lambda *_:set_text(explanation,self.explain.get()))
        ttk.Label(side,textvariable=self.metrics['updated'],wraplength=215,style='Muted.TLabel').pack(anchor='w')
        self.status_label=ttk.Label(outer,textvariable=self.status,wraplength=950);self.status_label.pack(fill='x',pady=4)
        ttk.Button(outer,textvariable=self.ai_status,command=self.show_ai).pack(anchor='e')

    def tooltip(self,widget,text):
        def enter(_):
            win=tk.Toplevel(self.root);win.overrideredirect(True);win.geometry(f'+{widget.winfo_rootx()}+{widget.winfo_rooty()+28}');ttk.Label(win,text=tr(text),padding=5).pack();widget.tip=win
        def leave(_):
            if hasattr(widget,'tip'):widget.tip.destroy()
        widget.bind('<Enter>',enter);widget.bind('<Leave>',leave)

    def user_state(self,info):
        from eur_listings import OWNER_HISTORY_POLICY,owner_history_info
        return owner_history_info(self.drafts.get(info['isin'],info),self.store.setting(OWNER_HISTORY_POLICY,{}))

    def source_mode(self,info):
        # Saved legacy provenance is still needed for untagged levels/drawings.
        # It no longer controls automatic chart source selection.
        return self.user_state(info).get('source_mode','gettex')

    def change_source(self,_=None):
        self.source_choice.set(tr('Proxy — автоматически · EUR'));self.user_view=None;self.refresh_chart()

    def frame_for(self,info,tf):
        import json
        state=json.dumps(self.user_state(info),sort_keys=True,default=str)
        scope=str(getattr(self,'analysis_at','')) if getattr(self,'five_year_scope',False) else None
        return self.store.read_cache.get(('resampled',info['isin'],tf,state,int(time.time()//60),scope),lambda:self._frame_uncached(info,tf),scoped=True,inputs=self.store.read_cache.token_for(['instrument:'+info['isin']]))

    def _frame_uncached(self,info,tf,required_identity=None):
        clock=getattr(self,'analysis_at',None)
        scoped=getattr(self,'five_year_scope',False)
        from calculation_window import scope_raw,scope_report
        from eur_listings import venue_allowed
        venue_lock=self.user_state(info).get('history_venue_lock')
        if venue_lock and venue_lock!='GETTEX':raise ValueError('Unsupported history venue lock')
        if required_identity is not None and not venue_allowed({'history_venue_lock':venue_lock},required_identity):
            raise ValueError('VENUE_EXCLUDED: выбрана только GETTEX/EUR')
        # Confirmed legacy daily caches do not enable network auto-discovery.
        # Keep saved preferences; Proxy intraday can select a separate EUR
        # GETTEX series under the owner's extended-session policy below.
        eur_cache = base_for(tf)=='1D' and bool(info.get('eur_listings')) and not venue_lock
        # One owner-authorized automatic policy, without rewriting preferences.
        mode='gettex' if venue_lock else 'proxy'
        pinned_official=required_identity==dict(source='gettex',symbol=info['isin'],venue='GETTEX',currency='EUR',timezone='Europe/Berlin')
        pinned_chart=False
        pinned_archive=False
        if required_identity is not None and required_identity.get('source')=='onvista':
            from onvista_archive import available_daily
            matches=[f for f in available_daily(self.store,info,clock)
                     if all(f.attrs.get(k)==v for k,v in required_identity.items())]
            if len(matches)==1:
                pinned_archive=True;imported_daily=matches[0]
        if required_identity is not None and required_identity.get('source')=='gettex_chart':
            import gettex_chart
            chart_base='1D' if base_for(tf)=='1D' else '5m' if tf in ('5m','10m') else '15m'
            chart_daily=gettex_chart.load(self.store,info,chart_base,clock)
            if chart_daily.empty and chart_base=='15m':
                chart_base='5m';chart_daily=gettex_chart.load(self.store,info,chart_base,clock)
            # Pin only a confirmed saved series of this exact provider.
            pinned_chart=all(chart_daily.attrs.get(k)==v for k,v in required_identity.items())
            # A confirmed requested series proves its own identity. Read
            # fallback witnesses only when that evidence is unavailable.
            if not pinned_chart:
                for witness_tf in ('15m','5m'):
                    witness=gettex_chart.load(self.store,info,witness_tf)
                    if all(witness.attrs.get(k)==v for k,v in required_identity.items()):
                        pinned_chart=True;break
            if pinned_chart and chart_daily.empty:
                chart_daily.attrs.update(required_identity,data_quality={'state':'SOURCE_UNAVAILABLE',
                    'issues':['Нет истории этого TF у того же GETTEX chart источника; другой ряд не подставляется']})
        if required_identity is not None:
            from data_quality import IDENTITY
            if tf not in TF:raise ValueError('Unsupported calculation timeframe')
            if not pinned_official and not pinned_chart and not pinned_archive and (required_identity.get('source')!='proxy' or
                required_identity.get('currency')!='EUR' or not any(
                    x.get('status')=='CONFIRMED' and x.get('isin')==info['isin'] and
                    all(x.get(k)==required_identity.get(k) and x.get(k) for k in IDENTITY)
                    for x in info.get('eur_listings',[]))):
                raise ValueError('Unconfirmed calculation listing')
        official_base=base_for(tf,'gettex')
        official=pd.DataFrame() if pinned_chart or pinned_archive else self.store.load_official(info['isin'],official_base)
        if not (pinned_chart or pinned_archive) and official.empty and official_base=='15m':
            official_base='5m';official=self.store.load_official(info['isin'],official_base)
        official.attrs['timeframe']=official_base
        if scoped:official=scope_raw(official,official_base,clock)
        official_resampled=official
        fallback=False
        if eur_cache and not pinned_official:
            from eur_listings import depth
            checked=resample(official,official_base,tf,now=clock) if not official.empty else official
            fallback=depth(checked,tf)['status']!='COMPLETE' or checked.attrs.get('data_quality',{}).get('state')!='READY'
        if pinned_official or (mode=='gettex' and required_identity is None):
            raw=official;base=official_base
        else:
            raw=pd.DataFrame();base='15m'
        if not venue_lock and not (pinned_official or pinned_chart or pinned_archive) and (mode!='gettex' or fallback or required_identity is not None):
            identity=required_identity or self.store.setting('history_identity:'+info['isin'],{})
            if eur_cache and identity.get('currency')!='EUR':identity={}
            # New cached archives must not wait for a network refresh to become
            # visible. Keep GETTEX precedence above and the existing DQ rank.
            cached_proxy=None
            if base_for(tf)=='1D' and required_identity is None:
                from eur_listings import cached_daily_history
                cached_proxy=cached_daily_history(self.store,info,tf,identity,analysis_at=clock if scoped else None)
                if cached_proxy is not None:identity=cached_proxy.attrs.copy()
            if identity:
                proxy_base=base_for(tf)
                proxy=cached_proxy if cached_proxy is not None else self.store.load_series(info['isin'],proxy_base,'proxy',identity['symbol'],identity.get('venue'),identity.get('currency'))
                if not proxy.empty:
                    proxy.attrs.update(identity)
                    if scoped:proxy=scope_raw(proxy,proxy_base,clock)
                    candidate=proxy
                    keep_official=(mode=='gettex' and prefer_recent_official(info,tf,official,candidate,clock))
                    if not keep_official and (not fallback or eur_cache or len(candidate)>len(official_resampled)):
                        raw=proxy;base=proxy_base;raw.attrs.update(identity)
                        if fallback:raw.attrs['fallback_reason']='Недостаточно истории GETTEX: '+str(len(official_resampled))+' свечей'
        from timeframes import MINUTES
        if required_identity is None and tf in MINUTES:
            previous_history,previous_base=raw,base
            # Whole-series selection, never append another venue's candles.
            # CP already refreshes/persists GETTEX on the existing feed lane.
            if prefer_recent_official(info,tf,official,raw,clock,extended_session=True):
                raw=official.copy();base=official_base
                raw.attrs['source_selection']={'policy':'proxy_extended_eur_v1','requested_mode':'proxy',
                    'selected':'GETTEX_EUR','session':'07:30–23:00 Europe/Berlin',
                    'reason':'Отдельный EUR-ряд GETTEX для расширенной сессии; без смешивания площадок'}
            else:
                selection={'policy':'proxy_extended_eur_v1','requested_mode':'proxy',
                    'selected':'PROXY_EUR_LIMITED','requested_session':'07:30–23:00 Europe/Berlin',
                    'reason':'GETTEX/EUR пока недоступен или старее текущего Proxy; показана фактическая сессия источника'}
                if not raw.empty and raw.attrs.get('currency')=='EUR':
                    raw=raw.copy();raw.attrs['source_selection']=selection
                else:
                    raw=pd.DataFrame();raw.attrs={'source_selection':dict(selection,selected='EUR_UNAVAILABLE'),
                        'data_quality':{'state':'SOURCE_UNAVAILABLE','issues':['Нет сохранённого подтверждённого EUR-ряда; USD не подставляется']}}
            import gettex_chart
            raw,base=gettex_chart.candidate(self.store,info,tf,raw,base,clock,analysis_at=clock if scoped else None)
            raw,base=gettex_chart.retain_deeper(info,tf,previous_history,previous_base,raw,base,clock)
            if scoped and not raw.empty:raw=scope_raw(raw,base,clock)
        elif required_identity is None:
            # Deep EUR daily/weekly/monthly archives remain separate from the
            # intraday GETTEX row. Never extend them with another venue's tail.
            if raw.empty or raw.attrs.get('currency')!='EUR':
                raw=official.copy();base=official_base
            if raw.empty:
                import gettex_chart
                raw=gettex_chart.load(self.store,info,'1D',clock);base='1D'
            import gettex_chart
            raw,base=gettex_chart.daily_candidate(self.store,info,tf,raw,base,official,official_base,clock,analysis_at=clock if scoped else None)
            if scoped and not raw.empty:raw=scope_raw(raw,base,clock)
            if not raw.empty and raw.attrs.get('currency')=='EUR':
                raw=raw.copy();raw.attrs['source_selection']={
                    'policy':'proxy_extended_eur_v1','requested_mode':'proxy',
                    'selected':'GETTEX_EUR' if raw.attrs.get('venue')=='GETTEX' else 'PROXY_EUR_ARCHIVE',
                    'history_policy':'gettex_primary_v1',
                    'reason':'GETTEX — основной EUR-ряд; Yahoo/Frankfurt сохранён отдельным резервом' if raw.attrs.get('venue')=='GETTEX' else 'GETTEX недоступен для этого TF; отдельный резервный EUR-архив'}
            else:
                raw=pd.DataFrame();raw.attrs={'data_quality':{'state':'SOURCE_UNAVAILABLE',
                    'issues':['Нет сохранённого EUR-архива; USD не подставляется']}}
        if pinned_chart:
            raw=chart_daily;base=chart_base
            if scoped:raw=scope_raw(raw,base,clock)
        if pinned_archive:
            raw=imported_daily;base='1D'
            if scoped:raw=scope_raw(raw,base,clock)
        if venue_lock:
            if not raw.empty and not venue_allowed({'history_venue_lock':venue_lock},raw.attrs):
                raise ValueError('VENUE_EXCLUDED: расчёт другого рынка запрещён')
            raw.attrs['source_selection']={**raw.attrs.get('source_selection',{}),
                'policy':'proxy_extended_eur_v1','requested_mode':'proxy','venue_lock':venue_lock,
                'selected':'GETTEX_EUR' if not raw.empty else 'GETTEX_UNAVAILABLE',
                'reason':'Только GETTEX/EUR по выбору владельца; резервные биржи не используются'}
            if raw.empty:
                raw.attrs['data_quality']={'state':'SOURCE_UNAVAILABLE','issues':['Нет данных GETTEX/EUR для этого TF; другая биржа не подставляется']}
        if raw.empty:
            if required_identity is not None:
                from data_quality import key as quality_key
                saved=self.store.setting(quality_key(info,required_identity['symbol'],base_for(tf)),{})
                report=saved if saved.get('state') in ('INVALID','GAP','SOURCE_GAP','STALE','RECOVERING','SOURCE_UNAVAILABLE') else {'state':'SOURCE_UNAVAILABLE','issues':['No saved candles for the required calculation listing']}
                raw.attrs.update(required_identity,timeframe=tf,data_quality=report)
            return raw
        from data_quality import audit as audit_quality
        input_clock=clock
        if scoped and getattr(self,'closed_parent_inputs',False):
            from trading_context import closed_parent_input
            raw,input_clock=closed_parent_input(raw,base,tf,clock)
        derived_quality=raw.attrs.get('data_quality') if raw.attrs.get('daily_origin') or raw.attrs.get('archive_invalid') else None
        raw.attrs['data_quality']=audit_quality(raw,base,now=input_clock,
            check_stale=not bool(raw.attrs.get('closed_parent_input') and base in MINUTES))
        if derived_quality and derived_quality.get('state') in ('INVALID','SOURCE_GAP','UNCONFIRMED','GAP','RECOVERING','SOURCE_UNAVAILABLE'):
            # A valid-looking daily OHLC must not erase a blocked child archive.
            raw.attrs['data_quality']=derived_quality
        from data_quality import key as quality_key
        persisted_quality=self.store.setting(quality_key(info,raw.attrs.get('symbol',''),base),{})
        if scoped:
            raw.attrs.setdefault('archive_quality_before_scope',persisted_quality or {'state':'UNKNOWN','issues':['No saved complete archive report']})
            persisted_quality=scope_report(persisted_quality,raw.attrs['analysis_scope']['start'])
        from data_quality import absence_report_resolved
        if absence_report_resolved(persisted_quality,raw,base):
            raw.attrs['archive_reclassification']={'previous_state':persisted_quality['state'],'reason':'All diagnosed missing intervals have NO_TRADES evidence','reference':raw.attrs.get('absence_evidence',{}).get('reference')}
            persisted_quality={}
        if persisted_quality.get('state') in ('INVALID','RECOVERING','SOURCE_UNAVAILABLE','SOURCE_GAP') and (persisted_quality.get('state')!='SOURCE_GAP' or raw.attrs['data_quality']['state'] in ('READY','STALE','GAP')):
            raw.attrs['data_quality'].update(state=persisted_quality['state'],issues=persisted_quality.get('issues',[]),ranges=persisted_quality.get('ranges',[]))
        markers=raw.attrs['data_quality'].get('non_candle_markers',[])
        if markers:raw=raw.loc[~raw.index.isin(pd.to_datetime(markers))]
        if scoped:
            from cp_controls import accepted_gap_input
            raw=accepted_gap_input(self.store,info,raw,base,input_clock)
        meta=raw.attrs.copy();out=resample(raw,base,tf,now=clock)
        if scoped:
            from cp_controls import accepted_gap_output
            out=accepted_gap_output(out,tf,clock)
        out.attrs['history_source']=meta.get('source','unknown')
        out.attrs['timeframe']=tf
        if eur_cache:
            from eur_listings import depth
            out.attrs['eur_history_depth']=depth(out,tf)
        out.attrs.setdefault('timezone','Europe/Berlin')
        if not official.empty:
            out.attrs['latest_gettex']={'price':float(official.iloc[-1].close),'at':str(official.index[-1]),'currency':'EUR','source':'gettex','note':'Последняя доступная архивная свеча, не гарантия текущей цены'}
        return out

    def _update_candlepilot(self):
        from candlepilot import activity,presentation
        state=activity(getattr(self,'compute_feed_update',None),self.closing,getattr(self,'cp_error',None))
        color,text=presentation(state)
        if getattr(self,'bridge',None):
            client=getattr(self,'compute_feed_update',None)
            self.bridge.cp_activity={'state':state,'current_kind':getattr(client,'active_kind',None) if state=='ACTIVE' else None,'observed_at':time.time()}
            if state=='ACTIVE':self.bridge.cp_activity.update(getattr(self,'_cp_active_feed',{}))
            if state=='ERROR':self.bridge.cp_activity['reason']=getattr(self,'cp_error',None)
        self.cp_label.configure(text=tr(text),foreground=color)
        if not self.closing:self.root.after(500,self._update_candlepilot)

    def _scheduled_refresh(self):
        if self.closing:return
        from candle_schedule import planned_feeds
        infos=copy.deepcopy(self.watchlist)
        focus={'isin':self.instrument_cfg()['isin'],'timeframe':self.timeframe.get()}
        def apply(plan):
            if self.closing:return
            self.cp_schedule_last=plan
            self._cp_fresh_request=dict(plan.get('fresh') or {})
            self._cp_fresh_requested_at=time.time()
            if plan.get('fresh'):
                self.start_feed(plan['fresh'])
                self.dispatcher.prioritize_feed('feed:'+plan['fresh']['isin'],1.1 if plan['fresh'].get('_cp_position') else
                    1.2 if plan['fresh'].get('_cp_focus',{}).get('priority_reason')=='TRADE_CONTEXT_MAINTENANCE' else 1.3)
            if plan.get('priority'):
                self.start_feed(plan['priority'])
                self.dispatcher.prioritize_feed('feed:'+plan['priority']['isin'])
            for info in plan.get('review',[]):self.start_feed(info)
            for info in plan.get('eligibility',[]):self.start_feed({**info,'_eligibility_only':True})
            for info,reason in plan['feeds']:self.start_feed(info)
        # This only reads the plan; acquisition stays on the serial feed lane.
        # Aged history jobs outrank misc and can postpone the very planner
        # which assigns fresh/recovery work. Reuse the existing read lane.
        self.job(lambda:planned_feeds(self.store,infos,focus=focus),apply,key='cp-schedule',lane='read')
        self.root.after(30000,self._scheduled_refresh)

    def refresh_all_async(self):
        startup=not getattr(self,'_performance_first_refresh',False)
        self._performance_first_refresh=True
        from data_quality import INTERVAL_SCOPE
        for info in sorted(self.watchlist,key=lambda i:i['isin'] not in INTERVAL_SCOPE):self.start_feed(info,only_if_due=startup)

    def _poll_priority_gettex(self):
        # Explicit owner-scoped, expiring request. Reuse the same feed lane,
        # worker and deduplication key; never create a concurrent writer.
        if self.closing:return
        policy=self.priority_gettex_poll
        try:
            if pd.Timestamp.now(tz='UTC')>=pd.Timestamp(policy['until']):return
            seconds=max(60,min(300,int(policy.get('seconds',300))))
        except (ValueError,TypeError,KeyError):return
        info=next((i for i in self.watchlist if i['isin']==policy.get('isin')),None)
        if info and info.get('venue')=='GETTEX' and info.get('currency')=='EUR' and self.source_mode(info)=='gettex':
            self.start_feed({**info,'_gettex_poll_only':True})
        self.root.after(seconds*1000,self._poll_priority_gettex)

    def start_feed(self,info,only_if_due=False):
        if self.closing or info['isin'] in self.feed_active:return
        from hashlib import sha256
        import json
        captured=copy.deepcopy(info);self.feed_active.add(info['isin'])
        generation=sha256(json.dumps(captured,sort_keys=True,default=str).encode()).hexdigest()
        accepted=self.job(lambda:self._gettex_worker(captured),lambda _:None,
            lambda error:self.feed_done(captured,[error]),key='feed:'+info['isin'],lane='feed_update',
            isin=info['isin'],durable=True,generation=generation,only_if_due=only_if_due)
        if not accepted:self.feed_active.discard(info['isin'])

    def _gettex_worker(self,info):
        # Keep the existing refresh queue, but isolate CPU-heavy history/DQ
        # and archive decoding from Tk's interpreter.
        with self.feed_slots:
            if self.closing:return
            try:
                focus=dict(getattr(self,'_cp_focus',{}))
                captured=dict(info)
                fresh=dict(getattr(self,'_cp_fresh_request',{}))
                if fresh.get('isin')==info['isin'] and 0<=time.time()-getattr(self,'_cp_fresh_requested_at',0)<90:
                    # Upgrade the already queued same-ISIN job; no second writer
                    # or cancellation. Archive maintenance remains due afterwards.
                    captured.update({k:fresh[k] for k in ('_gettex_poll_only','_cp_focus','_cp_foreground','_cp_position') if k in fresh})
                    if captured.get('_gettex_poll_only'):
                        # These routes return before acquisition in FeedContext.
                        # Their persisted work remains due after this live turn.
                        captured.pop('_eligibility_only',None)
                        captured.pop('_review_only',None)
                if focus.get('isin')==info['isin'] and not captured.get('_gettex_poll_only'):captured['_cp_focus']=focus
                info=captured
                self._cp_active_feed=dict(isin=info['isin'],name=info.get('name',info['isin']),
                    timeframe=info.get('_cp_focus',{}).get('timeframe'),
                    phase=('Переоценка исторических разрешений' if info.get('_review_only') else
                           'Пересчёт аналитического допуска' if info.get('_eligibility_only') else
                           'Свежие свечи и оценка позиции' if info.get('_cp_position') else
                           'Данные для торгового допуска' if info.get('_cp_focus',{}).get('priority_reason')=='TRADE_CONTEXT_MAINTENANCE' else
                           'Обновление и проверка истории'),started_at=time.time())
                errors=self.compute_feed_update.calculate('feed_update',{'info':captured})['errors']
                self.cp_error=None
            except Exception as error:
                self.cp_error=str(error);errors=['Feed worker: '+str(error)]
            finally:self._cp_active_feed={}
        if any('database is locked' in str(e).lower() for e in errors):self.dispatcher.sqlite_busy()
        if not self.closing:self.events.put((self.feed_done,(info,errors)))

    def _run_feed(self,info):
        import data_quality as quality
        errors=[]
        with self.feed_slots:
            # The existing fair dispatcher chooses the ISIN. Within its turn,
            # acquire the visible source before unrelated archive maintenance.
            # Complete all normal work afterwards; never cancel a writer.
            focus=info.get('_cp_focus',{})
            front_source=focus.get('source') if focus.get('isin')==info['isin'] else None
            if not front_source and not info.get('_gettex_poll_only'):
                import gettex_chart
                if gettex_chart.confirmed_state(info,self.store.setting(gettex_chart.state_key(info['isin'],'15m'),{})):
                    # A normal turn also acquires the confirmed primary tail
                    # before archive recovery can consume its source reservation.
                    # Existing two-turn preference/oldest-base fairness, rate
                    # limits and exact-source validation remain in turn().
                    front_source='gettex_chart';focus={'isin':info['isin'],'source':front_source,'timeframe':'15m'}
            if info.get('_gettex_poll_only') and front_source=='gettex_chart':
                # Existing serial CP lane: fetch the due visible price first.
                # Full maintenance stays queued normally for this and other ISINs.
                try:
                    import gettex_chart
                    result=gettex_chart.turn(info,self.store,preferred_tf=focus.get('timeframe'))
                    if result.get('state')=='ERROR':errors.append('GETTEX chart: '+result.get('reason','UNKNOWN'))
                    self.store.set_setting('gettex_poll:'+info['isin'],{'received_at':pd.Timestamp.now(tz='UTC').isoformat(),
                        'source':'gettex_chart','timeframe':focus.get('timeframe'),'result':result,'errors':errors})
                    import candle_schedule as scheduler
                    scheduler.fresh_finished(self.store,info,'gettex_chart',result.get('state','ERROR'))
                except Exception as error:errors.append('GETTEX chart: '+str(error))
                self.events.put((self.feed_done,(info,errors)))
                return
            if not self.closing and front_source=='gettex_chart':
                try:
                    import gettex_chart
                    result=gettex_chart.turn(info,self.store,preferred_tf=focus.get('timeframe'))
                    if result.get('state')=='ERROR':errors.append('GETTEX chart: '+result.get('reason','UNKNOWN'))
                except Exception as e:errors.append('GETTEX chart: '+str(e))
            if not self.closing and front_source=='proxy':
                try:_,extra=history_service.recover(info,self.store);errors+=extra
                except Exception as e:errors.append('История proxy: '+str(e))
            try:
                trades=self._download_gettex_trades(info['isin'])
                if not trades.empty:
                    self.store.save_trades(info['isin'],trades);all_trades=self.store.load_trades(info['isin'])
                    official=all_trades.price.resample('15min').ohlc().join(all_trades.volume.resample('15min').sum().rename('volume')).dropna()
                    self.store.save_frame(info['isin'],'gettex','15m',official)
                    smaller=all_trades.price.resample('5min').ohlc().join(all_trades.volume.resample('5min').sum().rename('volume')).dropna()
                    self.store.save_frame(info['isin'],'gettex','5m',smaller)
                    self.store.set_setting('gettex_last_success:'+info['isin'],pd.Timestamp.now(tz='UTC').isoformat())
                    file_errors=self.store.setting('gettex_file_errors:'+info['isin'],[])
                    if file_errors:errors.append('Не все файлы GETTEX загружены: '+str(len(file_errors))+' ошибок (подробности в диагностике)')
                for official_base in (() if info.get('_gettex_poll_only') else ('5m','15m')):
                    official=self.store.load_official(info['isin'],official_base)
                    if not official.empty:
                        quality.repair(info,self.store,info['isin'],official_base,official.attrs,
                            lambda symbol,interval,start,end=None,base=official_base:self._recover_gettex_range(symbol,start,end,base))
            except Exception as e:
                errors.append('GETTEX: '+str(e))
                quality.persist(self.store,info,info['isin'],'15m',{'state':'SOURCE_UNAVAILABLE','issues':[str(e)],'ranges':[]})
                quality.persist(self.store,info,info['isin'],'5m',{'state':'SOURCE_UNAVAILABLE','issues':[str(e)],'ranges':[]})
            if not self.closing and info.get('_gettex_poll_only'):
                # Current files were just acquired on this same serial writer.
                # Use their closed prefix now; no archive/network work in a poll.
                try:
                    import gettex_history
                    gettex_history.classify_cached_session(info,self.store,self._parse_gettex_csv,current_only=True)
                except Exception as e:errors.append('GETTEX evidence: '+str(e))
            if not self.closing and not info.get('_gettex_poll_only'):
                if front_source!='proxy':
                    try:_,extra=history_service.recover(info,self.store);errors+=extra
                    except Exception as e:errors.append('История proxy: '+str(e))
                try:
                    import gettex_history
                    gettex_history.maintain_evidence(info,self.store,self._http_bytes,self._parse_gettex_csv)
                except Exception as e:errors.append('GETTEX evidence: '+str(e))
                try:history_service.inspect_saved_archive(info,self.store,recover_gettex=self._recover_gettex_range)
                except Exception as e:errors.append('Archive audit: '+str(e))
                try:
                    import revision_watcher
                    revision_watcher.check(info,self.store,history_service.download)
                except Exception as e:errors.append('Revision watcher: '+str(e))
            if not self.closing and front_source!='gettex_chart' and not info.get('_gettex_poll_only'):
                # Bounded GETTEX website archive work on the existing feed worker.
                # Separate source/identity; never append to post-trade candles.
                try:
                    import gettex_chart
                    chart_result=gettex_chart.turn(info,self.store)
                    if chart_result.get('state')=='ERROR':errors.append('GETTEX chart: '+chart_result.get('reason','UNKNOWN'))
                except Exception as e:errors.append('GETTEX chart: '+str(e))
            if not self.closing:
                if info.get('_gettex_poll_only'):
                    self.store.set_setting('gettex_poll:'+info['isin'],{
                        'received_at':pd.Timestamp.now(tz='UTC').isoformat(),'ok':not errors,
                        'errors':errors,'latest':{tf:str(self.store.load_official(info['isin'],tf).index.max()) for tf in ('5m','15m')}})
                import candle_schedule as scheduler
                pending=getattr(self,'_cp_gettex_pending',None)
                if pending and pending[0]==info['isin']:
                    scheduler.finish(self.store,info['isin'],scheduler.gettex_meta(info['isin']),'5m',pending[1])
                    self._cp_gettex_pending=None
                if info.get('_gettex_poll_only'):
                    scheduler.fresh_finished(self.store,info,'gettex','ERROR' if errors else 'CHECKED')
                else:
                    scheduler.history_finished(self.store)
                    self.store.set_setting('cp_maintenance:'+info['isin'],{'at':pd.Timestamp.now(tz='UTC').isoformat()})
                self.store.event('feed_result',{'isin':info['isin'],'ok':not errors,'errors':errors,'last_success':self.store.setting('gettex_last_success:'+info['isin'])})
                self.events.put((self.feed_done,(info,errors)))

    def _download_gettex_trades(self,isin):
        import gettex_history
        import candle_schedule as scheduler
        meta=scheduler.gettex_meta(isin);plan=scheduler.decision(self.store,isin,meta,'5m')
        if not plan['due']:return pd.DataFrame()
        plan['started_at']=pd.Timestamp.now(tz='UTC').isoformat()
        try:result=gettex_history.download(isin,self._http_bytes,self._parse_gettex_csv,self.store)
        except Exception as error:
            scheduler.finish(self.store,isin,meta,'5m',plan,error);raise
        self._cp_gettex_pending=(isin,plan)
        return result

    def _recover_gettex_range(self,isin,start,end,base='15m'):
        import gettex_history
        trades=gettex_history.download(isin,self._http_bytes,self._parse_gettex_csv,self.store,start,end)
        if trades.empty:return pd.DataFrame()
        frequency='5min' if base=='5m' else '15min'
        frame=trades.price.resample(frequency).ohlc().join(trades.volume.resample(frequency).sum().rename('volume')).dropna()
        frame['source']='gettex'
        frame.attrs={'source':'gettex','symbol':isin,'venue':'GETTEX','currency':'EUR','timezone':'Europe/Berlin'}
        return frame

    def feed_done(self,info,errors):
        from freshness_runtime import feed_completed
        feed_completed(self,info)
        if info.get('_eligibility_only'):
            self.feed_active.discard(info['isin'])
            if errors:self._write_log('CandlePilot: '+'; '.join(errors))
            return
        if info.get('_gettex_poll_only'):
            self.feed_active.discard(info['isin'])
            # A current quote/provisional child can change without a closed
            # parent. Reuse the coalescing chart worker after this feed turn.
            if not getattr(self,'closing',False) and self.instrument_cfg()['isin']==info['isin']:self.refresh_chart()
            # This is ingestion only: preserve full-feed error/DQ diagnostics,
            # do not enqueue unrelated TF/Trading Engine calculations.
            if errors:self._write_log('Priority GETTEX: '+'; '.join(errors))
            return
        self.feed_active.discard(info['isin']);self.feed_errors[info['isin']]='; '.join(errors) if errors else None
        if self.instrument_cfg()['isin']==info['isin']:self.refresh_chart()
        from snapshot_worker import inputs
        values=inputs(self);store=self.store;tf=self.timeframe.get();state=copy.deepcopy(self.user_state(info))
        values['watchlist']=[copy.deepcopy(info)]
        values['symbol']=info['name']
        values['drafts']={k:v for k,v in values['drafts'].items() if k==info['isin']}
        values['instrument_revisions']={k:v for k,v in values['instrument_revisions'].items() if k=='instrument:'+info['isin']}
        def apply_feed(result):
            if self.closing:return
            if '_input_revisions' not in result or not store.read_cache.current(result['_input_revisions']):return
            if not any(item['isin']==info['isin'] for item in self.watchlist):return
            official,notification=result['official'],result['notification']
            self.data[info['name']]=official
            self.data_meta[info['name']]={'base_tf':'15m','source':'GETTEX','gettex':True}
            if notification and self.timeframe.get()==tf and self.user_state(info)==state:
                self.notify_signal(info,*notification)
        # At most one running and one replaceable pending result per instrument.
        # Workers only wait for the existing private-pipe client; CPU work runs
        # in one owned process, not competing with Tk via the GIL.
        from chart_worker import LatestRequest
        if not hasattr(self,'_feed_analysis_jobs'):self._feed_analysis_jobs={}
        request=self._feed_analysis_jobs.get(info['isin'])
        if request is None:
            request=LatestRequest(lambda work,done,failed:self.job(work,done,failed,lane='feed_analysis',key='feed-analysis:'+info['isin'],isin=info['isin']),lambda payload:payload[0](payload[1]),lambda error:self._write_log('Feed analysis: '+str(error)))
            self._feed_analysis_jobs[info['isin']]=request
        request.request(lambda:(apply_feed,self.compute_feed.calculate('feed',values)))

    def change_period(self):
        # Period is a viewport request, never changes interval or provider silently.
        self.user_view=None;self.refresh_chart()

    def reset_view(self):self.user_view=None;self.refresh_chart()

    def analyse_cached(self,info,frame,calculation):
        import hashlib,json
        # Hash values without copying the full DQ/provenance tree per column.
        # Metadata remains an independent, complete part of the cache key below.
        signature=hashlib.sha256(pd.util.hash_pandas_object(pd.DataFrame(frame,copy=False),index=True).values.tobytes()).digest()
        from analytics import bar_ends
        now=pd.Timestamp.now(tz='UTC')
        closed=(bar_ends(frame)<=now).tobytes() if frame.attrs.get('timeframe') else b'unknown'
        key=('analysis',closed,info['isin'],signature,json.dumps(frame.attrs,sort_keys=True,default=str),json.dumps(calculation,sort_keys=True,default=str))
        return self.store.read_cache.get(key,lambda:analyse(frame,calculation,now=now))

    def refresh_chart(self):
        if not hasattr(self,'canvas'):return
        from ui_performance import RenderTiming
        render_timing=RenderTiming(self.root)
        self.quality_signature=self.current_quality_signature()
        self._cancel_motion()
        self.store.set_setting('selected',{'symbol':self.symbol.get(),'timeframe':self.timeframe.get(),'period':self.period.get()})
        info=self.instrument_cfg();state=self.user_state(info);mode=self.source_mode(info)
        self.source_choice.set(tr('Proxy — автоматически · EUR'))
        key=(info['isin'],self.timeframe.get(),self.period.get(),mode)
        if key!=self.render_key:self.user_view=None
        self.render_key=key;self.cursor=[];self.drag=None;self.drawing_start=None
        if getattr(self,'hover_overlay',None):self.hover_overlay.close();self.hover_overlay=None
        render_timing.mark('state_sqlite')
        self.fig.clear();self.panels=[]
        render_timing.mark('clear_figure')
        settings={**DEFAULT_IND,**self.store.setting('global_indicators',{})}
        panel_names=[k for k in ('Объём','RSI','MACD','ATR','Относительный объём') if settings.get(k)]
        heights=[4]+[1]*len(panel_names);grid=self.fig.add_gridspec(len(heights),1,height_ratios=heights,left=.085,right=.81 if settings['Volume Profile'] else .97,top=.93,bottom=.12,hspace=.10)
        self.ax_price=self.fig.add_subplot(grid[0]);self.ax_vol=self.ax_price
        for index,name in enumerate(panel_names,1):
            ax=self.fig.add_subplot(grid[index],sharex=self.ax_price);self.panels.append((name,ax));self.ax_vol=ax
        axes=[self.ax_price]+[ax for _,ax in self.panels]
        for ax in axes:
            ax.set_facecolor(BG);ax.tick_params(colors=MUTED,labelsize=8);ax.grid(color='#273449',alpha=.5)
            for sp in ax.spines.values():sp.set_color('#273449')
        for ax in axes[:-1]:ax.tick_params(labelbottom=False)
        render_timing.mark('create_axes')
        frame=self.frame_for(info,self.timeframe.get());self.current_df=None
        self.instrument.set(info['name'])
        from trading_view import bind_engine
        bind_engine(self,(getattr(self,'_prepared_chart',None) or {}).get('trading_engine'))
        for v in self.metrics.values():v.set('—')
        if frame.empty:
            quality=frame.attrs.get('data_quality',{})
            self.source_status.set(tr('Данные: ') + quality.get('state', 'SOURCE_UNAVAILABLE') + tr(' • Нет свечей выбранного источника'))
            self.ax_price.text(.5,.5,tr('Нет свечей выбранного источника'),ha='center',color=TEXT,transform=self.ax_price.transAxes);self.status.set(tr(self.feed_errors.get(info['isin']) or 'Архив загружается при запуске.'));draw_localized(self);render_timing.mark('empty');render_timing.finish();return
        calculation=copy.deepcopy(state)
        if frame.attrs.get('currency')!=state.get('levels_currency',state.get('currency','EUR')) or state.get('levels_source',mode)!=frame.attrs.get('source'):calculation['levels']={}
        enriched,result=self.analyse_cached(info,frame,calculation);enriched.attrs=frame.attrs
        months=PERIODS.get(ALIASES.get(self.period.get(),self.period.get()))
        from chart_periods import visible_frame
        requested=visible_frame(enriched,ALIASES.get(self.period.get(),self.period.get()))
        # Retain earlier bars for panning; the period controls the initial viewport.
        first_visible=len(enriched)-len(requested)
        visible=enriched
        self.current_df=visible;self.chart_meta=frame.attrs;xs=np.arange(len(visible));width=.65
        render_timing.mark('bind_prepare')
        from chart_modes import draw_prices
        draw_prices(self.ax_price,visible,self.chart_mode.get())
        render_timing.mark('prices')
        if result.get('ohlcv_valid',False) or result.get('display_indicators_available',False):self.draw_indicators(enriched,visible,settings)
        render_timing.mark('indicators')
        if settings['Уровни анализа'] and (result.get('ohlcv_valid',False) or result.get('display_levels_available',False)):
            from significant_levels import zones
            for zone in self.level_zones(info,frame):
                color='#22c55e' if zone['side']=='support' else '#ef4444'
                self.ax_price.axhspan(zone['low'],zone['high'],color=color,alpha=.09 if zone['historical'] else .16)
                self.ax_price.axhline((zone['low']+zone['high'])/2,color=color,lw=1,ls='--' if zone['historical'] else '-',alpha=.5)
            self.ax_price.text(.01,.98,tr('S/R: сплошные — ближайшие; пунктир — исторические'),transform=self.ax_price.transAxes,va='top',fontsize=7,color=MUTED)
            if result.get('display_levels_available'):
                for field,color,label in (('support','#22c55e','Поддержка'),('resistance','#ef4444','Сопротивление')):
                    self.ax_price.axhline(result[field],color=color,lw=1,ls='--',label=label+' · закрытые свечи')
        render_timing.mark('zones')
        partial=frame.attrs.get('partial_chart')
        if not partial:
            self.draw_dividends(info,visible)
            self.draw_objects(state.get('drawings',[]),visible)
        render_timing.mark('dividends_drawings')
        currency=frame.attrs.get('currency','?');venue=frame.attrs.get('venue','?');label=f'GETTEX • Onvista / {frame.attrs.get("symbol","")}' if frame.attrs.get('source')=='onvista' else 'GETTEX • LSEG / financial.com' if frame.attrs.get('source')=='gettex_chart' else 'GETTEX • официальный post-trade' if frame.attrs.get('source')=='gettex' else f'PROXY • Yahoo / {venue} / {frame.attrs.get("symbol","")}'
        if frame.attrs.get('fallback_reason'):label+=' • fallback'
        selection=frame.attrs.get('source_selection',{})
        if selection.get('selected') in ('GETTEX_EUR','GETTEX_CHART_EUR'):label+=' • авто EUR 07:30–23:00'
        if self.timeframe.get() in ('2H','3H','4H'):label+=' • календарь сессий'
        self.ax_price.set_title(f'{info["name"]} • {TF[self.timeframe.get()]} • {label} • {currency}',color=TEXT,fontsize=10)
        self.ax_price.set_ylabel(currency,color=MUTED)
        def tick(value,_):
            i=int(round(value))
            return visible.index[i].strftime('%d.%m.%y\n%H:%M' if self.timeframe.get() not in ('1D','1W') else '%d.%m.%Y') if 0<=i<len(visible) else ''
        self.ax_vol.xaxis.set_major_formatter(FuncFormatter(tick))
        # A short genuine archive must not turn six bars into screen-wide blocks.
        if not self.user_view:
            self.ax_price.set_xlim(first_visible-1,first_visible+max(40,len(requested))+1)
            low=float(requested.low.min());high=float(requested.high.max());padding=max((high-low)*.06,abs(high)*.001)
            self.ax_price.set_ylim(low-padding,high+padding)
        if self.user_view:self.ax_price.set_xlim(self.user_view[0]);self.ax_price.set_ylim(self.user_view[1])
        render_timing.mark('labels_limits')
        from chart_indicators import volume_profile_rows
        profile_rows=volume_profile_rows(visible,result.get('ohlcv_valid',False)) if settings['Volume Profile'] else None
        if profile_rows is not None and not profile_rows.empty:
            profile_title='Volume Profile\nоценка OHLCV'+('\nчастичная история' if frame.attrs.get('partial_chart') else '')
            self.profile_axis=self.ax_price.inset_axes([1.03,0,.16,1],sharey=self.ax_price)
            self.profile_axis.set_facecolor(BG)
            self.profile_axis.tick_params(colors=MUTED,labelsize=6,labelleft=False)
            self.profile_axis.set_title(profile_title,color=TEXT,fontsize=7)
            def update_profile(axis):
                left,right=axis.get_xlim()
                portion=visible.iloc[max(0,int(np.ceil(left))):min(len(visible),int(np.floor(right))+1)]
                portion=volume_profile_rows(portion,result.get('ohlcv_valid',False))
                prices,volumes,widths=volume_profile(portion)
                self.profile_axis.clear();self.profile_axis.set_facecolor(BG)
                self.profile_axis.barh(prices,volumes,height=widths*.9,color=BLUE,alpha=.6)
                self.profile_axis.set_title(profile_title,color=TEXT,fontsize=7)
                self.profile_axis.tick_params(colors=MUTED,labelsize=6,labelleft=False)
                self.profile_axis.set_ylim(axis.get_ylim())
            update_profile(self.ax_price)
            self.ax_price.callbacks.connect('xlim_changed',update_profile)
        render_timing.mark('volume_profile')
        handles,labels=self.ax_price.get_legend_handles_labels()
        if handles:self.ax_price.legend(loc='upper left',fontsize=8,facecolor=BG,labelcolor=TEXT)
        last=enriched.iloc[-1];self.metrics['price'].set(number(last.close)+' '+currency)
        for k in ('ema10','ema30'):
            note=frame.attrs.get('partial_indicators',{})
            text=number(last[k])+(' '+currency if pd.notna(last[k]) else '')
            if pd.isna(last[k]) and note:
                text=('Недостоверный участок' if note.get('blocked_reason') else
                      f"Прогрев участка: {note['latest_segment_bars']}/{note['fixed_minimum_bars'][k]}")
            elif note.get('live_provisional'):text+=' ≈'
            self.metrics[k].set(text)
        for k in ('support','resistance','stop','add'):self.metrics[k].set(number(result[k])+(' '+currency if result[k] is not None else ''))
        pos=state.get('position',{});qty=pos.get('quantity');avg=pos.get('average_price')
        if qty and avg and not partial:
            if currency==pos.get('currency',info.get('currency','EUR')):
                returns=self.position_returns(info,pos,qty*(last.close-avg),currency)
                self.metrics['position'].set(f"{qty:g}{tr(' шт.\nPrice P/L: ')}{number(returns['price_pnl'])} {currency}\nDividends: {(number(returns['dividends']) if returns['dividends'] is not None else tr('нет данных'))}\nTotal Return: {(number(returns['total_return']) if returns['total_return'] is not None else tr('нет данных'))}")
            else:self.metrics['position'].set(tr('Валюта позиции отличается; P/L не вычисляется'))
        missing=months and frame.index[0]>frame.index[-1]-pd.DateOffset(months=months)+pd.Timedelta(days=7)
        coverage=f'{len(frame)} свечей • {frame.index[0]:%d.%m.%Y} — {frame.index[-1]:%d.%m.%Y}'
        quote=frame.attrs.get('latest_gettex')
        quote_text=('\nGETTEX: '+number(quote['price'])+' EUR • '+quote['at']+' (последняя архивная свеча)') if quote else '\nЦена GETTEX недоступна'
        render_timing.mark('metrics')
        identity=self.store.setting('history_identity:'+info['isin'],{})
        from history_backfill import display_coverage
        history_state,daily_coverage=display_coverage(self.store,info,frame.attrs.get('source'),self.timeframe.get(),base_for(self.timeframe.get(),frame.attrs.get('source','proxy')))
        if frame.attrs.get('eur_history_depth'):
            d=frame.attrs['eur_history_depth'];history_state=f"{d['status']} • закрытых свечей {d['count']}/{d['target']}";daily_coverage=''
        from data_quality import key as quality_key
        quality_base=base_for(self.timeframe.get(),frame.attrs.get('source','proxy'))
        quality=self.store.setting(quality_key(info,frame.attrs.get('symbol','') if frame.attrs.get('source')=='proxy' else info['isin'],quality_base),{})
        if quality.get('state') not in ('RECOVERING','SOURCE_UNAVAILABLE','INVALID','SOURCE_GAP') or frame.attrs.get('data_quality',{}).get('state')=='INVALID':
            quality=frame.attrs.get('data_quality',quality)
        quality_text=quality.get('state','INVALID')
        quality_detail='; '.join(quality.get('issues',[])+quality.get('warnings',[]))
        if partial:
            quality_text=str(frame.attrs.get('data_quality',{}).get('state','UNKNOWN'))+' / PARTIAL'
            history_state=f"{partial.get('segments',0)} участков; исключено {partial.get('excluded_rows',0)} / {partial.get('raw_count',len(frame))} строк"
            daily_coverage='\nИндикаторы по корректным участкам; после разрыва — новый прогрев. Живая свеча: предварительно. Торговые проверки сохранены.'
            if result.get('levels_note'):daily_coverage+='\n'+result['levels_note']
        self.source_status.set(tr('Данные: ') + quality_text + ' • ' + label + ' • ' + currency + ' • ' + history_state)
        selection_note=selection.get('reason','')
        self.metrics['updated'].set(coverage+quote_text+daily_coverage+('\n'+selection_note if selection_note else '')+('\n'+quality_detail if quality_detail else ''))
        self.status.set((tr('Запрошенный период покрыт не полностью. ') if missing else '') + coverage + (' • ' + self.feed_errors[info['isin']] if self.feed_errors.get(info['isin']) else ''))
        render_timing.mark('coverage_dq')
        draw_localized(self)
        render_timing.finish()

    def draw_indicators(self,full,visible,s):
        if full.attrs.get('partial_indicators'):
            from chart_indicators import draw_partial
            return draw_partial(self,full,visible,s,MUTED,BLUE)
        close=full.close;series={}
        if s['EMA']:
            for p in (s['ema_fast'],s['ema_slow']):series['EMA '+str(p)]=close.ewm(span=p,min_periods=p,adjust=False).mean()
        if s['SMA']:series['SMA '+str(s['sma'])]=close.rolling(s['sma']).mean()
        if s['Bollinger']:
            mid=close.rolling(s['bb']).mean();std=close.rolling(s['bb']).std();series['Боллинджер верх']=mid+s['bb_dev']*std;series['Боллинджер низ']=mid-s['bb_dev']*std
        if s['VWAP']:
            if self.timeframe.get() in ('1D','1W'):
                series['VWAP (с начала архива)']=((full.high+full.low+close)/3*full.volume).cumsum()/full.volume.cumsum().replace(0,np.nan)
            else:
                group=full.index.normalize();series['VWAP (сессия)']=(((full.high+full.low+close)/3*full.volume).groupby(group).cumsum()/full.volume.groupby(group).cumsum().replace(0,np.nan))
        for name,values in series.items():self.ax_price.plot(np.arange(len(visible)),values.reindex(visible.index),label=name,lw=1)
        x=np.arange(len(visible));volume_mean=full.volume.rolling(s['volume']).mean()
        for name,ax in self.panels:
            ax.set_ylabel(name,color=MUTED,fontsize=8)
            if name=='Объём':
                from chart_modes import draw_segments
                draw_segments(ax,np.stack((np.column_stack((x,np.zeros(len(x)))),np.column_stack((x,visible.volume.to_numpy()))),axis=1),color=BLUE,lw=1);ax.autoscale_view()
                if s['Средний объём']:ax.plot(x,volume_mean.reindex(visible.index),color='#f59e0b')
            elif name=='Относительный объём':ax.plot(x,(full.volume/volume_mean.replace(0,np.nan)).reindex(visible.index),color=BLUE)
            elif name=='RSI':
                delta=close.diff();up=delta.clip(lower=0).ewm(alpha=1/s['rsi'],min_periods=s['rsi'],adjust=False).mean();down=(-delta.clip(upper=0)).ewm(alpha=1/s['rsi'],min_periods=s['rsi'],adjust=False).mean();rsi=100-100/(1+up/down.replace(0,np.nan));rsi[(down==0)&(up>0)]=100;rsi[(down==0)&(up==0)]=50
                ax.plot(x,rsi.reindex(visible.index));ax.axhline(70,ls=':',color=MUTED);ax.axhline(30,ls=':',color=MUTED);ax.set_ylim(0,100)
            elif name=='MACD':
                line,average,histogram=macd_series(close,s['macd_fast'],s['macd_slow'],s['macd_signal'])
                ax.set_ylabel(f"MACD {s['macd_fast']}/{s['macd_slow']}/{s['macd_signal']}",color=MUTED,fontsize=8)
                ax.plot(x,line.reindex(visible.index));ax.plot(x,average.reindex(visible.index))
                from chart_modes import draw_segments
                values=histogram.reindex(visible.index).to_numpy();valid=~np.isnan(values)
                draw_segments(ax,np.stack((np.column_stack((x[valid],np.zeros(valid.sum()))),np.column_stack((x[valid],values[valid]))),axis=1),color=BLUE,lw=1,alpha=.4);ax.autoscale_view()
            elif name=='ATR':
                tr=pd.concat([full.high-full.low,(full.high-close.shift()).abs(),(full.low-close.shift()).abs()],axis=1).max(axis=1);ax.plot(x,tr.ewm(alpha=1/s['atr'],min_periods=s['atr'],adjust=False).mean().reindex(visible.index))

    def clear_cursor(self):
        if getattr(self,'hover_overlay',None):self.hover_overlay.hide()
        self.cursor=[]

    def on_hover(self,event):
        if self.current_df is None or event.inaxes not in [self.ax_price]+[ax for _,ax in self.panels] or event.xdata is None:return
        i=int(round(event.xdata))
        if not 0<=i<len(self.current_df):return
        r=self.current_df.iloc[i];dt=self.current_df.index[i];y=event.ydata if event.inaxes==self.ax_price else r.close
        if y is None:return
        if not getattr(self,'hover_overlay',None):
            from chart_overlay import Overlay
            self.hover_overlay=Overlay(self.canvas,[self.ax_price]+[ax for _,ax in self.panels],MUTED,TEXT,BG)
        text=f'{dt:%d.%m.%Y %H:%M} • {self.chart_meta.get("source")} / {self.chart_meta.get("currency")}\nОткр. {r.open:.2f}  Макс. {r.high:.2f}  Мин. {r.low:.2f}  Закр. {r.close:.2f}\nОбъём {r.volume:g} • EMA30 {number(r.ema30)}'
        self.hover_overlay.show(i,y,text)

    def on_leave(self,event):
        self._cancel_motion()
        if getattr(self,'measure_start',None) is not None:self.clear_measurement()
        if self.drag:return
        self.clear_cursor()

    def on_motion(self,event):
        self._latest_motion=event
        if getattr(self,'_motion_timer',None) is None:
            self._motion_timer=self.root.after(16,self._flush_motion)

    def _flush_motion(self):
        self._motion_timer=None
        event=getattr(self,'_latest_motion',None);self._latest_motion=None
        if event is not None:self._process_motion(event)

    def _cancel_motion(self):
        if getattr(self,'_motion_timer',None) is not None:
            self.root.after_cancel(self._motion_timer);self._motion_timer=None
        self._latest_motion=None

    def _process_motion(self,event):
        if getattr(self,'_drawing_drag',None) is not None:return
        if getattr(self,'measure_start',None) is not None:
            if event.inaxes==self.ax_price and event.xdata is not None and event.ydata is not None:
                from ruler import measurement
                a=self.measure_start;b=(max(0,min(len(self.current_df)-1,round(event.xdata))),event.ydata)
                for artist in getattr(self,'measure_artists',[]):artist.remove()
                line,=self.ax_price.plot([a[0],b[0]],[a[1],b[1]],color=BLUE,marker='o')
                label=measurement(self.current_df.index,a,b,self.current_df.attrs.get('currency','?'))
                text=self.ax_price.text(.5,.97,label,transform=self.ax_price.transAxes,ha='center',va='top',color=TEXT,fontsize=9,bbox=dict(facecolor=BG,alpha=.9),zorder=25)
                self.measure_artists=[line,text];draw_localized(self)
            return
        if self.drag:
            sx,sy,xlim,ylim,width,height=self.drag
            dx=(event.x-sx)*(xlim[1]-xlim[0])/width;dy=(event.y-sy)*(ylim[1]-ylim[0])/height
            self.ax_price.set_xlim(xlim[0]-dx,xlim[1]-dx);self.ax_price.set_ylim(ylim[0]-dy,ylim[1]-dy);draw_localized(self)
        else:self.on_hover(event)

    def start_measurement(self):
        self.clear_measurement();self.measuring=True;self.drawing_mode=None
        self.status.set(tr('Линейка: удерживайте левую кнопку и перемещайте мышь. Отпустите, чтобы убрать измерение.'))

    def clear_measurement(self):
        for artist in getattr(self,'measure_artists',[]):
            try:artist.remove()
            except ValueError:pass
        self.measure_artists=[];self.measure_start=None;self.measuring=False
        draw_localized(self)

    def on_press(self,event):
        if getattr(self,'measuring',False) and event.button==1 and event.inaxes==self.ax_price and event.xdata is not None and self.current_df is not None and len(self.current_df):
            self.clear_cursor();self.drag=None
            self.measure_start=(max(0,min(len(self.current_df)-1,round(event.xdata))),event.ydata)
            return

        if event.button==3 and event.inaxes==self.ax_price:
            for artist,index in reversed(getattr(self,'drawing_artists',[])):
                if artist.contains(event)[0]:
                    from drawing_actions import context_menu
                    context_menu(self,event,index);return
            return
        if event.button!=1 or event.inaxes!=self.ax_price or event.xdata is None or self.current_df is None:return
        if self.drawing_mode:self.drawing_click(event);return
        from drawing_actions import begin_drag
        if begin_drag(self,event):return
        if getattr(event,'dblclick',False):self.reset_view();return
        self.clear_cursor();b=self.ax_price.bbox;self.drag=(event.x,event.y,self.ax_price.get_xlim(),self.ax_price.get_ylim(),b.width,b.height)

    def on_release(self,event):
        from drawing_actions import finish_drag
        if finish_drag(self,event):return
        if getattr(self,"_motion_timer",None) is not None:
            self.root.after_cancel(self._motion_timer);self._flush_motion()
        if getattr(self,'measure_start',None) is not None:
            self.clear_measurement();return
        if self.drag:self.user_view=(self.ax_price.get_xlim(),self.ax_price.get_ylim())
        self.drag=None

    def on_scroll(self,event):
        if event.inaxes!=self.ax_price or event.xdata is None:return
        scale=.85 if event.button=='up' else 1/.85;x,y=event.xdata,event.ydata;a,b=self.ax_price.get_xlim();c,d=self.ax_price.get_ylim()
        if 5<(b-a)*scale<100000:self.ax_price.set_xlim(x+(a-x)*scale,x+(b-x)*scale)
        self.ax_price.set_ylim(y+(c-y)*scale,y+(d-y)*scale);self.user_view=(self.ax_price.get_xlim(),self.ax_price.get_ylim());draw_localized(self)

    def show_indicators(self):
        info=self.instrument_cfg();draft=copy.deepcopy(self.user_state(info));s={**DEFAULT_IND,**self.store.setting('global_indicators',{})}
        win=tk.Toplevel(self.root);win.title(tr('Индикаторы • все акции'));win.geometry('460x620')
        actions=ttk.Frame(win,padding=10);actions.pack(side='bottom',fill='x')
        container=ttk.Frame(win);container.pack(fill='both',expand=True)
        canvas=tk.Canvas(container,highlightthickness=0);scroll=ttk.Scrollbar(container,orient='vertical',command=canvas.yview)
        canvas.configure(yscrollcommand=scroll.set);scroll.pack(side='right',fill='y');canvas.pack(side='left',fill='both',expand=True)
        body=ttk.Frame(canvas,padding=12);window=canvas.create_window((0,0),window=body,anchor='nw')
        body.bind('<Configure>',lambda e:canvas.configure(scrollregion=canvas.bbox('all')))
        canvas.bind('<Configure>',lambda e:canvas.itemconfigure(window,width=e.width))
        values={}
        for row,(key,val) in enumerate(s.items()):
            if isinstance(val,bool):
                v=tk.BooleanVar(value=val);ttk.Checkbutton(body,text=tr(key),variable=v).grid(row=row,column=0,columnspan=2,sticky='w')
            else:
                labels={'ema_fast':'EMA быстрая','ema_slow':'EMA медленная','sma':'Период SMA','rsi':'Период RSI','atr':'Период ATR','bb':'Период Боллинджера','bb_dev':'Отклонение Боллинджера','volume':'Период среднего объёма','macd_fast':'MACD быстрый','macd_slow':'MACD медленный','macd_signal':'MACD сигнальный'}
                v=tk.StringVar(value=str(val));ttk.Label(body,text=tr(labels[key])).grid(row=row,column=0,sticky='w');ttk.Entry(body,textvariable=v,width=8).grid(row=row,column=1)
            values[key]=v
        def apply():
            try:
                selected={k:v.get() if isinstance(s[k],bool) else (float(v.get()) if k=='bb_dev' else int(v.get())) for k,v in values.items()}
                if any(not 1<=v<=500 for k,v in selected.items() if not isinstance(v,bool)):raise ValueError('Периоды: от 1 до 500')
                if selected['macd_fast']>=selected['macd_slow']:raise ValueError('MACD: быстрый период должен быть меньше медленного')
                self.store.set_setting('global_indicators',selected);win.destroy();self.refresh_chart()
            except ValueError as e:messagebox.showerror(tr('Параметры'),tr(str(e)),parent=win)
        ttk.Button(actions,text=tr('Применить'),command=apply).pack(side='right')
        ttk.Button(actions,text=tr('По умолчанию'),command=lambda:[values[k].set(v) for k,v in DEFAULT_IND.items()]).pack(side='left')
        ttk.Label(body,text=tr('Настройки графика. Сценарии используют EMA 10/30,\nRSI/ATR 14, MACD 12/26/9 и средний объём 20.')).grid(row=len(s)+1,column=0,columnspan=2,sticky='w')

    def show_drawing(self):
        info=self.instrument_cfg()
        windows=getattr(self,'_drawing_windows',{});self._drawing_windows=windows
        if info['isin'] in windows and windows[info['isin']].winfo_exists():windows[info['isin']].lift();return
        win=tk.Toplevel(self.root);windows[info['isin']]=win
        win.title(tr('Рисование • {name}',name=info['name']));body=ttk.Frame(win,padding=12);body.pack(fill='both',expand=True)
        for label,kind in [('Горизонталь','hline'),('Вертикаль','vline'),('Трендовая линия','trend'),('Луч','ray'),('Зона','zone'),('Маркер цены','marker')]:
            def choose(k=kind):self.drawing_mode=k;self.drawing_start=None;self.status.set(tr('Укажите ') + (tr('одну точку') if k in ('hline', 'vline', 'marker') else tr('две точки')) + tr(' на графике.'));win.destroy()
            ttk.Button(body,text=tr(label),command=choose).pack(fill='x',pady=2)
        objects=tk.Listbox(body,width=50,height=7,bg=BG,fg=TEXT);objects.pack(fill='x',pady=8)
        listed=copy.deepcopy(self.user_state(info).get('drawings',[]))
        for n,o in enumerate(listed):objects.insert('end',f'{n+1}: {o["type"]} • {o.get("at",o.get("price",""))}')
        def remove():
            if not objects.curselection():return
            index=objects.curselection()[0];expected=listed
            from drawing_actions import commit,modify
            if commit(self,info['isin'],expected,modify(expected,index,'delete')):win.destroy()
        ttk.Button(body,text=tr('Удалить выбранный объект'),command=remove).pack(fill='x')
        def history_action(operation):
            from drawing_actions import travel
            if travel(self,info['isin'],operation):win.destroy()
        for label,operation in [('Отменить','undo'),('Повторить','redo')]:
            ttk.Button(body,text=tr(label),command=lambda op=operation:history_action(op)).pack(fill='x',pady=2)

    def show_settings(self):
        super().show_settings()
        win=self.root.winfo_children()[-1]
        if isinstance(win,tk.Toplevel):ttk.Button(win,text=tr('Удалить выбранную акцию'),command=self.remove_instrument).pack(pady=5)

    def show_ai(self):
        if self.ai_window and self.ai_window.winfo_exists():self.update_ai_view();self.ai_window.lift();return
        win=self.ai_window=tk.Toplevel(self.root);win.title(tr('ИИ / Codex • связь и диагностика'));win.geometry('900x690')
        bar=ttk.Frame(win,padding=10);bar.pack(fill='x')
        ttk.Button(bar,text=tr('Подключение Codex'),command=self.show_connection).pack(side='left')
        ttk.Button(bar,text=tr('Дополнительный API'),command=self.ai_settings).pack(side='right')
        ttk.Button(bar,text=tr('Анализ через API'),command=self.run_ai).pack(side='right',padx=6)
        self.ai_text=text_area(win)
        # The existing snapshot worker fills this view. Opening a window must
        # not analyse every instrument's full archive on the Tk thread.
        self.update_ai_view()

    def drawing_click(self,event):
        i=int(round(event.xdata))
        if not 0<=i<len(self.current_df):return
        point={'at':str(self.current_df.index[i]),'price':float(event.ydata)}
        if not math.isfinite(point['price']) or point['price']<=0:return
        obj={'type':self.drawing_mode,**point,'source_mode':self.chart_meta.get('source'),'currency':self.chart_meta.get('currency')}
        if self.drawing_mode not in ('hline','vline','marker'):
            if self.drawing_start is None:self.drawing_start=point;self.status.set(tr('Укажите вторую точку.'));return
            obj.update(self.drawing_start);obj['end']=point
        info=self.instrument_cfg();expected=copy.deepcopy(self.user_state(info).get('drawings',[]))
        self.drawing_mode=None;self.drawing_start=None
        from drawing_actions import commit
        commit(self,info['isin'],expected,expected+[obj])

    def draw_objects(self,objects,frame):
        mode=frame.attrs.get('source');currency=frame.attrs.get('currency');self.drawing_artists=[]
        # Pandas may store a DatetimeIndex in microseconds; Timestamp.value is ns.
        # Normalize display coordinates only; never alter candle timestamps.
        coordinates=frame.index.as_unit('ns').asi8
        def locate(at):return float(np.interp(pd.Timestamp(at).value,coordinates,np.arange(len(frame))))
        for index,obj in enumerate(objects):
            if obj.get('source_mode',mode)!=mode or obj.get('currency',currency)!=currency:continue
            try:
                kind=obj['type'];price=float(obj.get('price',0));at=obj.get('at')
                if kind=='hline':self.drawing_artists.append((self.ax_price.axhline(price,color=BLUE,ls='--',lw=1.3,picker=6),index));continue
                if not at or not frame.index[0]<=pd.Timestamp(at)<=frame.index[-1]:continue
                x=locate(at)
                if kind=='vline':self.drawing_artists.append((self.ax_price.axvline(x,color=BLUE,ls=':',lw=1.3,picker=6),index));continue
                if kind=='marker':
                    artist,=self.ax_price.plot([x],[price],marker='o',color=BLUE,picker=6)
                    self.ax_price.annotate(f'{price:g}',(x,price),xytext=(5,5),textcoords='offset points',color=BLUE)
                    self.drawing_artists.append((artist,index));continue
                end=obj['end'];ex=locate(end['at']);ey=end['price']
                if kind=='zone':self.drawing_artists.append((self.ax_price.add_patch(Rectangle((min(x,ex),min(price,ey)),abs(ex-x),abs(ey-price),color=BLUE,alpha=.18)),index))
                else:
                    if kind=='ray' and ex!=x:ey=price+(ey-price)/(ex-x)*(len(frame)-1-x);ex=len(frame)-1
                    artist,=self.ax_price.plot([x,ex],[price,ey],color=BLUE,ls='--',lw=1.3,picker=6);self.drawing_artists.append((artist,index))
            except (KeyError,TypeError,ValueError):continue

    def save_user_changes(self):
        self.watchlist=[copy.deepcopy(self.drafts.get(i['isin'],i)) for i in self.watchlist]
        self.store.save_watchlist(self.watchlist);self.persisted=copy.deepcopy(self.watchlist);self.drafts.clear();self.user_dirty=False
        self.store.set_setting('selected',{'symbol':self.symbol.get(),'timeframe':self.timeframe.get(),'period':self.period.get()});self.status.set(tr('Изменения сохранены'))

    def confirm_close(self):
        if not self.user_dirty:return True
        answer=messagebox.askyesnocancel(tr('Сохранить изменения?'),tr('Сохранить пользовательские изменения?\nДа — сохранить; Нет — не сохранять; Отмена — продолжить работу.'))
        if answer is None:return False
        if answer:self.save_user_changes()
        return True

    def close(self,confirmed=False):
        if not confirmed and not self.confirm_close():return False
        self.closing=True;self.bridge.close();self.root.destroy();return True

    def position_dialog(self):
        info=self.instrument_cfg();draft=copy.deepcopy(self.user_state(info));win=tk.Toplevel(self.root);win.title(tr('Позиция и уровни • {name}',name=info['name']));body=ttk.Frame(win,padding=15);body.pack();values={}
        for group,key,label in [('position','quantity','Количество'),('position','average_price','Средняя цена'),('levels','support','Поддержка'),('levels','resistance','Сопротивление'),('levels','stop','Стоп'),('levels','target','Контрольная цель')]:
            ttk.Label(body,text=tr(label)).pack(anchor='w');v=tk.StringVar(value=str(draft.get(group,{}).get(key) or ''));values[group,key]=v;ttk.Entry(body,textvariable=v).pack(fill='x')
        currency=tk.StringVar(value=draft.get('position',{}).get('currency',info.get('currency','EUR')));ttk.Label(body,text=tr('Валюта позиции и пользовательских уровней')).pack();ttk.Entry(body,textvariable=currency).pack()
        position=draft.get('position',{})
        ttk.Label(body,text=tr('Дивиденды по позиции: дата сумма валюта, одна выплата на строку')).pack()
        receipts=tk.Text(body,height=4,width=55);receipts.pack(fill='x')
        receipts.insert('1.0','\n'.join(f"{r['date']} {r['amount']} {r['currency']}" for r in position.get('dividend_receipts',[])))
        complete=tk.BooleanVar(value=position.get('dividends_complete',False))
        ttk.Checkbutton(body,text=tr('Все выплаты за время этой позиции внесены (суммы после налогов)'),variable=complete).pack()
        def apply():
            try:
                from dividends import parse_receipts
                parsed=parse_receipts(receipts.get('1.0','end'))
                draft.setdefault('position',{})['dividend_receipts']=parsed
                draft['position']['dividends_complete']=complete.get()
                for (group,key),v in values.items():
                    n=float(v.get().replace(',','.')) if v.get().strip() else None
                    if n is not None and (not math.isfinite(n) or n<0):raise ValueError('Некорректное число')
                    draft.setdefault(group,{})[key]=n
                draft.setdefault('position',{})['currency']=currency.get().strip().upper();draft['currency']=currency.get().strip().upper();draft['levels_source']=self.source_mode(info);self.drafts[info['isin']]=draft;self.user_dirty=True;win.destroy();self.refresh_chart()
            except ValueError as e:messagebox.showerror(tr('Позиция'),tr(str(e)),parent=win)
        ttk.Button(body,text=tr('Применить'),command=apply).pack(pady=8)

    def connection_tick(self):
        if self.closing:return
        last=self.bridge.last_client
        transport=self.tunnel_runtime.snapshot()
        if time.time()-(transport.get('checked_at') or 0)>15:self.tunnel_runtime.poll()
        status='Connected' if transport['connected'] else 'Disconnected'
        suffix=tr(' • последний запрос ')+time.strftime('%H:%M:%S',time.localtime(last)) if last else tr(' • запросов ещё нет')
        self.ai_status.set('ChatGPT/MCP: ' + tr(status) + suffix)
        self.update_ai_view();self.root.after(2000,self.connection_tick)

    def show_portfolio(self):
        win=tk.Toplevel(self.root);win.title(tr('Портфель'));win.geometry('1120x440')
        tabs=ttk.Notebook(win);tabs.pack(fill='both',expand=True)
        real=ttk.Frame(tabs);paper=ttk.Frame(tabs)
        tabs.add(real,text=tr('Реальный портфель'));tabs.add(paper,text=tr('Демосчёт'))
        from paper_view import attach
        attach(self,paper)
        columns=('ticker','quantity','entry','price','currency','pnl','dividends','total_return','pct','stop','target','support','resistance','signal','source','at')
        body=ttk.Frame(real);body.pack(fill='both',expand=True,padx=10,pady=10)
        tree=ttk.Treeview(body,columns=columns,show='tree headings');tree.column('#0',width=35,stretch=False)
        labels=('Тикер','Количество','Средняя цена','Цена истории','Валюта цены','Price P/L','Dividends','Total Return','P/L %','Стоп','Цель','Поддержка','Сопротивление','Сигнал','Источник','Время свечи')
        for key,label in zip(columns,labels):tree.heading(key,text=tr(label));tree.column(key,width=170 if key=='at' else 110,stretch=False)
        scroll=ttk.Scrollbar(body,orient='horizontal',command=tree.xview);scroll.pack(side='bottom',fill='x');tree.configure(xscrollcommand=scroll.set)
        vertical=ttk.Scrollbar(body,orient='vertical',command=tree.yview);vertical.pack(side='right',fill='y');tree.configure(yscrollcommand=vertical.set)
        tree.pack(fill='both',expand=True)
        from snapshot_worker import inputs
        from trading_view import format_engine
        cached=getattr(self,'_latest_snapshot',{}) if getattr(self,'_latest_snapshot_inputs',None)==inputs(self) else {}
        for stock in cached.get('stocks',[]):
            info=next(i for i in self.watchlist if i['isin']==stock['isin']);state=self.user_state(info)
            position=state.get('position',{});levels=state.get('levels',{});q=position.get('quantity');avg=position.get('average_price')
            price=(stock.get('candles') or [{}])[-1].get('close');currency=stock.get('currency')
            valid=q is not None and avg is not None and price is not None and currency==position.get('currency',info.get('currency'))
            pnl=q*(price-avg) if valid else None;pct=(price/avg-1)*100 if valid and avg>0 and q>0 else None
            entry=f"{avg:g} {position.get('currency',info.get('currency','?'))}" if avg is not None else None
            level_currency=state.get('levels_currency',state.get('currency','?'))
            formatted_levels=[f'{levels[k]:g} {level_currency}' if levels.get(k) is not None else None for k in ('stop','target','support','resistance')]
            candle_at=(stock.get('candles') or [{}])[-1].get('at')
            returns=self.position_returns(info,position,pnl,currency)
            values=[info.get('symbol'),q,entry,price,currency,pnl,returns['dividends'],returns['total_return'],pct,*formatted_levels,format_engine(stock.get('trading_engine'))[0],stock.get('source'),candle_at]
            tree.insert('', 'end',iid=stock['isin'],values=['—' if v is None else f'{v:.2f}' if isinstance(v,float) else v for v in values])
            self.attach_logo(tree,stock['isin'],info)
        def edit():
            if tree.selection():
                self.symbol.set(next(i['name'] for i in self.watchlist if i['isin']==tree.selection()[0]));self.position_dialog()
        ttk.Button(real,text=tr('Изменить выбранную позицию'),command=edit).pack(pady=5)
        ttk.Label(real,text=tr('P/L — оценка по последней свече указанного источника, без комиссий. Не отчёт брокера.')).pack(pady=5)

    def decision_report(self,args):
        import json
        from decision_results import evaluate,summary,recorded_frame
        from commands import execute
        report=execute(self.store,'decision_journal',args);frames={}
        def model_frame(record):
            key=(record['isin'],json.dumps(record.get('price_identity',{}),sort_keys=True),json.dumps(record.get('history_session',{}),sort_keys=True))
            if key not in frames:frames[key]=recorded_frame(self.store,record)
            return frames[key]
        for record in report['decisions']:
            record['outcome']=evaluate(record,model_frame(record))
        report['summary']=summary(report['decisions']);report['scope']='last requested records; outcomes recomputed from current archive'
        # Model totals need the whole ledger, not just the last displayed page.
        from model_ledger import report as model_report
        with self.store.connect() as db:
            model_records=[{'id':r[0],'at':r[1],'isin':r[2],**json.loads(r[3])} for r in db.execute('SELECT id,at,isin,payload FROM decisions ORDER BY id')]
        if args.get('isin'):model_records=[r for r in model_records if r['isin']==args['isin']]
        report['model_portfolio']=model_report(model_records,model_frame)
        from dividends import model_returns
        report['model_portfolio']['policy'] += ' Dividends/Total Return рассчитаны отдельно по истории количества на ex-date; при неизвестной валюте, покрытии или FX результат отсутствует.'
        for row in report['model_portfolio']['positions']:
            info=next((i for i in self.watchlist if i['isin']==row['isin']),None)
            row.update(model_returns(row,self.dividends_for(info) if info else {}))
        for currency,totals in report['model_portfolio']['by_currency'].items():
            rows=[r for r in report['model_portfolio']['positions'] if r['identity']['currency']==currency]
            for key in ('dividends','total_return'):
                totals[key]=sum(r[key] for r in rows) if all(r[key] is not None for r in rows) else None
        return report

    def show_decision_journal(self):
        from commands import execute
        win=tk.Toplevel(self.root);win.title(tr('Решения ChatGPT — журнал'));win.geometry('1000x600')
        ttk.Label(win,text=tr('Рекомендации, не сделки. Оценки качества записываются отдельно; фактическая прибыль не предполагается.'),wraplength=940).pack(padx=10,pady=10)
        report=self.decision_report({'limit':500})
        today=report['summary']['today'];total=report['summary']['loaded_total']
        ttk.Label(win,text=tr('Сегодня записано {p0}, оценено {p1}. В последних 500: {p2} записей, {p3} оценок.\nСреднее направление сценария: {p4} %. Это не доходность портфеля.',p0=today['recorded'],p1=today['evaluated'],p2=total['recorded'],p3=total['evaluated'],p4=number(total['mean_scenario_direction_pct']) if total['mean_scenario_direction_pct'] is not None else tr('нет оценок')),wraplength=950).pack(padx=10)
        model=report['model_portfolio']
        from journal_presentation import model_summary, outcome_label, quality_label
        ttk.Label(win,text=model_summary(model),wraplength=950).pack(padx=10,pady=5)
        columns=('at','isin','signal','price','source','move','quality')
        tree=ttk.Treeview(win,columns=columns,show='headings',height=12)
        for key,label in zip(columns,('Время UTC','ISIN','Рекомендация','Цена','Источник','Изменение цены, %','Оценка')):tree.heading(key,text=tr(label));tree.column(key,width=125)
        tree.pack(fill='both',expand=True,padx=10)
        details=text_area(win)
        records={str(r['id']):r for r in report['decisions']}
        for key,r in records.items():
            reviews=r['reviews'];quality=reviews[-1]['classification'] if reviews else 'unassessed'
            move=r['outcome'].get('return_pct')
            tree.insert('','end',iid=key,values=(r['at'],r['isin'],r['signal'],r['price'] if r['price'] is not None else tr('нет цены'),r.get('source') or tr('неизвестен'),number(move) if move is not None else outcome_label(r['outcome']['status']),quality_label(quality)))
        def selected(_):
            if tree.selection():
                r=records[tree.selection()[0]]
                set_text(details,r['reason'] + '\n\n' + r.get('comment', '') + tr('\n\nЦена относится к свече: ') + str(r.get('price_at')) + tr('\nОценки:\n') + '\n'.join((x['at'] + ' — ' + x['classification'] + ': ' + x['reason'] for x in r['reviews'])))
                outcome=r['outcome'];description=outcome['reason']
                scenario=r.get('scenario')
                if scenario:
                    assessment=outcome.get('scenario',{})
                    description+=f"\n\nСценарий при записи: {scenario['horizon_hours']} ч.; цель {scenario['target']}, стоп/отмена {scenario['stop']} {scenario['currency']}.\nПроверка сценария: {assessment.get('reason','Нет сопоставимой истории')}\nУчитываются только полные наблюдаемые свечи; пропуски и порядок касаний внутри свечи не восстанавливаются."
                if outcome['status']=='evaluated':
                    description+=f"\nГоризонт: 24 часа, затем первое доступное закрытие.\nСвеча: {outcome['evaluated_bar_at']}\nЦена после: {outcome['price_after']:.2f} {outcome['currency']}\nИзменение цены: {outcome['return_pct']:+.2f}%\nДвижение в сторону сценария: {outcome['scenario_direction_pct']:+.2f}%\nЭто сравнение цен, без исполнения сделки и комиссий. Оно не определяет автоматически качество рекомендации."
                details.configure(state='normal');details.insert('end','\n\nПоследующее движение (пересчёт по архиву):\n'+description);details.configure(state='disabled')
        tree.bind('<<TreeviewSelect>>',selected)
        if not records:set_text(details,tr('Записанных рекомендаций пока нет.'))

    def update_ai_view(self):
        if not self.ai_window or not self.ai_window.winfo_exists():return
        last=self.bridge.last_client
        head=(tr('Связь через MCP подтверждена. Последнее обращение: ')+time.strftime('%d.%m.%Y %H:%M:%S',time.localtime(last))) if last else tr('Мост MCP запущен. В этом сеансе обращений ещё не было.')
        records=self.store.events('ai_response',1)
        text=head+tr('\n\nПродолжайте разговор в Codex: модель может читать состояние, источники и диагностику, обновлять данные, выбирать график и предлагать уровни с вашим подтверждением.\n\nДополнительный API-анализ: ')+(tr('не настроен; для общения через MCP ключ не нужен.') if self.ai_config['provider']=='local' else tr('настроен: ')+self.ai_config['provider'])
        if records:text+=tr('\n\nПоследний API-ответ (')+records[0]['at']+'):\n'+records[0]['text']
        text+=tr('\n\nЛокальные проверки по правилам:\n')+getattr(self,'local_report',tr('Ожидание снимка'))
        set_text(self.ai_text,text)

    def snapshot_stock(self,info):
        frame=self.frame_for(info,self.timeframe.get());state=self.user_state(info)
        s={'name':info['name'],'isin':info['isin'],'timeframe':self.timeframe.get(),'source_mode':self.source_mode(info),'error':self.feed_errors.get(info['isin']),'gettex_file_errors':self.store.setting('gettex_file_errors:'+info['isin'],[]),'candles':[],'count':len(frame),'position':copy.deepcopy(state.get('position',{}))}
        s['last_successful_gettex_fetch']=self.store.setting('gettex_last_success:'+info['isin'])
        if not frame.empty:
            calc=copy.deepcopy(state)
            if frame.attrs.get('currency')!=calc.get('levels_currency',calc.get('currency','EUR')) or state.get('levels_source',self.source_mode(info))!=frame.attrs.get('source'):calc['levels']={}
            enriched,result=self.analyse_cached(info,frame,calc);s.update(result);s.update(frame.attrs)
            # Pandas propagates attrs to every row: serializing 30 candles used
            # to copy the entire archive evidence 30 times. Export values only;
            # the unchanged DQ/identity/provenance are already in the snapshot.
            values=pd.DataFrame(enriched,copy=False).tail(30)
            s['candles']=[{'at':str(ts),**market.clean(row.to_dict())} for ts,row in values.iterrows()]
            s['coverage']={'start':str(frame.index[0]),'end':str(frame.index[-1])}
        history=self.store.setting('history_identity:'+info['isin'],{})
        if history:
            daily=self.store.load_series(info['isin'],'1D','proxy',history['symbol']);s['alternative_history']={**history,'daily_count':len(daily),'requires_explicit_selection':True}
        return s

    def snapshot(self,stock_consumer=None,stock_builder=None):
        stocks=[]
        for info in self.watchlist:
            s=stock_builder(info) if stock_builder else Desktop014.snapshot_stock(self,info)
            if stock_consumer is not None:stock_consumer(s,info)
            stocks.append(s)
        return {'schema_version':2,'version':'0.16-dev','stocks':stocks,'at':pd.Timestamp.now(tz='UTC').isoformat(),'database':'SQLite','ready':True,'selected':{'isin':self.instrument_cfg()['isin'],'timeframe':self.timeframe.get(),'period':self.period.get()},'ai':{'mcp_last_request':self.bridge.last_client,'api_provider':self.ai_config['provider'],'tunnel':self.tunnel_runtime.snapshot()},'execution':'analysis only'}

    def bridge_dispatch(self,action,args):
        from commands import ACTIONS, MUTATIONS, execute
        if action in ACTIONS:
            if action=='decision_journal':return self.decision_report(args)
            if action in MUTATIONS and not self.store.setting('mcp_allow_edits',False):
                raise ValueError('Enable application edits in Stock Monitor settings first. Broker trades are never supported.')
            if action in MUTATIONS and self.user_dirty:
                raise ValueError('Save or discard local drafts before remote edits.')
            result=execute(self.store,action,args,self.snapshot() if action=='record_decision' else None)
            if action in ('add_instrument','remove_instrument','update_position'):
                self.watchlist=self.store.instruments();self.persisted=copy.deepcopy(self.watchlist)
                self.symbol_box['values']=[i['name'] for i in self.watchlist]
                if self.symbol.get() not in self.symbol_box['values']:self.symbol.set(self.watchlist[0]['name'])
                self.refresh_chart();self.publish_bridge()
                if action=='add_instrument':
                    self.start_feed(next(i for i in self.watchlist if i['isin']==result['instrument']['isin']))
            return result
        if action=='select':
            info=next((i for i in self.watchlist if i['isin']==args.get('isin',self.instrument_cfg()['isin'])),None)
            if info is None:raise ValueError('Неизвестный ISIN')
            tf=args.get('timeframe',self.timeframe.get());period=ALIASES.get(args.get('period'),args.get('period',self.period.get()))
            if tf not in TF or period not in PERIODS:raise ValueError('Неподдерживаемый интервал/период')
            self.symbol.set(info['name']);self.timeframe.set(tf);self.period.set(period);self.refresh_chart();self.publish_bridge();return {'selected':info['isin'],'source_mode':self.source_mode(info)}
        if action=='set_levels':
            info=next((i for i in self.watchlist if i['isin']==args.get('isin')),None)
            if info is None:raise ValueError('Неизвестный ISIN')
            levels={k:args[k] for k in ('support','resistance','stop') if k in args}
            if not levels or any(isinstance(v,bool) or not isinstance(v,(int,float)) or not math.isfinite(v) or v<=0 for v in levels.values()):raise ValueError('Некорректные уровни')
            cid=args.get('_command_id');queue=self.bridge.commands
            source=self.source_mode(info);currency=info.get('currency','EUR')
            def approve():
                if not queue.proposal_valid(cid):
                    queue.proposal_result(cid,'EXPIRED',{'saved':False,'reason':'APPROVAL_EXPIRED_OR_PERMISSION_REVOKED'});return
                accepted=messagebox.askyesno(tr('Предложение Codex'),tr(info['name']+'\n'+str(levels)+'\nПрименить? Сохранение — кнопкой «Сохранить».'))
                if not accepted:
                    queue.proposal_result(cid,'REJECTED',{'saved':False});return
                current=next((i for i in self.watchlist if i['isin']==info['isin']),None)
                if not queue.proposal_valid(cid) or current is None or self.source_mode(current)!=source or current.get('currency','EUR')!=currency:
                    queue.proposal_result(cid,'EXPIRED',{'saved':False,'reason':'APPROVAL_CONTEXT_CHANGED'});return
                try:
                    draft=copy.deepcopy(self.user_state(current));draft.setdefault('levels',{}).update(levels)
                    draft['levels_source']=source;draft['levels_currency']=currency
                    self.drafts[info['isin']]=draft;self.user_dirty=True
                    queue.proposal_result(cid,'AWAITING_SAVE',{'isin':info['isin'],'levels':levels,'source':source,'currency':currency,'saved':False})
                    self.refresh_chart()
                except Exception:
                    queue.proposal_result(cid,'FAILED',{'saved':False,'reason':'DRAFT_APPLICATION_FAILED'})
            self.root.after(0,approve);return {'status':'awaiting user approval'}
        return super().bridge_dispatch(action,args)

    def check_updates(self,automatic=False):
        if DISTRIBUTION=='exe':
            if not automatic:messagebox.showinfo(tr('0.16 DEV'),tr('Изолированная рабочая версия. Публикация и установка отключены.'))
            return

        if DISTRIBUTION=='msix-store':
            if not automatic:
                import webbrowser
                webbrowser.open('ms-windows-store://downloadsandupdates')
            return
        if DISTRIBUTION=='msix-preview':
            if not automatic:messagebox.showinfo(tr('Обновление MSIX'),tr('Это локальный прототип упаковки. Канал Store ещё не подключён. Установщик GitHub здесь не запускается.'))
            return
        import updater
        if getattr(self,'updating',False):return
        self.updating=True;repo=self.store.setting('update_repository','swindler2010/StockMonitor')
        def failed(msg):
            self.updating=False;self.status.set(tr('Обновление: ') + msg)
            if getattr(self,'update_window',None) and self.update_window.winfo_exists():self.update_window.destroy()
            messagebox.showerror(tr('Не удалось обновить программу'),tr(str(msg)+'\nТекущая версия и данные сохранены. Можно повторить попытку.'))
        def found(release):
            self.updating=False
            if release is None:self.status.set(tr('Установлена актуальная версия 0.15'));return
            if not messagebox.askyesno(tr('Обновление'),tr('Доступна {version}. Скачать и установить?',version=release['version'])):return
            self.updating=True
            self.update_window=tk.Toplevel(self.root);self.update_window.title(tr('Обновление Stock Monitor'))
            self.update_window.geometry('460x140');self.update_window.transient(self.root)
            self.update_window.protocol('WM_DELETE_WINDOW',self.update_window.withdraw)
            self.update_text=tk.StringVar(value='Подключение к GitHub…')
            ttk.Label(self.update_window,textvariable=self.update_text,padding=15).pack()
            self.update_progress=ttk.Progressbar(self.update_window,maximum=release['size'],length=400);self.update_progress.pack(padx=15,pady=10)
            self.update_window.lift()
            last_notice=[0.0]
            def render_progress(size,total):
                text=f'Загружено {size/1048576:.1f} из {total/1048576:.1f} МБ ({size*100/total:.0f}%)'
                self.status.set(tr(text))
                if self.update_window.winfo_exists():self.update_text.set(text);self.update_progress['value']=size
            def progress(size,total):
                now=time.monotonic()
                if size==total or now-last_notice[0]>=.25:
                    last_notice[0]=now;self.events.put((render_progress,(size,total)))
            def ready(path):
                import subprocess
                self.updating=False
                if self.update_window.winfo_exists():self.update_window.destroy()
                if not self.confirm_close():return
                self.store.backup(DATA_DIR/'before-update.sqlite3')
                subprocess.Popen([str(path),'--wait-pid',str(os.getpid())],close_fds=True)
                self.close(confirmed=True)
            self.job(lambda:updater.download(release,DATA_DIR/'updates',progress),ready,failed)
        self.job(lambda:updater.latest(repo,'0.15'),found,failed)


def prefer_recent_official(info,tf,official,proxy,now=None,extended_session=False):
    """Preserve a newer real GETTEX bar instead of a closed-session proxy.

    Selection only: downstream DQ, full aggregates and price-age guards remain.
    """
    from timeframes import MINUTES,base_for,endpoints
    from eur_listings import confirmed_listing
    if tf not in MINUTES or official.empty:return False
    if official.attrs.get('source')!='gettex' or not confirmed_listing(info,official.attrs):return False
    clock=pd.Timestamp(now or pd.Timestamp.now(tz='UTC'))
    def last(frame,source):
        tail=frame.iloc[-4:].copy();tail.attrs={**frame.attrs,'timeframe':frame.attrs.get('timeframe') or base_for(tf,source)}
        ends=endpoints(tail);closed=ends[ends<=clock]
        return closed.max() if len(closed) else pd.NaT
    try:
        a=last(official,'gettex')
        if pd.isna(a):return False
        if proxy.empty or proxy.attrs.get('currency')!='EUR':return extended_session
        b=last(proxy,'proxy')
        if pd.isna(b):return extended_session
        # Stable owner-selected primary venue. Price age and DQ remain
        # independent gates, even when the reserve has a newer trading day.
        if extended_session:
            return True  # Primary GETTEX row; actual age/DQ remain mandatory.
        return bool(a>b)
    except (ValueError,KeyError,TypeError):return False
