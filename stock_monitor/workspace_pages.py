"""Actual asset sections in the main workspace; no orders or new calculations."""
import math
import tkinter as tk
from tkinter import ttk
from tkinter.scrolledtext import ScrolledText
from i18n import tr,language

SECTIONS={'overview':'Обзор','chart':'График','analysis':'Анализ','finances':'Финансы','reports':'Отчёты','news':'Новости','events':'События'}
IDENTITY=('isin','name','symbol','profile_symbol','history_symbol','venue','currency','kind')

def identity(info):return tuple(info.get(k) for k in IDENTITY)

def load(app,info):
    """Read/format auxiliary records in the existing worker, never load a feed."""
    from fundamentals_view import cache_matches,legacy_card,overview_rows,freshness
    card=app.store.setting('card:'+info['isin'],{})
    symbol=info.get('profile_symbol') or info.get('history_symbol')
    if not isinstance(card,dict) or not symbol or card.get('symbol')!=symbol:card={}
    overview=card.get('overview') or {}
    if not isinstance(overview,dict) or overview.get('symbol') not in (None,symbol):card={};overview={}
    data=app.store.setting('fundamentals:'+info['isin'],{})
    if not cache_matches(data,info) or data.get('status')=='IDENTITY_MISMATCH':data={}
    overview_metadata={'source':card.get('source'),'updated_at':card.get('fetched_at')} if overview else data
    if not overview:overview=data.get('overview') or {}
    if not isinstance(overview,dict) or overview.get('symbol') not in (None,symbol):overview={}
    if not data:
        data={'metrics':legacy_card(card),'valuation_metrics':overview_rows(overview,card.get('fetched_at'),card.get('source','UNKNOWN')),
              'source':card.get('source'),'updated_at':card.get('fetched_at'),'status':'LEGACY_SAVED_QUARTERLY' if card else 'NOT_LOADED'}
    rows=[r for r in data.get('metrics',[]) if isinstance(r,dict)]
    valuation=[r for r in data.get('valuation_metrics',[]) if isinstance(r,dict)]
    calendar=card.get('events') or data.get('calendar') or {}
    events_metadata={'source':card.get('source'),'updated_at':card.get('fetched_at')} if card.get('events') else data
    auto=app.store.setting('company_events:'+info['isin'],{})
    if cache_matches(auto,info):
        if not rows:
            rows=[r for r in auto.get('reports',[]) if isinstance(r,dict)]
            if rows:data={**data,'source':auto.get('source') or 'Yahoo Finance','updated_at':auto.get('reports_fetched_at'),'status':auto.get('reports_status') or 'AVAILABLE'}
        if auto.get('calendar'):
            calendar=auto['calendar'];events_metadata={'source':auto.get('source'),'updated_at':auto.get('calendar_fetched_at')}
    filings=[r for r in card.get('filings',[]) if isinstance(r,dict)] if isinstance(card.get('filings'),list) else []
    result=dict(info=info,card=card,overview=overview,data=data,rows=rows,valuation=valuation,calendar=calendar,filings=filings,
                freshness=freshness(data),technical=None,overview_metadata=overview_metadata,events_metadata=events_metadata)
    return result


def technical(app,info,tf,period,cached):
    """Reuse the accepted calculation with the same revision/file guards."""
    result={'technical':None}
    if not cached:return result
    from snapshot_worker import ALIASES
    values,prepared=cached
    selected=next((i for i in values.get('watchlist',[]) if i.get('name')==values.get('symbol')),{})
    if identity(selected)!=identity(info):return result
    if values.get('symbol')!=info['name'] or values.get('timeframe')!=tf or values.get('period')!=ALIASES.get(period,period):return result
    token=prepared.get('_input_revisions');cache=getattr(app.store,'read_cache',None)
    try:
        if token is None or cache is None or not cache.current(token):return result
    except Exception:return result
    from partial_chart import current
    files=prepared.get('frame').attrs.get('partial_chart',{}).get('files',[]) if prepared.get('frame') is not None else []
    try:
        if not current(files):return result
    except Exception:return result
    analysis=prepared.get('analysis');frame=prepared.get('frame')
    if frame is None or not len(frame):return result
    stats={};indicators={};latest=None;count=0;levels={}
    if analysis and len(analysis[0]):
        import pandas as pd
        numeric=pd.DataFrame(analysis[0],copy=False);row=numeric.iloc[-1]
        for key in ('ema10','ema30','rsi','atr','macd','macd_signal','volume','volume_mean20'):
            try:
                value=float(row.get(key))
                if math.isfinite(value):indicators[key]=value
            except (ValueError,TypeError):pass
        levels={k:analysis[1].get(k) for k in ('support','resistance','stop')}
        count=analysis[1].get('closed_bar_count',0)
    latest=str(frame.index[-1]);stats={k:frame.attrs.get(k) for k in ('source','venue','currency')}
    result['technical']=dict(engine=prepared.get('trading_engine'),indicators=indicators,levels=levels,closed=count,
        bars=len(frame),latest=latest,source=stats,token=token,files=files,prepared=prepared,tf=tf)
    return result


