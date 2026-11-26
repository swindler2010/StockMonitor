"""Actual asset sections in the main workspace; no orders or new calculations."""
import math
import tkinter as tk
from tkinter import ttk
from tkinter.scrolledtext import ScrolledText
from i18n import tr,language

SECTIONS={'overview':'Обзор','chart':'График','analysis':'Анализ','finances':'Финансы','reports':'Отчёты','news':'Новости','events':'События','forecast':'Прогноз аналитиков'}
IDENTITY=('isin','name','symbol','profile_symbol','history_symbol','venue','currency','kind','history_source','history_venue','timezone','unit','return_basis','volume_policy')

def identity(info):return tuple(info.get(k) for k in IDENTITY)

def load(app,info):
    """Read/format auxiliary records in the existing worker, never load a feed."""
    if info.get('kind')=='benchmark':
        from benchmark_display import information
        return dict(info=info,benchmark=information(app.store,info),technical=None)
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
    result={'technical':None,'info':info}
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


def overview_metrics(overview):
    from financial_format import format_value
    from card_view import MONEY,QUOTE
    rows=[]
    for key,label in [('marketCap','Капитализация'),('trailingPE','P/E TTM'),('trailingEps','EPS TTM'),('totalRevenue','Выручка'),('netIncomeToCommon','Чистая прибыль'),('totalDebt','Долг'),('totalCash','Денежные средства')]:
        currency=(overview.get('financialCurrency') or tr('Валюта неизвестна')) if key in MONEY else (overview.get('currency') or tr('Валюта неизвестна')) if key in QUOTE else None
        kind='price' if key=='trailingEps' else 'amount' if currency else 'number'
        value=format_value(overview.get(key),language(),kind=kind,currency=currency,compact=key not in ('trailingPE','trailingEps'),digits=2)
        raw=overview.get(key);exact=tr('Нет данных') if raw is None else str(raw)+(' '+currency if currency else '')
        rows.append((tr(label),value,exact))
    return rows


def statement_value(row,with_unit=False):
    from financial_format import format_value
    unit=row.get('unit');kind='percent' if unit=='ratio' else 'price' if unit=='per_share' else 'number'
    value=format_value(row.get('value'),language(),kind=kind,compact=kind=='number' and unit!='multiple',digits=2)
    if with_unit and unit!='ratio':value+=' '+str(row.get('currency') or tr(unit or '—'))
    return value


def calendar_text(data):
    from card_view import LABELS,DATES
    from financial_format import format_date,format_value
    from datetime import datetime,timezone
    if not isinstance(data,dict) or not data:return tr('События не получены от источника.')
    def value(raw,key):
        if isinstance(raw,(list,tuple)):return '; '.join(value(v,key) for v in raw) or tr('Нет данных')
        if raw is None:return tr('Нет данных')
        if key in DATES:
            if isinstance(raw,(int,float)) and not isinstance(raw,bool):
                try:return format_date(datetime.fromtimestamp(raw,tz=timezone.utc),language(),clock=True)
                except (ValueError,OverflowError,OSError):return tr('Нет данных')
            return format_date(raw,language())
        if isinstance(raw,(int,float)) and not isinstance(raw,bool):return format_value(raw,language(),kind='number',compact=True)
        return str(raw)
    return '\n'.join(tr(LABELS.get(k,k))+'\t'+value(v,k) for k,v in data.items())+'\n\n'+tr('Даты и прогнозы могут измениться')


