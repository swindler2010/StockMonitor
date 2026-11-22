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


def accepted_tv_absence(problem):
    # `accepted` comes from effective_accepted's current revision/policy checks.
    # A stored observation alone, a manual risk grant or an old repair receipt
    # must never turn an unresolved input into a completed TV review.
    evidence=problem.get('acceptance_evidence') or {}
    return bool(problem.get('accepted') is True
                and problem.get('accepted_by')=='candlepilot_owner_tv_policy'
                and problem.get('kind')=='archive'
                and problem.get('state') in ('UNCONFIRMED','GAP','SOURCE_GAP')
                and evidence.get('status')=='OWNER_ACCEPTED_TV_ABSENCE'
                and evidence.get('price_model')=='actual_closed_bars_only_v1'
                and evidence.get('identity')==problem.get('identity')
                and evidence.get('timeframe')==problem.get('tf')
                and evidence.get('range')==problem.get('range')
                and evidence.get('job_ids') and evidence.get('native_binding'))


def native_empty_resolution(db,problem,jobs):
    """Reuse the live official-empty validator, never a terminal job label alone.

    Called only inside the existing readonly view transaction. The revision
    map is captured from that transaction for the validator; it is not an
    analytical dependency proof or a permission to execute a paper order.
    """
    if (problem.get('kind')!='archive' or problem.get('tf')!='15m'
            or problem.get('state') not in ('GAP','SOURCE_GAP','UNCONFIRMED')
            or not problem.get('range') or len(problem['range'])!=2
            or problem['range'][0]!=problem['range'][1]):return None
    for job in jobs:
        if (not same_problem(problem,job) or job.get('status')!='RESOLVED_NATIVE'
                or (job.get('finding') or {}).get('state')!='NO_TRADES'
                or (job.get('official_timebase_repair') or {}).get('state')!='APPLIED'):continue
        from cp_reconciliation import _spec
        from cp_controls import _official_empty_problem_current
        from revision_watcher import RETIME_PREFIX,RETIME_POLICY
        try:
            spec=_spec(problem['isin'],problem['identity'],'15m',*problem['range'])
            receipt=RETIME_PREFIX+problem['isin']+':'+spec['id']
            scopes=['setting:'+receipt,'setting:'+RETIME_POLICY,'setting:interval_evidence:'+problem['isin']]
            revisions={k:r for k,r in db.execute('SELECT scope,revision FROM input_revisions WHERE scope IN (?,?,?)',scopes)}
            if not _official_empty_problem_current(db,problem,dict(revisions=revisions)):continue
            return dict(state='CONFIRMED_NO_TRADES',current=True,isin=problem['isin'],identity=problem['identity'],
                        tf='15m',range=problem['range'],job_id=spec['id'],receipt_key=receipt,
                        checked_at=job.get('checked_at'),revisions=revisions)
        except (ValueError,KeyError,TypeError,sqlite3.OperationalError):continue
    return None


def confirmed_native_absence(problem):
    proof=problem.get('native_resolution') or {}
    return bool(proof.get('current') is True and proof.get('state')=='CONFIRMED_NO_TRADES'
                and proof.get('isin')==problem.get('isin') and proof.get('identity')==problem.get('identity')
                and proof.get('tf')==problem.get('tf')=='15m' and proof.get('range')==problem.get('range')
                and problem.get('kind')=='archive' and problem.get('state') in ('GAP','SOURCE_GAP','UNCONFIRMED')
                and proof.get('receipt_key') and proof.get('job_id'))


