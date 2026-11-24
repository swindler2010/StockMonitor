"""Prospective signal comparisons. Independent observations, no order calls."""
from datetime import datetime, timedelta, timezone
from decimal import Decimal
import json
from zoneinfo import ZoneInfo

from paper_account import POLICY, money

VERSION = 'v5_context_1'
NY = ZoneInfo('America/New_York')
FEE = Decimal(POLICY['commission'])
HALF = Decimal(POLICY['spread_bps']) / 20000
QUARTER = 15 * 60

# Curated schedule for this study. Times are announced ET and do not imply
# that the content was available before actual publication.
BLS = 'https://www.bls.gov/schedule/2026/'
FED = 'https://www.federalreserve.gov/newsevents/2026-december.htm'
NVIDIA = 'https://investor.nvidia.com/news/press-release-details/2026/NVIDIA-Sets-Conference-Call-for-Third-Quarter-Financial-Results/'
EVENTS = (
    ('2026-11-17 13:20','America/Los_Angeles','EARNINGS','US67066G1040',NVIDIA),
    ('2026-11-17 08:30','America/New_York','IMPORT_EXPORT',None,BLS),
    ('2026-11-18 14:00','America/New_York','FOMC_MINUTES',None,
     'https://www.federalreserve.gov/monetarypolicy.htm'),
    ('2026-12-04 08:30','America/New_York','EMPLOYMENT',None,BLS),
    ('2026-12-09 14:00','America/New_York','FOMC',None,FED),
    ('2026-12-10 08:30','America/New_York','CPI',None,BLS),
    ('2026-12-15 08:30','America/New_York','PPI',None,BLS),
    ('2026-12-17 08:30','America/New_York','IMPORT_EXPORT',None,BLS),
)

RULES = {
    'after_us_open': 'После открытия США',
    'skip_us_first_hour': 'Вне первого часа США',
    'skip_friday': 'Пропускать пятницу',
    'skip_month_edges': 'Вне двух первых и двух последних сессий месяца',
    'skip_near_macro': 'Вне ±60 мин от выбранных публикаций ФРС/BLS',
    'skip_macro_release_day': 'Вне дней CPI, PPI, занятости и ставки ФРС',
    'skip_near_earnings': 'Вне трёх часов до и после отчёта NVIDIA',
    'group_unanimous': 'Растут все допущенные акции',
    'reversal_level_held': 'Уровень разворота сохранился к исполнению',
}


def _calendar(identity, clock):
    from data_quality import calendar
    from timeframes import schedule
    local = clock.astimezone(ZoneInfo(identity['timezone']))
    trading_day = local.date()
    first = trading_day.replace(day=1)
    next_month = (first.replace(day=28)+timedelta(days=4)).replace(day=1)
    dates = [date.date() for date in schedule(identity,first,next_month-timedelta(days=1)).index]
    edge = trading_day in dates[:2]+dates[-2:]
    us = clock.astimezone(NY)
    sessions = calendar('XNYS',us.year,us.year).schedule
    date = us.date().isoformat()
    if date in sessions.index:
        record = sessions.loc[date]
        minutes_open = (clock-record.open.to_pydatetime()).total_seconds()/60
        minutes_close = (record.close.to_pydatetime()-clock).total_seconds()/60
    else:
        minutes_open = minutes_close = None
    return dict(berlin=local.isoformat(),new_york=us.isoformat(),weekday=local.weekday(),
                day_of_month=local.day,month_edge=edge,minutes_from_us_open=minutes_open,
                minutes_to_us_close=minutes_close)


def _events(clock, isin):
    result=[]
    for announced,zone,kind,stock,source in EVENTS:
        if stock not in (None,isin):
            continue
        event=datetime.fromisoformat(announced).replace(tzinfo=ZoneInfo(zone))
        seconds=(clock-event).total_seconds()
        if abs(seconds)<=3*3600:
            result.append(dict(kind=kind,minutes_from=round(seconds/60,2),
                               announced_at=event.isoformat(),source=source))
    return result


