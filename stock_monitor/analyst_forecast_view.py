"""Analyst forecast screen in the Stock Monitor theme; all data prepared off Tk."""
import math
import tkinter as tk
from tkinter import ttk
from datetime import date, timedelta, datetime, timezone
from i18n import language
from financial_format import format_value,format_date
from analyst_forecast import state,moment,TTL

WORDS={
 'forecast':('Прогноз','Prognose','Forecast'),
 'price':('Прогноз цены · 12 месяцев','Kursziele · 12 Monate','Price targets · 12 months'),
 'rating':('Аналитический рейтинг','Analystenbewertung','Analyst rating'),
 'recent':('Последние решения аналитиков','Letzte Analystenentscheidungen','Latest analyst decisions'),
 'unknown':('Нет данных от источника','Keine Quelldaten','No data from provider'),
 'strongbuy':('Активно покупать','Starker Kauf','Strong buy'),'buy':('Покупать','Kaufen','Buy'),
 'hold':('Держать','Halten','Hold'),'sell':('Продавать','Verkaufen','Sell'),
 'strongsell':('Активно продавать','Starker Verkauf','Strong sell'),
 'mean':('Средняя','Mittel','Average'),'high':('Максимум','Maximum','Maximum'),
 'low':('Минимум','Minimum','Minimum'),'current':('Цена источника','Quellkurs','Provider price'),
 'analysts':('аналитиков','Analysten','analysts'),'month':('Текущий месяц','Aktueller Monat','Current month'),
 'date':('Дата','Datum','Date'),'firm':('Компания','Unternehmen','Firm'),
 'analyst':('Аналитик','Analyst','Analyst'),'action':('Действие','Aktion','Action'),
 'grade':('Изменение рейтинга','Bewertungsänderung','Rating change'),
 'target':('Изменение цели','Kurszieländerung','Target change'),
 'up':('Повышение','Hochstufung','Upgrade'),'down':('Понижение','Herabstufung','Downgrade'),
 'main':('Подтверждение','Bestätigung','Reiteration'),'init':('Начало покрытия','Analysebeginn','Initiation'),
 'reit':('Подтверждение','Bestätigung','Reiteration'),
 'stale':('Устарело','Veraltet','Stale'),'available':('Обновлено','Aktualisiert','Updated'),
 'future':('Дата источника в будущем','Quelldatum in der Zukunft','Source timestamp is in the future'),
 'units':('Исходная валюта поставщика · без пересчёта в EUR','Quellwährung · keine EUR-Umrechnung','Provider currency · no EUR conversion'),
 'auxiliary':('Оценки аналитиков — отдельно от торгового сигнала СМ. Привязка по настроенному тикеру; ISIN источником не подтверждён.',
              'Analystenschätzungen sind vom SM-Handelssignal getrennt. Zuordnung über den konfigurierten Ticker; ISIN nicht vom Anbieter bestätigt.',
              'Analyst estimates are separate from the SM trading signal. Bound to configured ticker; ISIN not confirmed by provider.'),
 'horizon':('Цели на 12 месяцев; точные даты целей источник не сообщил.',
             '12-Monats-Ziele; genaue Zieldaten fehlen beim Anbieter.',
             '12-month targets; exact target dates not supplied by provider.'),
 'history':('История · до 2 лет','Verlauf · bis 2 Jahre','History · up to 2 years'),
 'historymissing':('История в этой валюте не получена','Kein Verlauf in dieser Währung','No history in this currency'),
 'targetmissing':('Цели не предоставлены или валюта не подтверждена','Keine Ziele oder unbestätigte Währung','Targets unavailable or currency unconfirmed'),
 'period':('Снимок рекомендаций за текущий месяц; периоды не суммируются.',
           'Empfehlungen des aktuellen Monats; Zeiträume werden nicht addiert.',
           'Current-month recommendation snapshot; periods are not added together.'),
 'uncurrency':('Валюта цели не указана источником','Zielwährung fehlt beim Anbieter','Target currency not supplied'),
 'partial':('Часть данных недоступна; прежние значения сохранены','Einige Daten fehlen; frühere Werte bleiben erhalten','Some data unavailable; previous values retained'),
 'benchmark':('Аналитический прогноз предназначен для акций','Analystenprognosen gelten für Aktien','Analyst forecasts apply to stocks'),
 'expand':('Открыть отдельно','Separat öffnen','Open separately'),
 'refresh':('Обновить','Aktualisieren','Refresh'),'loading':('Загрузка прогноза…','Prognose wird geladen…','Loading forecast…'),
 'identitymissing':('Не подтверждён тикер для прогноза: источник не установил точное соответствие ISIN.', 'Ticker für Prognose nicht bestätigt: keine eindeutige ISIN-Zuordnung.', 'Forecast ticker unconfirmed: provider could not verify the exact ISIN.'),
 'resolved':('Тикер прогноза найден по ISIN · торговый источник не изменён', 'Prognoseticker über ISIN ermittelt · Handelsquelle unverändert', 'Forecast ticker resolved by ISIN · trading source unchanged'),
 'cooldown':('Обновление не чаще одного раза в 15 минут','Aktualisierung frühestens nach 15 Minuten','Refresh at most once per 15 minutes'),
 'raw':('Оригинал рейтингов: ','Bewertungen im Original: ','Original ratings: ')
}