def repair_capability(problem,job):
    if confirmed_native_absence(problem):
        return dict(check=False,known=True,color='Green',label='Подтверждено GETTEX',
                    reason='Отсутствие сделок подтверждено GETTEX; свечи не создавались')
    if accepted_tv_absence(problem):
        return dict(check=False,known=True,color='Green',label='Принято по TV',
                    reason='Пропуск сверён с TV и принят; реальные свечи не изменены')
    meta=problem.get('identity') or {}
    check=(problem.get('kind')=='context' or
           (problem.get('kind')=='archive' and problem.get('range') and problem.get('tf') in ('5m','15m','1H','1D')
            and meta.get('venue')=='GETTEX' and meta.get('currency')=='EUR' and meta.get('source') in ('gettex','gettex_chart')))
    # Capability is the existing service_request route, not a previous repair's
    # success. Missing TV/native evidence is precisely why a recovery is needed.
    reason=('СР сам проверит архив и источник; исправление зависит от подтверждённых данных'
            if check else 'Нет штатного адресного исправления для этого источника или ТФ')
    result=dict(check=bool(check),known=bool(check),color='Green' if check else 'Red',label='Автоисправление' if check else 'Нужна помощь',reason=reason)
    receipt=problem.get('repair') or {};state=receipt.get('state')
    labels={'QUEUED':('Amber','В очереди'),'RUNNING':('Blue','Исправляется'),
            'WAITING':('Red','Ждёт источник'),'BLOCKED':('Red','Не исправлено'),
            'SUPERSEDED':('Red','Перепроверить'),'COMPLETED':('Amber','Перепроверка'),
            'RECHECKED':('Amber','Перепроверка')}
    if check and state in labels:
        result['color'],result['label']=labels[state];result['known']=False
        result['reason']=receipt.get('reason') or result['label']
    return result



def priority_request(store,problem):
    # The existing writer revalidates DQ/identity/evidence, retries and risk.
    if not repair_capability(problem,None)['check']:raise ValueError('Нет штатного адресного исправления для этого источника или ТФ')
    from cp_controls import request_repair
    return request_repair(store,problem,priority=True)


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
    tv_accepted=accepted_tv_absence(problem);native=confirmed_native_absence(problem)
    severity='Информация' if native else 'Низкая' if accepted else 'Высокая' if state in ('INVALID','ANALYSIS_BLOCKED') else 'Информация' if state=='SOURCE_LIMITED' else 'Средняя'
    influence='Подтверждение источника' if native else 'Исторический риск принят' if accepted else 'Требуется проверка анализа' if problem.get('kind')=='context' else 'Диагностика архива'
    tv,job=tv_result(problem,jobs);repair=problem.get('repair') or {};review=problem.get('historical_review') or {}
    reason=repair.get('reason') or review.get('reason') or ((job or {}).get('finding') or {}).get('reason')
    next_step=('Отсутствие сделок подтверждено GETTEX; свечи не создавались' if native else 'Пропуск сверён с TV и принят; реальные свечи не изменены' if tv_accepted else reason or ('Ожидание штатного восстановления' if repair.get('state') in ('QUEUED','RUNNING') else 'Нет сохранённого задания восстановления'))
    return dict(problem=problem,action=repair_capability(problem,job),tf=problem.get('tf','—'),span=' — '.join(str(v) for v in problem.get('range') or []) or '—',
                title='Отсутствие сделок подтверждено GETTEX' if native else 'Пропуск сверён с TV и принят' if tv_accepted else STATES.get(state,state),severity=severity,tv='Совпало отсутствие свечей; принято' if tv_accepted else tv,influence=influence,next=next_step,
                source=' / '.join(str((problem.get('identity') or {}).get(k) or '—') for k in ('source','venue','currency')),
                last=repair.get('finished_at') or repair.get('checked_at') or (job or {}).get('checked_at') or '—',
                next_retry=None if tv_accepted or native else repair.get('next_retry') or review.get('next_retry'),job=job,work=tr('Подтверждено GETTEX') if native else tr('Сверка принята') if tv_accepted else repair_progress(repair))