def _news(db, key, clock):
    row = db.execute('SELECT value FROM settings WHERE key=?', ('news:'+key,)).fetchone()
    data = json.loads(row[0]) if row else {}
    try:
        fetched=datetime.fromisoformat(data['fetched_at'])
        age=(clock-fetched).total_seconds()
    except (KeyError,ValueError,TypeError):
        return dict(state='UNAVAILABLE',items=[])
    if not 0<=age<=3600 or data.get('status') not in ('AVAILABLE','PARTIAL','EMPTY'):
        return dict(state='STALE_OR_UNAVAILABLE',items=[])
    items=[]
    for article in data.get('articles',[]):
        try:
            published=datetime.fromisoformat(article['published'])
            if not 0<=(clock-published).total_seconds()<=10800:
                continue
        except (KeyError,ValueError,TypeError):
            continue
        items.append(dict(published=article['published'],source=article.get('url'),
                          tags=article.get('events',[]),basis=article.get('event_basis'),
                          official=article.get('relevance')=='OFFICIAL_ISSUER_FEED'))
        if len(items)==8:
            break
    return dict(state=data['status'],seen_at=fetched.isoformat(),items=items)


def context(db, info, result, signal, quote, clock, universe_size, period):
    from paper_v5 import macro_release_day
    features=result['execution_context']['trading_engine']['v5_opening_range']
    events=_events(clock,info['isin'])
    opened=period['minutes_from_us_open']
    closing=period['minutes_to_us_close']
    reversal=signal['setup']=='DAILY_REVERSAL'
    level=features.get('prev_high') if reversal else features.get('open_high')
    day=clock.astimezone(NY).date().isoformat()
    macro_covered='2026-11-14'<=day<='2026-12-31'
    macro_day=macro_release_day(clock)
    # A single verified issuer event is an event-specific sample.
    earnings_window=info['isin']=='US67066G1040' and '2026-11-16'<=day<='2026-11-19'
    earning_event=any(event['kind']=='EARNINGS' for event in events)
    flags=dict(after_us_open=0<=opened and closing>0 if opened is not None else None,
        skip_us_first_hour=not 0<=opened<60 if opened is not None else None,
        skip_friday=period['weekday']!=4,skip_month_edges=not period['month_edge'],
        skip_near_macro=not any(e['kind']!='EARNINGS' and abs(e['minutes_from'])<=60 for e in events) if macro_covered else None,
        skip_macro_release_day=(macro_day['state']=='CLEAR' if macro_day['state']!='CALENDAR_UNCONFIRMED' else None),
        skip_near_earnings=not earning_event if earnings_window else None,
        group_unanimous=(signal['breadth_positive']==universe_size if signal['breadth_available']==universe_size else None),
        reversal_level_held=quote['close']>level if reversal and level is not None else None)
    return dict(time=period,events=events,macro_day=macro_day,variants=flags,level=level,setup=signal['setup'],
                model=signal['model'],news=dict(company=_news(db,info['isin'],clock),market=_news(db,'market',clock)),
                relative_volume=signal['relative_volume'],atr=signal['atr'],breadth=signal['breadth_positive'])


def _tables(db):
    db.execute('''CREATE TABLE IF NOT EXISTS v5_context_study(
        version TEXT, epoch TEXT, isin TEXT, signal_at TEXT, state TEXT, payload TEXT,
        PRIMARY KEY(version,epoch,isin,signal_at))''')
    db.execute('CREATE INDEX IF NOT EXISTS v5_context_active ON v5_context_study(isin,state)')


