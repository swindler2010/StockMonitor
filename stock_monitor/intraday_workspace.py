"""Stock Monitor's multi-timeframe charts, history screener and replay."""
from datetime import datetime
from pathlib import Path
import tkinter as tk
from tkinter import ttk, filedialog
import webbrowser

from gettex_sessions import CALENDAR_URL, warning
from i18n import tr
from intraday_alerts import add, disable
from intraday_data import (ALERT_KEY, BERLIN, BOOKS, DEFAULT_LAYOUT, LAYOUT_KEY, SOURCES, TF,
                           TRAINING_KEY, Replay, coverage, export_frame, money, panels, save_training)
from timeframes import MINUTES


def today():
    return datetime.now(BERLIN).date().isoformat()


def show(app):
    current=getattr(app,'_intraday_workspace',None)
    if current and current.win.winfo_exists():
        current.win.lift()
        return
    app._intraday_workspace=Workspace(app)


def choice(parent,var,values,width=13):
    box=ttk.Combobox(parent,textvariable=var,values=values,state='readonly',width=width)
    box.pack(side='left',padx=(0,5))
    return box


def button(parent,label,action):
    w=ttk.Button(parent,text=tr(label),command=action)
    w.pack(side='left',padx=(0,5))
    return w


def strip(parent,side='top'):
    box=ttk.Frame(parent)
    box.pack(side=side,fill='x',padx=8,pady=4)
    return box


def table(parent,columns):
    host=ttk.Frame(parent)
    host.pack(fill='both',expand=True,padx=8,pady=5)
    tree=ttk.Treeview(host,columns=[key for key,_,_ in columns],show='headings')
    for key,title,width in columns:
        tree.heading(key,text=tr(title))
        tree.column(key,width=width,minwidth=60,stretch=key in ('name','reason'))
    sy=ttk.Scrollbar(host,orient='vertical',command=tree.yview)
    sx=ttk.Scrollbar(host,orient='horizontal',command=tree.xview)
    tree.configure(yscrollcommand=sy.set,xscrollcommand=sx.set)
    host.columnconfigure(0,weight=1)
    host.rowconfigure(0,weight=1)
    tree.grid(row=0,column=0,sticky='nsew')
    sy.grid(row=0,column=1,sticky='ns')
    sx.grid(row=1,column=0,sticky='ew')
    return tree


