"""Isolated forward comparison. Reads the main DB; writes only its own ledger.

No calls to paper_account.execute, no provider calls and no broker capability.
Both books see one snapshot and the same execution bars/costs. Historic replay
uses the same pure model but never writes into the forward portfolio.
"""
import copy,json,sqlite3,time
from contextlib import closing
from pathlib import Path
import paper_strategy_v2 as model
from paper_hourly import digest

ALLOWED=('US0378331005','US67066G1040')
DB_NAME='paper_strategy_shadow.sqlite3'

def utc(value):
    import pandas as pd
    stamp=pd.Timestamp(value)
    if stamp.tzinfo is None:raise ValueError('SHADOW_TIMESTAMP_WITHOUT_ZONE')
    return stamp.tz_convert('UTC').isoformat()

def validate(db,info,result,now):
    """The existing guards, without requiring V1 to say BUY for V2 Early Entry."""
    import pandas as pd
    from paper_hourly import authority,VERSION
    from paper_account import quote,registered_instrument
    from performance_cache import validate_closed_inputs
    from calculation_window import benchmark_ready
    from cp_controls import manual_gate
    from eur_listings import stored_identity
    from trading_engine import VERSION as CORE_VERSION
    from timeframes import schedule
    if info['isin'] not in ALLOWED:raise ValueError('SHADOW_UNIVERSE_BLOCKED')
    info=registered_instrument(db,info['isin'])
    cfg=authority(db,info['isin'],result.get('identity'))
    h=result['hourly'];contract=result['execution_context'];engine=contract['trading_engine']
    if (h.get('version')!=VERSION or h.get('config_digest')!=digest(cfg) or h.get('epoch')!=cfg['epoch']):
        raise ValueError('SHADOW_CONFIG_CHANGED')
    clock=pd.Timestamp(now,unit='s',tz='UTC')
    if not pd.Timestamp(h['checked_at'])<=clock<pd.Timestamp(h['expires_at']):raise ValueError('SHADOW_EXPIRED')
    if contract.get('isin')!=info['isin'] or not contract.get('analysis_ready') or engine.get('status')!='READY':
        raise ValueError('SHADOW_CORE_DATA_BLOCKED')
    if engine.get('rules_version')!=CORE_VERSION:raise ValueError('SHADOW_CORE_VERSION_CHANGED')
    if engine.get('demo_initialization') and not engine['demo_initialization'].get('consistent'):
        raise ValueError('SHADOW_SEED_UNSTABLE')
    required={'1H','4H','1D'}
    own=[s for s in h['proof']['series'] if s['spec'].get('isin')==info['isin']]
    if (set(h['required_tf'])!=required or not required<={s['spec']['timeframe'] for s in own if s['count']>=35}
        or any(s['spec']['identity']!=result['identity'] for s in own)):
        raise ValueError('SHADOW_DEPENDENCY_BLOCKED')
    validate_closed_inputs(db,h['proof'])
    if contract.get('status')=='MANUAL_RISK_ACCEPTED':
        if manual_gate(db,info['isin'],result['identity'],'1H',h['proof'])!=contract.get('manual_risk'):
            raise ValueError('SHADOW_HISTORICAL_PERMISSION_CHANGED')
    elif contract.get('status')!='HOURLY_DEMO_READY' or any(engine['timeframe_context'][t]['data_quality']['state']!='READY' for t in required):
        raise ValueError('SHADOW_DQ_BLOCKED')
    if not benchmark_ready(engine.get('benchmark_context',{})):raise ValueError('SHADOW_BENCHMARK_BLOCKED')
    if result.get('quote_blocker'):raise ValueError(result['quote_blocker'])
    q=result['execution_quote']
    if q['identity']!=result['identity']:raise ValueError('SHADOW_QUOTE_IDENTITY_BLOCKED')
    identity,bar=quote(info,q,now,stored_listing=stored_identity(db,info,result['identity'],q['timeframe']))
    # The quoted candle is itself immutable input, independent of the 1H proof.
    opening=pd.Timestamp(bar['open_at']).tz_convert(identity['timezone']).tz_localize(None)
    row=db.execute('SELECT open,high,low,close,volume FROM series WHERE isin=? AND source=? AND venue=? '
        'AND currency=? AND symbol=? AND base_tf=? AND ts=?',(info['isin'],identity['source'],identity['venue'],
        identity['currency'],identity['symbol'],q['timeframe'],str(opening))).fetchone()
    if row is None or tuple(row)!=tuple(bar[k] for k in ('open','high','low','close','volume')):
        raise ValueError('SHADOW_EXECUTION_BAR_CHANGED')
    from analytics import bar_ends
    import data_quality as dq
    frame=pd.DataFrame([dict(zip(dq.FIELDS,row))],index=pd.DatetimeIndex([opening]));frame.attrs={**identity,'timeframe':q['timeframe']}
    if bar_ends(frame)[0]!=pd.Timestamp(bar['close_at']) or dq.audit(frame,q['timeframe'],identity,clock)['state']!='READY':
        raise ValueError('SHADOW_EXECUTION_BAR_INVALID')
    local=clock.tz_convert(identity['timezone']).tz_localize(None)
    sessions=schedule(identity,local.normalize(),local.normalize())
    if not any(r.open<=clock<r.close and not (pd.notna(r.break_start) and r.break_start<=clock<r.break_end) for _,r in sessions.iterrows()):
        raise ValueError('PAPER_SESSION_CLOSED')
    return bar