def observe(db, info, result, cfg, signal, bar, now, period):
    """One fresh, revalidated quote. A missed quarter invalidates the path."""
    from paper_v5 import levels
    isin=info['isin']
    _tables(db)
    close_at=datetime.fromisoformat(bar['close_at']).timestamp()
    raw=Decimal(str(bar['close']))
    outcome=None
    for version,epoch,stamp,payload in db.execute(
            "SELECT version,epoch,signal_at,payload FROM v5_context_study WHERE isin=? AND state='OPEN'",(isin,)).fetchall():
        row=json.loads(payload)
        if epoch!=cfg['epoch'] or row['identity']!=result['identity']:
            state='INCOMPLETE'
            row['exit_reason']='CONTEXT_CHANGED'
        elif close_at<=row['last_quote']:
            continue
        elif close_at-row['last_quote']>QUARTER:
            state='INCOMPLETE'
            row['exit_reason']='MISSED_EXECUTION_QUOTE'
        else:
            state='OPEN'
            row['last_quote']=close_at
            if close_at>row['entry_at'] and bar['volume']>=row['quantity']:
                net=money(raw*(1-HALF)*row['quantity'])-FEE-Decimal(row['cost'])
                row['best']=str(max(Decimal(row['best']),net))
                row['worst']=str(min(Decimal(row['worst']),net))
                reason=('STOP' if raw<=Decimal(row['stop']) else 'TARGET' if raw>=Decimal(row['target'])
                        else 'TIME' if close_at>=row['entry_at']+row['hold_seconds'] else None)
                if reason:
                    state='CLOSED'
                    row.update(exit_reason=reason,exit_at=now,exit_quote_at=close_at,net=str(net))
        db.execute('UPDATE v5_context_study SET state=?,payload=? WHERE version=? AND epoch=? AND isin=? AND signal_at=?',
                   (state,json.dumps(row),version,epoch,isin,stamp))
        outcome=state
    if not signal or signal.get('action')!='BUY':
        return outcome
    stamp=result['execution_context']['trading_engine']['timestamp']
    if db.execute('SELECT 1 FROM v5_context_study WHERE version=? AND epoch=? AND isin=? AND signal_at=?',
                  (VERSION,cfg['epoch'],isin,stamp)).fetchone():
        return outcome
    entry=raw*(1+HALF)
    quantity=min(10,int((Decimal(POLICY['starting_cash'])-FEE)//entry))
    cost=money(entry*quantity)+FEE
    state='OPEN'
    reason=None
    if quantity<1 or cost>Decimal(POLICY['starting_cash']):
        state,reason='BLOCKED','PAPER_INSUFFICIENT_CASH'
    elif bar['volume']<quantity:
        state,reason='BLOCKED','PAPER_QUOTE_VOLUME_BELOW_SIZE'
    stop,target=levels(raw,Decimal(str(signal['atr'])))
    if not 0<stop<raw<target:
        state,reason='BLOCKED','PAPER_STRATEGY_LEVELS_INVALID_AT_FILL'
    row=context(db,info,result,signal,bar,datetime.fromtimestamp(now,timezone.utc),len(cfg['identities']),period)
    row.update(identity=result['identity'],entry_at=now,last_quote=close_at,entry_price=str(entry),
               quantity=quantity,cost=str(cost),stop=str(stop),target=str(target),best='0',worst='0',
               hold_seconds=signal['hold_minutes']*60,exit_reason=reason)
    db.execute('INSERT INTO v5_context_study VALUES (?,?,?,?,?,?)',
               (VERSION,cfg['epoch'],isin,stamp,state,json.dumps(row)))
    return state


def capture(path, info, result, now, *, completed=False):
    """Reuse published 1H inputs and PAPER validation outside the Tk thread."""
    from contextlib import closing
    import sqlite3
    from paper_account import quote, validate_execution_inputs, validate_session
    from paper_entry_modes import decision
    from paper_hourly import authority, ledger_token, validate_analysis
    from eur_listings import stored_identity

    identity=result['identity']
    validate_session(identity,now)
    quote_result=result['execution_quote']
    if quote_result.get('identity')!=identity:
        raise ValueError('PAPER_IDENTITY_BLOCKED')
    signal=decision(result['execution_context']['trading_engine'],'AGGRESSIVE','1H')
    period=_calendar(identity,datetime.fromtimestamp(now,timezone.utc)) if signal['action']=='BUY' else None
    with closing(sqlite3.connect(path,timeout=.2)) as db, db:
        db.execute('BEGIN IMMEDIATE')
        cfg=authority(db,info['isin'],identity)
        if completed:
            # Only in the observer: account state may have changed because the
            # main order ran first. Price, analysis, config and input revisions
            # are still revalidated; this comparison does not own a portfolio.
            result={**result,'hourly':{**result['hourly'],'ledger_token':ledger_token(db)}}
        validate_execution_inputs(db,result,info['isin'])
        validate_analysis(db,info,result,now,cfg)
        _,bar=quote(info,quote_result,now,stored_listing=stored_identity(db,info,identity,'15m'))
        return observe(db,info,result,cfg,signal,bar,now,period)


def summary(db):
    if not db.execute("SELECT 1 FROM sqlite_master WHERE name='v5_context_study'").fetchone():
        return dict(states={},variants={},setups={})
    from collections import Counter
    states=Counter()
    variants={}
    setups={}
    for state,payload in db.execute('SELECT state,payload FROM v5_context_study WHERE version=?',(VERSION,)):
        states[state]+=1
        if state!='CLOSED':
            continue
        row=json.loads(payload)
        net=Decimal(row['net'])
        item=setups.setdefault(row['setup'],dict(count=0,net=Decimal(0)))
        item['count']+=1
        item['net']+=net
        for key,flag in row['variants'].items():
            item=variants.setdefault(key,dict(known=0,accepted=0,baseline=Decimal(0),net=Decimal(0)))
            if flag is not None:
                item['known']+=1
                item['baseline']+=net
                if flag:
                    item['accepted']+=1
                    item['net']+=net
    return dict(states=dict(states),variants=variants,setups=setups)
