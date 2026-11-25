"""V1 real-account UI. Broker executions are recorded, never submitted."""
import tkinter as tk
from datetime import datetime
from tkinter import ttk
from uuid import uuid4
from zoneinfo import ZoneInfo

from i18n import tr, language
from financial_format import format_date, format_value
from reference_design import signal_reason, wrap_label


def amount(value):
    return '—' if value is None else format_value(value, language(), kind='amount', currency='EUR')


def local_time(value):
    if not value:return '—'
    from real_portfolio import moment
    try:return moment(value).astimezone(ZoneInfo('Europe/Berlin')).strftime('%d.%m.%Y %H:%M:%S %Z')
    except (ValueError,TypeError):return str(value)


DECISIONS = {
    'WAIT': ('Ждать', 'Сейчас V1 не рекомендует покупку или продажу.'),
    'HOLD': ('Держать', 'V1 рекомендует сохранить текущую позицию.'),
    'BUY': ('К покупке', 'V1 нашёл условия для покупки.'),
    'SELL': ('К продаже', 'V1 нашёл условия для выхода из позиции.'),
}

REASONS = {
    'MACD выше signal, histogram усиливается': 'Импульс роста усиливается (MACD).',
    'MACD ниже signal, отрицательный histogram усиливается': 'Импульс снижения усиливается (MACD).',
    'Timing TF сохраняет bearish структуру': 'На интервале поиска входа сохраняется снижение.',
    'Средний и старший TF не согласованы bullish': 'Рост не подтверждён одновременно на двух старших интервалах.',
    'Нет подтверждённого breakout/retest/reclaim': 'Пробой или возврат выше уровня пока не подтверждён.',
    'Momentum входа не подтверждён': 'Импульс для входа пока не подтверждён.',
    'V1_WEEKLY_HISTORY_WAIT': 'Не хватает закрытых недельных данных.',
    'V1_WEEKLY_TREND_WAIT': 'Недельный тренд пока не подтверждает вход.',
    'V1_DAILY_HISTORY_WAIT': 'Не хватает закрытых дневных данных.',
    'V1_DAILY_SETUP_WAIT': 'На дневном графике пока нет сигнала для входа.',
    'V1_NEXT_SESSION_WAIT': 'Условия входа нужно проверить в следующую сессию.',
    'DAILY_20_SESSION_BREAKOUT': 'Дневная свеча закрылась выше максимума за 20 сессий.',
    'DAILY_MA20_REBOUND': 'Дневная цена отскочила от средней за 20 сессий.',
}

OPERATION_NAMES = {
    'BUY':'Покупка', 'SELL':'Продажа', 'DIVIDEND':'Дивиденды',
    'DEPOSIT':'Пополнение', 'WITHDRAWAL':'Вывод средств', 'FEE':'Комиссия',
}
OPERATION_STATUSES = {
    'SETTLED':'Рассчитано', 'FILLED':'Исполнено', 'EXECUTED':'Исполнено',
    'PARTIALLY_FILLED':'Частично исполнено', 'PARTIAL_FILLED':'Частично исполнено',
    'PENDING':'Ожидает исполнения', 'OPEN':'Открыта', 'CREATED':'Создана',
    'PLACED':'Размещена', 'CANCELLED':'Отменена', 'REJECTED':'Отклонена',
    'EXPIRED':'Срок истёк', 'OWNER_RECORDED':'Внесено владельцем',
}


def operation_fields(event, names):
    entry = next((event[key] for key in ('security','cash','nonTradeSecurity','eltif')
                  if isinstance(event.get(key),dict) and event[key]), event)
    manual = event.get('source') == 'OWNER_RECORDED_EXECUTION'
    kind = entry.get('side') or entry.get('transactionType') or event.get('kind')
    status = event.get('status') or ('OWNER_RECORDED' if manual else None)
    currency = entry.get('currency') or event.get('currency') or ('EUR' if manual else None)
    isin = entry.get('isin') or event.get('isin')
    name = event.get('description') or entry.get('name') or names.get(isin) or isin
    return entry, manual, kind, status, currency, isin, name


