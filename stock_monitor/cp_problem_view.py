"""Stock-scoped, read-only problem view over existing CP evidence and requests."""
import json,sqlite3
from contextlib import closing
from pathlib import Path
from datetime import datetime,timezone
import tkinter as tk
from tkinter import ttk
from i18n import tr
from indicator_dialog import styles
from themes import THEMES

KEYS=('source','venue','currency','symbol','timezone')
STATES={'INVALID':'Некорректные цены или объёмы','GAP':'Пропуск свечей','SOURCE_GAP':'Пропуск у источника',
        'UNCONFIRMED':'Интервал пока не подтверждён','SOURCE_UNAVAILABLE':'Источник недоступен',
        'SOURCE_LIMITED':'Ограничение глубины источника','STALE':'Данные или оценка устарели',
        'ANALYSIS_BLOCKED':'Требуется проверка входов анализа'}


def same_problem(problem,job):
    if problem.get('isin')!=job.get('isin') or problem.get('tf')!=job.get('tf'):return False
    a,b=problem.get('identity') or {},job.get('identity') or {}
    if any(not a.get(k) or a.get(k)!=b.get(k) for k in KEYS):return False
    span=problem.get('range')
    if not isinstance(span,(list,tuple)) or len(span)!=2:return False
    import pandas as pd
    try:return [pd.Timestamp(v) for v in span]==[pd.Timestamp(job[k]) for k in ('first','last')]
    except (KeyError,ValueError,TypeError):return False


def tv_result(problem,jobs):
    matches=[j for j in jobs if same_problem(problem,j)]
    if not matches:return 'Не проверено',None
    job=max(matches,key=lambda j:str(j.get('checked_at') or ''))
    obs=job.get('observation') or {};comparison=job.get('source_comparison') or {}
    if not obs:return ('В очереди сверки' if job.get('status')=='RECHECK_QUEUED' else 'Не проверено'),job
    if job.get('status')=='RECHECK_QUEUED':return 'Ожидает повторной проверки',job
    current=(bool(obs.get('input_fingerprint')) and obs.get('input_fingerprint')==job.get('input_fingerprint')
             and obs.get('identity_confirmed') is True and comparison.get('observation_current') is True
             and comparison.get('closed') is True)
    if not current:return 'Нужна повторная сверка',job
    if comparison.get('ohlcv_match') is True:return 'Ранее совпали OHLCV',job
    if comparison.get('ohlcv_match') is False or comparison.get('value_discrepancy') is True:return 'Расходятся OHLCV',job
    if comparison.get('presence_match') is True:return 'Ранее совпало наличие свечей',job
    if comparison.get('presence_match') is False:return 'Расходится наличие свечей',job
    return 'Данных недостаточно',job


def present(problem,jobs):
    state=problem.get('state','UNKNOWN');accepted=problem.get('accepted') is True
    severity='Низкая' if accepted else 'Высокая' if state in ('INVALID','ANALYSIS_BLOCKED') else 'Информация' if state=='SOURCE_LIMITED' else 'Средняя'
    influence='Исторический риск принят' if accepted else 'Требуется проверка анализа' if problem.get('kind')=='context' else 'Диагностика архива'
    tv,job=tv_result(problem,jobs);repair=problem.get('repair') or {};review=problem.get('historical_review') or {}
    reason=repair.get('reason') or review.get('reason') or ((job or {}).get('finding') or {}).get('reason')
    next_step=reason or ('Ожидание штатного восстановления' if repair.get('state') in ('QUEUED','RUNNING') else 'CP проверит доступные источники')
    return dict(problem=problem,tf=problem.get('tf','—'),span=' — '.join(str(v) for v in problem.get('range') or []) or '—',
                title=STATES.get(state,state),severity=severity,tv=tv,influence=influence,next=next_step,
                source=' / '.join(str((problem.get('identity') or {}).get(k) or '—') for k in ('source','venue','currency')),
                last=repair.get('finished_at') or repair.get('checked_at') or (job or {}).get('checked_at') or '—',
                next_retry=repair.get('next_retry') or review.get('next_retry'),job=job)


