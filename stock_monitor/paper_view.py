from i18n import tr
"""Existing paper ledger projection; all database work uses the app dispatcher."""
from datetime import datetime
from decimal import Decimal
import tkinter as tk
from tkinter import ttk
from zoneinfo import ZoneInfo

JOURNAL_ZONE=ZoneInfo('Europe/Berlin')

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
    from paper_entry_modes import STRATEGY_CODES, V2_RESEARCH_BLOCKER
    from paper_hourly import TIMEFRAME_POLICY, configured_strategy, configured_timeframes, variable_size
    size=('количество автоматически • до 45% свободных денег на вход • расчётный риск 1%'
        if variable_size(auto) else 'максимум по 1 акции')
    try:
        signal_intervals=' → '.join(configured_timeframes(auto))
        mode=configured_strategy(auto)
    except ValueError:
        signal_intervals='недоступно'
        mode='V1'
    if mode in ('MOMENTUM','AGGRESSIVE'):
        size='до 10 акций на позицию • доступные средства'
    if mode=='V2':
        size=tr('V2: одна позиция, до 10 акций, до 45% средств и 0,5% расчётного риска с издержками.')
    lines=['\nАвтоторговля демо: '+('ВКЛЮЧЕНА' if auto.get('enabled') else 'ПАУЗА')+
        ' • '+STRATEGY_CODES[mode]+' • ТФ автоматически ('+signal_intervals+') • только GETTEX/EUR\n'+size]
    if auto.get('independent'):
        running=', '.join(state.get('automatic_portfolios',[]))
        unready=[name for name,book in state.get('portfolios',{}).items()
                 if book.get('readiness_blocker')=='V2_SEED_PROOF_MISSING']
        if unready:
            lines.append(', '.join(unready)+': ждут подтверждения устойчивости индикаторов.')
        if mode=='V2' and state.get('portfolios',{}).get('V2',{}).get('readiness_blocker')==V2_RESEARCH_BLOCKER:
            lines.append(tr('V2 30m проходит проверку. Автоматический вход приостановлен.'))
        lines.insert(0,'\n'+(
            'Фоновая демоторговля • '+running if running else
            'Фоновая демоторговля на паузе'))
    from paper_protection import enabled
    from paper_v1_long_term import enabled as long_term_enabled
    try:protection=enabled(auto)
    except ValueError:protection=False
    lines.append('Цена исполнения берётся с закрытой свечи 15 мин.')
    from paper_v5_four_hour import (
        enabled as four_hour_enabled, WEEKLY_UNAVAILABLE, WEEKLY_NOT_RISING,
        RECOVERY_UNAVAILABLE, WEIGHTED_UNAVAILABLE, WEIGHTED_PRICE_WAIT,
        INDIVIDUAL_KEY, INDIVIDUAL_WINDOW_WAIT, RECLAIM_KEY,
        RECLAIM_DATA_WAIT, RECLAIM_EARLIER_WAIT, RECLAIM_BUY,
    )
    if long_term_enabled(auto):
        lines.append(tr('Недельный тренд, дневной вход и подтягиваемый стоп.'))
    elif four_hour_enabled(auto):
        lines.append(tr('V5 4H: недельный рост или дневное восстановление. Цена входа должна быть выше взвешенной цены двух торговых часов.'))
        if auto.get(RECLAIM_KEY,True) is True:
            lines.append(tr('V5 4H проверяет цену до четвёртой свечи 15 мин, если первая не подтвердила пробой или взвешенную цену. Для Micron действует первая проверка.'))
        for isin,rule in auto.get(INDIVIDUAL_KEY,{}).get('rules',{}).items():
            name=auto.get('names',{}).get(isin,isin)
            for clock,minutes in sorted(rule.get('hold_by_close',{}).items()):
                lines.append(tr('{name} · вход {clock} · удержание {hours:g} ч',
                                name=name,clock=clock,hours=minutes/60))
            if rule.get('allowed_closes'):
                lines.append(tr('{name} · окна 4H {closes}',name=name,closes=', '.join(rule['allowed_closes'])))
        from paper_v5_daily_pullback import enabled as daily_pullback_enabled
        if daily_pullback_enabled(auto):
            lines.append(tr('Дневной откат V5 использует предыдущую закрытую дневную свечу и вторую закрытую свечу 15 мин для входа. Счёт общий.'))
    elif mode=='AGGRESSIVE':
        lines.append('V5 сверяет 11 ТФ. При одновременных BUY приоритет у подходящего сигнала 4H.')
    if mode in ('MOMENTUM','AGGRESSIVE'):
        from datetime import datetime, timezone
        from paper_v5 import MACRO_VERIFIED_THROUGH, macro_release_day
        calendar=macro_release_day(datetime.now(timezone.utc))
        if calendar['state']=='CALENDAR_UNCONFIRMED':
            lines.append('Макрокалендарь на текущую дату не проверен. Действуют обычные правила входа.')
        elif calendar['state']=='RELEASE_DAY':
            lines.append('Сегодня без новых BUY — '+calendar['kind']+'. Сопровождение позиций продолжается.')
        else:
            lines.append('Календарь CPI, PPI, занятости и ставки ФРС проверен до '+MACRO_VERIFIED_THROUGH+'.')
    lines.append('Стоп / цель используют ту же цену исполнения.'
        if protection or mode!='V1' else 'Выходы по сигналу V1.')
    if auto.get('target_equity'):
        equity=state.get('equity');target=Decimal('10500.00' if mode=='MOMENTUM' else auto['target_equity'])
        progress=('оценка счёта недоступна' if equity is None else
            'осталось '+str(max(Decimal(0),target-Decimal(equity)))+' EUR')
        lines.append('Цель: '+str(target)+' EUR • '+progress+' • достижение и предел убытка не гарантируются.')
    for isin,row in auto.get('instruments',{}).items():
        name=auto.get('names',{}).get(isin,isin)
        if (row.get('timeframe_policy')!=TIMEFRAME_POLICY
                or row.get('strategy_mode','V1')!=mode):
            lines.append(f'{name}: ожидается расчёт после смены режима.')
            continue
        analysis=row.get('analysis_timeframes',{})
        if analysis and mode=='AGGRESSIVE':
            checked=datetime.fromisoformat(row['analysis_at']).astimezone(JOURNAL_ZONE)
            context_only=[f"{tf} {data.get('quality') or data.get('core')}"
                          for tf,data in analysis.items() if tf not in row['candidate_timeframes']]
            lines.append(f"{name} • анализ {len(analysis)} ТФ, {checked:%d.%m %H:%M:%S}\n"+
                         'Дополнительные графики: '+', '.join(context_only))
        for tf, outcome in row.get('timeframes',{}).items():
            selected = ' • основной результат' if tf==row.get('signal_timeframe') and len(row['timeframes'])>1 else ''
            code=outcome.get('execution_blocker') or outcome.get('quote_blocker')
            detail={'PAPER_STALE_QUOTE':'Цена исполнения устарела — ожидаем свежую свечу.',
                    'PAPER_DATA_BLOCKED':'Качество текущей цены не подтверждено.',
                    'V1_DAILY_HISTORY_WAIT':tr('Нужна полная история за 26 дневных сессий.'),
                    'V1_WEEKLY_HISTORY_WAIT':tr('Нужна подтверждённая недельная история.'),
                    'V1_WEEKLY_TREND_WAIT':tr('Недельный рост пока не подтверждён.'),
                    'V1_DAILY_SETUP_WAIT':tr('Ждём дневного пробоя или восстановления после отката.'),
                    'V1_NEXT_SESSION_WAIT':tr('Вход возможен в следующую торговую сессию.'),
                    'V1_DAILY_TRAILING_STOP':tr('Позицию ведёт дневной подтягиваемый стоп.'),
                    'PAPER_V1_ENTRY_LEVEL_LOST':tr('Цена исполнения потеряла уровень дневного входа.'),
                    'PAPER_V1_QUOTE_OUTSIDE_SIGNAL_SESSION':tr('Нужна закрытая цена следующей сессии.'),
                    'DAILY_20_SESSION_BREAKOUT':tr('Дневной пробой максимума 20 сессий.'),
                    'DAILY_MA20_REBOUND':tr('Дневное восстановление после отката.'),
                    'V5_SELECTED_CORE_WAIT':'На этом интервале CORE пока не подтвердил BUY.',
                    'V5_PORTFOLIO_4H_BREAKOUT_ONLY':'На 4H ждём пробоя диапазона первого часа.',
                    'V5_PORTFOLIO_52W_WAIT':'Предыдущее дневное закрытие дальше 5% от годового максимума.',
                    'V5_PORTFOLIO_DAILY_DATA_WAIT':'Для правил V5 пока недостаточно подтверждённых дневных свечей.',
                    'V5_PORTFOLIO_1D_STRENGTH_WAIT':'На дневке пока нет относительной силы или подтверждённого сжатия волатильности.',
                    'V5_STEADY_DAILY_DATA_WAIT':'Для проверки плавного роста на 1H не хватает дневной истории акции или группы.',
                    'V5_STEADY_GROWTH_WAIT':'Перед сигналом 1H акция не показала плавного роста относительно своей группы.',
                    WEEKLY_UNAVAILABLE:tr('Для проверки недельного тренда не хватает подтверждённой истории.'),
                    WEEKLY_NOT_RISING:tr('Недельный рост и дневное восстановление пока не подтвердились.'),
                    RECOVERY_UNAVAILABLE:tr('Для проверки дневного восстановления не хватает подтверждённых свечей.'),
                    WEIGHTED_UNAVAILABLE:tr('Для взвешенной цены нужны как минимум шесть свечей с объёмом за два торговых часа.'),
                    WEIGHTED_PRICE_WAIT:tr('Цена входа пока не выше взвешенной цены последних двух торговых часов.'),
                    RECLAIM_DATA_WAIT:tr('Для повторного входа пока не подтверждена первая котировка.'),
                    RECLAIM_EARLIER_WAIT:tr('Подходящая цена уже была на предыдущей свече.'),
                    RECLAIM_BUY:tr('Цена восстановилась, повторный вход 4H подтверждён.'),
                    'V5_4H_WAIT_FIRST_QUOTE':'Для входа нужна первая закрытая котировка следующего интервала 4H.',
                    'V5_4H_OUTSIDE_OPENING_WINDOW':tr('Ожидаем следующего окна входа после закрытия 4H.'),
                    'V5_4H_OPENING_WINDOW_EXPIRED':'Окно входа после закрытия 4H прошло.',
                    'V5_4H_SIGNAL_UNAVAILABLE':'Закрытая свеча для проверки пробоя на 4H ещё не подтверждена.',
                    'V5_4H_SIGNAL_CANDLE_CHANGED':'Ожидаем согласования последней закрытой свечи 4H.',
                    'V5_4H_NO_COMPRESSION_BREAKOUT':'На 4H нет пробоя узкого диапазона с подтверждением объёма.',
                    'V5_4H_COMPRESSION_BREAKOUT':'На 4H подтверждён пробой узкого диапазона.',
                    'V5_4H_TIMEFRAME_ONLY':'Текущий режим V5 открывает позиции по 4H.',
                    INDIVIDUAL_WINDOW_WAIT:tr('Для этой акции вход 4H разрешён в другом окне.'),
                    'V5_D1_OUTSIDE_OPENING_WINDOW':tr('Дневной откат проверяется в начале торговой сессии.'),
                    'V5_D1_WAIT_SECOND_QUOTE':tr('Для дневного отката нужна вторая закрытая свеча 15 мин текущей сессии.'),
                    'V5_D1_PULLBACK_DATA_WAIT':tr('Для дневного отката пока недостаточно подтверждённой истории акции.'),
                    'V5_D1_PULLBACK_PATTERN_WAIT':tr('Дневная свеча пока не подтвердила вход после отката.'),
                    'V5_D1_DEFENSIVE_SCORE_WAIT':tr('Дневной откат не прошёл фильтр волатильности и силы восстановления.'),
                    'V5_D1_MARKET_CONTEXT_WAIT':tr('Для дневного отката нужны достаточная история группы и неотрицательная медиана её доходности за пять сессий.'),
                    'V5_D1_PULLBACK_PRICE_WAIT':tr('Цена отклонилась от дневного закрытия больше чем на 0,8 ATR.'),
                    'V5_D1_SIGNAL_CANDLE_CHANGED':tr('Ожидаем согласования последней закрытой дневной свечи.'),
                    'V5_D1_OUTSIDE_FROZEN_COHORT':tr('Акция остаётся доступна для 4H. Дневной откат откалиброван на другом составе акций.'),
                    'V5_D1_OPENING_WINDOW_EXPIRED':tr('Окно входа по дневному откату закрылось.'),
                    'V5_D1_DEFENSIVE_PULLBACK':tr('Дневной откат подтвердил вход.'),
                    'PAPER_V5_SAME_QUOTE_REENTRY':tr('На этой свече исполнения позиция уже закрывалась.'),
                    'PAPER_V5_4H_ENTRY_TOO_FAR':'Цена ушла более чем на 0,75 ATR выше пробоя.',
                    'PAPER_V5_GAP_ENTRY_QUOTE':'Для входа после гэпа нужна вторая закрытая 15m свеча сессии.',
                    'PAPER_V5_GAP_ENTRY_WINDOW':'Окно входа после гэпа уже прошло.',
                    'PAPER_V5_GAP_LEVEL_OR_DISTANCE':'Цена потеряла уровень гэпа или отклонилась больше чем на 0,50 ATR.',
                    'PAPER_V5_DAILY_GROUP_CHANGED':'Изменился состав группы сравнения. Сигнал пересчитается.',
                    'PAPER_V5_RANKING_PENDING':'Сравниваем подтверждённые BUY с той же ценой закрытия по времени.',
                    'PAPER_V5_ENTRY_LEVEL_LOST':'Цена исполнения вернулась к пробитому уровню или ниже.',
                    'V4_SELECTED_CANDLE_UNCONFIRMED':'Закрытая свеча выбранного интервала ещё не подтверждена.',
                    'V4_ENTRY_NOT_CONFIRMED':'На выбранном интервале нет подтверждения пробоя, разворота или входа после отчёта.',
                    'PAPER_V4_QUOTE_BEFORE_SIGNAL':'Ожидаем закрытую котировку 15 мин после сигнала.',
                    'PAPER_QUOTE_BEFORE_STRATEGY_SIGNAL':'Ожидаем закрытую цену исполнения после сигнала.',
                    'PAPER_V4_EARNINGS_LEVEL_LOST':'Цена исполнения вернулась под уровень пробоя после отчёта.',
                    'V5_OUTSIDE_OPEN_RANGE_WINDOW':'V5 ищет пробой во 2–6-й час сессии.',
                    'V5_NO_FRESH_OPEN_RANGE_BREAK':'Свежего пробоя диапазона первого часа нет.',
                    'V5_OPEN_RANGE_TOO_WIDE':'Диапазон первого часа превышает 2 ATR.',
                    'V5_OPEN_RANGE_UNCONFIRMED':'Закрытый диапазон первого часа пока не подтверждён.',
                    'V5_INCOMPLETE_HOUR_OR_WARMUP':'Недостаточно полных часовых свечей для V5.',
                    'V5_BREADTH_INPUTS_UNCONFIRMED':'Недостаточно проверенных часовых свечей по группе.',
                    'V5_BREADTH_TOO_WEAK':'В группе растёт меньше 75% бумаг.',
                    'V5_REVERSAL_DAILY_UNCONFIRMED':'Для оценки разворота нужны четыре подтверждённых дневных закрытия.',
                    'V5_REVERSAL_DAILY_WEAKNESS_NOT_CONFIRMED':'Перед часовым разворотом не было двух снижений за три закрытые сессии.',
                    'V5_NO_VOLUME_EXPANSION':'Закрытие и объём свечи ещё не подтверждают пробой.',
                    'PAPER_V5_QUOTE_VOLUME_BELOW_SIZE':'Объём свечи исполнения меньше размера позиции.',
                    'PAPER_V5_REVERSAL_LEVEL_LOST':'Вход пропущен: закрытая цена 15 мин вернулась к пробитому максимуму или ниже.',
                    'V5_POSITION_MANAGED_BY_BRACKET':'Позиция сопровождается по стопу, цели и времени.'}
            strategy_reason=outcome.get('reason')
            selection_wait=(WEEKLY_UNAVAILABLE,WEEKLY_NOT_RISING,RECOVERY_UNAVAILABLE,WEIGHTED_UNAVAILABLE)
            reason=(strategy_reason if strategy_reason in selection_wait
                    else code or strategy_reason or 'Ожидается первая проверка')
            normalized=reason.removeprefix('ValueError: ')
            macro='PAPER_V5_MACRO_RELEASE_DAY:'
            if normalized.startswith(macro):
                reason='Новые покупки приостановлены до следующей сессии — '+normalized[len(macro):]+'.'
            else:
                reason=detail.get(normalized,reason)
            if outcome.get('state')=='COMPLETED':
                reason='Виртуальная сделка • количество '+str(outcome.get('quantity'))
            lines.append(f"{name} {outcome.get('strategy',STRATEGY_CODES[mode])} / {tf}: "
                f"{outcome.get('state','ОЖИДАНИЕ')} • {outcome.get('signal','WAIT')}{selected}\n"+reason)
    if state.get('archived_fills'):lines.append('Предыдущий прогон сохранён в архиве: '+str(state['archived_fills'])+' операций.')
    return '\n'.join(lines)