def word(key):return WORDS.get(key,(key,key,key))[{'ru':0,'de':1,'en':2}.get(language(),0)]

def price(value,currency=None):return format_value(value,language(),kind='price',currency=currency,digits=2)

def stamp(packet,key=None):
    at=packet.get(key+'_at') if key else packet.get('updated_at')
    dt=moment(at);now=datetime.now(timezone.utc)
    status=word('unknown') if dt is None else word('future') if dt>now else word('stale') if (now-dt).total_seconds()>=TTL or (key in packet.get('errors',{}) if key else packet.get('errors')) else word('available')
    return status+' · '+format_date(at,language(),clock=True)

def rating_word(score):
    if score is None:return word('unknown')
    return word('strongbuy' if score>=.6 else 'buy' if score>=.2 else 'strongsell' if score<=-.6 else 'sell' if score<=-.2 else 'hold')

def grade(raw):
    known={'Strong Buy':'strongbuy','Strong Sell':'strongsell','Buy':'buy','Hold':'hold','Sell':'sell','Neutral':'hold'}
    extra={
       'Outperform':('Выше рынка','Übergewichten','Outperform'),
       'Overweight':('Увеличивать долю','Übergewichten','Overweight'),
       'Underperform':('Ниже рынка','Untergewichten','Underperform'),
       'Underweight':('Сокращать долю','Untergewichten','Underweight'),
       'Equal-Weight':('Наравне с рынком','Gleichgewichten','Equal-weight'),
       'Market Perform':('Наравне с рынком','Marktkonform','Market perform'),
       'Peer Perform':('Наравне с сектором','Sektorkonform','Peer perform'),
       'Sector Perform':('Наравне с сектором','Sektorkonform','Sector perform')}
    if raw in known:return word(known[raw])
    return extra[raw][{'ru':0,'de':1,'en':2}.get(language(),0)] if raw in extra else raw or '—'

