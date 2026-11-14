from i18n import tr
"""Existing paper ledger projection; all database work uses the app dispatcher."""
from decimal import Decimal
import tkinter as tk
from tkinter import ttk

def price_text(value):
    return '—' if value is None else f'{Decimal(str(value)):.4f}'

def _auto_refresh(parent,app,refresh,interval_ms=5000):
    """Schedule existing read jobs only; stop with the tab or application."""
    timer=None
    def tick():
        nonlocal timer
        timer=None
        if app.closing or not parent.winfo_exists():return
        refresh()
        timer=parent.after(interval_ms,tick)
    def stop(event):
        nonlocal timer
        if event.widget is parent and timer is not None:
            parent.after_cancel(timer);timer=None
    parent.bind('<Destroy>',stop,add='+')
    tick()

def automation_summary(state):
    auto=state.get('automation',{})
    if not auto:return ''
    from paper_hourly import variable_size
    size=('количество автоматически • до 45% свободных денег на вход • расчётный риск 1%'
        if variable_size(auto) else 'максимум по 1 акции')
    lines=['\nАвтоторговля демо: '+('ВКЛЮЧЕНА' if auto.get('enabled') else 'ПАУЗА')+
        ' • входы 1H и 4H, подтверждение 1H/4H/1D • только GETTEX/EUR\n'+size]
    from paper_protection import enabled
    try:protection=enabled(auto)
    except ValueError:protection=False
    lines.append('Стоп / цель: по закрытой проверенной 5m/15m-свече; цена уровня не гарантируется.'
        if protection else 'Стоп / цель основного счёта: не активированы; выход по CORE. V2 — отдельно в тени.')
    if auto.get('target_equity'):
        equity=state.get('equity');target=Decimal(auto['target_equity'])
        progress=('оценка счёта недоступна' if equity is None else
            'осталось '+str(max(Decimal(0),target-Decimal(equity)))+' EUR')
        lines.append('Цель: '+str(target)+' EUR • '+progress+' • достижение и предел убытка не гарантируются.')
    for isin,row in auto.get('instruments',{}).items():
        name=auto.get('names',{}).get(isin,isin)
        for tf, outcome in row.get('timeframes', {'1H': row}).items():
            code=outcome.get('execution_blocker') or outcome.get('quote_blocker')
            detail={'PAPER_STALE_QUOTE':'Цена исполнения устарела — ожидаем свежую свечу.',
                    'PAPER_DATA_BLOCKED':'Качество текущей цены не подтверждено.'}
            reason=detail.get(code,code or outcome.get('reason') or 'Ожидается первая проверка')
            if outcome.get('state')=='COMPLETED':
                reason='Виртуальная сделка • количество '+str(outcome.get('quantity'))
            lines.append(f"{name} {tf}: {outcome.get('state','ОЖИДАНИЕ')} • CORE "
                f"{outcome.get('core','UNKNOWN')} • {outcome.get('signal','WAIT')}\n"+reason)
    if state.get('archived_fills'):lines.append('Предыдущий прогон сохранён в архиве: '+str(state['archived_fills'])+' операций.')
    return '\n'.join(lines)


def summary(state):
    if state.get('status') != 'ACTIVE': return tr('Демосчёт: ') + str(state.get('status',tr('недоступен')))
    start=state['policy']['starting_cash'];equity=state.get('equity')
    change=tr('не определена') if equity is None else f'{(Decimal(equity)/Decimal(start)-1)*100:.2f}%'
    def value(key): return str(state[key])+' EUR' if state.get(key) is not None else tr('DATA BLOCKED / нет оценки')
    universe=(f"\n{tr('Подключено акций: ')}{len(state.get('allowed_isins',[]))}"+
              tr(' • Новые подключаются автоматически; качество данных и допуск проверяются отдельно.')) if state.get('universe',{}).get('automatic_new_instruments') else ''
    return (f"{state['account']}{tr(' • только виртуальные операции\nНачальный капитал: ')}{start}{tr(' EUR • Доходность: ')}{change}\nCash: {value('cash')} • Equity: {value('equity')}{tr('\nРеализованный P&L: ')}{value('realized')}{tr(' • Нереализованный: ')}{value('unrealized')}{tr(' • Комиссии: ')}{value('fees')}"+universe+automation_summary(state))