def v2_daily_summary(state):
    sessions = state.get('v2_daily_sessions', [])
    if not sessions:
        return ''
    labels = {'PASS': 'цель выполнена', 'NO_ROUND_TRIP': 'без закрытой сделки в этот день',
              'LOSS': 'убыток', 'FLAT': 'ноль', 'IN_PROGRESS': 'сессия продолжается'}
    lines = ['',tr('V2 · Последние сессии')]
    for row in reversed(sessions):
        line = tr('{day} · {status} · покупок {buys}, продаж {sells} · {net} EUR',
                  day=row['day'],status=tr(labels[row['status']]),buys=row['buys'],
                  sells=row['sells'],net=row['net'])
        if row['profit_lock'] and row['status'] == 'IN_PROGRESS':
            line += tr(' · новые покупки остановлены')
        if not row['round_trips'] and row['reasons']:
            line += ' · ' + ', '.join(reason for reason, _ in row['reasons'])
        lines.append(line)
    return '\n'.join(lines)


def summary(state):
    if state.get('status') != 'ACTIVE': return tr('Демосчёт: ') + str(state.get('status',tr('недоступен')))
    start=state['policy']['starting_cash'];equity=state.get('equity')
    change=tr('не определена') if equity is None else f'{(Decimal(equity)/Decimal(start)-1)*100:.2f}%'
    def value(key): return str(state[key])+' EUR' if state.get(key) is not None else tr('DATA BLOCKED / нет оценки')
    universe=(f"\nПодключено к демоторговле {len(state.get('automation',{}).get('identities',{}))}"
              f" из {len(state.get('allowed_isins',[]))}."+
              tr(' • Новые подключаются автоматически; качество данных и допуск проверяются отдельно.')) if state.get('universe',{}).get('automatic_new_instruments') else ''
    portfolios=state.get('portfolios',{})
    balances=("\n"+tr('Свободные средства по счетам')+"  "+
              "  |  ".join(f"{name} {book['cash']} EUR" for name,book in portfolios.items())
              if portfolios else "")
    return (f"{state['account']}{tr(' • только виртуальные операции\nНачальный капитал: ')}{start}{tr(' EUR • Доходность: ')}{change}\nCash: {value('cash')} • Equity: {value('equity')}{tr('\nРеализованный P&L: ')}{value('realized')}{tr(' • Нереализованный: ')}{value('unrealized')}{tr(' • Комиссии: ')}{value('fees')}"+balances+universe+automation_summary(state)+v2_daily_summary(state))