def repair_progress(receipt):
    state=receipt.get('state')
    label={'QUEUED':'Приоритетная очередь' if receipt.get('owner_priority_at') else 'В очереди',
           'RUNNING':'Проверка и восстановление','WAITING':'Ожидание источника',
           'BLOCKED':'Не исправлено','SUPERSEDED':'Исходные данные изменились',
           'COMPLETED':'Результат записан; обновление списка','RECHECKED':'Оценка обновлена'}.get(state,'Ещё не запускалось')
    text=tr(label)
    if receipt.get('next_retry'):
        from zoneinfo import ZoneInfo
        try:until=datetime.fromisoformat(receipt['next_retry'].replace('Z','+00:00')).astimezone(ZoneInfo('Europe/Berlin')).strftime('%d.%m %H:%M:%S')
        except (ValueError,TypeError):until=str(receipt['next_retry'])
        text+=' · '+tr('Следующая попытка')+': '+until
    return text


def read_requests(path,isin):
    from cp_controls import read
    with closing(sqlite3.connect(Path(path).resolve().as_uri()+'?mode=ro',uri=True)) as db:
        db.execute('PRAGMA query_only=ON')
        return read(db,'cp_repair_requests:'+isin,{})


def receipt_clock(receipt):
    values=[]
    for key in ('requested_at','owner_priority_at','started_at','finished_at'):
        try:values.append(datetime.fromisoformat(receipt.get(key,'').replace('Z','+00:00')).timestamp())
        except (ValueError,TypeError,AttributeError):pass
    return max(values,default=0)


def read(path,isin,page=0,include_old=False,now=None,accepted_only=False,native_only=False):
    if accepted_only and native_only:raise ValueError('Choose one review view')
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
        def with_native_resolution(p):
            proof=native_empty_resolution(db,p,indexed.get(binding(p.get('isin'),p.get('tf'),p.get('identity') or {},p.get('range')),[]))
            return dict(p,native_resolution=proof) if proof else p
        visible=[with_native_resolution(p) for p in visible]
        # Keep current, revision-checked acceptances in the shared persisted CP
        # registry, but do not present them as active errors. Revoked/changed
        # scopes return here automatically; no DQ record or evidence is deleted.
        native_count=sum(confirmed_native_absence(p) for p in visible)
        accepted_count=sum(accepted_tv_absence(p) and not confirmed_native_absence(p) for p in visible)
        active_count=len(visible)-accepted_count-native_count
        visible=[p for p in visible if (confirmed_native_absence(p) if native_only else
                 accepted_tv_absence(p) and not confirmed_native_absence(p) if accepted_only else
                 not (accepted_tv_absence(p) or confirmed_native_absence(p)))]
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
    return dict(isin=isin,at=datetime.now(timezone.utc).isoformat(),total=len(rows),accepted_count=accepted_count,native_count=native_count,active_count=active_count,accepted_only=accepted_only,native_only=native_only,old_archive=old_count,archived_count=archived_count,include_old=include_old,page=page,pages=pages,rows=rows[page*100:(page+1)*100])