class Plot(tk.Canvas):
    def __init__(self,parent,packet,**kwargs):
        self.packet=packet;self.pending=None
        super().__init__(parent,highlightthickness=0,height=270,**kwargs)
        self.bind('<Configure>',self.schedule);self.bind('<Destroy>',self.cancel,add='+')
    def schedule(self,_=None):
        if self.pending is None:self.pending=self.after_idle(self.paint)
    def cancel(self,event):
        if event.widget is self and self.pending is not None:
            self.after_cancel(self.pending);self.pending=None
    def paint(self):
        self.pending=None
        if not self.winfo_exists():return
        self.delete('all');w=self.winfo_width();h=self.winfo_height()
        if w<120:return
        t=self.packet.get('targets') or {};currency=t.get('currency');series=self.packet.get('history') or []
        t_at=moment(self.packet.get('targets_at'));h_at=moment(self.packet.get('history_at'))
        # History and targets must be the same provider snapshot and currency.
        comparable=bool(currency and t_at and h_at and t_at==h_at)
        series=series[-550:] if comparable else []
        low,mean,high=(t.get(k) if t.get('status')=='AVAILABLE' and currency else None for k in ('low','mean','high'))
        current=t.get('current') if currency else None
        vals=[v for v in (low,mean,high,current) if v is not None]+[v for _,v in series]
        if not vals:
            self.create_text(w/2,h/2,text=word('targetmissing'),fill='#92a7bb',width=w-30,font=('Segoe UI',10));return
        today=t_at.date() if t_at else date.today()
        try:
            if t.get('quote_at'):today=datetime.fromtimestamp(t['quote_at'],timezone.utc).date()
        except (ValueError,TypeError,OverflowError,OSError):pass
        end=date.fromisoformat(series[-1][0]) if series else today
        start=date.fromisoformat(series[0][0]) if series else end-timedelta(days=730)
        future=end+timedelta(days=365);duration=max(1,(future-start).days)
        left,top,right,bottom=12,20,max(50,w-94),h-38
        lo,hi=min(vals),max(vals);pad=max((hi-lo)*.12,hi*.03,.01);lo-=pad;hi+=pad
        x=lambda d:left+(right-left)*(d-start).days/duration
        y=lambda v:bottom-(bottom-top)*(v-lo)/(hi-lo)
        for n in range(5):
            v=lo+(hi-lo)*n/4;yy=y(v)
            self.create_line(left,yy,right,yy,fill='#1c3044');self.create_text(right+6,yy,text=price(v),fill='#92a7bb',anchor='w',font=('Segoe UI',8))
        if series:
            points=[coordinate for day,value in series for coordinate in (x(date.fromisoformat(day)),y(value))]
            if len(points)>=4:self.create_line(*points,fill='#22a8ff',width=2)
            self.create_text(left,bottom+20,text=str(start.year),anchor='w',fill='#92a7bb',font=('Segoe UI',8))
        else:self.create_text(left,top+15,text=word('historymissing'),anchor='w',fill='#92a7bb',font=('Segoe UI',9))
        xx=x(end);xf=x(future)
        self.create_line(xx,top,xx,bottom,fill='#607789',dash=(2,4))
        self.create_text(xx,bottom+20,text=end.strftime('%d.%m.%y'),fill='#92a7bb',font=('Segoe UI',8))
        self.create_text(xf,bottom+20,text=future.strftime('%d.%m.%y'),fill='#92a7bb',font=('Segoe UI',8))
        anchor=current if current is not None else series[-1][1] if series else None
        if anchor is not None:
            if low is not None and high is not None:
                self.create_polygon(xx,y(anchor),xf,y(high),xf,y(low),fill='#103c3c',outline='')
            for key,value in [('high',high),('mean',mean),('low',low)]:
                if value is None:continue
                self.create_line(xx,y(anchor),xf,y(value),fill='#16c4a3',width=2 if key=='mean' else 1,dash=(3,3))
            if current is not None:self.create_line(xx,y(current),xf,y(current),fill='#22a8ff',width=2)
            self.create_oval(xx-3,y(anchor)-3,xx+3,y(anchor)+3,fill='#22a8ff',outline='')

