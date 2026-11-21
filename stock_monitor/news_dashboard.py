"""Bounded news presentation over the existing NewsRuntime and source cache."""
import math
import time
import tkinter as tk
from tkinter import ttk
from datetime import datetime,timezone
import webbrowser

from i18n import tr, language
from indicator_dialog import styles
from themes import THEMES
from news_engine import normalize,safe_url

EVENTS={'earnings':'Отчётность','guidance':'Прогноз','dividend':'Дивиденды','deal':'Сделки','management':'Руководство'}
PAGE_SIZE=12


def prepare(value):
    value=value if isinstance(value,dict) else {}
    rows=value.get('articles') or []
    rows=[row for row in rows[:100] if isinstance(row,dict)] if isinstance(rows,(list,tuple)) else []
    from news_translation import project
    articles = normalize(rows)
    if value.get('_translation_target'): articles = project(articles, value.get('_translation_cache', {}), value['_translation_target'])
    return dict(snapshot=value,articles=articles)


def chart_preview(app,isin):
    """At most 60 already accepted chart points; informational archive only."""
    cached=getattr(app,'_accepted_trading_chart',None)
    if not cached:return None
    values,prepared=cached
    info=next((i for i in values.get('watchlist',[]) if i['name']==values.get('symbol')),None)
    if not info or info['isin']!=isin:return None
    frame=prepared.get('frame')
    if frame is None or not len(frame):return None
    try:
        prices=[float(v) for v in frame['close'].tail(60)]
        if not prices or not all(math.isfinite(v) for v in prices):return None
        return dict(prices=prices,at=str(frame.index[-1]),currency=frame.attrs.get('currency') or '—',
                    venue=frame.attrs.get('venue') or '—',timeframe=values.get('timeframe') or '—')
    except (KeyError,TypeError,ValueError):return None


def filtered(rows,query='',days=None,provider='Все',event='Все',oldest=False,now=None):
    now=time.time() if now is None else now
    key=next((k for k,v in EVENTS.items() if v==event),None)
    out=[]
    for row in rows:
        if query.casefold() not in (row['title']+' '+row.get('summary','')+' '+row.get('original_title','')+' '+row.get('original_summary','')).casefold():continue
        if provider!='Все' and row.get('provider')!=provider:continue
        if key and key not in row.get('events',[]):continue
        if days is not None:
            try:age=now-datetime.fromisoformat(row['published']).timestamp()
            except (ValueError,TypeError,KeyError):continue
            if not 0<=age<=days*86400:continue
        out.append(row)
    return sorted(out,key=lambda r:r.get('published') or '',reverse=not oldest)


def date_note(article):
    return tr({'CURRENT':'Дата: последние 7 дней','STALE':'Дата: старше 7 дней','FUTURE':'Дата источника в будущем'}.get(article.get('date_status'),'Дата неизвестна'))


def translation_note(article):
    status=article.get('translation',{}).get('status')
    return tr({'TRANSLATED':'Машинный перевод', 'ALREADY_TARGET':'На выбранном языке', 'PENDING':'Перевод ожидается; показан оригинал'}.get(status,'Перевод недоступен; показан оригинал'))


def details(article):
    publisher=tr('Издатель указан источником') if article.get('publisher_status')=='NAMED_BY_FEED' else tr('Показан домен ссылки; первичный издатель может отличаться')
    link=tr('Ссылка агрегатора ведёт к публикации') if article.get('link_kind')=='AGGREGATOR_LINK_TO_ORIGINAL' else tr('Ссылка на публикацию')
    scope=tr('В тексте упомянута компания; это не проверка биржевой записи.') if article.get('scope')=='company' else tr('Рынок и экономика')
    if article.get('relevance')=='OFFICIAL_ISSUER_FEED':scope=tr('Официальная лента компании')
    lines=[article['title'],str(article.get('published') or tr('Дата неизвестна'))+' · '+str(article.get('provider') or '—'),
           publisher,article.get('summary') or tr('Краткое содержание не предоставлено источником'),scope,
           tr('Состояние даты')+': '+date_note(article),
           tr('Возможные события по заголовку, не подтверждённый календарь')+': '+(', '.join(tr(EVENTS[k]) for k in article.get('events',[]) if k in EVENTS) or '—'),
           translation_note(article),link+': '+article['url']]
    if article.get('translation',{}).get('error'):lines.append(tr('Ошибка перевода')+': '+str(article['translation']['error']))
    if article.get('text_quality')=='UPSTREAM_REPLACEMENT_CHARACTERS':lines.append(tr('Источник содержит повреждённые символы'))
    return '\n\n'.join(lines)