def event_from(result,bar,available_at):
    import pandas as pd
    from analytics import bar_ends
    e=result['execution_context']['trading_engine'];f=e['timeframe_context']['1H']['facts'];ident=result['identity']
    signal=pd.Timestamp(e['timestamp'])
    if signal.tzinfo is None:signal=signal.tz_localize(ident['timezone'])
    frame=pd.DataFrame([dict(open=f['price'],high=f['price'],low=f['price'],close=f['price'],volume=1)],
        index=pd.DatetimeIndex([signal.tz_convert(ident['timezone']).tz_localize(None)]))
    frame.attrs={**ident,'timeframe':'1H'};end=bar_ends(frame)[0]
    if end>pd.Timestamp(available_at):raise ValueError('SHADOW_OPEN_SIGNAL_BAR')
    q={**bar,'open_at':utc(bar['open_at']),'close_at':utc(bar['close_at'])}
    v1=dict(action=e['signal'] if e['signal'] in ('BUY','SELL') else 'WAIT',mode='STANDARD',
        atr=f['atr'],support=f['support'],stop=min(f['support']*.995,f['price']-2*f['atr']),
        reason='; '.join(e.get('entry_blockers',[])) if e['signal'] in ('WAIT','HOLD') and e.get('entry_blockers') else 'UNCHANGED_CORE_V1')
    # No checked_at or execution-quote timestamp in the decision binding. Both
    # are expected to move while a previously closed signal awaits execution.
    binding=dict(identity=ident,contexts={t:{k:c.get(k) for k in ('signal_at','facts','accepted_risk')} for t,c in e['timeframe_context'].items()},
        signal=v1['action'],policy=model.VERSION,manual=result['execution_context'].get('manual_risk'))
    v2=model.proposal(e)
    return dict(isin=result['isin'],identity=ident,quote=q,signal_at=utc(signal),signal_close=utc(end),input_proof=result['hourly']['proof'],
        available_at=utc(available_at),decision_fingerprint=digest(binding),hourly_price=f['price'],v1=v1,v2=v2,
        decision_fingerprints=dict(V1=digest(binding),V2=digest([binding,v2,{t:c.get('scope_initialization') for t,c in e['timeframe_context'].items()}])))

def connect(path):
    db=sqlite3.connect(path,timeout=1)
    db.execute('PRAGMA foreign_keys=ON')
    db.executescript('CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY,value TEXT NOT NULL);'
        'CREATE TABLE IF NOT EXISTS books(name TEXT PRIMARY KEY,state TEXT NOT NULL);'
        'CREATE TABLE IF NOT EXISTS observations(id TEXT PRIMARY KEY,isin TEXT,at TEXT,payload TEXT NOT NULL);'
        'CREATE TABLE IF NOT EXISTS journal(id INTEGER PRIMARY KEY,observation TEXT,book TEXT,payload TEXT NOT NULL);'
        'CREATE TABLE IF NOT EXISTS status(isin TEXT PRIMARY KEY,payload TEXT NOT NULL);')
    policy=json.dumps(dict(version=model.VERSION,policy=model.POLICY,universe=ALLOWED),sort_keys=True)
    db.execute('INSERT OR IGNORE INTO meta VALUES (?,?)',('policy',policy))
    if db.execute('SELECT value FROM meta WHERE key=?',('policy',)).fetchone()[0]!=policy:
        db.close();raise ValueError('SHADOW_POLICY_CHANGED_REQUIRES_NEW_EXPERIMENT')
    for name in ('V1','V2'):db.execute('INSERT OR IGNORE INTO books VALUES (?,?)',(name,json.dumps(model.book())))
    db.commit();return db