class Workspace:
    def __init__(self,app):
        self.app=app
        palette={'Тёмная':'#101827','Светлая':'#f5f7fb'}
        self.win=tk.Toplevel(app.root,bg=palette.get(app.store.setting('theme','Тёмная'),'#101827'))
        self.win.title(tr('Stock Monitor • Рабочее место'))
        self.win.geometry('1280x780')
        self.win.minsize(900,600)
        self.win.protocol('WM_DELETE_WINDOW',self.close)
        self.closed=False
        self.busy=set()
        self.generation=0
        self.packet=None
        self.replay=None
        self.replay_timer=None
        self.save_pending=None
        self.scan_rows=[]
        self.autofollow=True
        self.vars=[]
        self.targets={f"{i['name']} ({i['isin']})":i for i in app.watchlist if i.get('kind','stock')=='stock'}
        initial=next((k for k,v in self.targets.items() if v['name']==app.symbol.get()),next(iter(self.targets),''))
        fallback=app.store.setting(LAYOUT_KEY,{})
        saved=fallback if isinstance(fallback,dict) else {}
        source=saved.get('source')
        self.source=self.var(SOURCES.get(source,SOURCES['gettex_chart']))
        self.selected=self.var(initial)
        self.day=self.var(today())
        self.lookback=self.var(str(saved.get('lookback')) if saved.get('lookback') in (15,35,90,120) else '35')
        self.book=self.var('V5')
        self.status=self.var('')
        self.hover=self.var('')
        self.sim_status=self.var('')
        self.scantf=self.var('15m')
        top=strip(self.win)
        choice(top,self.selected,list(self.targets),37).bind('<<ComboboxSelected>>',lambda _:self.changed())
        choice(top,self.source,tuple(SOURCES.values()),19).bind('<<ComboboxSelected>>',lambda _:self.changed())
        ttk.Label(top,text=tr('Свечи GETTEX / EUR')).pack(side='left',padx=(3,14))
        button(top,'Календарь биржи ↗',lambda:webbrowser.open(CALENDAR_URL))
        ttk.Label(self.win,text=warning(datetime.now(BERLIN).date()),wraplength=1150).pack(anchor='w',padx=8)
        footer=strip(self.win,'bottom')
        ttk.Label(footer,textvariable=self.status,wraplength=1170).pack(side='left')
        self.tabs=ttk.Notebook(self.win)
        self.tabs.pack(fill='both',expand=True,padx=7,pady=5)
        self.chart=ttk.Frame(self.tabs)
        self.scan=ttk.Frame(self.tabs)
        self.alerts=ttk.Frame(self.tabs)
        for tab,label in ((self.chart,'Графики'),(self.scan,'История и скринер'),(self.alerts,'Уведомления')):
            self.tabs.add(tab,text=tr(label))
        self.build_charts(saved)
        self.build_scan()
        self.build_alerts()
        self.tabs.bind('<<NotebookTabChanged>>',lambda _:self.refresh())
        self.refresh()
        self.timer=self.win.after(45000,self.tick)

    def var(self,value):
        return tk.StringVar(self.win,value=value)

    def source_key(self):
        return next(k for k,v in SOURCES.items() if v==self.source.get())

    def changed(self):
        self.stop_replay(False)
        self.generation+=1
        self.packet=None
        self.plot.render({},[])
        self.hover.set('')
        self.refresh()

    def job(self,name,work,done,lane='read',durable=False):
        if self.closed or self.app.closing or name in self.busy:
            return
        self.busy.add(name)

        def finish(value=None,error=None):
            self.busy.discard(name)
            if self.closed or self.app.closing:
                return
            if error:
                self.status.set(str(error))
            else:
                done(value)
        if self.app.job(work,finish,lambda message:finish(error=message),lane=lane,durable=durable,
                        key=('intraday-workspace',id(self),name)) is False:
            self.busy.discard(name)
            self.status.set(tr('Ожидается свободное чтение базы'))

    def refresh(self):
        if self.closed:
            return
        active=self.tabs.select()
        if active!=str(self.chart) and self.replay_timer:
            self.win.after_cancel(self.replay_timer)
            self.replay_timer=None
        if active==str(self.chart):
            if not self.replay:
                self.load_charts()
        elif active==str(self.scan):
            self.load_scan()
        else:
            self.load_alerts()

    def tick(self):
        if not self.closed and not self.app.closing:
            self.refresh()
            self.timer=self.win.after(45000,self.tick)

    def close(self):
        self.stop_replay(False)
        self.closed=True
        self.win.after_cancel(self.timer)
        self.win.destroy()

    def layout(self):
        return tuple(dict.fromkeys(v.get() for v in self.vars[:int(self.count.get())]))

    def build_charts(self,saved):
        from intraday_plot import Plot
        bar=strip(self.chart)
        layout=saved.get('layout')
        if not isinstance(layout,list) or not layout or len(layout)>4 or any(tf not in TF for tf in layout):
            layout=list(DEFAULT_LAYOUT)
        self.count=self.var(str(len(layout)))
        for tf in (*layout,*DEFAULT_LAYOUT)[:4]:
            value=self.var(tf)
            self.vars.append(value)
        choice(bar,self.count,('1','2','3','4'),4).bind('<<ComboboxSelected>>',lambda _:self.changed())
        for value in self.vars:
            choice(bar,value,TF,5).bind('<<ComboboxSelected>>',lambda _:self.changed())
        date_box=ttk.Entry(bar,textvariable=self.day,width=12)
        date_box.pack(side='left',padx=(7,3))
        date_box.bind('<Return>',lambda _:self.goto())
        button(bar,'К дате',self.goto)
        choice(bar,self.lookback,('15','35','90','120'),5).bind('<<ComboboxSelected>>',lambda _:self.changed())
        ttk.Label(bar,text=tr('дней')).pack(side='left',padx=(0,9))
        button(bar,'Сегодня',self.go_today)
        button(bar,'Сохранить раскладку',self.save_layout)
        button(bar,'CSV по ТФ',self.export_csv)
        bar=strip(self.chart)
        self.replay_tf=self.var('15m')
        self.replay_day=self.var(today())
        self.quantity=self.var('1')
        self.speed=self.var('1×')
        self.delay=self.var('0')
        ttk.Label(bar,text=tr('Воспроизведение')).pack(side='left',padx=(0,8))
        choice(bar,self.replay_tf,tuple(MINUTES),5)
        ttk.Entry(bar,textvariable=self.replay_day,width=12).pack(side='left',padx=(0,5))
        button(bar,'Начать',self.start_replay)
        button(bar,'Свеча ▶',self.step)
        button(bar,'Пуск / пауза',self.toggle_play)
        choice(bar,self.speed,('1×','2×','4×'),4)
        button(bar,'В рынок',self.stop_replay)
        ttk.Entry(bar,textvariable=self.quantity,width=5).pack(side='left',padx=(10,3))
        button(bar,'BUY',lambda:self.order('BUY'))
        button(bar,'SELL',lambda:self.order('SELL'))
        button(bar,'Сделки тренировки',self.replay_log)
        info=strip(self.chart)
        ttk.Label(info,text=tr('Задержка архива, мин')).pack(side='left',padx=(0,5))
        choice(info,self.delay,('0','5','15'),4)
        ttk.Label(info,textvariable=self.sim_status).pack(side='left',padx=8)
        ttk.Label(self.chart,textvariable=self.hover).pack(side='bottom',anchor='w',padx=8)
        graph=ttk.Frame(self.chart)
        graph.pack(fill='both',expand=True,padx=4)
        self.plot=Plot(graph,self.hover.set)

    def goto(self):
        self.autofollow=False
        self.changed()

    def go_today(self):
        self.autofollow=True
        self.day.set(today())
        self.changed()

    def save_layout(self):
        value={'source':self.source_key(),'layout':list(self.layout()),'lookback':int(self.lookback.get())}
        self.job('save-layout',lambda:self.app.store.set_setting(LAYOUT_KEY,value),
                 lambda _:self.status.set(tr('Раскладка сохранена')),lane='misc')

    def load_charts(self):
        if self.selected.get() not in self.targets:
            return
        if self.autofollow:
            self.day.set(today())
        generation=self.generation
        isin=self.targets[self.selected.get()]['isin']
        source=self.source_key()
        path=self.app.store.path
        intervals=self.layout()
        day=self.day.get().strip()
        lookback=int(self.lookback.get())
        if self.packet is None:
            self.status.set(tr('Читаю архив свечей…'))

        def done(packet):
            if generation!=self.generation:
                self.refresh()
                return
            preserve=self.packet is not None
            self.packet=packet
            self.draw(preserve)
            stats=' · '.join(f'{tf}  {len(f)}' for tf,f in packet['frames'].items())
            self.status.set(tr('Закрытые свечи')+' · '+stats+'   |   '+tr('Жёлтые отметки — разрывы и события'))
        self.job('chart',lambda:panels(path,isin,source,intervals,day,lookback),done)

    def draw(self,preserve=False):
        if self.packet is None:
            return
        if self.replay:
            self.plot.render(self.packet['frames'],[],self.replay.last_visible_close,preserve)
        else:
            self.plot.render(self.packet['frames'],self.packet['events'],preserve=preserve)

    def start_replay(self):
        if self.packet is None:
            return
        self.stop_replay(False)
        try:
            tf=self.replay_tf.get()
            if tf not in self.packet['frames']:
                raise ValueError('Добавьте этот ТФ в раскладку')
            self.replay=Replay(self.packet['frames'][tf],self.replay_day.get().strip(),int(self.delay.get()))
        except ValueError as e:
            self.status.set(str(e))
            return
        self.generation+=1
        self.replay_status()
        self.draw()

    def stop_replay(self,redraw=True):
        if self.replay_timer:
            self.win.after_cancel(self.replay_timer)
            self.replay_timer=None
        previous=self.replay
        self.replay=None
        self.sim_status.set('')
        if previous and redraw:
            self.draw()
            self.load_charts()

    def replay_status(self):
        r=self.replay
        t=r.clock.tz_convert(BERLIN)
        close=r.frame.close.iloc[r.cursor]
        capital=r.cash+money(close)*r.quantity
        pending=' · '+tr('Следующее открытие')+' '+r.pending[0] if r.pending else ''
        self.sim_status.set(tr('Тренировка')+f" · {t:%d.%m.%Y %H:%M}  ·  EUR {r.cash}  ·  {tr('Акций')} {r.quantity}  ·  {tr('Капитал')} {capital}{pending}")

    def order(self,side):
        if not self.replay:
            self.status.set(tr('Сначала запустите воспроизведение'))
            return
        try:
            self.replay.order(side,int(self.quantity.get()))
        except ValueError as e:
            self.status.set(str(e))
        else:
            self.replay_status()

    def step(self):
        if not self.replay:
            return False
        size=len(self.replay.history)
        if self.replay.step():
            if len(self.replay.history)>size:
                self.save_replay()
            self.replay_status()
            self.draw()
            return True
        self.status.set(tr('Достигнут конец закрытой истории'))
        return False

    def toggle_play(self):
        if self.replay_timer:
            self.win.after_cancel(self.replay_timer)
            self.replay_timer=None
        elif self.replay:
            self.play()

    def play(self):
        self.replay_timer=None
        if self.step():
            self.replay_timer=self.win.after(1400//int(self.speed.get()[0]),self.play)

    def replay_log(self):
        def display(sessions):
            popup=tk.Toplevel(self.win)
            popup.title(tr('Сделки тренировки'))
            popup.geometry('1000x420')
            rows=table(popup,(('name','Акция / ТФ',185),('time','Время Берлин',170),('side','Сторона',70),('qty','Акции',80),
                               ('price','Цена EUR',90),('reason','Результат',230)))
            for session in reversed(sessions):
                title=session['name']+' '+session['tf']
                for row in session['history']:
                    rows.insert('','end',values=(title,*row))
        self.job('replay-log',lambda:self.app.store.setting(TRAINING_KEY,[]),display)

    def save_replay(self):
        r=self.replay
        self.save_pending=dict(id=r.id,name=self.targets[self.selected.get()]['name'],isin=self.packet['isin'],
            source=self.packet['source'],day=r.day,tf=r.frame.attrs['timeframe'],
            cash=str(r.cash),quantity=r.quantity,delay_minutes=r.delay.total_seconds()/60,
            history=[list(row) for row in r.history])
        self.submit_replay_save()

    def submit_replay_save(self):
        if not self.save_pending or 'training-write' in self.busy:
            return
        record=self.save_pending
        self.save_pending=None
        self.job('training-write',lambda:save_training(self.app.store,record),
                 lambda _:self.submit_replay_save(),lane='misc',durable=True)

    def export_csv(self):
        frame=self.packet.get('frames',{}).get(self.replay_tf.get()) if self.packet else None
        if frame is None or frame.empty:
            self.status.set(tr('Выберите существующий ТФ в поле воспроизведения'))
            return
        selected=self.packet['isin']
        path=filedialog.asksaveasfilename(parent=self.win,defaultextension='.csv',initialdir=Path(self.app.store.directory).parent,
            initialfile=f"{selected}-{frame.attrs['timeframe']}.csv",filetypes=[('CSV','*.csv')])
        if path:
            if self.replay:
                count=sum(end<=self.replay.last_visible_close for end in frame.attrs['ends'])
                ends=frame.attrs['ends'][:count]
                frame=frame.iloc[:count].copy()
                frame.attrs['ends']=ends
            self.job('csv',lambda:export_frame(path,selected,frame),
                     lambda _:self.status.set(tr('CSV сохранён')),lane='misc')

    def build_scan(self):
        bar=strip(self.scan)
        ttk.Label(bar,text=tr('Интервал')).pack(side='left',padx=(0,5))
        choice(bar,self.scantf,TF,6).bind('<<ComboboxSelected>>',lambda _:self.load_scan())
        choice(bar,self.book,BOOKS,6).bind('<<ComboboxSelected>>',lambda _:self.load_scan())
        self.filter=self.var('Все')
        choice(bar,self.filter,('Все','Задержка / пробелы','BUY','SELL','Рост 1W','Объём от 1,5×'),22).bind(
            '<<ComboboxSelected>>',lambda _:self.render_scan())
        button(bar,'Обновить',self.load_scan)
        self.scan_status=self.var('')
        ttk.Label(self.scan,textvariable=self.scan_status).pack(anchor='w',padx=8)
        self.scan_tree=table(self.scan,(
            ('name','Акция',176),('base','База',58),('first','От',103),('last','До, Берлин',138),
            ('count','Свечей базы',83),('gaps','Нет записей 7д',94),('quality','Архив / качество',133),('age','Мин от начала',90),
            ('trend','Тренд',117),('volume','Объём ×',76),('signal','Сигнал',65),('reason','Проверка / причина',260)))
        self.scan_tree.bind('<Double-1>',self.open_scanned)

    def load_scan(self):
        source,tf,book=self.source_key(),self.scantf.get(),self.book.get()
        path=self.app.store.path
        if not self.scan_rows:
            self.scan_status.set(tr('Читаю покрытие истории и сигналы…'))

        def done(rows):
            if (source,tf,book)!=(self.source_key(),self.scantf.get(),self.book.get()):
                self.load_scan()
                return
            self.scan_rows=rows
            self.render_scan()
        self.job('scan',lambda:coverage(path,source,tf,book),done)

    def render_scan(self):
        f=self.filter.get()
        self.scan_tree.delete(*self.scan_tree.get_children())
        matches=[]
        for row in self.scan_rows:
            if f in ('BUY','SELL') and (row['signal']!=f or row['core']!='READY' or not row['signal_fresh']):
                continue
            if f=='Рост 1W' and row['trend']!='WEEKLY_RISING':
                continue
            if f=='Объём от 1,5×' and (row['volume'] or 0)<1.5:
                continue
            if (f=='Задержка / пробелы' and not row['missing']
                    and row['state'] in ('Архив','Есть свежие записи','Рынок закрыт') and row['archive_quality'] in (None,'READY')):
                continue
            matches.append(row)
        for row in sorted(matches,key=lambda r:r['name'].casefold()):
            self.scan_tree.insert('','end',iid=row['isin'],values=(row['name'],row['base'],(row['first'] or '—')[:10],
                (row['last'] or '—')[:16],row['count'],row['missing'] if row['missing'] is not None else '—',row['archive_quality'] or row['state'],
                '—' if row['age'] is None else round(row['age']),row['trend'] or '—',row['volume'] if row['volume'] is not None else '—',
                row['signal']+(' · архив' if row['signal']!='—' and not row['signal_fresh'] else ''),
                ' · '.join(filter(None,(row['source_reason'],row['reason']))) or row['state']))
        self.scan_status.set(f"{len(matches)} / {len(self.scan_rows)} · "+tr('Пустые интервалы включают периоды без сделок. Причины — из проверок CandlePilot.'))

    def open_scanned(self,_):
        selected=self.scan_tree.selection()
        if not selected:
            return
        match=next((name for name,info in self.targets.items() if info['isin']==selected[0]),None)
        if match is not None:
            self.selected.set(match)
            self.vars[0].set(self.scantf.get())
            self.tabs.select(self.chart)
            self.changed()

    def build_alerts(self):
        bar=strip(self.alerts)
        self.rule=self.var('Цена вверх')
        self.level=self.var('')
        choice(bar,self.rule,('Цена вверх','Цена вниз','BUY','SELL'),16)
        ttk.Label(bar,text=tr('Уровень EUR')).pack(side='left')
        ttk.Entry(bar,textvariable=self.level,width=10).pack(side='left',padx=5)
        choice(bar,self.book,BOOKS,6)
        button(bar,'Добавить для выбранной акции',self.add_alert)
        button(bar,'Отключить',self.disable_alert)
        button(bar,'Обновить',self.load_alerts)
        self.alert_tree=table(self.alerts,(('name','Акция',245),('rule','Условие',200),
            ('status','Статус',125),('reason','Событие',400)))

    def load_alerts(self):
        def done(rows):
            self.alert_tree.delete(*self.alert_tree.get_children())
            for rule in reversed(rows):
                name=rule['kind']
                if name in ('UP','DOWN'):
                    name=('↑ ' if name=='UP' else '↓ ')+str(rule['level'])+' EUR'
                else:
                    name=rule['book']+' '+name
                hit=rule.get('hit')
                state=tr('Сработало') if hit else tr('Ожидает') if rule['enabled'] else tr('Отключено')
                self.alert_tree.insert('','end',iid=rule['id'],values=(rule['name'],name,state,hit['text'] if hit else '—'))
        self.job('alerts',lambda:self.app.store.setting(ALERT_KEY,[]),done)

    def add_alert(self):
        info=self.targets.get(self.selected.get())
        if not info:
            return
        config=dict(isin=info['isin'],name=info['name'],source=self.source_key(),book=self.book.get(),
            kind={'Цена вверх':'UP','Цена вниз':'DOWN'}.get(self.rule.get(),self.rule.get()),
            level=self.level.get().replace(',','.').strip())
        self.job('alert-write',lambda:add(self.app.store,config),lambda _:self.load_alerts(),lane='misc')

    def disable_alert(self):
        selected=self.alert_tree.selection()
        if selected:
            self.job('alert-write',lambda:disable(self.app.store,selected[0]),lambda _:self.load_alerts(),lane='misc')