class Dialog:
    def __init__(self,app):
        from main_screen import native_titlebar
        self.app=app;self.closed=False;self.generation=0;self.page=0;self.days=None;self.packet={};self.rows=[];self.shown=[]
        self.render_timer=self.freshness_timer=self.translation_timer=None
        self.win=win=tk.Toplevel(app.root);app._news_window=win
        width=min(1500,max(1000,app.root.winfo_width()-40),win.winfo_screenwidth()-70)
        height=min(870,max(620,app.root.winfo_height()-70),win.winfo_screenheight()-90)
        win.geometry(f'{width}x{height}+{max(0,app.root.winfo_rootx()+20)}+{max(0,app.root.winfo_rooty()+35)}');win.minsize(1000,620)
        self.palette=palette=THEMES.get(app.store.setting('theme','Тёмная'),THEMES['Тёмная'])
        self.p=p=styles(app.root,palette)
        self.targets=[(tr('Рынок и экономика'),'market',None)]+[(i['name'],i['isin'],i.get('profile_symbol') or i.get('history_symbol')) for i in app.watchlist if i.get('kind')!='benchmark']
        self.query=tk.StringVar(win);self.provider=tk.StringVar(win,tr('Все'));self.category=tk.StringVar(win,tr('Все'));self.sort=tk.StringVar(win,tr('Сначала новые'))
        self.status=tk.StringVar(win);self.result_text=tk.StringVar(win);self.heading=tk.StringVar(win);self.compact=tk.BooleanVar(win,False)
        base=ttk.Frame(win,padding=10,style=p+'.TFrame');base.pack(fill='both',expand=True)
        top=ttk.Frame(base,style=p+'.TFrame');top.pack(fill='x',pady=(0,10))
        self.choice=ttk.Combobox(top,values=[x[0] for x in self.targets],state='readonly',width=42)
        self.choice.pack(side='left',padx=(0,10))
        selected=next((n for n,x in enumerate(self.targets) if x[0]==app.symbol.get()),0);self.choice.current(selected)
        ttk.Label(top,textvariable=self.status,style='DeskMuted.TLabel',wraplength=700).pack(side='left',fill='x',expand=True)
        ttk.Button(top,text=tr('↻ Обновить'),command=self.refresh).pack(side='right')
        workspace=ttk.Frame(base,style=p+'.TFrame');workspace.pack(fill='both',expand=True)
        side_host=ttk.Frame(workspace,width=330,style=p+'.TFrame');side_host.pack(side='right',fill='y',padx=(10,0));side_host.pack_propagate(False)
        self.side_canvas=tk.Canvas(side_host,background=palette['bg'],highlightthickness=0)
        side_scroll=ttk.Scrollbar(side_host,command=self.side_canvas.yview,style=p+'.Vertical.TScrollbar');side_scroll.pack(side='right',fill='y')
        self.side_canvas.configure(yscrollcommand=side_scroll.set);self.side_canvas.pack(fill='both',expand=True)
        self.side=ttk.Frame(self.side_canvas,style=p+'.TFrame');side_window=self.side_canvas.create_window(0,0,window=self.side,anchor='nw')
        self.side_canvas.bind('<Configure>',lambda event:self.side_canvas.itemconfigure(side_window,width=event.width))
        self.side.bind('<Configure>',lambda _:self.side_canvas.configure(scrollregion=self.side_canvas.bbox('all')))
        centre=ttk.Frame(workspace,padding=10,style='DeskCard.TFrame');centre.pack(fill='both',expand=True)
        ttk.Label(centre,textvariable=self.heading,style='DeskTitle.TLabel',font=('Segoe UI',18,'bold')).pack(anchor='w',pady=(0,10))
        periods=ttk.Frame(centre,style='Desk.TFrame');periods.pack(fill='x',pady=(0,8))
        self.period_buttons={}
        for label,days in [('Все',None),('24 ч',1),('3 д',3),('7 д',7),('14 д',14),('30 д',30),('3 мес',90)]:
            b=ttk.Button(periods,text=tr(label),command=lambda value=days:self.period(value));b.pack(side='left',padx=(0,3));self.period_buttons[days]=b
        filters=ttk.Frame(centre,style='Desk.TFrame');filters.pack(fill='x',pady=(0,8))
        self.provider_box=ttk.Combobox(filters,textvariable=self.provider,values=[tr('Все')],state='readonly',width=20);self.provider_box.pack(side='left',padx=(0,5))
        ttk.Combobox(filters,textvariable=self.category,values=[tr('Все'),*(tr(v) for v in EVENTS.values())],state='readonly',width=18).pack(side='left',padx=5)
        ttk.Label(filters,text=tr('Тональность: не рассчитана'),style='DeskMuted.TLabel').pack(side='left',padx=8)
        search=ttk.Entry(filters,textvariable=self.query,style=p+'.TEntry',font=('Segoe UI',10));search.pack(side='left',fill='x',expand=True,padx=(5,0))
        app.tooltip(search,'Поиск в заголовках и кратком содержании')
        toolbar=ttk.Frame(centre,style='Desk.TFrame');toolbar.pack(fill='x',pady=(0,7))
        ttk.Label(toolbar,textvariable=self.result_text,style='DeskMuted.TLabel').pack(side='left')
        ttk.Combobox(toolbar,textvariable=self.sort,values=[tr('Сначала новые'),tr('Сначала старые')],state='readonly',width=20).pack(side='left',padx=12)
        ttk.Checkbutton(toolbar,text=tr('Компактный список'),variable=self.compact,command=self.schedule).pack(side='right')
        paging=ttk.Frame(centre,style='Desk.TFrame');paging.pack(side='bottom',fill='x',pady=(7,0))
        self.page_text=tk.StringVar(win)
        ttk.Button(paging,text='‹',width=3,command=lambda:self.change_page(-1)).pack(side='left')
        ttk.Label(paging,textvariable=self.page_text,style='DeskMuted.TLabel').pack(side='left',padx=8)
        ttk.Button(paging,text='›',width=3,command=lambda:self.change_page(1)).pack(side='left')
        ttk.Button(paging,text=tr('Источники и ошибки'),command=self.sources).pack(side='right')
        area=ttk.Frame(centre,style='Desk.TFrame');area.pack(fill='both',expand=True)
        self.canvas=tk.Canvas(area,background=palette['bg'],highlightthickness=0)
        scroll=ttk.Scrollbar(area,command=self.canvas.yview,style=p+'.Vertical.TScrollbar');scroll.pack(side='right',fill='y')
        self.canvas.configure(yscrollcommand=scroll.set);self.canvas.pack(fill='both',expand=True)
        self.cards=ttk.Frame(self.canvas,style='Desk.TFrame');window=self.canvas.create_window(0,0,window=self.cards,anchor='nw')
        self.canvas.bind('<Configure>',lambda e:self.canvas.itemconfigure(window,width=e.width))
        self.cards.bind('<Configure>',lambda _:self.canvas.configure(scrollregion=self.canvas.bbox('all')))
        def wheel(event):
            target=self.side_canvas if str(event.widget).startswith(str(self.side_canvas)) else self.canvas
            target.yview_scroll(-int(event.delta/120) or (-1 if event.delta>0 else 1),'units')
        win.bind('<MouseWheel>',wheel)
        self.choice.bind('<<ComboboxSelected>>',lambda _:self.load())
        for variable in (self.query,self.provider,self.category,self.sort):variable.trace_add('write',lambda *_:self.schedule())
        self.listener=lambda key,value:self.project(key,value)
        app._news_listener=self.listener
        win.protocol('WM_DELETE_WINDOW',self.close);win.bind('<Escape>',lambda _:self.close())
        win.bind('<Destroy>',lambda event:self.cleanup() if event.widget is win else None,add='+')
        win.bind('<Map>',lambda event:win.after_idle(lambda:native_titlebar(win,palette)) if event.widget is win else None,add='+')
        self.load();self.tick()

    def target(self):return self.targets[self.choice.current()]

    def load(self,clear=True):
        self.generation+=1;generation=self.generation;name,key,_=self.target()
        if clear:self.rows=[];self.packet={};self.page=0;self.render()
        self.heading.set(tr('Новости')+' • '+name);self.win.title(tr('Новости')+' • '+name);self.status.set(tr('Чтение сохранённых новостей…'))
        def done(packet):self.receive(key,generation,packet)
        def failed(message):
            if not self.closed and generation==self.generation:self.status.set(tr('Кэш новостей временно недоступен')+': '+str(message or 'QUEUE_BUSY')[:160])
        from news_translation import cached_snapshot
        target=language()
        if self.app.job(lambda:prepare(cached_snapshot(self.app.store,key,target)),done,failed,lane='read',key=('news-panel-cache',id(self.win),generation)) is False:failed(None)

    def project(self,key,value):
        if self.closed or key!=self.target()[1]:return
        generation=self.generation
        from news_translation import cached_snapshot
        target=language()
        self.app.job(lambda:prepare(cached_snapshot(self.app.store,key,target)),lambda packet:self.receive(key,generation,packet),lambda _:None,lane='read',key=('news-panel-projection',id(self.win),generation))

    def receive(self,key,generation,packet):
        if self.closed or key!=self.target()[1] or generation!=self.generation:return
        self.packet=packet.get('snapshot',{});self.rows=packet.get('articles',[])
        providers=sorted({r.get('provider') or '—' for r in self.rows})
        if tuple(self.provider_box['values'])!=(tr('Все'),*providers):self.provider_box.configure(values=[tr('Все'),*providers])
        if self.provider.get() not in (tr('Все'),*providers):self.provider.set(tr('Все'))
        state=self.packet.get('status','NOT_LOADED')
        try:
            if time.time()-datetime.fromisoformat(self.packet['attempted_at']).timestamp()>3600 and self.rows:state='STALE_CACHE'
        except (ValueError,TypeError,KeyError):pass
        self.status.set(tr({'AVAILABLE':'Загружено','PARTIAL':'Часть источников недоступна','EMPTY':'Материалов нет','UNAVAILABLE':'Источники недоступны','STALE_CACHE':'Сохранённая лента: обновление не подтверждено','NOT_LOADED':'Ещё не загружено','ERROR':'Ошибка загрузки'}.get(state,state)))
        self.render()

    def refresh(self):
        _,key,symbol=self.target()
        self.status.set(tr('Загрузка…') if self.app.news_runtime.refresh(key,symbol) else tr('Предыдущее обновление ещё выполняется'))

    def period(self,days):self.days=days;self.schedule()

    def schedule(self):
        if self.closed:return
        self.page=0
        if self.render_timer is not None:self.win.after_cancel(self.render_timer)
        def draw():self.render_timer=None;self.render();self.canvas.yview_moveto(0)
        self.render_timer=self.win.after(100,draw)

    def change_page(self,direction):
        self.page=max(0,min(self.pages-1,self.page+direction));self.render();self.canvas.yview_moveto(0)

    def render(self):
        if self.closed:return
        provider='Все' if self.provider.get()==tr('Все') else self.provider.get()
        category=next((v for v in EVENTS.values() if tr(v)==self.category.get()),'Все')
        rows=filtered(self.rows,self.query.get(),self.days,provider,category,self.sort.get()==tr('Сначала старые'))
        self.pages=max(1,(len(rows)+PAGE_SIZE-1)//PAGE_SIZE);self.page=min(self.page,self.pages-1)
        self.shown=rows[self.page*PAGE_SIZE:(self.page+1)*PAGE_SIZE]
        self.result_text.set(tr('Найдено')+f': {len(rows)} / {len(self.rows)}')
        self.page_text.set(f'{self.page+1} / {self.pages}')
        for days,button in self.period_buttons.items():button.configure(style='DeskActive.TButton' if days==self.days else 'TButton')
        for child in self.cards.winfo_children():child.destroy()
        if not self.shown:ttk.Label(self.cards,text=tr('Материалы отсутствуют или не подходят фильтрам. Это не означает отсутствия событий.'),style='DeskMuted.TLabel',wraplength=650).pack(anchor='w',pady=20)
        for article in self.shown:
            card=ttk.Frame(self.cards,padding=12,style='DeskCard.TFrame');card.pack(fill='x',pady=(0,7))
            title=ttk.Label(card,text=article['title'],style='Desk.TLabel',font=('Segoe UI',12,'bold'),wraplength=700,justify='left')
            title.pack(fill='x',anchor='w')
            card.bind('<Configure>',lambda event,label=title:label.configure(wraplength=max(200,event.width-28)))
            ttk.Label(card,text=translation_note(article)+' · '+str(article.get('provider') or '—')+' · '+str(article.get('published') or tr('Дата неизвестна'))+' · '+date_note(article),style='DeskMuted.TLabel',font=('Segoe UI',9)).pack(anchor='w',pady=(5,3))
            if not self.compact.get():
                summary=article.get('summary') or tr('Краткое содержание не предоставлено источником')
                summary_label=ttk.Label(card,text=summary[:220]+('…' if len(summary)>220 else ''),style='Desk.TLabel',wraplength=700,justify='left')
                summary_label.pack(anchor='w',fill='x',pady=3)
                card.bind('<Configure>',lambda event,label=summary_label:label.configure(wraplength=max(200,event.width-28)),add='+')
            actions=ttk.Frame(card,style='Desk.TFrame');actions.pack(fill='x',pady=(5,0))
            ttk.Button(actions,text=tr('Подробнее / источник'),command=lambda row=article:self.article(row)).pack(side='left')
            ttk.Button(actions,text=tr('Оригинал ↗'),command=lambda row=article:self.original(row)).pack(side='right')
        self.summary()
        self.schedule_translation()

    def schedule_translation(self):
        if self.closed or self.translation_timer is not None: return
        if not any(r.get('translation',{}).get('status')=='PENDING' for r in self.shown): return
        def run():
            self.translation_timer=None
            if self.closed: return
            _,key,_=self.target();generation=self.generation;target=language()
            # Pass original source rows, never translate a translation again.
            requested=[{**r,'title':r.get('original_title',r['title']),'summary':r.get('original_summary',r.get('summary',''))} for r in self.shown]
            def received(value):
                if not self.closed and target==language(): self.receive(key,generation,prepare(value))
            if not self.app.news_runtime.translate_news(key,requested,target,received): self.schedule_translation()
        self.translation_timer=self.win.after(1000,run)

    def summary(self):
        for widget in self.side.winfo_children():widget.destroy()
        _,key,_=self.target();info=next((i for i in self.app.watchlist if i['isin']==key),None)
        frame=ttk.Frame(self.side,padding=14,style='DeskCard.TFrame');frame.pack(fill='x',pady=(0,10))
        image=getattr(getattr(self.app,'main_screen',None),'header_images',{}).get(key)
        if image:ttk.Label(frame,image=image,style='Desk.TLabel').pack(anchor='w',pady=(0,7))
        ttk.Label(frame,text=self.target()[0],style='DeskTitle.TLabel',wraplength=280).pack(anchor='w')
        if info:ttk.Label(frame,text=str(info.get('symbol') or '')+' · '+str(info.get('currency') or ''),style='DeskMuted.TLabel').pack(anchor='w',pady=7)
        preview=chart_preview(self.app,key)
        if preview:
            ttk.Label(frame,text=f"{preview['prices'][-1]:.2f} {preview['currency']}",style='DeskPrice.TLabel').pack(anchor='w',pady=5)
            ttk.Label(frame,text=tr('Архив открытого графика')+' · '+preview['venue']+' · '+preview['timeframe']+'\n'+preview['at'],style='DeskMuted.TLabel',wraplength=280).pack(anchor='w')
            graph=tk.Canvas(frame,height=120,highlightthickness=0,background=self.palette['bg']);graph.pack(fill='x',pady=10)
            def paint(event=None):
                graph.delete('all');prices=preview['prices'];width=max(1,graph.winfo_width());height=120
                low,high=min(prices),max(prices);spread=max(high-low,.000001)
                points=[point for n,price in enumerate(prices) for point in (8+n*(width-16)/max(1,len(prices)-1),height-8-(price-low)*(height-16)/spread)]
                if len(points)>=4:graph.create_line(*points,fill='#2be870' if prices[-1]>=prices[0] else '#ff5966',width=2)
            graph.bind('<Configure>',paint);paint()
        else:ttk.Label(frame,text=tr('Цена: нет соответствующего открытого графика'),style='DeskMuted.TLabel',wraplength=280).pack(anchor='w',pady=9)
        ttk.Label(frame,text=tr('Публикаций в сохранённой ленте')+f': {len(self.rows)}',style='Desk.TLabel',wraplength=280).pack(anchor='w',pady=7)
        tone=ttk.Frame(self.side,padding=14,style='DeskCard.TFrame');tone.pack(fill='x',pady=(0,10))
        ttk.Label(tone,text=tr('Тональность новостей'),style='DeskTitle.TLabel').pack(anchor='w')
        ttk.Label(tone,text=tr('Не рассчитана. Процент влияния на цену источник не предоставляет.'),style='DeskMuted.TLabel',wraplength=280,justify='left').pack(anchor='w',pady=10)
        events=ttk.Frame(self.side,padding=14,style='DeskCard.TFrame');events.pack(fill='x',pady=(0,10))
        ttk.Label(events,text=tr('Упоминания событий'),style='DeskTitle.TLabel').pack(anchor='w')
        for key,label in EVENTS.items():
            number=sum(key in row.get('events',[]) for row in self.rows)
            ttk.Label(events,text=tr(label)+f': {number}',style='Desk.TLabel').pack(anchor='w',pady=4)
        ttk.Label(events,text=tr('По словам в заголовке. Это не подтверждённые события или календарь.'),style='DeskMuted.TLabel',wraplength=280).pack(anchor='w',pady=10)

    def text_window(self,title,geometry):
        from main_screen import native_titlebar
        win=tk.Toplevel(self.win);win.title(tr(title));win.geometry(geometry);win.configure(background=self.palette['bg'])
        win.bind('<Escape>',lambda _:win.destroy())
        win.bind('<Map>',lambda event:win.after_idle(lambda:native_titlebar(win,self.palette)) if event.widget is win else None,add='+')
        return win

    def article(self,row):
        win=self.text_window('Материал и первоисточник','800x560')
        from tkinter.scrolledtext import ScrolledText
        footer=ttk.Frame(win,padding=(12,8));footer.pack(side='bottom',fill='x')
        if row.get('translation',{}).get('status')=='TRANSLATED':
            ttk.Button(footer,text=tr('Показать исходный текст'),command=lambda:self.source_text(row)).pack(side='left')
        ttk.Button(footer,text=tr('Открыть оригинал'),command=lambda:self.original(row)).pack(side='right')
        box=ScrolledText(win,wrap='word',background=self.palette['bg'],foreground=self.palette['text'],font=('Segoe UI',11),padx=14,pady=14)
        box.pack(fill='both',expand=True);box.insert('1.0',details(row));box.configure(state='disabled')

    def source_text(self,row):
        from tkinter.scrolledtext import ScrolledText
        win=self.text_window('Исходный текст','800x450')
        box=ScrolledText(win,wrap='word',background=self.palette['bg'],foreground=self.palette['text'],font=('Segoe UI',11),padx=14,pady=14)
        box.pack(fill='both',expand=True);box.insert('1.0',row.get('original_title','')+'\n\n'+row.get('original_summary',''));box.configure(state='disabled')

    def original(self,row):
        url=safe_url(row.get('url'))
        if url:webbrowser.open(url)

    def sources(self):
        from tkinter.scrolledtext import ScrolledText
        win=self.text_window('Источники новостей','760x450')
        box=ScrolledText(win,wrap='word',background=self.palette['bg'],foreground=self.palette['text'],font=('Segoe UI',10));box.pack(fill='both',expand=True)
        lines=[tr('Получено')+': '+str(self.packet.get('fetched_at') or '—'),tr('Ошибки')+': '+str(self.packet.get('errors') or '—')]
        lines.extend(str(a.get('provider','—'))+': '+str(a.get('status','—'))+' '+str(a.get('error') or '') for a in self.packet.get('source_attempts',[]))
        box.insert('1.0','\n\n'.join(lines));box.configure(state='disabled')

    def tick(self):
        if self.closed:return
        def again():self.load(clear=False);self.tick()
        self.freshness_timer=self.win.after(60000,again)

    def cleanup(self):
        if self.closed:return
        self.closed=True
        for timer in (self.render_timer,self.freshness_timer,self.translation_timer):
            if timer is not None:
                try:self.win.after_cancel(timer)
                except tk.TclError:pass
        if getattr(self.app,'_news_listener',None) is self.listener:self.app._news_listener=None

    def close(self):self.cleanup();self.win.destroy()


def show(app):
    from news_runtime import NewsRuntime
    existing=getattr(app,'_news_window',None)
    if existing is not None and existing.winfo_exists():existing.deiconify();existing.lift();return existing
    if not hasattr(app,'news_runtime'):app.news_runtime=NewsRuntime(app);app.news_runtime.start()
    app._news_dialog=Dialog(app)
    return app._news_dialog.win