def metadata_summary(metadata):
    from financial_format import format_date
    from fundamentals_view import freshness
    source=str(metadata.get('source') or '—')
    if source=='Yahoo Finance (через yfinance), не GETTEX':source='Yahoo Finance'
    labels={'CURRENT_CACHE':'Актуально','STALE':'Устарело','FUTURE_TIMESTAMP':'Дата в будущем','UNKNOWN':'Дата неизвестна'}
    state=freshness(metadata)
    return source+' · '+format_date(metadata.get('updated_at'),language(),clock=True)+' · '+tr(labels[state]),state


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
        ttk.Button(self.heading,text=tr('Обновить'),command=lambda:self.request(refresh=True,force=True)).pack(side='right')
        if section=='forecast' and self.context[0].get('kind')!='benchmark':
            from analyst_forecast_view import show,word
            ttk.Button(self.heading,text=word('expand'),command=lambda:show(self.app,dict(self.context[0]))).pack(side='right',padx=5)
        self.body=ttk.Frame(self.host,padding=12,style='DeskCard.TFrame');self.body.pack(fill='both',expand=True)
        self.note=ttk.Label(self.body,text=tr('Чтение сохранённых данных…'),style='DeskMuted.TLabel');self.note.pack(anchor='w',pady=8)
        self.request()
    def request(self,refresh=False,force=False):
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
            prior=getattr(self,'model',None)
            unchanged=(section=='forecast' and packet.get('_forecast_key') is not None and
                       prior is not None and prior.get('_forecast_key')==packet['_forecast_key'])
            self.model=packet
            if not unchanged:self.render(packet)
            if section=='forecast' and not refresh and info.get('kind')!='benchmark':
                from analyst_forecast import due
                if due(packet.get('forecast',{}),info):self.request(refresh=True)
        def failed(_):
            if not self.closed and serial==self.sequence and section==self.active and self.same(captured):
                for child in self.body.winfo_children():child.destroy()
                ttk.Label(self.body,text=tr('Данные временно недоступны. Повторите обновление.'),style='DeskMuted.TLabel',wraplength=600).pack(anchor='w')
        if section=='forecast':
            from analyst_forecast import load as forecast_load,presentation_key
            def work():
                value=forecast_load(self.app,info,refresh=refresh,force=force)
                return dict(info=info,forecast=value,technical=None,_forecast_key=presentation_key(value))
            key=('analyst-forecast',info['isin']) if refresh else ('workspace-section',id(self),serial)
        else:
            work=(lambda:technical(self.app,info,tf,period,cached)) if section=='analysis' else (lambda:load(self.app,info))
            key=('workspace-section',id(self),serial)
        if self.app.job(work,done,failed,lane='misc' if section=='forecast' and refresh else 'chart',key=key) is False:
            if section=='forecast' and self.model:
                from analyst_forecast_view import word
                ttk.Label(self.body,text=word('partial'),style='DeskMuted.TLabel').pack(anchor='w')
            else:failed(None)
        if self.timer is not None:self.app.root.after_cancel(self.timer)
        self.timer=self.app.root.after(60000,self.periodic)
    def periodic(self):self.timer=None;self.request()
    def text(self,text):
        style=ttk.Style(self.host);bg=style.lookup('Desk.TFrame','background');fg=style.lookup('Desk.TLabel','foreground')
        widget=ScrolledText(self.body,height=8,wrap='word',font=('Segoe UI',11),background=bg or '#0b1220',foreground=fg or '#e5e7eb',relief='flat',padx=12,pady=12)
        widget.pack(fill='both',expand=True);widget.insert('1.0',text);widget.configure(state='disabled',spacing1=2,spacing3=4)
        widget.bind('<Configure>',lambda e:widget.configure(tabs=(max(190,int(e.width*.6)),)))
        return widget
    def render(self,packet):
        for child in self.body.winfo_children():child.destroy()
        kind=self.active
        if kind=='analysis':self.analysis(packet);return
        if kind=='forecast':
            from analyst_forecast_view import Panel
            self.forecast_panel=Panel(self.body,self.app,packet['info'],packet.get('forecast',{}),compact=True);return
        if packet.get('info',{}).get('kind')=='benchmark':
            from benchmark_display import information_text
            self.text(information_text(packet['info'],packet['benchmark'],kind));return
        data=packet['data'];info=packet['info']
        actions=ttk.Frame(self.body,style='Desk.TFrame');actions.pack(side='bottom',fill='x',pady=(6,0))
        if kind=='overview':ttk.Button(actions,text=tr('Карточка'),command=self.app.show_card).pack(side='right')
        if kind in ('finances','reports'):ttk.Button(actions,text=tr('Подробная отчётность'),command=lambda:self.statements(info)).pack(side='right')
        from fundamentals_view import freshness
        metadata=packet['overview_metadata'] if kind=='overview' else packet['events_metadata'] if kind=='events' else data
        summary,state=metadata_summary(metadata)
        note=ttk.Label(self.body,text=summary,style='DeskMuted.TLabel',wraplength=740);note.pack(anchor='w',pady=(0,5))
        self.hint(note,tr('Источник')+': '+str(metadata.get('source') or '—')+'\n'+tr('Обновлено')+': '+str(metadata.get('updated_at') or '—')+'\n'+tr(freshness(metadata)))
        binding=ttk.Label(self.body,text=tr('Данные эмитента · ISIN не проверен'),style='DeskMuted.TLabel',font=('Segoe UI',9));binding.pack(anchor='w',pady=(0,8))
        self.hint(binding,tr('Привязка по настроенному тикеру; ISIN независимо не подтверждён'))
        if kind=='overview':self.overview(packet)
        elif kind=='finances':self.finances(packet)
        elif kind=='reports':self.reports(packet)
        elif kind=='events':
            self.text(calendar_text(packet['calendar']))
    def statements(self,info):
        from fundamentals_panel import show
        show(self.app,info)
    def hint(self,widget,text):
        self.app.tooltip(widget,text)
        def cleanup(event):
            tip=getattr(widget,'tip',None)
            if event.widget is widget and tip is not None:
                try:tip.destroy()
                except tk.TclError:pass
        widget.bind('<Destroy>',cleanup,add='+')
    def overview(self,packet):
        info=packet['info']
        context=str(info.get('symbol') or info.get('profile_symbol') or '—')+' · '+str(info.get('venue') or '—')+' / '+str(info.get('currency') or '—')+' · '+info['isin']
        ttk.Label(self.body,text=context,style='DeskMuted.TLabel',font=('Segoe UI',10)).pack(anchor='w',pady=(2,10))
        footer=ttk.Label(self.body,text=tr('Суммы округлены · точные значения при наведении'),style='DeskMuted.TLabel',font=('Segoe UI',9));footer.pack(side='bottom',anchor='w',pady=(6,0))
        outer=ttk.Frame(self.body,style='Desk.TFrame');outer.pack(fill='both',expand=True)
        style=ttk.Style(self.body);canvas=tk.Canvas(outer,highlightthickness=0,background=style.lookup('Desk.TFrame','background'))
        scroll=ttk.Scrollbar(outer,command=canvas.yview,style='Desk.Vertical.TScrollbar');scroll.pack(side='right',fill='y');canvas.pack(side='left',fill='both',expand=True);canvas.configure(yscrollcommand=scroll.set)
        table=ttk.Frame(canvas,style='Desk.TFrame',padding=(8,0));window=canvas.create_window(0,0,window=table,anchor='nw')
        table.columnconfigure(0,weight=1);table.columnconfigure(1,weight=1)
        style.configure('DeskSummary.TSeparator',background=style.lookup('DeskCard.TFrame','bordercolor') or '#26384c')
        table.bind('<Configure>',lambda _:canvas.configure(scrollregion=canvas.bbox('all')))
        canvas.bind('<Configure>',lambda e:canvas.itemconfigure(window,width=e.width))
        for row,(label,value,exact) in enumerate(overview_metrics(packet['overview'])):
            name=ttk.Label(table,text=label,style='DeskMuted.TLabel',font=('Segoe UI',10));name.grid(row=row*2,column=0,sticky='w',pady=3)
            amount=ttk.Label(table,text=value,style='Desk.TLabel',font=('Segoe UI',11,'bold'),anchor='e');amount.grid(row=row*2,column=1,sticky='e',padx=(14,8),pady=3)
            self.hint(amount,label+': '+exact)
            ttk.Separator(table,style='DeskSummary.TSeparator').grid(row=row*2+1,column=0,columnspan=2,sticky='ew')
            for widget in (name,amount):widget.bind('<MouseWheel>',lambda e:canvas.yview_scroll(-int(e.delta/120),'units'))
        canvas.bind('<MouseWheel>',lambda e:canvas.yview_scroll(-int(e.delta/120),'units'))
    def analysis(self,packet):
        from trading_view import format_engine
        from financial_format import format_value,format_date
        technical=packet.get('technical')
        if technical is None:
            title,body=format_engine(None);self.text(title+'\n\n'+body);return
        index=packet.get('info',{}).get('kind')=='benchmark'
        title,body=(tr('ИНДЕКС'),tr('Рыночный контекст; торговые сигналы не рассчитываются.')) if index else format_engine(technical['engine'])
        source=technical['source'];lang=language()
        lines=[title,technical['tf']+' · '+str(source.get('source') or '—')+' / '+str(source.get('venue') or '—')+' / '+str(source.get('currency') or '—'),
               tr('Последняя свеча')+': '+format_date(technical['latest'],language(),clock=True,assume_zone='Europe/Berlin'),tr('Закрытых свечей')+': '+str(technical['closed']),body,'',tr('Индикаторы последней свечи; формирующаяся свеча предварительная')]
        labels={'ema10':'EMA 10','ema30':'EMA 30','rsi':'RSI (14)','atr':'ATR (14)','macd':'MACD','macd_signal':'Signal MACD','volume':'Объём','volume_mean20':'Средний объём'}
        for key,label in labels.items():
            if index and key in ('volume','volume_mean20'):continue
            lines.append(tr(label)+'\t'+format_value(technical['indicators'].get(key),lang,kind='number',compact=key in ('volume','volume_mean20'),digits=2 if key in ('volume','volume_mean20','ema10','ema30','rsi') else 3))
        for key,label in [('support','Поддержка'),('resistance','Сопротивление'),('stop','Стоп сценария')]:
            if index and key=='stop':continue
            lines.append(tr(label)+'\t'+format_value(technical['levels'].get(key),lang,kind='number',digits=3))
        self.text('\n'.join(lines))
    def finances(self,packet):
        from financial_format import format_value,format_date
        from localized_widgets import CanonicalCombobox
        frequency=tk.StringVar(self.body,self._finance_frequency)
        box=CanonicalCombobox(self.body,textvariable=frequency,values=['quarterly','yearly','trailing','valuation'],state='readonly',width=20);box.pack(anchor='w',pady=(0,8))
        empty=ttk.Label(self.body,style='DeskMuted.TLabel');empty.pack(side='bottom',anchor='w',fill='x',pady=6)
        columns=('metric','period','value','currency','state');table=ttk.Frame(self.body);table.pack(fill='both',expand=True)
        tree=ttk.Treeview(table,columns=columns,show='headings',style='Desk.Treeview')
        for key,label,width in [('metric','Показатель',200),('period','Период',115),('value','Значение',140),('currency','Валюта',75),('state','Статус',150)]:tree.heading(key,text=tr(label));tree.column(key,width=width,stretch=key in ('metric','state'))
        vs=ttk.Scrollbar(table,command=tree.yview);vs.pack(side='right',fill='y');hs=ttk.Scrollbar(table,orient='horizontal',command=tree.xview);hs.pack(side='bottom',fill='x');tree.configure(yscrollcommand=vs.set,xscrollcommand=hs.set);tree.pack(fill='both',expand=True)
        def draw(_=None):
            self._finance_frequency=frequency.get();tree.delete(*tree.get_children())
            rows=packet['valuation'] if frequency.get()=='valuation' else [r for r in packet['rows'] if r.get('frequency')==frequency.get()]
            for row in rows:
                tree.insert('','end',values=(tr(row.get('label') or row.get('metric') or '—'),format_date(row.get('period_end'),language()) if row.get('period_end') else tr('Период не указан'),
                    statement_value(row),row.get('currency') or '—',tr(row.get('cache_status') or row.get('status') or '—')))
            empty.configure(text='' if rows else tr('Отчётность ещё не загружена. Откройте подробную отчётность для обновления.'))
        def inspect(_=None):
            selected=tree.selection()
            if not selected:return
            item=tree.index(selected[0]);rows=packet['valuation'] if frequency.get()=='valuation' else [r for r in packet['rows'] if r.get('frequency')==frequency.get()]
            if item<len(rows):
                row=rows[item];empty.configure(text=tr('Значение источника')+': '+str(row.get('value'))+' '+str(row.get('currency') or tr(row.get('unit') or '—')))
        tree.bind('<<TreeviewSelect>>',inspect)
        box.bind('<<ComboboxSelected>>',draw);draw()
    def reports(self,packet):
        from card_view import reports
        from financial_format import format_value,format_date
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
                period=row.get('period_end');lines.append('\n'+tr('Период')+': '+(format_date(period,language()) if period else tr('Период не указан')))
            lines.append(tr(row.get('label') or row.get('metric') or '—')+'\t'+statement_value(row,with_unit=True))
        if packet['filings']:
            from news_engine import safe_url
            lines.append('\n'+tr('Публикации эмитента:'))
            for filing in packet['filings']:
                lines.append(format_date(filing.get('date'),language())+' · '+str(filing.get('type') or '—')+'\n'+safe_url(filing.get('edgarUrl') or filing.get('url')))
        self.text('\n'.join(lines))
    def close(self):
        self.closed=True;self.sequence+=1
        for timer in (self.timer,self.engine_timer):
            if timer is not None:
                try:self.app.root.after_cancel(timer)
                except tk.TclError:pass
        if self.news is not None:self.news.close();self.news=None