def apply(db,event,*,guard=lambda:None,previous_guard=None):
    key=digest([model.VERSION,event['isin'],event['quote']['close_at']])
    with db:
        db.execute('BEGIN IMMEDIATE')
        if db.execute('SELECT 1 FROM observations WHERE id=?',(key,)).fetchone():return {'state':'UNCHANGED','id':key}
        guard()
        previous=db.execute('SELECT value FROM meta WHERE key=?',('proof:'+event['isin'],)).fetchone()
        if previous and previous_guard:
            try:previous_guard(json.loads(previous[0]))
            except (ValueError,RuntimeError):event={**event,'revoke_pending':True}
        reports={}
        for name,payload in db.execute('SELECT name,state FROM books ORDER BY name').fetchall():
            state=json.loads(payload)
            for record in model.advance(state,name,event):
                db.execute('INSERT INTO journal(observation,book,payload) VALUES (?,?,?)',(key,name,json.dumps(record)))
            db.execute('UPDATE books SET state=? WHERE name=?',(json.dumps(state),name));reports[name]=model.metrics(state)
        guard()
        db.execute('INSERT INTO observations VALUES (?,?,?,?)',(key,event['isin'],event['available_at'],json.dumps(event)))
        if event.get('input_proof'):db.execute('INSERT OR REPLACE INTO meta VALUES (?,?)',('proof:'+event['isin'],json.dumps(event['input_proof'])))
    return dict(state='OBSERVED',id=key,books=reports)

def read(path):
    if not Path(path).is_file():return dict(state='WAITING_FIRST_CYCLE',books={})
    with sqlite3.connect(Path(path).resolve().as_uri()+'?mode=ro',uri=True) as db:
        books={name:model.metrics(json.loads(payload)) for name,payload in db.execute('SELECT name,state FROM books')}
        status={i:json.loads(p) for i,p in db.execute('SELECT isin,payload FROM status')}
        journal=[dict(book=b,**json.loads(p)) for b,p in db.execute('SELECT book,payload FROM journal ORDER BY id DESC LIMIT 100')]
        return dict(state='SHADOW_ONLY',books=books,status=status,journal=journal,
            observations=db.execute('SELECT count(*) FROM observations').fetchone()[0])

class Runtime:
    def __init__(self,app):self.app=app;self.busy=False;self.cursor=0;self.next_check=0
    def start(self):self.app.root.after(20000,self.tick)
    def tick(self):
        app=self.app
        if app.closing:return
        if not self.busy and not app.user_dirty and time.time()>=self.next_check:
            isin=ALLOWED[self.cursor%len(ALLOWED)];self.cursor+=1
            info=next((i for i in app.watchlist if i['isin']==isin),None)
            if info:
                values=dict(watchlist=[copy.deepcopy(info)],drafts={},feed_errors={},symbol=info['name'],timeframe='1H',period='Max',
                    ai_config=copy.deepcopy(app.ai_config),instrument_revisions=app.store.read_cache.token_for(['instrument:'+isin]))
                self.busy=True
                def work():
                    target=app.store.directory/DB_NAME
                    with closing(connect(target)) as ledger:
                        try:
                            result=app.compute_feed.calculate('paper_hourly',values)
                            def guard():
                                if app.closing or app.user_dirty:raise ValueError('SHADOW_APP_NOT_READY')
                                with closing(sqlite3.connect(Path(app.store.path).resolve().as_uri()+'?mode=ro',uri=True,timeout=.2)) as source:
                                    return validate(source,info,result,time.time())
                            bar=guard();event=event_from(result,bar,__import__('datetime').datetime.now(__import__('datetime').timezone.utc).isoformat())
                            def previous_guard(proof):
                                from performance_cache import validate_closed_inputs
                                with closing(sqlite3.connect(Path(app.store.path).resolve().as_uri()+'?mode=ro',uri=True,timeout=.2)) as source:
                                    validate_closed_inputs(source,proof)
                            report=apply(ledger,event,guard=guard,previous_guard=previous_guard)
                        except Exception as error:report=dict(state='BLOCKED',reason=type(error).__name__+': '+str(error))
                        if report['state']=='BLOCKED':
                            prior=ledger.execute('SELECT payload FROM status WHERE isin=?',(isin,)).fetchone()
                            if not prior or json.loads(prior[0]).get('reason')!=report['reason']:
                                for name in ('V1','V2'):
                                    ledger.execute('INSERT INTO journal(book,payload) VALUES (?,?)',(name,json.dumps(dict(kind='BLOCKED',isin=isin,at=time.time(),reason=report['reason']))))
                        report.update(at=time.time(),next_check=time.time()+120)
                        ledger.execute('INSERT OR REPLACE INTO status VALUES (?,?)',(isin,json.dumps(report)))
                        ledger.commit()
                    return report
                def done(_):self.busy=False;self.next_check=time.time()+60
                def failed(_):self.busy=False;self.next_check=time.time()+60
                if app.job(work,done,failed,lane='read',key='paper_strategy_shadow',isin=isin) is False:self.busy=False
        app.root.after(15000,self.tick)