def operation_view(event, names):
    entry, manual, kind, status, currency, isin, name = operation_fields(event, names)

    def formatted(raw, field='amount'):
        if raw is None or raw == '':
            return '—'
        return format_value(raw,language(),kind=field,currency=currency,digits=8 if field=='quantity' else 2)

    date = event.get('lastEventAt') or event.get('at')
    when = format_date(date,language(),clock=True)
    operation = tr(OPERATION_NAMES.get(kind,kind)) if kind else '—'
    state = tr(OPERATION_STATUSES.get(status,status)) if status else '—'
    quantity = formatted(entry.get('quantity'),'quantity')
    total = formatted(entry.get('amount'))
    price = formatted(entry.get('price') if manual else entry.get('executionPrice'),'price')
    fields = [
        ('Операция',operation), ('Инструмент',name), ('ISIN',isin),
        ('Дата записи',when), ('Статус',state), ('Количество по записи',quantity),
        ('Цена исполнения',price), ('Сумма по записи',total),
        ('Комиссия',formatted(entry.get('fee'))), ('Налоги',formatted(entry.get('taxes'))),
        ('Валюта',currency), ('Номер записи',event.get('id')),
    ]
    if event.get('isCancellation'):
        fields.append(('Корректировка',tr('Сторнирование')))
    details = '\n'.join(tr(label)+': '+str(value or '—') for label,value in fields)
    return kind, (when,name or '—',operation,state,quantity,total), details


def recommendation(result):
    signal = result.get('signal')
    heading, summary = DECISIONS.get(signal, ('Нет рекомендации', ''))
    if result.get('core') == 'DATA BLOCKED':
        heading, summary = 'Расчёт недоступен', 'Для рекомендации не хватает подтверждённых данных.'
    reasons, warnings = [], []
    for part in (result.get('reason') or '').split('; '):
        if not part:
            continue
        if ': предупреждение CandlePilot ' in part:
            tf = part.split(':', 1)[0]
            warnings.append(tr('{tf}: CandlePilot отметил замечание к свечам. Расчёт использует фактические данные GETTEX.',tf=tf))
        elif part != 'расчёт по фактическим GETTEX-барам' or not warnings:
            reasons.append(tr(REASONS[part]) if part in REASONS else signal_reason(part))
    facts = result.get('long_term') or {}
    currency = (result.get('identity') or {}).get('currency')
    levels = []
    for key, label in (('signal_close','Закрытие дня'),('support','Поддержка'),('entry_level','Ориентир для входа')):
        if facts.get(key) is not None:
            levels.append((tr(label),format_value(facts[key],language(),kind='price',currency=currency)))
    return tr(heading), tr(summary), reasons, warnings, levels