def attach(app, parent):
    from paper_orders import order_choices, submit
    from uuid import uuid4
    order_box=ttk.LabelFrame(parent,text='Поручение владельца • только PAPER');order_box.pack(fill='x',padx=10,pady=4)
    row=ttk.Frame(order_box);row.pack(fill='x')
    choices={}
    selected=tk.StringVar(value='');quantity=tk.StringVar(value='1')
    instrument_box=ttk.Combobox(row,textvariable=selected,values=[],state='readonly',width=24)
    instrument_box.pack(side='left',padx=4)
    ttk.Label(row,text='Количество:').pack(side='left')
    quantity_box=ttk.Entry(row,textvariable=quantity,width=10);quantity_box.pack(side='left',padx=4)
    message=tk.StringVar(value='WAIT V1 не запрещает ваше поручение. Качество, свежесть цены и риск обязательны.')
    ttk.Label(order_box,textvariable=message,wraplength=1000,justify='left').pack(anchor='w',padx=4,pady=4)
    def changed(*_):message.set('Поручение изменено; проверьте цену выбранной акции заново.')
    selected.trace_add('write',changed);quantity.trace_add('write',changed)
    order_buttons=[]
    def place(side,preview=False):
        isin=next((k for k,v in choices.items() if v==selected.get()),None)
        if isin is None:message.set('Нет выбранной акции с действующим подключением к PAPER.');return
        try:qty=float(quantity.get().replace(',','.'))
        except ValueError:message.set('Укажите положительное количество акций.');return
        args=dict(isin=isin,side=side,quantity=qty,
            timeframe='AUTO',request_id='manual-'+str(uuid4()))
        instrument_box.configure(state='disabled');quantity_box.configure(state='disabled')
        for button in order_buttons:button.configure(state='disabled')
        message.set('Проверяю данные, цену и риск…')
        future=submit(app,args,'MANUAL',preview)
        def poll():
            if not parent.winfo_exists() or app.closing:return
            if not future.done():parent.after(100,poll);return
            instrument_box.configure(state='readonly');quantity_box.configure(state='normal')
            for button in order_buttons:button.configure(state='normal')
            answer=future.result()
            if answer.get('status') in ('PREVIEW','COMPLETED'):
                f=answer['fill'];plan=f.get('smart_eligibility',{}).get('protection') or {}
                message.set(('Оценка' if preview else 'Исполнено')+f": {choices.get(f['isin'],f['isin'])} {f['side']} {f['quantity']} по {price_text(f['price'])} EUR • "
                    f"комиссия {f['fee']} EUR • стоп {price_text(plan.get('stop'))} • цель {price_text(plan.get('target'))}"+
                    (' • при исполнении цена проверяется заново' if preview else ''))
                refresh()
            else:message.set('Отказ: '+str(answer.get('code'))+' • '+str(answer.get('detail','')))
        poll()
    for label,side,preview in [('Цена BUY','BUY',True),('Цена SELL','SELL',True),('BUY','BUY',False),('SELL','SELL',False)]:
        b=ttk.Button(row,text=label,command=lambda s=side,p=preview:place(s,p));b.pack(side='left',padx=3);order_buttons.append(b)
    # Keep order controls and the ledger visible in the existing small window.
    summary_box=ttk.Frame(parent);summary_box.pack(fill='x',padx=10,pady=4)
    head=tk.Text(summary_box,height=8,wrap='word',font='TkDefaultFont')
    summary_scroll=ttk.Scrollbar(summary_box,orient='vertical',command=head.yview)
    summary_scroll.pack(side='right',fill='y');head.configure(yscrollcommand=summary_scroll.set)
    head.pack(side='left',fill='x',expand=True)
    def set_summary(text):
        offset=head.yview()[0]
        head.configure(state='normal');head.delete('1.0','end');head.insert('1.0',text);head.configure(state='disabled')
        head.yview_moveto(offset)
    set_summary(tr('Загрузка демосчёта…'))
    controls=ttk.Frame(parent);controls.pack(side='bottom',fill='x',padx=10,pady=4)
    notebook=ttk.Notebook(parent);notebook.pack(fill='both',expand=True,padx=10)
    def table(title,columns):
        body=ttk.Frame(notebook);notebook.add(body,text=tr(title))
        tree=ttk.Treeview(body,columns=columns,show='headings',height=6)
        for col in columns: tree.heading(col,text=tr(col));tree.column(col,width=130,stretch=False)
        x=ttk.Scrollbar(body,orient='horizontal',command=tree.xview);x.pack(side='bottom',fill='x')
        y=ttk.Scrollbar(body,orient='vertical',command=tree.yview);y.pack(side='right',fill='y')
        tree.configure(xscrollcommand=x.set,yscrollcommand=y.set);tree.pack(fill='both',expand=True)
        return tree
    positions=table(tr('Открытые позиции'),('ISIN',tr('Количество'),tr('Затраты'),tr('Цена'),'Стоп','Цель',tr('Текущая стоимость'),'P&L',tr('Площадка'),tr('Валюта'),tr('Время оценки'),'DQ'))
    fills=table(tr('История BUY / SELL'),(tr('Время'),'ISIN',tr('Операция'),tr('Количество'),tr('Цена'),'Инициатор','Стоп','Цель',tr('Комиссия'),tr('Площадка'),tr('Валюта'),tr('Свеча')))
    shadow=ttk.Frame(notebook);notebook.add(shadow,text=tr('V1 / V2 — теневые портфели'))
    from paper_strategy_shadow import attach as attach_shadow
    attach_shadow(app,shadow)
    ttk.Label(controls,text=tr('Только демо. WAIT — ожидание условий стратегии; прибыль не гарантируется.')).pack(anchor='w')
    busy=False
    def alive(): return bool(parent.winfo_exists()) and not app.closing
    def done(state):
        nonlocal busy,choices
        busy=False
        if not alive(): return
        current=next((k for k,v in choices.items() if v==selected.get()),None)
        choices=order_choices(state)
        instrument_box.configure(values=list(choices.values()))
        label=choices.get(current,next(iter(choices.values()),''))
        if selected.get()!=label:selected.set(label)
        set_summary(summary(state))
        for tree in (positions,fills):
            tree.delete(*tree.get_children())
        for p in state.get('positions',[]):
            if not Decimal(p['quantity']): continue
            ident=p['identity']
            detail=(tr('Цена проверена')+' '+p.get('mark_timeframe','')) if p.get('mark_context')=='CURRENT_CLOSED_BAR' else p['mark_status']
            if p.get('archive_quality') not in (None,'READY'):detail+=tr(' • архив: ')+p['archive_quality']
            if p.get('mark_reason'):detail+=': '+p['mark_reason']
            plan=p.get('protection') or {}
            positions.insert('','end',values=(p['isin'],p['quantity'],p['cost'],p.get('mark','—'),price_text(plan.get('stop')),price_text(plan.get('target')),p.get('market_value','—'),p.get('unrealized','—'),ident['venue'],ident['currency'],p.get('mark_at','—'),detail))
        for f in state.get('fills',[]):
            plan=f.get('smart_eligibility',{}).get('protection') or {}
            fills.insert('','end',values=(f['at'],f['isin'],f['side'],f['quantity'],price_text(f['price']),f.get('initiator','Ранее'),price_text(plan.get('stop')),price_text(plan.get('target')),f['fee'],f['identity']['venue'],f['identity']['currency'],f['bar_close_at']))
    def failed(message):
        nonlocal busy
        busy=False
        if alive(): set_summary(tr('Демосчёт временно недоступен; данные не изменены.'))
    def refresh():
        nonlocal busy
        if busy or not alive(): return
        busy=True
        from paper_account import status
        if app.job(lambda:status(app.store.path),done,failed,lane='read',key='paper_portfolio_read:'+str(parent)) is False:busy=False
    ttk.Button(controls,text=tr('Обновить демосчёт'),command=refresh).pack(side='left',padx=4)
    def toggle():
        def work():
            import json
            from paper_hourly import CONFIG,read
            with app.store.connect() as db:
                db.execute('BEGIN IMMEDIATE');cfg=read(db,CONFIG,{})
                if cfg.get('approved') is not True:raise ValueError('Нет настроенного разрешения владельца')
                cfg['enabled']=not cfg.get('enabled',False)
                db.execute('INSERT OR REPLACE INTO settings VALUES (?,?)',(CONFIG,json.dumps(cfg)))
        app.job(work,lambda _:refresh(),failed,lane='misc',key='paper_auto_toggle')
    ttk.Button(controls,text=tr('Автоторговля 1H: пауза / продолжить'),command=toggle).pack(side='left',padx=4)
    _auto_refresh(parent,app,refresh)