def attach(app, parent):
    from calendar_panel import label as translated_label
    from paper_orders import order_choices, submit
    from paper_entry_modes import MODES, STRATEGY_CODES
    from paper_hourly import STRATEGY_KEY, set_trading_mode
    from paper_v1_long_term import enabled as v1_enabled
    from paper_v1_news_view import Panel as V1NewsPanel, show_saved as show_saved_news
    from uuid import uuid4
    order_box=ttk.LabelFrame(parent,text='Поручение владельца • только PAPER');order_box.pack(fill='x',padx=10,pady=4)
    row=ttk.Frame(order_box);row.pack(fill='x')
    choices={}
    independent=False
    viewed_book=None
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
        if saving_mode:
            return
        isin=next((k for k,v in choices.items() if v==selected.get()),None)
        if isin is None:message.set('Нет выбранной акции с действующим подключением к PAPER.');return
        try:qty=float(quantity.get().replace(',','.'))
        except ValueError:message.set('Укажите положительное количество акций.');return
        args=dict(isin=isin,side=side,quantity=qty,
            timeframe='AUTO',request_id='manual-'+str(uuid4()))
        if independent and viewed_book:
            args['_paper_portfolio']=viewed_book
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
    auto_box=ttk.LabelFrame(parent,text='Автоторговля демосчёта')
    auto_box.pack(fill='x',padx=10,pady=4)
    auto_controls=ttk.Frame(auto_box)
    auto_controls.pack(fill='x',padx=4,pady=3)
    strategy_labels={key:label for key,label,_ in MODES}
    strategy=tk.StringVar()
    mode_state=tk.StringVar()
    selector_label=ttk.Label(auto_controls,text='Стратегия')
    selector_label.pack(side='left',padx=(0,4))
    strategy_box=ttk.Combobox(auto_controls,textvariable=strategy,values=list(strategy_labels.values()),
        state='disabled',width=22)
    strategy_box.pack(side='left',padx=(0,12))
    ttk.Label(auto_controls,text='ТФ автоматически').pack(side='left',padx=(0,8))
    mode_label=ttk.Label(auto_controls,textvariable=mode_state)
    mode_label.pack(side='left',padx=4)
    saved_mode='V1'
    editing_mode=False
    saving_mode=False
    mode_revision=0
    notebook=ttk.Notebook(parent);notebook.pack(fill='both',expand=True,padx=10)
    def table(title,columns,body=None,height=6):
        if body is None:
            body=ttk.Frame(notebook);notebook.add(body,text=tr(title))
        tree=ttk.Treeview(body,columns=columns,show='headings',height=height)
        for col in columns: tree.heading(col,text=tr(col));tree.column(col,width=130,stretch=False)
        x=ttk.Scrollbar(body,orient='horizontal',command=tree.xview);x.pack(side='bottom',fill='x')
        y=ttk.Scrollbar(body,orient='vertical',command=tree.yview);y.pack(side='right',fill='y')
        tree.configure(xscrollcommand=x.set,yscrollcommand=y.set);tree.pack(fill='both',expand=True)
        return tree
    positions=table(tr('Открытые позиции'),('ISIN','Стратегия / ТФ',tr('Количество'),tr('Затраты'),tr('Цена'),'Стоп','Цель',tr('Текущая стоимость'),'P&L',tr('Площадка'),tr('Валюта'),tr('Время оценки'),'DQ'))
    study_tab=ttk.Frame(notebook)
    notebook.add(study_tab,text='Факторы V5')
    study_label=tk.StringVar()
    ttk.Label(study_tab,textvariable=study_label,wraplength=900).pack(anchor='w',padx=4,pady=3)
    study_rows=table('',('Проверка','Оценено','Прошло','База, €','С фильтром, €'),
                     body=study_tab,height=8)
    study_rows.column('Проверка',width=345)
    from paper_v5_context_study import RULES as STUDY_RULES
    displayed_study=None

    def render_study(state):
        nonlocal displayed_study
        report=state.get('v5_context_study',{})
        active=state.get('automation',{}).get('enabled') and saved_mode=='AGGRESSIVE'
        if (report,active)==displayed_study:
            return
        displayed_study=(report,active)
        counts=report.get('states',{})
        label=('Факторы часовой ветви V5.' if active else
               'Сбор начнётся при включении V5.')
        study_label.set(label+f" Закрыто {counts.get('CLOSED',0)}, открыто {counts.get('OPEN',0)}, "
                        f"неполных {counts.get('INCOMPLETE',0)}, отказов {counts.get('BLOCKED',0)}. "
                        "Оценки отдельных сигналов после издержек.")
        study_rows.delete(*study_rows.get_children())
        for key, title in STUDY_RULES.items():
            row=report.get('variants',{}).get(key,{})
            study_rows.insert('','end',values=(title,row.get('known',0),row.get('accepted',0),
                f"{row['baseline']:.2f}" if row.get('known') else '—',
                f"{row['net']:.2f}" if row.get('known') else '—'))
    from paper_journal import CODES, OTHER, save_note

    journal_tab=ttk.Frame(notebook)
    notebook.add(journal_tab,text='Журнал V1–V5')
    news_tab=ttk.Frame(notebook)
    notebook.add(news_tab,text=translated_label('Новости V1','V1-Nachrichten','V1 news'))
    V1NewsPanel(app,notebook,news_tab)
    notebook.hide(news_tab)

    def show_news_tab(book):
        notebook.tab(news_tab,state='normal' if book=='V1' else 'hidden')

    filter_row=ttk.Frame(journal_tab)
    filter_row.pack(fill='x',pady=2)
    scope=tk.StringVar(value='Текущий счёт')
    strategy_filter=tk.StringVar(value='Все стратегии')
    legacy_label='До разделения'
    scope_box=ttk.Combobox(filter_row,textvariable=scope,values=('Текущий счёт','Архив'),
                           state='readonly',width=16)
    scope_box.pack(side='left',padx=(0,6))
    filter_box=ttk.Combobox(filter_row,textvariable=strategy_filter,
                            values=('Все стратегии',*CODES[:-1],'Ручные / другие'),
                            state='readonly',width=18)
    filter_box.pack(side='left')
    page_label=tk.StringVar()
    ttk.Label(filter_row,textvariable=page_label).pack(side='right',padx=4)
    next_page=ttk.Button(filter_row,text='›',width=3,command=lambda:move_page(1),state='disabled')
    next_page.pack(side='right')
    prev_page=ttk.Button(filter_row,text='‹',width=3,command=lambda:move_page(-1),state='disabled')
    prev_page.pack(side='right')
    page=0
    page_size=100

    statistics_body=ttk.Frame(journal_tab)
    statistics_body.pack(fill='x')
    statistics=ttk.Treeview(statistics_body,columns=('Стратегия','BUY','SELL','Плюс','Минус',
                            'Ноль','P&L, €','Комиссии, €'),show='headings',height=5)
    widths=(122,50,50,52,52,50,90,92)
    for column,width in zip(statistics['columns'],widths):
        statistics.heading(column,text=column)
        statistics.column(column,width=width,stretch=False,anchor='w' if column=='Стратегия' else 'e')
    stats_scroll=ttk.Scrollbar(statistics_body,orient='vertical',command=statistics.yview)
    stats_scroll.pack(side='right',fill='y');statistics.configure(yscrollcommand=stats_scroll.set)
    stats_horizontal=ttk.Scrollbar(statistics_body,orient='horizontal',command=statistics.xview)
    stats_horizontal.pack(side='bottom',fill='x');statistics.configure(xscrollcommand=stats_horizontal.set)
    statistics.pack(fill='x')
    ttk.Label(journal_tab,text='Плюс, минус и ноль считаются при полном закрытии позиции. Время по Берлину.').pack(anchor='w')

    note_row=ttk.Frame(journal_tab)
    note_row.pack(side='bottom',fill='x',pady=2)
    ttk.Label(note_row,text='Примечание').pack(side='left',padx=(0,5))
    trade_note=tk.StringVar()
    note_input=ttk.Entry(note_row,textvariable=trade_note,state='disabled')
    note_input.pack(side='left',fill='x',expand=True)
    note_status=tk.StringVar()
    ttk.Label(journal_tab,textvariable=note_status).pack(side='bottom',anchor='w')
    fills_body=ttk.Frame(journal_tab)
    fills_body.pack(fill='both',expand=True)
    fills=table('',('Время','ISIN','Стратегия','Операция','Кол-во','Цена','P&L, €','Причина',
                    'Примечание','Инициатор','ТФ','Стоп','Цель','Комиссия','Площадка','Валюта','Свеча'),
                body=fills_body,height=5)
    for column,width in zip(fills['columns'],(148,112,82,68,62,80,76,260,220)):
        fills.column(column,width=width)
    journal={}
    visible_entries={}
    selected_fill=None
    note_busy=False

    def select_fill(_=None):
        nonlocal selected_fill
        selection=fills.selection()
        fill_id=selection[0] if selection else None
        if fill_id==selected_fill:
            return
        selected_fill=fill_id
        trade_note.set(visible_entries.get(fill_id,{}).get('note',''))
        note_status.set('')
        set_note_controls()

    def set_note_controls():
        state='normal' if selected_fill and not note_busy else 'disabled'
        note_input.configure(state=state)
        note_button.configure(state=state)
        news_button.configure(state='normal' if selected_news() else 'disabled')

    def selected_news():
        fill=visible_entries.get(selected_fill,{}).get('fill',{})
        return (fill.get('smart_eligibility') or {}).get('news_background')

    def open_saved_news():
        background=selected_news()
        if background:
            show_saved_news(parent,background)

    def commit_note():
        nonlocal note_busy
        fill_id=selected_fill
        note=trade_note.get()
        if fill_id is None or note_busy:
            return
        if len(note.strip())>1000:
            note_status.set('До 1000 символов')
            return
        note_busy=True
        set_note_controls()
        def work():
            with app.store.connect() as db:
                db.execute('BEGIN IMMEDIATE')
                save_note(db,fill_id,note)
        def finish(saved):
            nonlocal note_busy
            note_busy=False
            if not alive():
                return
            set_note_controls()
            note_status.set('Сохранено' if saved else 'Примечание не сохранилось')
            if saved:
                refresh()
        if app.job(work,lambda _:finish(True),lambda _:finish(False),lane='misc',
                   key='paper_fill_note:'+str(parent)) is False:
            finish(False)

    note_button=ttk.Button(note_row,text='Сохранить',command=commit_note,state='disabled')
    note_button.pack(side='left',padx=4)
    news_button=ttk.Button(filter_row,text=translated_label('Новости сделки','Nachrichten zum Trade','Trade news'),
                           command=open_saved_news,state='disabled')
    news_button.pack(side='left',padx=4)

    def render_journal():
        nonlocal page
        view={'Архив':'archive',legacy_label:'legacy'}.get(scope.get(),'current')
        group=strategy_filter.get()
        group={'Ручные / другие':OTHER,'Все стратегии':'all'}.get(group,group)
        rows=journal.get('totals',{}).get(view,{})
        indices=journal.get('indices',{}).get(view,{}).get(group,[])
        last=max(0,(len(indices)-1)//page_size)
        page=min(last,max(page,0))
        page_label.set(f'{page+1} / {last+1}' if indices else '0 операций')
        prev_page.configure(state='normal' if page else 'disabled')
        next_page.configure(state='normal' if page<last else 'disabled')
        scroll=statistics.yview()[0]
        statistics.delete(*statistics.get_children())
        for code,row in rows.items():
            label='Ручные / другие' if code==OTHER else code
            statistics.insert('','end',values=(label,row['buys'],row['sells'],row['winners'],
                row['losers'],row['flat'],row['realized'],row['fees']))
        statistics.yview_moveto(scroll)
        offset=fills.yview()[0]
        fills.delete(*fills.get_children())
        visible_entries.clear()
        for index in indices[page*page_size:(page+1)*page_size]:
            entry=journal['entries'][index]
            f=entry['fill']
            fill_id=f['request_id']
            visible_entries[fill_id]=entry
            plan=f.get('smart_eligibility',{}).get('protection') or {}
            executed=datetime.fromisoformat(f['at']).astimezone(JOURNAL_ZONE).strftime('%d.%m.%Y %H:%M:%S')
            model=f.get('smart_eligibility',{}).get('strategy_model')
            code_label=' / '.join(entry['codes'])
            if model and ('V4' in entry['codes'] or 'V5' in entry['codes']):
                code_label+=' · '+f.get('timeframe','—')
            fills.insert('','end',iid=fill_id,values=(executed,f['isin'],code_label,
                f['side'],f['quantity'],price_text(f['price']),entry['pnl'] or '—',entry['reason'],
                entry['note'],f.get('initiator','Ранее'),f.get('timeframe','—'),price_text(plan.get('stop')),
                price_text(plan.get('target')),f['fee'],f['identity']['venue'],f['identity']['currency'],
                f['bar_close_at']))
        if selected_fill in visible_entries:
            fills.selection_set(selected_fill)
        else:
            select_fill()
        set_note_controls()
        fills.yview_moveto(offset)

    fills.bind('<<TreeviewSelect>>',select_fill)
    def move_page(delta):
        nonlocal page
        page=page+delta if delta else 0
        render_journal()
    scope_box.bind('<<ComboboxSelected>>',lambda _:move_page(0))
    filter_box.bind('<<ComboboxSelected>>',lambda _:move_page(0))
    ttk.Label(controls,text=tr('Только демо. WAIT — ожидание условий стратегии; прибыль не гарантируется.')).pack(anchor='w')
    busy=False
    v1_active=False
    def alive(): return bool(parent.winfo_exists()) and not app.closing
    def done(state,revision):
        nonlocal busy,choices,saved_mode,journal,v1_active,independent,viewed_book
        busy=False
        if not alive(): return
        if revision!=mode_revision:
            refresh();return
        current=next((k for k,v in choices.items() if v==selected.get()),None)
        choices=order_choices(state)
        auto=state.get('automation',{})
        independent=bool(auto.get('independent'))
        if independent:
            viewed_book=state.get('account')
            selector_label.configure(text='Счёт')
            apply_button.pack_forget()
            order_box.configure(text='Поручение владельца • '+str(viewed_book))
        if not (saving_mode or editing_mode):
            saved_mode=auto.get(STRATEGY_KEY,'V1')
            v1_active=v1_enabled(auto)
            strategy.set(strategy_labels.get(saved_mode,''))
            controls_state='readonly' if independent or auto.get('approved') is True else 'disabled'
            strategy_box.configure(state=controls_state)
        show_news_tab(viewed_book if independent else 'V1' if v1_active else None)
        instrument_box.configure(values=list(choices.values()))
        label=choices.get(current,next(iter(choices.values()),''))
        if selected.get()!=label:selected.set(label)
        set_summary(summary(state))
        positions.delete(*positions.get_children())
        for p in state.get('positions',[]):
            if not Decimal(p['quantity']): continue
            ident=p['identity']
            detail=(tr('Цена проверена')+' '+p.get('mark_timeframe','')) if p.get('mark_context')=='CURRENT_CLOSED_BAR' else p['mark_status']
            if p.get('archive_quality') not in (None,'READY'):detail+=tr(' • архив: ')+p['archive_quality']
            if p.get('mark_reason'):detail+=': '+p['mark_reason']
            plan=p.get('protection') or {}
            entry=p.get('auto_entry') or {}
            mode_label=f"{STRATEGY_CODES[entry['strategy_mode']]} / {entry['timeframe']}" if entry else '—'
            positions.insert('','end',values=(p['isin'],mode_label,p['quantity'],p['cost'],p.get('mark','—'),price_text(p.get('active_stop',plan.get('stop'))),price_text(plan.get('target')),p.get('market_value','—'),p.get('unrealized','—'),ident['venue'],ident['currency'],p.get('mark_at','—'),detail))
        next_journal=state.get('journal',{})
        if 'legacy' in next_journal.get('indices',{}):
            scope_box.configure(values=('Текущий счёт','Архив',legacy_label))
        if next_journal!=journal:
            journal=next_journal
            render_journal()
        render_study(state)
    def failed(message):
        nonlocal busy
        busy=False
        if alive(): set_summary(tr('Демосчёт временно недоступен; данные не изменены.'))
    def refresh():
        nonlocal busy
        if busy or not alive(): return
        busy=True
        from paper_account import status
        revision=mode_revision
        book=viewed_book if independent else None
        if app.job(lambda:status(app.store.path,include_journal=True,portfolio=book),lambda state:done(state,revision),failed,
                   lane='read',key='paper_portfolio_read:'+str(parent)) is False:busy=False
    def draft_mode():
        return next((key for key,label in strategy_labels.items() if label==strategy.get()),None)
    def edit_mode(_):
        nonlocal editing_mode,saved_mode,viewed_book,journal,mode_revision
        draft=draft_mode()
        if independent:
            if draft is None:
                return
            notebook.select(journal_tab)
            if draft!=saved_mode:
                mode_revision+=1
                saved_mode=draft
                viewed_book=STRATEGY_CODES[draft]
                show_news_tab(viewed_book)
                order_box.configure(text='Поручение владельца • '+viewed_book)
                mode_state.set('')
                scope.set('Текущий счёт')
                strategy_filter.set('Все стратегии')
                journal={}
                positions.delete(*positions.get_children())
                render_study({})
                move_page(0)
                set_summary(tr('Загрузка демосчёта…')+' '+viewed_book)
                refresh()
            return
        editing_mode=draft is not None and (draft!=saved_mode or draft=='V1' and not v1_active)
        apply_button.configure(state='normal' if editing_mode else 'disabled')
        mode_state.set('')
    def choose_mode():
        nonlocal saving_mode
        requested_strategy=draft_mode()
        if not requested_strategy:
            return
        saving_mode=True
        apply_button.configure(state='disabled')
        strategy_box.configure(state='disabled')
        mode_state.set('Сохраняю…')
        def work():
            with app.store.connect() as db:
                return set_trading_mode(db,requested_strategy)
        def finish(value, text):
            nonlocal saving_mode,saved_mode,mode_revision,editing_mode
            saving_mode=False
            editing_mode=False
            mode_revision+=1
            saved_mode=value
            if not alive():return
            strategy.set(strategy_labels[saved_mode])
            strategy_box.configure(state='readonly')
            mode_state.set(text)
            refresh()
        if app.job(work,lambda value:finish(value,'Сохранено'),
                   lambda _:finish(saved_mode,'Выбор не сохранился'),
                   lane='misc',key='paper_auto_trading_mode') is False:
            finish(saved_mode,'Выбор не сохранился')
    apply_button=ttk.Button(auto_controls,text='Применить',command=choose_mode,state='disabled')
    apply_button.pack(side='left',before=mode_label)
    strategy_box.bind('<<ComboboxSelected>>',edit_mode)
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
    ttk.Button(controls,text=tr('Автоторговля: пауза / продолжить'),command=toggle).pack(side='left',padx=4)
    _auto_refresh(parent,app,refresh)