class Dialog:
    def __init__(self,app):
        from main_screen import native_titlebar
        self.app=app;self.closed=False;self.generation=0;self.page=0;self.packet={};self.shown={};self.busy=False;self.timer=None
        self.win=w=tk.Toplevel(app.root);app._cp_problem_window=w;self.action_buttons={};self.paint_timer=None;self.action_busy=False;self.receipts={};self.watch_timer=None;self.watch_busy=False
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
        self.view_mode=ttk.Combobox(base,values=[tr('Активные проблемы'),tr('Принятые сверки TV'),tr('Подтверждено GETTEX')],state='readonly',style=p+'.TCombobox',width=30)
        self.view_mode.current(0);self.view_mode.pack(anchor='w',pady=(0,6))
        self.view_mode.bind('<<ComboboxSelected>>',lambda _:self.select())
        self.include_old=tk.BooleanVar(w,False)
        ttk.Checkbutton(base,text=tr('Показывать старые и другие архивы'),variable=self.include_old,command=self.select,style=p+'.TCheckbutton').pack(anchor='w',pady=(0,6))
        host=ttk.Frame(base,style=p+'.TFrame');host.pack(fill='both',expand=True)
        columns=('tf','action','work','span','title','severity','tv','influence','source')
        style=ttk.Style(w);style.configure(p+'.CP.Treeview',background=palette['bg'],fieldbackground=palette['bg'],foreground=palette['text'],rowheight=32,font=('Segoe UI',10),borderwidth=0,relief='flat')
        style.map(p+'.CP.Treeview',background=[('selected','#075585')],foreground=[('selected','#ffffff')])
        style.layout(p+'.CP.Treeview.Heading',[('Treeheading.padding',{'sticky':'nsew','children':[('Treeheading.text',{'sticky':'we'})]})])
        style.configure(p+'.CP.Treeview.Heading',background=palette['panel'],foreground=palette['text'],font=('Segoe UI',10,'bold'),padding=(6,8))
        style.configure(p+'.CP.Horizontal.TScrollbar',background=palette['grid'],troughcolor=palette['bg'],bordercolor=palette['bg'],lightcolor=palette['bg'],darkcolor=palette['bg'],arrowcolor=palette['muted'])
        self.action_styles(p,palette)
        self.tree=ttk.Treeview(host,columns=columns,show='headings',style=p+'.CP.Treeview',selectmode='browse')
        for key,label,width in zip(columns,('ТФ','Действие','Работа СР','Дата / диапазон','Проблема','Серьёзность','Сверка TV','Влияние','Источник / площадка'),(55,140,235,205,195,95,195,155,180)):
            self.tree.heading(key,text=tr(label));self.tree.column(key,width=width,minwidth=60)
        vertical=ttk.Scrollbar(host,command=self.tree.yview,style=p+'.Vertical.TScrollbar');vertical.pack(side='right',fill='y')
        self.tree.configure(yscrollcommand=lambda a,b:(vertical.set(a,b),self.schedule_buttons()));self.tree.pack(fill='both',expand=True)
        horizontal=ttk.Scrollbar(base,orient='horizontal',command=self.tree.xview,style=p+'.CP.Horizontal.TScrollbar');horizontal.pack(fill='x');self.tree.configure(xscrollcommand=lambda a,b:(horizontal.set(a,b),self.schedule_buttons()))
        self.tree.bind('<Configure>',lambda _:self.schedule_buttons(),add='+')
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
        self.page=0;self.generation+=1;self.receipts={};self.tree.delete(*self.tree.get_children());self.shown={};self.clear_detail();self.refresh()

    def action_styles(self,p,palette):
        from PIL import Image,ImageDraw,ImageTk
        cache=getattr(self.app.root,'_cp_action_images',None)
        if cache is None:self.app.root._cp_action_images=cache={}
        if p in cache:return
        cache[p]=[];style=ttk.Style(self.win)
        for name,fill,edge in [('Green','#104c30','#25e772'),('Red','#4c1b27','#ff5866'),('Amber','#574020','#f1bd49'),('Blue','#123f65','#35b9ff')]:
            bitmap=Image.new('RGBA',(96,96));ImageDraw.Draw(bitmap).rounded_rectangle((2,2,93,93),radius=15,fill=fill,outline=edge,width=4)
            image=ImageTk.PhotoImage(bitmap.resize((32,32),Image.Resampling.LANCZOS),master=self.app.root);cache[p].append(image)
            element=p+'.CP'+name;style.element_create(element,'image',image,border=6,sticky='nsew')
            style.layout(element+'.TButton',[(element,{'sticky':'nsew','children':[('Button.padding',{'sticky':'nsew','children':[('Button.label',{'sticky':'nsew'})]})]})])
            style.configure(element+'.TButton',foreground='#ffffff',font=('Segoe UI',9,'bold'),padding=(3,1))

    def schedule_buttons(self):
        if not self.closed and self.paint_timer is None:self.paint_timer=self.win.after_idle(self.paint_buttons)

    def paint_buttons(self):
        self.paint_timer=None
        if self.closed:return
        for key,button in list(self.action_buttons.items()):
            if key not in self.shown:button.destroy();self.action_buttons.pop(key)
        visible=set()
        for key,row in self.shown.items():
            bounds=self.tree.bbox(key,'action')
            if not bounds:continue
            visible.add(key)
            button=self.action_buttons.get(key)
            if button is None:
                button=ttk.Button(self.tree,command=lambda k=key:self.act(k));self.action_buttons[key]=button
            cap=row['action'];button.configure(text=tr(cap['label']),style=self.p+'.CP'+cap['color']+'.TButton',state='disabled' if self.action_busy else 'normal')
            x,y,width,height=bounds;button.place(x=x+2,y=y+2,width=max(1,width-4),height=max(1,height-4))
            button.lift()
        for key,button in self.action_buttons.items():
            if key not in visible:button.place_forget()

    def act(self,key):
        if self.closed or self.action_busy or key not in self.shown:return
        row=self.shown[key];self.tree.selection_set(key);self.details()
        if not row['action']['check']:
            self.clear_detail(self.detail.get('1.0','end')+'\n'+tr(row['action']['reason']));return
        self.action_busy=True;self.schedule_buttons();self.status.set(tr('Постановка приоритетного задания…'));problem=row['problem'];isin=problem['isin'];generation=self.generation
        def done(value):
            self.action_busy=False
            # An explicit queued owner request survives closing this read-only view.
            info=next((i for i in self.app.watchlist if i.get('isin')==isin),None)
            if info is not None and hasattr(self.app,'start_feed'):
                self.app.start_feed(info)
                if hasattr(self.app,'dispatcher'):self.app.dispatcher.prioritize_feed('feed:'+isin)
            if self.closed:return
            if generation==self.generation:
                from cp_controls import repair_id
                self.apply_receipts({repair_id(problem):value})
                self.status.set(tr('Приоритетная задача поставлена в штатную очередь')+' · '+repair_progress(value))
                self.schedule_watch(500)
        def failed(error):
            self.action_busy=False
            if self.closed:return
            self.schedule_buttons()
            if generation==self.generation:self.status.set(tr('Не удалось поставить задачу')+': '+str(error))
        if self.app.job(lambda:priority_request(self.app.store,problem),done,failed,lane='read',key=('cp-owner-repair',isin,problem['id'])) is False:failed(tr('Очередь временно занята'))

    def row_values(self,row):
        return [tr(row['action']['label']) if k=='action' else tr(str(row[k])) for k in ('tf','action','work','span','title','severity','tv','influence','source')]

    def apply_receipts(self,receipts):
        from cp_controls import repair_id
        resolved=False
        for key,row in list(self.shown.items()):
            target=repair_id(row['problem']);receipt=receipts.get(target)
            if receipt is None:continue
            old=max((self.receipts.get(target) or {},row['problem'].get('repair') or {}),key=receipt_clock)
            if receipt_clock(receipt)<receipt_clock(old):continue
            self.receipts[target]=receipt
            if receipt.get('state') in ('COMPLETED','RECHECKED','SUPERSEDED') and receipt!=old:resolved=True
            updated=present({**row['problem'],'repair':receipt},[row['job']] if row.get('job') else [])
            self.shown[key]=updated
            self.tree.item(key,values=self.row_values(updated))
        self.schedule_buttons();self.details()
        return resolved

    def schedule_watch(self,delay=3000):
        if self.closed:return
        if self.watch_timer is not None:self.win.after_cancel(self.watch_timer)
        self.watch_timer=self.win.after(delay,self.watch)

    def watch(self):
        if self.watch_timer is not None:self.win.after_cancel(self.watch_timer)
        self.watch_timer=None
        if self.closed or self.choice.current()<0:return
        if self.watch_busy:self.schedule_watch();return
        self.watch_busy=True;generation=self.generation;isin=self.infos[self.choice.current()]['isin']
        def done(value):
            self.watch_busy=False
            if self.closed:return
            if generation==self.generation and self.apply_receipts(value):self.refresh()
            self.schedule_watch()
        def failed(error):
            self.watch_busy=False
            if self.closed:return
            if generation==self.generation:self.status.set(tr('Не удалось обновить ход исправления')+': '+str(error))
            self.schedule_watch()
        if self.app.job(lambda:read_requests(self.app.store.path,isin),done,failed,lane='read',key=('cp-repair-watch',id(self.win),generation)) is False:failed(tr('Очередь временно занята'))

    def clear_detail(self,text=''):
        self.detail.configure(state='normal');self.detail.delete('1.0','end');self.detail.insert('1.0',text);self.detail.configure(state='disabled')

    def details(self,event=None):
        picks=self.tree.selection()
        if not picks:return
        r=self.shown[picks[0]];problem=r['problem'];meta=problem.get('identity') or {}
        text='\n'.join([tr(r['title'])+' · '+r['span'],
            ' · '.join(str(meta.get(k) or '—') for k in KEYS),
            tr('Действие')+': '+tr(r['action']['reason']),
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
        isin=self.infos[self.choice.current()]['isin'];generation=self.generation;page=self.page;include_old=self.include_old.get();accepted_only=self.view_mode.current()==1;native_only=self.view_mode.current()==2
        self.status.set(tr('Чтение сохранённых проблем…'))
        def done(value):
            self.busy=False
            if self.closed:return
            self.refresh_button.configure(state='normal')
            if generation!=self.generation:self.refresh();return
            selected=self.tree.selection()
            self.packet=value;self.page=value['page'];self.tree.delete(*self.tree.get_children());self.shown={};self.clear_detail()
            for n,r in enumerate(value['rows']):
                key=r['problem']['id'];self.shown[key]=r
                self.tree.insert('','end',iid=key,values=self.row_values(r),tags=('high' if r['severity']=='Высокая' else 'medium' if r['severity']=='Средняя' else '',))
            if selected and selected[0] in self.shown:self.tree.selection_set(selected[0])
            self.apply_receipts(self.receipts);self.schedule_watch()
            self.schedule_buttons()
            self.status.set(tr('Активные проблемы')+f": {value.get('active_count',value['total'])} · "+tr('Принятые сверки TV')+f": {value.get('accepted_count',0)} · "+tr('Подтверждено GETTEX')+f": {value.get('native_count',0)} · {self.page+1}/{value['pages']} · "+tr('Архив отдельно')+f": {value.get('archived_count',0)} · "+str(value['at']))
            if not value['rows']:self.clear_detail(tr('Подтверждённых интервалов GETTEX пока нет' if native_only else 'Принятых сверок TV пока нет' if accepted_only else 'В сохранённых отчётах проблем не найдено. Это не подтверждает полноту истории или готовность торговли.'))
            self.timer=self.win.after(60000,self.refresh)
        def failed(error):
            self.busy=False
            if self.closed:return
            if generation!=self.generation:self.refresh();return
            self.refresh_button.configure(state='normal');self.status.set(tr('Не удалось прочитать проблемы')+': '+str(error))
        if self.timer is not None:self.win.after_cancel(self.timer);self.timer=None
        if self.app.job(lambda:read(self.app.store.path,isin,page,include_old,accepted_only=accepted_only,native_only=native_only),done,failed,lane='read',key=('cp-problem-view',id(self.win),generation,page)) is False:failed(tr('Очередь временно занята'))

    def cleanup(self):
        self.closed=True;self.generation+=1
        if self.timer is not None:self.win.after_cancel(self.timer);self.timer=None
        if self.paint_timer is not None:self.win.after_cancel(self.paint_timer);self.paint_timer=None
        if self.watch_timer is not None:self.win.after_cancel(self.watch_timer);self.watch_timer=None

    def close(self):self.cleanup();self.win.destroy()


def show(app):
    old=getattr(app,'_cp_problem_window',None)
    if old is not None and old.winfo_exists():old.lift();return old
    app._cp_problem_dialog=Dialog(app);return app._cp_problem_dialog.win