class Panel:
    def __init__(self,parent,app,info,packet):
        self.parent=parent;self.app=app;self.info=info;self.packet=packet or {};self.widgets=[]
        style=ttk.Style(parent);self.bg=style.lookup('DeskCard.TFrame','background') or '#0d1927'
        outer=ttk.Frame(parent,style='Desk.TFrame');outer.pack(fill='both',expand=True)
        self.canvas=tk.Canvas(outer,background=self.bg,highlightthickness=0)
        scroll=ttk.Scrollbar(outer,orient='vertical',command=self.canvas.yview)
        scroll.pack(side='right',fill='y');self.canvas.pack(side='left',fill='both',expand=True);self.canvas.configure(yscrollcommand=scroll.set)
        self.content=ttk.Frame(self.canvas,style='Desk.TFrame',padding=(18,14));self.window=self.canvas.create_window(0,0,window=self.content,anchor='nw')
        self.content.bind('<Configure>',lambda _:self.canvas.configure(scrollregion=self.canvas.bbox('all')))
        self.canvas.bind('<Configure>',self.resize)
        self.canvas.bind('<MouseWheel>',self.wheel)
        self.build()
    def wheel(self,e):self.canvas.yview_scroll(-int(e.delta/120),'units')
    def label(self,parent,text,large=False,muted=False):
        label=ttk.Label(parent,text=text,style='DeskMuted.TLabel' if muted else 'Desk.TLabel',font=('Segoe UI',18 if large else 10,'bold' if large else 'normal'),wraplength=650,justify='left')
        label.pack(anchor='w',pady=3);label.bind('<MouseWheel>',self.wheel);return label
    def resize(self,e):
        self.canvas.itemconfigure(self.window,width=e.width)
        for label in self.widgets:label.configure(wraplength=max(200,e.width-32))
        if not hasattr(self,'rating'):return
        row=0 if e.width>=850 else 1;column=1 if row==0 else 0
        if self.rating.grid_info().get('row')!=row or self.rating.grid_info().get('column')!=column:
            self.rating.grid(row=row,column=column,sticky='nsew',padx=(12 if column else 0,0),pady=(10 if row else 0,0))
    def build(self):
        p=self.packet
        if self.info.get('kind')=='benchmark':self.label(self.content,word('benchmark'));return
        self.widgets.append(self.label(self.content,SOURCE_LINE(p,self.info),muted=True))
        self.widgets.append(self.label(self.content,word('auxiliary'),muted=True))
        if p.get('resolved_profile'):self.widgets.append(self.label(self.content,word('resolved'),muted=True))
        if p.get('errors',{}).get('identity'):self.widgets.append(self.label(self.content,word('identitymissing'),muted=True))
        self.top=ttk.Frame(self.content,style='Desk.TFrame');self.top.pack(fill='x',pady=(14,8))
        self.top.columnconfigure(0,weight=1);self.top.columnconfigure(1,weight=0)
        left=ttk.Frame(self.top,style='Desk.TFrame');left.grid(row=0,column=0,sticky='nsew')
        self.label(left,word('price'),large=True)
        t=p.get('targets') or {};currency=t.get('currency');value=t.get('mean') if t.get('status')=='AVAILABLE' else None
        headline=self.label(left,price(value,currency) if value is not None else word('unknown'),large=True)
        current=t.get('current');change=(value/current-1)*100 if value is not None and current else None
        if change is not None:headline.configure(foreground='#19d7a7' if change>=0 else '#ff6077');self.label(left,('+' if change>=0 else '')+format_value(change,language(),kind='number',digits=2)+'%  ·  '+word('current')+': '+price(current,currency),muted=True)
        self.label(left,stamp(p,'targets'),muted=True)
        try:
            if t.get('quote_at'):self.label(left,word('current')+' · '+format_date(datetime.fromtimestamp(t['quote_at'],timezone.utc).isoformat(),language(),clock=True),muted=True)
        except (TypeError,ValueError,OverflowError,OSError):pass
        if currency:self.label(left,currency+' · '+word('units'),muted=True)
        plot=Plot(left,p,background=self.bg);plot.pack(fill='x',expand=True);plot.bind('<MouseWheel>',self.wheel)
        cards=ttk.Frame(left,style='Desk.TFrame');cards.pack(fill='x')
        for key in ('low','mean','high'):
            box=ttk.Frame(cards,style='Desk.TFrame');box.pack(side='left',expand=True,fill='x',padx=4)
            self.label(box,word(key),muted=True);self.label(box,price(t.get(key),currency) if t.get('status')=='AVAILABLE' else '—')
        self.label(left,word('horizon'),muted=True)
        if t.get('analysts') is not None:self.label(left,str(t['analysts'])+' '+word('analysts'),muted=True)
        self.rating=ttk.Frame(self.top,style='Desk.TFrame');self.rating.grid(row=0,column=1,sticky='nsew',padx=(12,0))
        self.label(self.rating,word('rating'),large=True)
        r=p.get('ratings') or {};score=r.get('score');self.gauge(self.rating,score)
        self.label(self.rating,rating_word(score),large=True)
        self.label(self.rating,stamp(p,'ratings'),muted=True)
        self.label(self.rating,word('period'),muted=True).configure(wraplength=320)
        for key,count in zip(('strongbuy','buy','hold','sell','strongsell'),r.get('counts',[None]*5)):
            row=ttk.Frame(self.rating,style='Desk.TFrame');row.pack(fill='x',pady=4)
            ttk.Label(row,text=word(key),style='Desk.TLabel',width=19).pack(side='left')
            bar=tk.Canvas(row,width=120,height=12,background=self.bg,highlightthickness=0);bar.pack(side='left',fill='x',expand=True,padx=5)
            def meter(event,bar=bar,count=count,key=key):
                bar.delete('all');end=max(8,event.width-4);bar.create_line(4,6,end,6,fill='#26394b',width=6,capstyle='round')
                if count:
                    color='#13b99d' if key in ('strongbuy','buy') else '#a7b7c9' if key=='hold' else '#ff9950'
                    bar.create_line(4,6,4+(end-4)*count/max(1,r.get('total',0)),6,fill=color,width=6,capstyle='round')
            bar.bind('<Configure>',meter)
            ttk.Label(row,text='—' if count is None else str(count),style='Desk.TLabel',width=4).pack(side='right')
        self.label(self.content,word('recent'),large=True)
        self.label(self.content,stamp(p,'decisions'),muted=True)
        columns=('date','firm','analyst','action','grade','target');frame=ttk.Frame(self.content);frame.pack(fill='both',expand=True,pady=6)
        tree=ttk.Treeview(frame,columns=columns,show='headings',height=8,style='Desk.Treeview');self.tree=tree
        for key,width in zip(columns,(105,170,100,140,200,190)):
            tree.heading(key,text=word(key));tree.column(key,width=width,minwidth=80,stretch=True)
        tree.pack(side='left',fill='both',expand=True)
        sy=ttk.Scrollbar(frame,orient='vertical',command=tree.yview);sy.pack(side='right',fill='y');tree.configure(yscrollcommand=sy.set)
        sx=ttk.Scrollbar(self.content,orient='horizontal',command=tree.xview);sx.pack(fill='x');tree.configure(xscrollcommand=sx.set)
        for n,row in enumerate(p.get('decisions') or []):
            original=row.get('from_grade');updated=row.get('to_grade');rating=(grade(original)+' → ' if original else '')+grade(updated)
            amounts=[price(row.get(k)) for k in ('target_before','target_after') if row.get(k) is not None]
            target=' → '.join(amounts)+(' · ?' if amounts else '')
            tree.insert('','end',iid=str(n),values=(format_date(row.get('at'),language()),row.get('firm') or '—',row.get('analyst') or '—',word(row.get('action','')) or '—',rating,target or '—'))
        if not p.get('decisions'):self.label(self.content,word('unknown'),muted=True)
        detail=self.label(self.content,word('uncurrency'),muted=True)
        def inspect(_):
            selected=tree.selection()
            if selected:
                row=p['decisions'][int(selected[0])];detail.configure(text=word('raw')+(row.get('from_grade') or '—')+' → '+(row.get('to_grade') or '—')+' · '+word('uncurrency'))
        tree.bind('<<TreeviewSelect>>',inspect)
        if p.get('errors'):self.widgets.append(self.label(self.content,word('partial')+' · '+', '.join(p['errors']),muted=True))
        self.label(self.content,word('cooldown'),muted=True)
    def gauge(self,parent,score):
        gauge=tk.Canvas(parent,width=240,height=115,background=self.bg,highlightthickness=0);gauge.pack(anchor='center')
        colors=('#ff7444','#f6a63b','#9aabbc','#1dc6a4','#087a75')
        for n,color in enumerate(colors):gauge.create_arc(20,12,220,212,start=180-(n+1)*36+3,extent=30,style='arc',outline=color,width=9)
        if score is not None:
            angle=math.pi*(1-(score+1)/2);x=120+82*math.cos(angle);y=112-82*math.sin(angle)
            gauge.create_line(120,112,x,y,fill='#e2eef8',width=4);gauge.create_oval(116,108,124,116,fill='#e2eef8',outline='')
        else:gauge.create_text(120,80,text='—',fill='#92a7bb',font=('Segoe UI',20))