def attach(app, parent):
    from real_portfolio import status
    from paper_view import _auto_refresh

    frame = ttk.Frame(parent, padding=10);frame.pack(fill='both', expand=True)
    ttk.Label(frame, text=tr('V1 — реальный счёт'), font=('Segoe UI',16,'bold')).pack(anchor='w')
    provenance = tk.StringVar(value=tr('Загрузка реального счёта…'))
    wrap_label(ttk.Label(frame, textvariable=provenance, wraplength=900)).pack(anchor='w', pady=5)
    kpis = ttk.Frame(frame);kpis.pack(fill='x')
    values = {}
    for col,(key,label) in enumerate((('equity','Чистая стоимость счёта'),('securities','Стоимость бумаг'),
            ('loan','Использованный кредит'),('available','Доступно без кредита'))):
        box=ttk.LabelFrame(kpis,text=tr(label),padding=8);box.grid(row=0,column=col,sticky='nsew',padx=3)
        kpis.columnconfigure(col,weight=1,uniform='real-kpi')
        values[key]=tk.StringVar(value='—')
        ttk.Label(box,textvariable=values[key],font=('Segoe UI',14,'bold')).pack(anchor='w')
    details=tk.StringVar()
    wrap_label(ttk.Label(frame,textvariable=details,wraplength=900)).pack(anchor='w',pady=6)
    bar=ttk.Frame(frame);bar.pack(fill='x',pady=4)
    notebook=ttk.Notebook(frame);notebook.pack(fill='both',expand=True)
    state=None;busy=False;generation=0;page=0;journal=[];read_revision=None;operation_names={}

    def table(title, columns):
        tab=ttk.Frame(notebook);notebook.add(tab,text=tr(title))
        body=ttk.Frame(tab);body.pack(fill='both',expand=True)
        tree=ttk.Treeview(body,columns=[k for k,_ in columns],show='headings',height=11)
        for key,label in columns:
            tree.heading(key,text=tr(label));tree.column(key,width=155 if key in ('name','at','id') else 115,stretch=False)
        x=ttk.Scrollbar(body,orient='horizontal',command=tree.xview);x.pack(side='bottom',fill='x')
        y=ttk.Scrollbar(body,orient='vertical',command=tree.yview);y.pack(side='right',fill='y')
        tree.configure(xscrollcommand=x.set,yscrollcommand=y.set);tree.pack(fill='both',expand=True)
        return tab,tree

    _,positions=table('Позиции',(('name','Название'),('isin','ISIN'),('quantity','Количество'),
        ('fifo','Цена FIFO'),('mark','Цена брокера'),('value','Текущая стоимость'),('pnl','Нереализованный P&L'),
        ('method','Метод себестоимости'),
        ('stop','Стоп'),('target','Цель'),('at','Время цены'),('coverage','Расчёт V1')))
    journal_tab,operations=table('Операции счёта',(('at','Дата'),('name','Инструмент / описание'),('kind','Операция'),
        ('status','Статус'),('quantity','Количество'),('amount','Сумма')))
    operations.column('name',width=245,stretch=True)
    operations.column('status',width=160)
    pager=ttk.Frame(journal_tab);pager.pack(fill='x',pady=4)
    page_text=tk.StringVar()
    filters={tr(label):kind for label,kind in (('Все операции',None),('Покупки','BUY'),('Продажи','SELL'))}
    journal_filter=ttk.Combobox(pager,values=list(filters),state='readonly',width=17)
    journal_filter.current(0)
    journal_filter.pack(side='left',padx=(0,10))
    details_box=ttk.LabelFrame(journal_tab,text=tr('Детали операции'),padding=5)
    details_box.pack(fill='x')
    log_detail=tk.Text(details_box,height=7,wrap='word',padx=8,pady=5)
    log_scroll=ttk.Scrollbar(details_box,orient='vertical',command=log_detail.yview)
    log_scroll.pack(side='right',fill='y')
    log_detail.configure(yscrollcommand=log_scroll.set)
    log_detail.pack(fill='x')
    visible_operations={}
    advice=ttk.Frame(notebook,padding=8);notebook.add(advice,text=tr('Рекомендации V1'))
    selection=tk.StringVar();choices={}
    row=ttk.Frame(advice);row.pack(fill='x')
    ttk.Label(row,text=tr('Инструмент')).pack(side='left')
    selector=ttk.Combobox(row,textvariable=selection,state='readonly',width=48);selector.pack(side='left',padx=5)
    result_text=tk.Text(advice,wrap='word',height=12,padx=12,pady=10,relief='flat',borderwidth=0)
    scroll=ttk.Scrollbar(advice,orient='vertical',command=result_text.yview)
    scroll.pack(side='right',fill='y',pady=8)
    result_text.configure(yscrollcommand=scroll.set)
    result_text.pack(fill='both',expand=True,pady=8)
    result_text.tag_configure('decision',font=('Segoe UI',17,'bold'),spacing3=6)
    result_text.tag_configure('section',font=('Segoe UI',11,'bold'),spacing1=12,spacing3=4)
    result_text.tag_configure('body',font=('Segoe UI',11),spacing3=3)
    result_text.tag_configure('meta',font=('Segoe UI',10),spacing3=4)
    notice=tk.StringVar()
    ttk.Label(frame,textvariable=notice,wraplength=1100).pack(anchor='w',pady=4)

    def text(widget,value):
        widget.configure(state='normal');widget.delete('1.0','end');widget.insert('1.0',value);widget.configure(state='disabled')

    text(result_text,tr('Выбери акцию и нажми «Проверить рекомендацию».'))
    operation_hint=tr('Выбери операцию в списке.')
    text(log_detail,operation_hint)

    def show_recommendation(result):
        heading, summary, reasons, warnings, levels = recommendation(result)
        result_text.configure(state='normal')
        result_text.delete('1.0','end')
        def line(value, tag='body'):
            result_text.insert('end',value+'\n',tag)
        line(heading,'decision')
        if summary:line(summary)
        quantity = format_value(result['real_quantity'],language(),kind='quantity',digits=8)
        line(tr('В портфеле: {quantity} шт.',quantity=quantity),'meta')
        if reasons:
            line(tr('Почему V1 так считает'),'section')
            for reason in reasons:line('• '+reason)
        if levels:
            line(tr('Уровни на графике'),'section')
            for label, value in levels:line(label+'  '+value)
        if warnings:
            line(tr('Качество данных'),'section')
            for warning in warnings:line(warning)
        line(tr('Данные расчёта'),'section')
        closed=(result.get('long_term') or {}).get('signal_day') or result.get('signal_at')
        line(tr('Дневная свеча {date} · расчёт {at}',date=format_date(closed,language()),
            at=local_time(result['generated_at'])),'meta')
        result_text.configure(state='disabled')
        result_text.yview_moveto(0)

    def journal_rows():
        nonlocal page
        selected=filters.get(journal_filter.get())
        rows=[(i,event) for i,(kind,event) in enumerate(journal) if selected is None or kind==selected]
        pages=max(1,(len(rows)+49)//50)
        page=min(page,pages-1)
        operations.delete(*operations.get_children())
        visible_operations.clear()
        for i,event in rows[page*50:(page+1)*50]:
            _,row_values,row_details=operation_view(event,operation_names)
            operations.insert('','end',iid=str(i),values=row_values)
            visible_operations[str(i)]=row_details
        text(log_detail,operation_hint if rows else tr('Операций по этому фильтру нет.'))
        page_text.set(tr('Страница {page} из {pages} · записей {count}',page=page+1,pages=pages,count=len(rows)))

    def turn(delta):
        nonlocal page
        page=max(0,page+delta);journal_rows()

    def change_filter(_event):
        nonlocal page
        page=0
        journal_rows()

    journal_filter.bind('<<ComboboxSelected>>',change_filter)
    ttk.Button(pager,text='◀',command=lambda:turn(-1)).pack(side='left')
    ttk.Label(pager,textvariable=page_text).pack(side='left',padx=8)
    ttk.Button(pager,text='▶',command=lambda:turn(1)).pack(side='left')
    def selected_event(_=None):
        selected=operations.selection()
        if selected and selected[0] in visible_operations:
            text(log_detail,visible_operations[selected[0]])
    operations.bind('<<TreeviewSelect>>',selected_event)

    def render(value):
        nonlocal state,journal,read_revision,generation,operation_names
        if not parent.winfo_exists():return
        state=value
        if value.get('status')!='AVAILABLE':
            provenance.set(tr('Реальный счёт V1 ещё не заполнен.'));return
        current=value['revision']
        if current==read_revision:return
        if read_revision!=current:
            generation+=1
            if read_revision is not None:text(result_text,tr('Портфель изменился. Запроси расчёт V1 заново.'))
        read_revision=current
        observed=local_time(value['observed_at']);stamp=local_time(value['broker_timestamps'].get('valuationTimestampUtc'))
        provenance.set(tr('{broker} · сохранённые данные от {observed}',broker=value['broker'],observed=observed))
        values['equity'].set(amount(None if value['local_events'] else value['broker_valuation'].get('total')))
        values['securities'].set(amount(value['broker_valuation'].get('securities')))
        values['loan'].set(amount(value['credit'].get('loaned')))
        values['available'].set(amount(None if value['local_events'] else value['broker_cash'].get('buyingPowerWithoutCredit')))
        details.set(tr('Позиции: {count} · денежный остаток: {cash} · оценка брокера: {stamp}',
            count=sum(float(p['quantity'])>0 for p in value['positions']),cash=amount(value['cash']),stamp=stamp))
        if value['local_events']:
            details.set(details.get()+'\n'+tr('После снимка внесены операции. Для актуальной стоимости счёта нужен новый снимок SC.'))
        selected=positions.selection()
        positions.delete(*positions.get_children())
        for p in value['positions']:
            if float(p['quantity'])<=0:continue
            levels=p.get('levels',{})
            positions.insert('','end',iid=p['isin'],values=(p['name'],p['isin'],p['quantity'],p.get('fifo_price') or '—',
                p.get('mark') or '—',amount(p.get('market_value')),amount(p.get('unrealized')),
                tr({'BROKER_FIFO':'FIFO брокера','OWNER_WEIGHTED_COST_ESTIMATE':'Средняя стоимость, оценка'}.get(p.get('cost_method'),'Неизвестно')),
                levels.get('stop','—'),levels.get('target','—'),local_time(p.get('mark_at')),
                tr('Доступен запрос') if p['registered'] else tr('Нет в каталоге СМ')))
        if selected and positions.exists(selected[0]):positions.selection_set(selected[0])
        operation_names={p['isin']:p['name'] for p in value['positions']}
        events=sorted(value['broker_transactions']+value['local_events'],
                      key=lambda e:e.get('lastEventAt') or e.get('at') or '',reverse=True)
        journal=[(operation_fields(event,operation_names)[2],event) for event in events]
        journal_rows()
        choices.clear();choices.update({i['isin']:i['name']+' · '+i['isin'] for i in value['instruments']})
        selector.configure(values=list(choices.values()))
        if selection.get() not in choices.values():selection.set(next(iter(choices.values()),''))
        notice.set(tr('V1 — учёт реального счёта и рекомендации. Исполнение сделок — у брокера.'))

    def failed(error):
        nonlocal busy
        busy=False
        if parent.winfo_exists():notice.set(tr('Не удалось прочитать данные: {error}',error=str(error)))

    def refresh():
        nonlocal busy
        if busy or not parent.winfo_exists() or app.closing:return
        busy=True
        def done(value):
            nonlocal busy
            busy=False;render(value)
        if app.job(lambda:status(app.store.path),done,failed,lane='read',key='real-v1:'+str(parent)) is False:busy=False

    def calculate():
        nonlocal generation
        if state is None or state.get('status')!='AVAILABLE':return
        isin=next((k for k,v in choices.items() if v==selection.get()),None)
        if isin is None:return
        from analytical_read import capture
        values=capture(app,dict(isin=isin,timeframe='1D'))
        values['real_revision']=state['revision']
        generation+=1;ticket=generation
        text(result_text,tr('Расчёт V1 по закрытым данным…'))
        def done(result):
            if not parent.winfo_exists() or ticket!=generation:return
            from real_portfolio import moment
            if datetime.now(ZoneInfo('UTC'))>=moment(result['valid_until']):
                text(result_text,tr('Срок расчёта истёк. Запроси расчёт V1 заново.'));return
            try:app.store.read_cache.assert_current(result.get('_input_revisions',{}))
            except Exception as error:
                text(result_text,tr('Входные данные изменились: {error}',error=str(error)));return
            show_recommendation(result)
        def error(message):
            if parent.winfo_exists() and ticket==generation:text(result_text,tr('Расчёт недоступен: {error}',error=str(message)))
        app.job(lambda:app.compute_chart.calculate('real_v1',values),done,error,lane='read',key='real-v1-advice:'+str(parent))

    ttk.Button(row,text=tr('Проверить рекомендацию'),command=calculate).pack(side='left',padx=5)
    def show_position(_=None):
        if positions.selection():
            isin=positions.selection()[0]
            if isin in choices and selection.get()!=choices[isin]:
                selection.set(choices[isin])
                change_instrument()
    positions.bind('<<TreeviewSelect>>',show_position)
    def change_instrument(_=None):
        nonlocal generation
        generation+=1
        text(result_text,tr('Инструмент изменён. Запроси расчёт V1 заново.'))
    selector.bind('<<ComboboxSelected>>',change_instrument)

    def record():
        if state is None or state.get('status')!='AVAILABLE':return
        from real_portfolio import operation
        win=tk.Toplevel(parent);win.title(tr('Учесть исполненную операцию V1'))
        body=ttk.Frame(win,padding=12);body.pack(fill='both',expand=True)
        fields={}
        def field(key,label,value=''):
            ttk.Label(body,text=tr(label)).pack(anchor='w');var=tk.StringVar(win,value)
            ttk.Entry(body,textvariable=var,width=48).pack(fill='x');fields[key]=var
        kind=tk.StringVar(win,'BUY')
        ttk.Label(body,text=tr('Тип операции')).pack(anchor='w')
        ttk.Combobox(body,textvariable=kind,values=('BUY','SELL','DEPOSIT','WITHDRAWAL','DIVIDEND','FEE'),state='readonly').pack(fill='x')
        field('isin','ISIN',positions.selection()[0] if positions.selection() else '')
        field('quantity','Количество');field('price','Цена исполнения, EUR');field('amount','Сумма, EUR')
        field('fee','Комиссия, EUR','0')
        field('at','Время исполнения с часовым поясом',datetime.now(ZoneInfo('Europe/Berlin')).isoformat(timespec='seconds'))
        confirmed=tk.BooleanVar(win,False)
        ttk.Checkbutton(body,text=tr('Подтверждаю: операция уже исполнена у брокера'),variable=confirmed).pack(pady=8)
        info=tk.StringVar();ttk.Label(body,textvariable=info,wraplength=420).pack()
        revision=state['revision'];request_id='real-v1-'+str(uuid4())
        def save():
            event={k:v.get().strip().replace(',','.') if k not in ('at','isin') else v.get().strip() for k,v in fields.items()}
            event.update(kind=kind.get(),id=request_id,confirmed_execution=confirmed.get())
            event['fee']=event.get('fee') or '0'
            from real_portfolio import write_operation
            button.configure(state='disabled')
            def done(_):
                if win.winfo_exists():win.destroy()
                refresh()
            def error(message):
                if win.winfo_exists():button.configure(state='normal');info.set(str(message))
            if app.job(lambda:write_operation(app.store.path,event,revision),done,error,
                       lane='misc',key=request_id) is False:button.configure(state='normal')
        button=ttk.Button(body,text=tr('Сохранить в учёте V1'),command=save);button.pack(pady=8)

    ttk.Button(bar,text=tr('Перечитать сохранённые данные'),command=refresh).pack(side='left',padx=3)
    ttk.Button(bar,text=tr('Учесть исполненную операцию'),command=record).pack(side='left',padx=3)
    def edit_levels():
        if state is None or not positions.selection():return
        from real_portfolio import write_levels
        isin=positions.selection()[0]
        current=next(p for p in state['positions'] if p['isin']==isin)
        win=tk.Toplevel(parent);win.title(tr('Стоп и цель V1'))
        body=ttk.Frame(win,padding=12);body.pack()
        fields={}
        for key,label in (('stop','Стоп'),('target','Цель')):
            ttk.Label(body,text=tr(label)+' · EUR').pack(anchor='w')
            fields[key]=tk.StringVar(win,current.get('levels',{}).get(key,''))
            ttk.Entry(body,textvariable=fields[key],width=30).pack(fill='x')
        message=tk.StringVar();ttk.Label(body,textvariable=message,wraplength=320).pack()
        revision=state['revision']
        def save():
            levels={k:v.get().strip().replace(',','.') or None for k,v in fields.items()}
            def done(_):
                if win.winfo_exists():win.destroy()
                refresh()
            def error(value):
                if win.winfo_exists():message.set(str(value))
            app.job(lambda:write_levels(app.store.path,isin,levels['stop'],levels['target'],revision),done,error,
                    lane='misc',key='real-v1-levels:'+isin)
        ttk.Button(body,text=tr('Сохранить в учёте V1'),command=save).pack(pady=8)
    ttk.Button(bar,text=tr('Стоп и цель V1'),command=edit_levels).pack(side='left',padx=3)
    _auto_refresh(parent,app,refresh,interval_ms=15000)