def read(path,isin,page=0,include_old=False,now=None):
    from cp_controls import view
    with closing(sqlite3.connect(Path(path).resolve().as_uri()+'?mode=ro',uri=True)) as db:
        db.execute('PRAGMA query_only=ON');db.execute('BEGIN')
        problems=view(db,isin)
        from eur_listings import owner_history_info,venue_allowed
        exists=db.execute("SELECT 1 FROM sqlite_master WHERE name='instruments'").fetchone()
        info=db.execute('SELECT payload FROM instruments WHERE isin=?',(isin,)).fetchone() if exists else None
        info=json.loads(info[0]) if info else {}
        saved_policy=db.execute("SELECT value FROM settings WHERE key='cp_gettex_source_policy'").fetchone()
        info=owner_history_info(info,json.loads(saved_policy[0]) if saved_policy else {})
        # Equality on JSON identity avoids LIKE wildcard confusion in owner-added IDs.
        prefix='cp_reconciliation_job:'+isin+':'
        jobs=[json.loads(v) for v, in db.execute('SELECT value FROM settings WHERE key>=? AND key<? ORDER BY key',(prefix,prefix+'\uffff'))]
        import pandas as pd
        def binding(isin,tf,meta,span):
            if not span or len(span)!=2 or any(not meta.get(k) for k in KEYS):return None
            try:
                stamps=[pd.Timestamp(v) for v in span]
                if any(pd.isna(v) for v in stamps):return None
                return (isin,tf,*(meta[k] for k in KEYS),*(v.isoformat() for v in stamps))
            except (ValueError,TypeError):return None
        indexed={}
        for job in jobs:
            key=binding(job.get('isin'),job.get('tf'),job.get('identity') or {},[job.get('first'),job.get('last')])
            if key is not None:indexed.setdefault(key,[]).append(job)
        cutoff=pd.Timestamp(now or datetime.now(timezone.utc)).tz_convert('Europe/Berlin').tz_localize(None)-pd.DateOffset(years=5)
        def old_archive(problem):
            if problem.get('kind')!='archive' or not problem.get('range'):return False
            try:
                end=pd.Timestamp(problem['range'][-1])
                if end.tzinfo is not None:end=end.tz_convert('Europe/Berlin').tz_localize(None)
                return bool(end<cutoff)
            except (ValueError,TypeError):return False
        old_count=sum(old_archive(p) for p in problems)
        def other_archive(problem):
            meta=problem.get('identity') or {}
            return problem.get('kind')=='archive' and all(meta.get(k) for k in ('source','venue','currency')) and not venue_allowed(info,meta)
        archived_count=sum(old_archive(p) or other_archive(p) for p in problems)
        visible=problems if include_old else [p for p in problems if not (old_archive(p) or other_archive(p))]
        rows=[present(p,indexed.get(binding(p.get('isin'),p.get('tf'),p.get('identity') or {},p.get('range')),[])) for p in visible]
    order={'Высокая':0,'Средняя':1,'Низкая':2,'Информация':3}
    def scope(row):
        p=row['problem'];meta=p.get('identity') or {}
        if p.get('kind')=='context':return 0
        if meta.get('venue') and info.get('history_venue_lock'):return 0 if venue_allowed(info,meta) else 2
        if meta.get('venue') and info.get('venue'):return 0 if meta.get('venue')==info.get('venue') and meta.get('currency')==info.get('currency') else 2
        return 1
    rows.sort(key=lambda r:(scope(r),order[r['severity']],r['tf'],r['span']))
    pages=max(1,(len(rows)+99)//100);page=min(max(0,int(page)),pages-1)
    return dict(isin=isin,at=datetime.now(timezone.utc).isoformat(),total=len(rows),old_archive=old_count,archived_count=archived_count,include_old=include_old,page=page,pages=pages,rows=rows[page*100:(page+1)*100])


class Dialog:
    def __init__(self,app):
        from main_screen import native_titlebar
        self.app=app;self.closed=False;self.generation=0;self.page=0;self.packet={};self.shown={};self.busy=False;self.timer=None
        self.win=w=tk.Toplevel(app.root);app._cp_problem_window=w
        w.title(tr('CandlePilot • проблемы выбранной акции'));w.geometry('1280x740');w.minsize(950,600)
        palette=THEMES.get(app.store.setting('theme','Тёмная'),THEMES['Тёмная']);self.p=p=styles(app.root,palette)
        base=ttk.Frame(w,padding=12,style=p+'.TFrame');base.pack(fill='both',expand=True)
        ttk.Label(base,text=tr('CandlePilot • проблемы выбранной акции'),style=p+'.TLabel',font=('Segoe UI',18,'bold')).pack(anchor='w',pady=(0,10))
        top=ttk.Frame(base,style=p+'.TFrame');top.pack(fill='x',pady=(0,8))
        self.infos=[dict(i) for i in app.watchlist if i.get('kind')!='benchmark']
        self.choice=ttk.Combobox(top,values=[i['name']+' · '+i['isin'] for i in self.infos],state='readonly',style=p+'.TCombobox',width=52)
        self.choice.pack(side='left',fill='x',expand=True)
        selected=next((n for n,i in enumerate(self.infos) if i['isin']==app.instrument_cfg()['isin']),0)
        if self.infos:self.choice.current(selected)
        self.choice.bind('<<ComboboxSelected>>',lambda _:self.select())
        self.refresh_button=ttk.Button(top,text=tr('Обновить'),style=p+'.Secondary.TButton',command=self.refresh);self.refresh_button.pack(side='left',padx=7)
        ttk.Button(top,text=tr('Подробная диагностика…'),style=p+'.Secondary.TButton',command=lambda:__import__('candlepilot_health').show_advanced(app)).pack(side='left')
        self.status=tk.StringVar(w,tr('Чтение сохранённых проблем…'));ttk.Label(base,textvariable=self.status,style=p+'.TLabel').pack(anchor='w',pady=6)
        self.include_old=tk.BooleanVar(w,False)
        ttk.Checkbutton(base,text=tr('Показывать старые и другие архивы'),variable=self.include_old,command=self.select,style=p+'.TCheckbutton').pack(anchor='w',pady=(0,6))
        host=ttk.Frame(base,style=p+'.TFrame');host.pack(fill='both',expand=True)
        columns=('tf','span','title','severity','tv','influence','source')
        style=ttk.Style(w);style.configure(p+'.CP.Treeview',background=palette['bg'],fieldbackground=palette['bg'],foreground=palette['text'],rowheight=32,font=('Segoe UI',10),borderwidth=0,relief='flat')
        style.map(p+'.CP.Treeview',background=[('selected','#075585')],foreground=[('selected','#ffffff')])
        style.layout(p+'.CP.Treeview.Heading',[('Treeheading.padding',{'sticky':'nsew','children':[('Treeheading.text',{'sticky':'we'})]})])
        style.configure(p+'.CP.Treeview.Heading',background=palette['panel'],foreground=palette['text'],font=('Segoe UI',10,'bold'),padding=(6,8))
        style.configure(p+'.CP.Horizontal.TScrollbar',background=palette['grid'],troughcolor=palette['bg'],bordercolor=palette['bg'],lightcolor=palette['bg'],darkcolor=palette['bg'],arrowcolor=palette['muted'])
        self.tree=ttk.Treeview(host,columns=columns,show='headings',style=p+'.CP.Treeview',selectmode='browse')
        for key,label,width in zip(columns,('ТФ','Дата / диапазон','Проблема','Серьёзность','Сверка TV','Влияние','Источник / площадка'),(60,230,220,100,210,180,190)):
            self.tree.heading(key,text=tr(label));self.tree.column(key,width=width,minwidth=60)
        vertical=ttk.Scrollbar(host,command=self.tree.yview,style=p+'.Vertical.TScrollbar');vertical.pack(side='right',fill='y')
        self.tree.configure(yscrollcommand=vertical.set);self.tree.pack(fill='both',expand=True)
        horizontal=ttk.Scrollbar(base,orient='horizontal',command=self.tree.xview,style=p+'.CP.Horizontal.TScrollbar');horizontal.pack(fill='x');self.tree.configure(xscrollcommand=horizontal.set)
        self.tree.tag_configure('high',foreground='#ff5866');self.tree.tag_configure('medium',foreground='#efc462')
        self.tree.bind('<<TreeviewSelect>>',self.details)
        self.detail=tk.Text(base,height=7,wrap='word',background=palette['bg'],foreground=palette['text'],font=('Segoe UI',10),relief='flat')
        self.detail.pack(fill='x',pady=8);self.detail.configure(state='disabled')
        footer=ttk.Frame(base,style=p+'.TFrame');footer.pack(fill='x')
        ttk.Button(footer,text='‹',command=lambda:self.navigate(-1),style=p+'.Secondary.TButton').pack(side='left')
        ttk.Button(footer,text='›',command=lambda:self.navigate(1),style=p+'.Secondary.TButton').pack(side='left',padx=5)
        ttk.Label(footer,text=tr('Серьёзность — категория диагностики, не вероятность убытка. TV не выдаёт торговый допуск.'),wraplength=850,style=p+'.TLabel',font=('Segoe UI',9)).pack(side='left',fill='x',expand=True)
        ttk.Button(footer,text=tr('Закрыть'),command=self.close,style=p+'.Primary.TButton').pack(side='right')
        w.bind('<Escape>',lambda _:self.close());w.protocol('WM_DELETE_WINDOW',self.close)
        w.bind('<Destroy>',lambda e:self.cleanup() if e.widget is w else None,add='+')
        w.bind('<Map>',lambda e:w.after_idle(lambda:native_titlebar(w,palette)) if e.widget is w else None,add='+')
        self.refresh()

    def select(self):
        self.page=0;self.generation+=1;self.tree.delete(*self.tree.get_children());self.shown={};self.clear_detail();self.refresh()

    def clear_detail(self,text=''):
        self.detail.configure(state='normal');self.detail.delete('1.0','end');self.detail.insert('1.0',text);self.detail.configure(state='disabled')

    def details(self,event=None):
        picks=self.tree.selection()
        if not picks:return
        r=self.shown[picks[0]];problem=r['problem'];meta=problem.get('identity') or {}
        text='\n'.join([tr(r['title'])+' · '+r['span'],
            ' · '.join(str(meta.get(k) or '—') for k in KEYS),
            tr('Что делает CP')+': '+str(tr(r['next'])),tr('Следующая попытка')+': '+str(r['next_retry'] or '—'),
            tr('Последняя проверка')+': '+str(r['last']),
            tr('Исходные причины')+': '+json.dumps(problem.get('reasons') or [],ensure_ascii=False),
            tr('Сверка TV')+': '+tr(r['tv'])+' · '+tr('Сохранённое наблюдение; отсутствие в TV не доказывает отсутствие сделок.')])
        self.clear_detail(text)

    def navigate(self,delta):
        self.page=max(0,min(self.packet.get('pages',1)-1,self.page+delta));self.generation+=1;self.refresh()

    def refresh(self):
        if self.closed or self.choice.current()<0:return
        if self.busy:return
        self.busy=True;self.refresh_button.configure(state='disabled')
        isin=self.infos[self.choice.current()]['isin'];generation=self.generation;page=self.page;include_old=self.include_old.get()
        self.status.set(tr('Чтение сохранённых проблем…'))
        def done(value):
            self.busy=False
            if self.closed:return
            self.refresh_button.configure(state='normal')
            if generation!=self.generation:self.refresh();return
            self.packet=value;self.page=value['page'];self.tree.delete(*self.tree.get_children());self.shown={};self.clear_detail()
            for n,r in enumerate(value['rows']):
                key=str(n);self.shown[key]=r
                self.tree.insert('','end',iid=key,values=[tr(str(r[k])) for k in ('tf','span','title','severity','tv','influence','source')],tags=('high' if r['severity']=='Высокая' else 'medium' if r['severity']=='Средняя' else '',))
            self.status.set(tr('Сохранённые проблемы')+f": {value['total']} · {self.page+1}/{value['pages']} · "+tr('Архив отдельно')+f": {value.get('archived_count',0)} · "+str(value['at']))
            if not value['rows']:self.clear_detail(tr('В сохранённых отчётах проблем не найдено. Это не подтверждает полноту истории или готовность торговли.'))
            self.timer=self.win.after(60000,self.refresh)
        def failed(error):
            self.busy=False
            if self.closed:return
            if generation!=self.generation:self.refresh();return
            self.refresh_button.configure(state='normal');self.status.set(tr('Не удалось прочитать проблемы')+': '+str(error))
        if self.timer is not None:self.win.after_cancel(self.timer);self.timer=None
        if self.app.job(lambda:read(self.app.store.path,isin,page,include_old),done,failed,lane='read',key=('cp-problem-view',id(self.win),generation,page)) is False:failed(tr('Очередь временно занята'))

    def cleanup(self):
        self.closed=True;self.generation+=1
        if self.timer is not None:self.win.after_cancel(self.timer);self.timer=None

    def close(self):self.cleanup();self.win.destroy()


def show(app):
    old=getattr(app,'_cp_problem_window',None)
    if old is not None and old.winfo_exists():old.lift();return old
    app._cp_problem_dialog=Dialog(app);return app._cp_problem_dialog.win