class Pages:
    def __init__(self,screen):
        self.screen=screen;self.app=screen.app;self.active='chart';self.sequence=0;self.model=None;self.context=None
        self.closed=False;self.timer=None;self.engine_timer=None;self.news=None;self._finance_frequency='quarterly'
        self.host=ttk.Frame(screen.workspace,style='Desk.TFrame');self.buttons={}
    def capture(self):
        app=self.app;info=next((i for i in app.watchlist if i['name']==app.symbol.get()),{})
        item={k:info.get(k) for k in IDENTITY}
        return item,app.timeframe.get(),app.period.get(),language()
    def same(self,capture):
        now=self.capture()
        return identity(now[0])==identity(capture[0]) and now[1:]==capture[1:]
    def context_changed(self):
        if self.active!='chart' and self.context!=self.capture():self.select(self.active,force=True)
    def engine_changed(self):
        if self.active!='analysis' or self.closed or self.engine_timer is not None:return
        def again():self.engine_timer=None;self.request()
        self.engine_timer=self.app.root.after(100,again)
    def clear(self):
        if self.news is not None:self.news.close();self.news=None
        for child in self.host.winfo_children():child.destroy()
    def select(self,section,force=False):
        if section not in SECTIONS:raise ValueError('UNKNOWN_WORKSPACE_SECTION')
        if self.closed:return
        if section==self.active and not force:
            if section=='chart':self.host.place_forget();self.app.canvas.get_tk_widget().focus_set()
            return
        self.active=section;self.sequence+=1;self.context=self.capture();self.model=None;self.clear()
        for key,button in self.buttons.items():button.configure(style='DeskActive.TButton' if key==section else 'TButton')
        if section=='chart':self.host.place_forget();self.app.canvas.get_tk_widget().focus_set();return
        # Overlay instead of replacing/unmapping/resizing the chart canvas.
        self.host.place(in_=self.app.canvas.get_tk_widget(),x=0,y=0,relwidth=1,relheight=1);self.host.lift()
        if section=='news':
            from news_dashboard import Dialog
            self.news=Dialog(self.app,parent=self.host);return
        self.heading=ttk.Frame(self.host,padding=(15,10),style='DeskCard.TFrame');self.heading.pack(fill='x',pady=(3,8))
        self.title=ttk.Label(self.heading,text=tr(SECTIONS[section])+' • '+str(self.context[0].get('name') or ''),style='DeskTitle.TLabel');self.title.pack(side='left')
        ttk.Button(self.heading,text=tr('Обновить'),command=self.request).pack(side='right')
        self.body=ttk.Frame(self.host,padding=12,style='DeskCard.TFrame');self.body.pack(fill='both',expand=True)
        self.note=ttk.Label(self.body,text=tr('Чтение сохранённых данных…'),style='DeskMuted.TLabel');self.note.pack(anchor='w',pady=8)
        self.request()
    def request(self):
        if self.closed or self.active in ('chart','news'):return
        captured=self.capture();self.context=captured;self.sequence+=1;serial=self.sequence;section=self.active
        info,tf,period,lang=captured
        if not info.get('isin'):return
        cached=getattr(self.app,'_accepted_trading_chart',None) if section=='analysis' else None
        if cached:
            from chart_worker import capture
            try:
                if cached[0]!=capture(self.app):cached=None
            except Exception:cached=None
        def done(packet):
            if self.closed or self.app.closing or serial!=self.sequence or section!=self.active or not self.same(captured):return
            technical=packet.get('technical')
            if technical:
                from partial_chart import current
                cache=getattr(self.app.store,'read_cache',None)
                actual=getattr(self.app,'_accepted_trading_chart',None)
                from chart_worker import capture
                try:
                    valid=(actual and actual[1] is technical['prepared'] and actual[0]==capture(self.app)
                           and cache is not None and cache.current(technical['token']) and current(technical['files']))
                except Exception:valid=False
                if not valid:packet['technical']=None
            self.model=packet;self.render(packet)
        def failed(_):
            if not self.closed and serial==self.sequence and section==self.active and self.same(captured):
                for child in self.body.winfo_children():child.destroy()
                ttk.Label(self.body,text=tr('Данные временно недоступны. Повторите обновление.'),style='DeskMuted.TLabel',wraplength=600).pack(anchor='w')
        work=(lambda:technical(self.app,info,tf,period,cached)) if section=='analysis' else (lambda:load(self.app,info))
        if self.app.job(work,done,failed,lane='chart',key=('workspace-section',id(self),serial)) is False:failed(None)
        if self.timer is not None:self.app.root.after_cancel(self.timer)
        self.timer=self.app.root.after(60000,self.periodic)
    def periodic(self):self.timer=None;self.request()
    def text(self,text):
        style=ttk.Style(self.host);bg=style.lookup('Desk.TFrame','background');fg=style.lookup('Desk.TLabel','foreground')
        widget=ScrolledText(self.body,height=8,wrap='word',font=('Segoe UI',11),background=bg or '#0b1220',foreground=fg or '#e5e7eb',relief='flat',padx=12,pady=12)
        widget.pack(fill='both',expand=True);widget.insert('1.0',text);widget.configure(state='disabled');return widget
    def render(self,packet):
        for child in self.body.winfo_children():child.destroy()
        kind=self.active
        if kind=='analysis':self.analysis(packet);return
        data=packet['data'];info=packet['info']
        actions=ttk.Frame(self.body,style='Desk.TFrame');actions.pack(side='bottom',fill='x',pady=(6,0))
        if kind=='overview':ttk.Button(actions,text=tr('Полная карточка в отдельном окне'),command=self.app.show_card).pack(side='right')
        if kind in ('finances','reports'):ttk.Button(actions,text=tr('Подробная отчётность'),command=lambda:self.statements(info)).pack(side='right')
        from fundamentals_view import freshness
        metadata=packet['overview_metadata'] if kind=='overview' else packet['events_metadata'] if kind=='events' else data
        source=str(metadata.get('source') or '—');at=str(metadata.get('updated_at') or '—')
        ttk.Label(self.body,text=tr('Источник')+': '+source+' · '+tr('Обновлено')+': '+at+' · '+tr(freshness(metadata)),style='DeskMuted.TLabel',wraplength=700).pack(anchor='w',pady=(0,7))
        ttk.Label(self.body,text=tr('Привязка по настроенному тикеру; ISIN независимо не подтверждён'),style='DeskMuted.TLabel',wraplength=700).pack(anchor='w',pady=(0,7))
        if kind=='overview':self.overview(packet)
        elif kind=='finances':self.finances(packet)
        elif kind=='reports':self.reports(packet)
        elif kind=='events':
            from card_view import events
            self.text(events({'events':packet['calendar']}))
    def statements(self,info):
        from fundamentals_panel import show
        show(self.app,info)
    def overview(self,packet):
        from card_view import fundamentals
        info=packet['info'];ov=packet['overview']
        lines=[info['name'],str(info.get('symbol') or info.get('profile_symbol') or '—')+' · '+info['isin'],
               str(info.get('venue') or '—')+' / '+str(info.get('currency') or '—'),'',
               fundamentals(ov,{'marketCap':'Капитализация','trailingPE':'P/E TTM','trailingEps':'EPS TTM','totalRevenue':'Выручка','netIncomeToCommon':'Чистая прибыль','totalDebt':'Долг','totalCash':'Денежные средства'})]
        self.text('\n'.join(lines))
    def analysis(self,packet):
        from trading_view import format_engine
        from financial_format import format_value
        technical=packet.get('technical')
        if technical is None:
            title,body=format_engine(None);self.text(title+'\n\n'+body);return
        title,body=format_engine(technical['engine']);source=technical['source'];lang=language()
        lines=[title,technical['tf']+' · '+str(source.get('source') or '—')+' / '+str(source.get('venue') or '—')+' / '+str(source.get('currency') or '—'),
               tr('Последняя свеча')+': '+technical['latest'],tr('Закрытых свечей')+': '+str(technical['closed']),body,'',tr('Индикаторы последней свечи; формирующаяся свеча предварительная')]
        labels={'ema10':'EMA 10','ema30':'EMA 30','rsi':'RSI (14)','atr':'ATR (14)','macd':'MACD','macd_signal':'Signal MACD','volume':'Объём','volume_mean20':'Средний объём'}
        for key,label in labels.items():lines.append(tr(label)+': '+format_value(technical['indicators'].get(key),lang,kind='number',digits=3))
        for key,label in [('support','Поддержка'),('resistance','Сопротивление'),('stop','Стоп сценария')]:lines.append(tr(label)+': '+format_value(technical['levels'].get(key),lang,kind='number',digits=3))
        self.text('\n\n'.join(lines))
    def finances(self,packet):
        from financial_format import format_value
        from localized_widgets import CanonicalCombobox
        frequency=tk.StringVar(self.body,self._finance_frequency)
        box=CanonicalCombobox(self.body,textvariable=frequency,values=['quarterly','yearly','trailing','valuation'],state='readonly',width=20);box.pack(anchor='w',pady=(0,8))
        columns=('metric','period','value','currency','state');table=ttk.Frame(self.body);table.pack(fill='both',expand=True)
        tree=ttk.Treeview(table,columns=columns,show='headings',style='Desk.Treeview')
        for key,label,width in [('metric','Показатель',200),('period','Период',115),('value','Значение',140),('currency','Валюта',75),('state','Статус',150)]:tree.heading(key,text=tr(label));tree.column(key,width=width,stretch=key in ('metric','state'))
        vs=ttk.Scrollbar(table,command=tree.yview);vs.pack(side='right',fill='y');hs=ttk.Scrollbar(table,orient='horizontal',command=tree.xview);hs.pack(side='bottom',fill='x');tree.configure(yscrollcommand=vs.set,xscrollcommand=hs.set);tree.pack(fill='both',expand=True)
        empty=ttk.Label(self.body,style='DeskMuted.TLabel');empty.pack(anchor='w',pady=6)
        def draw(_=None):
            self._finance_frequency=frequency.get();tree.delete(*tree.get_children())
            rows=packet['valuation'] if frequency.get()=='valuation' else [r for r in packet['rows'] if r.get('frequency')==frequency.get()]
            for row in rows:
                tree.insert('','end',values=(tr(row.get('label') or row.get('metric') or '—'),row.get('period_end') or tr('Период не указан'),
                    format_value(row.get('value'),language(),kind='percent' if row.get('unit')=='ratio' else 'number',digits=3),row.get('currency') or '—',tr(row.get('cache_status') or row.get('status') or '—')))
            empty.configure(text='' if rows else tr('Отчётность ещё не загружена. Откройте подробную отчётность для обновления.'))
        box.bind('<<ComboboxSelected>>',draw);draw()
    def reports(self,packet):
        from card_view import reports
        from financial_format import format_value
        rows=[r for r in packet['rows'] if r.get('frequency')=='quarterly']
        if not rows:
            fields=('date','type','url','edgarUrl')
            card={**packet['card'],'filings':[{k:v for k,v in filing.items() if k in fields} for filing in packet['filings']]}
            self.text(reports(card));return
        lines=[tr('Квартальная отчётность. Валюта: ')+str(packet['data'].get('financial_currency') or tr('Валюта указана у каждого показателя')),
               tr('Показатели на акцию и количество акций имеют собственные единицы; пересчёта в EUR нет.')]
        period=None
        for row in sorted(rows,key=lambda r:str(r.get('period_end') or ''),reverse=True):
            if period!=row.get('period_end'):
                period=row.get('period_end');lines.append('\n'+tr('Период')+': '+str(period or tr('Период не указан')))
            lines.append(tr(row.get('label') or row.get('metric') or '—')+': '+format_value(row.get('value'),language(),kind='number',digits=3)+' '+str(row.get('currency') or tr(row.get('unit') or '—')))
        if packet['filings']:
            from news_engine import safe_url
            lines.append('\n'+tr('Публикации эмитента:'))
            for filing in packet['filings']:
                lines.append(str(filing.get('date') or '—')+' · '+str(filing.get('type') or '—')+'\n'+safe_url(filing.get('edgarUrl') or filing.get('url')))
        self.text('\n'.join(lines))
    def close(self):
        self.closed=True;self.sequence+=1
        for timer in (self.timer,self.engine_timer):
            if timer is not None:
                try:self.app.root.after_cancel(timer)
                except tk.TclError:pass
        if self.news is not None:self.news.close();self.news=None