def SOURCE_LINE(packet,info):
    return str(packet.get('source') or 'Yahoo Finance / yfinance')+' · '+str(packet.get('symbol') or info.get('profile_symbol') or info.get('history_symbol') or '—')+' · '+stamp(packet)


def show(app,info):
    """Larger read-only view of this same stock; no changes to the chart selection."""
    from analyst_forecast import load,identity
    windows=getattr(app,'_forecast_windows',{});app._forecast_windows=windows
    key=info['isin'];old=windows.get(key)
    if old is not None and old.winfo_exists():old.deiconify();old.lift();return old
    win=tk.Toplevel(app.root);windows[key]=win;win.title(str(info.get('name') or '')+' · '+word('forecast'));win.geometry('1280x860')
    top=ttk.Frame(win,padding=10);top.pack(fill='x');note=tk.StringVar(value=word('loading'))
    ttk.Label(top,textvariable=note).pack(side='left');host=ttk.Frame(win,padding=10);host.pack(fill='both',expand=True)
    counter=[0]
    def render(packet):
        if not win.winfo_exists():return
        for child in host.winfo_children():child.destroy()
        Panel(host,app,info,packet);note.set(SOURCE_LINE(packet,info))
    def refresh():
        counter[0]+=1;serial=counter[0];button.configure(state='disabled');note.set(word('loading'))
        def done(packet):
            if app.closing or not win.winfo_exists() or counter[0]!=serial:return
            button.configure(state='normal')
            current=next((i for i in app.watchlist if i.get('isin')==key),{})
            if identity(current)!=identity(info):note.set(word('unknown'));return
            render(packet)
        def failed(_):
            if win.winfo_exists() and counter[0]==serial:button.configure(state='normal');note.set(word('partial'))
        if app.job(lambda:load(app,info,True),done,failed,key=('analyst-forecast',key),lane='misc',isin=key) is False:failed(None)
    button=ttk.Button(top,text=word('refresh'),command=refresh);button.pack(side='right');refresh();return win