def attach(app,parent):
    """Read-only view in the existing Demo window, no order controls."""
    import tkinter as tk
    from tkinter.scrolledtext import ScrolledText
    from paper_view import _auto_refresh
    text=ScrolledText(parent,wrap='word',height=14,font=('Segoe UI',10));text.pack(fill='both',expand=True)
    busy=False
    def done(data):
        nonlocal busy
        busy=False
        if app.closing or not parent.winfo_exists():return
        lines=['Теневое сравнение V1 / V2 • два отдельных портфеля по 10 000 €',
            'Основной демосчёт не затрагивается. Одинаковые данные, спред 10 bps, комиссия 1 €.',
            'Вход — на следующей доступной закрытой 5m/15m свече. Стоп имеет приоритет при неоднозначном OHLC.',
            'Прибыльность V2 ещё не доказана. Оценка портфеля — по последним принятым котировкам.']
        for name,m in data['books'].items():
            lines.append(f"\n{name}: капитал {m['equity']:.2f} €; деньги {m['cash']:.2f} €; результат {m['net_profit']:.2f} €; комиссии {m['commissions']:.2f} €; просадка {m['max_drawdown_pct']:.2f}%")
            pf='нет достаточных сделок' if m['profit_factor'] is None else f"{m['profit_factor']:.2f}"
            expectation='нет закрытых сделок' if m['expectancy'] is None else f"{m['expectancy']:.2f} €"
            lines.append(f"Закрыто: {m['closed_trades']}; прибыльных {m['wins']}, убыточных {m['losses']}; Profit Factor: {pf}; средний результат сделки: {expectation}")
        names=dict(zip(ALLOWED,('Apple','NVIDIA')))
        states={'BLOCKED':'Заблокировано','OBSERVED':'Данные обработаны','UNCHANGED':'Ожидание новой свечи'}
        reasons={'NO_CLOSED_STRUCTURE_TRIGGER':'нет пробоя или разворота на закрытом 1H','MOMENTUM_NOT_CONFIRMED':'MACD/RSI не подтвердили вход',
            '4H_NOT_IMPROVING':'улучшение 4H не подтверждено','1D_STRONG_BEARISH_IMPULSE':'сильный нисходящий импульс 1D',
            'ValueError: SHADOW_CORE_DATA_BLOCKED':'исходный CORE заблокирован — качество данных не подтверждено',
            'UNCHANGED_CORE_V1':'решение существующей V1','STRUCTURAL_RR_BELOW_2':'потенциал прибыли к риску меньше 2:1',
            'PAPER_SESSION_CLOSED':'торговая сессия закрыта'}
        def reason(value):return '; '.join(reasons.get(v,v) for v in value.split(';'))
        for isin,s in data.get('status',{}).items():lines.append('\n'+names.get(isin,isin)+': '+states.get(s['state'],s['state'])+' • '+reason(s.get('reason','')))
        lines.append('\nЖурнал последних событий:')
        from datetime import datetime
        from zoneinfo import ZoneInfo
        for row in data.get('journal',[]):
            stamp=row.get('observed_at',row.get('at'));instant=datetime.fromtimestamp(stamp,ZoneInfo('Europe/Berlin')) if isinstance(stamp,(int,float)) else datetime.fromisoformat(stamp).astimezone(ZoneInfo('Europe/Berlin'))
            decision=row.get('decision',{});detail=reason(decision.get('reason',row.get('reason','')))
            if row['kind']=='FILL':detail=f"{row['side']} {row['quantity']} × {row['price']:.4f} €; комиссия {row['commission']:.2f} €; "+detail
            lines.append(f"{instant:%d.%m %H:%M:%S} • {row['book']} • {names.get(row['isin'],row['isin'])} • {decision.get('action',row['kind'])}: {detail}")
        text.configure(state='normal');text.delete('1.0','end');text.insert('1.0','\n'.join(lines));text.configure(state='disabled')
    def refresh():
        nonlocal busy
        if busy:return
        busy=True
        if app.job(lambda:read(app.store.directory/DB_NAME),done,lambda _:done(dict(books={},status={},journal=[])),
            lane='read',key='paper_shadow_view') is False:busy=False
    _auto_refresh(parent,app,refresh,10000)
