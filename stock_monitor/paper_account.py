"""EUR-only paper account. No broker transport; quotes come from guarded analytics."""
import json, hashlib, math, sqlite3, time, re
from contextlib import closing
from decimal import Decimal, ROUND_HALF_UP
from datetime import datetime, timezone

from paper_entry_modes import V2_RESEARCH_BLOCKER, V2_TIMEFRAME

ALLOWED={'DE0005140008','DE0007236101','DE0007164600','DE0008404005','US0378331005'}
# Keep the original cohort only for already-authorized historical-risk policies
# and queue priority. Account participation now follows registered instruments;
# it does not authorize a trade or accept any historical defect.
UNIVERSE_VERSION='registered_eur_equities_v1'
ACCOUNT='five-eur-10000-v1'
PORTFOLIOS=('V1','V2','V3','V4','V5')
PORTFOLIO_SETTING='paper_portfolios'
PORTFOLIO_VERSION=1
AUTO_PORTFOLIOS_SETTING='paper_auto_portfolios'
AUTO_PORTFOLIOS_VERSION=2
VIEW_PORTFOLIO_SETTING='paper_view_portfolio'
READY_PORTFOLIOS=('V1','V4','V5')
READINESS_BLOCKERS={'V2':V2_RESEARCH_BLOCKER,'V3':'V2_SEED_PROOF_MISSING'}
EXECUTION_TF='15m'
# Stored account contract; legacy rows retain the original description.
POLICY={'currency':'EUR','starting_cash':'10000.00','commission':'1.00','spread_bps':'10','max_bar_age_seconds':900,'execution':'closed 5m/15m close +/- model half-spread; virtual only'}


def instrument_allowed(info):
    """Universe eligibility only; the exact saved EUR listing is checked later."""
    return bool(isinstance(info,dict) and info.get('kind','stock')=='stock'
                and re.fullmatch(r'[A-Z]{2}[A-Z0-9]{9}[0-9]',str(info.get('isin',''))))


def registered_instrument(db,isin):
    row=db.execute('SELECT payload FROM instruments WHERE isin=?',(isin,)).fetchone()
    info=json.loads(row[0]) if row else None
    if not instrument_allowed(info) or info.get('isin')!=isin:
        raise ValueError('PAPER_INSTRUMENT_BLOCKED')
    return info


def registered_isins(db):
    return sorted(i['isin'] for (v,) in db.execute('SELECT payload FROM instruments')
                  if instrument_allowed(i:=json.loads(v)))

def money(x):
    return Decimal(str(x)).quantize(Decimal('.01'),rounding=ROUND_HALF_UP)

def initialize(db):
    db.execute('CREATE TABLE IF NOT EXISTS paper_accounts(id TEXT PRIMARY KEY, policy TEXT NOT NULL)')
    db.execute('CREATE TABLE IF NOT EXISTS paper_fills(id TEXT PRIMARY KEY, digest TEXT NOT NULL, payload TEXT NOT NULL)')
    db.execute('INSERT OR IGNORE INTO paper_accounts VALUES (?,?)',(ACCOUNT,json.dumps(POLICY,sort_keys=True)))
    assert json.loads(db.execute('SELECT policy FROM paper_accounts WHERE id=?',(ACCOUNT,)).fetchone()[0])==POLICY,'PAPER_POLICY_MISMATCH'

def initialize_portfolios(db):
    """One transaction, no mutation of the old combined history or balances."""
    initialize(db)
    db.execute('CREATE TABLE IF NOT EXISTS paper_portfolio_fills '
               '(id TEXT PRIMARY KEY, portfolio TEXT NOT NULL, digest TEXT NOT NULL, payload TEXT NOT NULL)')
    db.execute('CREATE INDEX IF NOT EXISTS paper_fills_by_portfolio ON paper_portfolio_fills(portfolio)')
    db.execute('CREATE TABLE IF NOT EXISTS paper_portfolio_archive '
               '(id TEXT PRIMARY KEY, portfolio TEXT NOT NULL, digest TEXT NOT NULL, '
               'payload TEXT NOT NULL, reset_epoch TEXT NOT NULL)')
    db.execute('CREATE INDEX IF NOT EXISTS paper_archive_by_portfolio ON paper_portfolio_archive(portfolio)')
    policy=json.dumps(POLICY,sort_keys=True)
    db.executemany('INSERT OR IGNORE INTO paper_accounts VALUES (?,?)',
                   [(name,policy) for name in PORTFOLIOS])
    for name in PORTFOLIOS:
        if db.execute('SELECT policy FROM paper_accounts WHERE id=?',(name,)).fetchone()[0]!=policy:
            raise ValueError('PAPER_POLICY_MISMATCH')
    db.execute('INSERT OR IGNORE INTO settings VALUES (?,?)',
               (PORTFOLIO_SETTING,json.dumps(PORTFOLIO_VERSION)))


def portfolios_enabled(db):
    from paper_hourly import read
    return read(db,PORTFOLIO_SETTING)==PORTFOLIO_VERSION


def portfolio_for_mode(config, enabled):
    if not enabled:
        return ACCOUNT
    from paper_entry_modes import STRATEGY_CODES
    return STRATEGY_CODES[config.get('strategy_mode','V1')]


def automatic_portfolios(policy):
    if not isinstance(policy,dict) or policy.get('version')!=AUTO_PORTFOLIOS_VERSION:
        return None
    ready=policy.get('ready')
    return tuple(book for book in PORTFOLIOS if book in ready) if isinstance(ready,list) else ()


def independent_portfolios(db):
    from paper_hourly import read
    return automatic_portfolios(read(db,AUTO_PORTFOLIOS_SETTING))


def portfolio_config(config,portfolio):
    from paper_entry_modes import STRATEGY_CODES
    from paper_v1_long_term import PROFILE_KEY,MODEL
    modes={code:mode for mode,code in STRATEGY_CODES.items()}
    if portfolio not in modes:
        return config
    result=dict(config,strategy_mode=modes[portfolio])
    if portfolio=='V1':
        result[PROFILE_KEY]=MODEL
    return result


def selected_portfolio(db):
    from paper_hourly import CONFIG, read
    split=portfolios_enabled(db)
    viewed=read(db,VIEW_PORTFOLIO_SETTING) if split and independent_portfolios(db) is not None else None
    if viewed in PORTFOLIOS:
        return viewed
    return portfolio_for_mode(read(db,CONFIG,{}),split)


def requested_view(store):
    if store.setting(PORTFOLIO_SETTING,None)!=PORTFOLIO_VERSION:
        return ACCOUNT
    view=store.setting(VIEW_PORTFOLIO_SETTING,None)
    if view in PORTFOLIOS and automatic_portfolios(store.setting(AUTO_PORTFOLIOS_SETTING,None)) is not None:
        return view
    return portfolio_for_mode(store.setting('paper_auto_1h',{}),True)


def entry_status_key(portfolio,isin):
    return f'paper_auto_status:{portfolio}:{isin}'


def initialize_independent_automation(db):
    from paper_hourly import CONFIG, STATUS, read
    if independent_portfolios(db) is not None:
        return
    cfg=read(db,CONFIG,{})
    previous=portfolio_for_mode(cfg,True)
    db.execute('INSERT OR IGNORE INTO settings VALUES (?,?)',(VIEW_PORTFOLIO_SETTING,json.dumps(previous)))
    db.execute('INSERT OR REPLACE INTO settings VALUES (?,?)',(AUTO_PORTFOLIOS_SETTING,
        json.dumps(dict(version=AUTO_PORTFOLIOS_VERSION,ready=list(READY_PORTFOLIOS)))))
    for isin in cfg.get('identities',{}):
        status=read(db,STATUS+isin)
        if status and status.get('strategy_mode','V1')==cfg.get('strategy_mode','V1'):
            db.execute('INSERT OR IGNORE INTO settings VALUES (?,?)',
                       (entry_status_key(previous,isin),json.dumps(status)))


def result_portfolio(db,result):
    return result.get('paper_portfolio') or selected_portfolio(db)


def portfolio_epoch_key(portfolio):
    return 'paper_epoch' if portfolio==ACCOUNT else 'paper_epoch:'+portfolio


def operation_rows(db,portfolio,*,archived=False,revision_only=False):
    """All receipts in one portfolio, in original execution order."""
    if portfolio==ACCOUNT:
        table='paper_fill_archive' if archived else 'paper_fills'
        if archived and not db.execute(
                'SELECT 1 FROM sqlite_master WHERE type=? AND name=?',('table',table)).fetchone():
            return []
        where,args='',()
    else:
        table='paper_portfolio_archive' if archived else 'paper_portfolio_fills'
        where,args=' WHERE portfolio=?',(portfolio,)
    if revision_only:
        columns='id,digest'
    else:
        columns='id,digest,payload,reset_epoch' if archived else 'id,digest,payload'
    return db.execute(f'SELECT {columns} FROM {table}{where} ORDER BY rowid',args).fetchall()


def saved_fill(db,request_id,*,include_archive=True):
    """Request IDs are unique across ledgers, including the old journal."""
    row=db.execute('SELECT digest,payload FROM paper_fills WHERE id=?',(request_id,)).fetchone()
    if row is not None:
        return ACCOUNT,*row,False
    if portfolios_enabled(db):
        row=db.execute('SELECT portfolio,digest,payload FROM paper_portfolio_fills WHERE id=?',(request_id,)).fetchone()
        if row is not None:
            return *row,False
        if include_archive:
            row=db.execute('SELECT portfolio,digest,payload FROM paper_portfolio_archive WHERE id=?',(request_id,)).fetchone()
            if row is not None:
                return *row,True
    if include_archive and db.execute(
            "SELECT 1 FROM sqlite_master WHERE name='paper_fill_archive'").fetchone():
        row=db.execute('SELECT digest,payload FROM paper_fill_archive WHERE id=?',(request_id,)).fetchone()
        if row is not None:
            return ACCOUNT,*row,True
    return None


def maintained_positions(db):
    if not portfolios_enabled(db):
        return ledger(db).get('positions',[])
    return [p for name in (*PORTFOLIOS,ACCOUNT) for p in ledger(db,name).get('positions',[])
            if Decimal(p['quantity'])>0]


def ledger(db,portfolio=None):
    if not db.execute("SELECT 1 FROM sqlite_master WHERE name='paper_accounts'").fetchone():
        return {'status':'NOT_INITIALIZED','virtual':True}
    portfolio=portfolio or selected_portfolio(db)
    if portfolio!=ACCOUNT and (portfolio not in PORTFOLIOS or not portfolios_enabled(db)):
        raise ValueError('PAPER_PORTFOLIO_UNKNOWN')
    from paper_protection import management_key
    from paper_hourly import read
    cash=money(POLICY['starting_cash']);realized=money(0);fees=money(0);positions={};fills=[]
    for _,_,payload in operation_rows(db,portfolio):
        e=json.loads(payload);qty=Decimal(e['quantity']);gross=Decimal(e['gross']);fee=Decimal(e['fee'])
        key=(e['isin'],json.dumps(e['identity'],sort_keys=True))
        p=positions.setdefault(key,{'isin':key[0],'identity':e['identity'],'quantity':Decimal(0),'cost':money(0)})
        p['timeframe']=e['timeframe']
        smart=e.get('smart_eligibility',{})
        if e['side']=='BUY':
            p['quantity']+=qty;p['cost']+=gross+fee;cash-=gross+fee
            p['protection']=smart.get('protection')
            p['partial_exit']=False
            p.pop('auto_entry',None)
            if smart.get('strategy_mode') and e.get('initiator') not in ('MANUAL','CHATGPT'):
                p['auto_entry']=dict(strategy_mode=smart['strategy_mode'],timeframe=e['timeframe'],
                    signal_at=smart['signal_at'],request_id=e['request_id'])
                if smart.get('strategy_model'):
                    p['auto_entry']['strategy_model']=smart['strategy_model']
        else:
            if smart.get('protective_exit',{}).get('reason')=='PARTIAL_2R':
                p['partial_exit']=True
            cost=p['cost']*qty/p['quantity'];p['quantity']-=qty;p['cost']-=cost;cash+=gross-fee;realized+=gross-fee-cost
        fees+=fee;fills.append(e)
    output=[]
    for p in positions.values():
        if p['quantity'] and p.get('auto_entry') and p.get('protection'):
            management=read(db,management_key(p['auto_entry']['request_id']),{})
            if management:
                p['active_stop']=management['stop']
        if portfolio!=ACCOUNT or portfolios_enabled(db):
            p['portfolio']=portfolio
        output.append({**p,'quantity':str(p['quantity']),'cost':str(money(p['cost'])),'unrealized':None if p['quantity'] else '0.00','mark_status':'REQUIRES_FRESH_READY_QUOTE' if p['quantity'] else 'FLAT'})
    flat=all(Decimal(p['quantity'])==0 for p in output)
    return {'status':'ACTIVE','virtual':True,'account':portfolio,'policy':POLICY,'allowed_isins':registered_isins(db),
            'universe':{'version':UNIVERSE_VERSION,'automatic_new_instruments':True,'scope':'Registered stocks; verified EUR listing and all execution gates required'},
            'execution_timeframe':EXECUTION_TF,'cash':str(cash),'realized':str(money(realized)),'fees':str(fees),'unrealized':'0.00' if flat else None,'equity':str(cash) if flat else None,'positions':output,'fills':fills,'currency':'EUR','broker_orders':False}

def quote(info,result,now,accepted_historical_risk=False,stored_listing=None):
    if not instrument_allowed(info) or result.get('isin')!=info['isin']:raise ValueError('PAPER_INSTRUMENT_BLOCKED')
    identity=result.get('identity',{})
    if result.get('timeframe')!=EXECUTION_TF:raise ValueError('PAPER_EXECUTION_REQUIRES_15M')
    if identity.get('currency')!='EUR':raise ValueError('PAPER_EUR_INTRADAY_REQUIRED')
    from eur_listings import confirmed_listing
    meta={k:identity.get(k) for k in ('source','symbol','venue','currency','timezone')}
    if stored_listing and all(stored_listing.get(k)==identity.get(k) for k in ('source','symbol','venue','currency','timezone')):
        meta=stored_listing
    confirmed=confirmed_listing(info,meta)
    if identity.get('source')=='gettex_chart' and not stored_listing:confirmed=False
    if not confirmed or identity.get('venue') not in ('GER','FRA','GETTEX'):raise ValueError('PAPER_IDENTITY_BLOCKED')
    from gettex_first import source_contract
    factual_gettex=source_contract(info,meta)
    quality=result.get('data_quality',{})
    if quality.get('state')!='READY' and not accepted_historical_risk and not factual_gettex:
        detail='; '.join(str(issue) for issue in quality.get('issues', []))
        state=quality.get('state') or 'UNKNOWN'
        raise ValueError(f"PAPER_DATA_BLOCKED ({result['timeframe']}, {state})" + (f": {detail}" if detail else ""))
    if not factual_gettex:
        if any(quality.get('identity',{}).get(k)!=identity.get(k) for k in ('source','symbol','venue','currency','timezone')):raise ValueError('PAPER_DQ_IDENTITY_BLOCKED')
        checked=datetime.fromisoformat(quality.get('checked_at',''))
        if checked.tzinfo is None or not 0<=now-checked.timestamp()<=900:raise ValueError('PAPER_STALE_DQ')
    bars=[b for b in result.get('bars',[]) if b.get('closed')]
    if not bars:raise ValueError('PAPER_NO_CLOSED_BAR')
    b=bars[-1];stamp=datetime.fromisoformat(b['close_at'])
    if stamp.tzinfo is None or not 0<=now-stamp.timestamp()<=POLICY['max_bar_age_seconds']:raise ValueError('PAPER_STALE_QUOTE')
    vals=[b.get(k) for k in ('open','high','low','close','volume')]
    if any(isinstance(v,bool) or not isinstance(v,(int,float)) or not math.isfinite(v) for v in vals):raise ValueError('PAPER_BAD_OHLCV')
    o,h,l,c,v=vals
    if min(o,h,l,c)<=0 or v<=0 or h<max(o,l,c) or l>min(o,h,c):raise ValueError('PAPER_BAD_OHLCV')
    if accepted_historical_risk or (factual_gettex and quality.get('state')!='READY'):
        # The quote itself is NOT a waivable historical problem. Recheck its
        # actual session alignment, closed boundary and OHLCV independently.
        import pandas as pd
        import data_quality as dq
        from analytics import bar_ends
        opening=pd.Timestamp(b.get('open_at'))
        if pd.isna(opening) or opening.tzinfo is None:raise ValueError('PAPER_QUOTE_TIMESTAMP_UNCONFIRMED')
        frame=pd.DataFrame([dict(zip(dq.FIELDS,vals))],index=pd.DatetimeIndex([opening.tz_convert(identity['timezone']).tz_localize(None)]))
        frame.attrs=dict(identity,timeframe=result['timeframe'])
        report=dq.audit(frame,result['timeframe'],identity,pd.Timestamp(now,unit='s',tz='UTC'))
        if report['state']!='READY' or bar_ends(frame)[0]!=pd.Timestamp(b['close_at']):raise ValueError('PAPER_QUOTE_DQ_BLOCKED')
    return identity,b

def validate_session(identity,now):
    from timeframes import schedule
    import pandas as pd
    instant=pd.Timestamp(now,unit='s',tz='UTC');local=instant.tz_convert(identity['timezone']).tz_localize(None)
    sessions=schedule(identity,local.normalize(),local.normalize())
    if not any(r.open<=instant<r.close and not (pd.notna(r.break_start) and r.break_start<=instant<r.break_end) for _,r in sessions.iterrows()):
        raise ValueError('PAPER_SESSION_CLOSED')
    return instant

def validate_execution_inputs(db,result,isin):
    token=result.get('_input_revisions')
    if not token or 'instrument:'+isin not in token or not any(k.startswith('market:'+isin+':') for k in token):raise ValueError('PAPER_NO_INPUT_REVISIONS')
    if db.execute("SELECT 1 FROM settings WHERE key='paper_epoch'").fetchone() and 'setting:paper_epoch' not in token:
        raise ValueError('PAPER_ACCOUNT_RESET')
    if portfolios_enabled(db):
        scope='setting:'+portfolio_epoch_key(result_portfolio(db,result))
        if scope not in token or 'setting:'+PORTFOLIO_SETTING not in token:
            raise ValueError('PAPER_ACCOUNT_RESET')
        if independent_portfolios(db) is not None and 'setting:'+AUTO_PORTFOLIOS_SETTING not in token:
            raise ValueError('PAPER_STALE_INPUTS')
    for scope,rev in token.items():
        row=db.execute('SELECT revision FROM input_revisions WHERE scope=?',(scope,)).fetchone()
        if (row[0] if row else 0)!=rev:raise ValueError('PAPER_STALE_INPUTS')
    return token

def execute(db,info,result,args,now=None):
    """Caller holds BEGIN IMMEDIATE on the same source DB through commit."""
    live_clock=now is None
    now=time.time() if live_clock else now
    if now>=args['_deadline']:raise ValueError('PAPER_EXPIRED')
    if args.get('isin')!=info['isin']:raise ValueError('PAPER_INSTRUMENT_BLOCKED')
    if args.get('side') not in ('BUY','SELL'):raise ValueError('PAPER_BAD_SIDE')
    qty=args.get('quantity')
    if isinstance(qty,bool) or not isinstance(qty,(int,float)) or not math.isfinite(qty) or qty<=0:raise ValueError('PAPER_BAD_QUANTITY')
    q=Decimal(str(qty))
    if q.as_tuple().exponent < -6:raise ValueError('PAPER_QUANTITY_PRECISION')
    intent=result.get('discretionary')
    selected=selected_portfolio(db)
    portfolio=result_portfolio(db,result)
    split=portfolios_enabled(db)
    if split and not result.get('paper_portfolio'):
        raise ValueError('PAPER_PORTFOLIO_REQUIRED')
    if portfolio not in (*PORTFOLIOS,ACCOUNT) or not split and portfolio!=ACCOUNT:
        raise ValueError('PAPER_PORTFOLIO_UNKNOWN')
    if args.get('_paper_portfolio',portfolio)!=portfolio:
        raise ValueError('PAPER_PORTFOLIO_CHANGED')
    manage_only=result.get('paper_manage_only') is True
    ready=independent_portfolios(db)
    if manage_only and (intent or args['side']!='SELL'):
        raise ValueError('PAPER_PORTFOLIO_CHANGED')
    if not manage_only:
        if ready is None and portfolio!=selected:
            raise ValueError('PAPER_PORTFOLIO_CHANGED')
        if ready is not None and portfolio not in PORTFOLIOS:
            raise ValueError('PAPER_PORTFOLIO_CHANGED')
        if ready is not None and 'hourly' in result and not intent and portfolio not in ready:
            raise ValueError('PAPER_STRATEGY_NOT_READY')
    digest_fields={k:args.get(k) for k in ('request_id','isin','side','quantity','timeframe')}
    if portfolio!=ACCOUNT:
        digest_fields['paper_portfolio']=portfolio
    if intent:
        from paper_orders import check_binding
        check_binding(result,args)
        digest_fields['discretionary']=intent
    digest=hashlib.sha256(json.dumps(digest_fields,sort_keys=True).encode()).hexdigest()
    prior=saved_fill(db,args['request_id'])
    if prior:
        if prior[:2]!=(portfolio,digest):raise ValueError('PAPER_REQUEST_REUSED')
        return {'status':'COMPLETED','fill':json.loads(prior[2]),'idempotent':True,'archived':prior[3]}
    token=validate_execution_inputs(db,result,info['isin'])
    persisted=registered_instrument(db,info['isin'])
    contract=result.get('execution_context') or {}
    hourly=result.get('timeframe') in ('1H','4H') or 'hourly' in result
    hourly_receipt=None
    if hourly:
        if intent:
            from paper_orders import validate
        else:
            from paper_hourly import validate
        hourly_receipt=validate(db,persisted,result,args,now)
    manual=None
    if not hourly and contract.get('status')=='MANUAL_RISK_ACCEPTED':
        from cp_controls import manual_gate
        manual=manual_gate(db,info['isin'],result.get('identity',{}),result['timeframe'],contract.get('manual_inputs'))
        if contract.get('manual_risk')!=manual:raise ValueError('PAPER_MANUAL_CONTEXT_CHANGED')
        if not contract.get('analysis_ready') or contract.get('trading_engine',{}).get('status')!='READY':
            raise ValueError('PAPER_ENGINE_DATA_BLOCKED')
    from eur_listings import stored_identity
    quote_result=result['execution_quote'] if hourly else result
    if result.get('identity',{}).get('source')=='gettex_chart' and 'setting:gettex_chart:'+info['isin']+':'+quote_result['timeframe'] not in token:
        raise ValueError('PAPER_NO_LISTING_REVISION')
    analysis_identity=result.get('identity',{})
    origin=analysis_identity
    if hourly and not intent:
        from paper_v2_quotes import bound_identity
        origin=bound_identity(db,persisted,result)
    listing=stored_identity(db,persisted,origin,quote_result['timeframe'])
    identity,b=quote(persisted,quote_result,now,accepted_historical_risk=manual is not None,stored_listing=listing)
    execution_identity=identity
    if identity!=analysis_identity:
        # Only the exact-row V2 adapter above may separate price provenance
        # from the unchanged canonical position/analysis binding.
        if identity!=origin or not hourly or intent:raise ValueError('PAPER_AUTO_QUOTE_IDENTITY_BLOCKED')
        identity=analysis_identity
    if (not hourly and contract.get('status')!='READY' and manual is None) or contract.get('isin')!=info['isin']:
        raise ValueError('PAPER_WINDOW_BLOCKED')
    if manual is None and contract.get('trading_engine',{}).get('status')!='READY':raise ValueError('PAPER_ENGINE_DATA_BLOCKED')
    checked=datetime.fromisoformat(contract.get('checked_at',''))
    if checked.tzinfo is None or not 0<=now-checked.timestamp()<=60:raise ValueError('PAPER_STALE_WINDOW')
    windows=contract.get('windows',{})
    if not hourly and manual is None and (set(windows)!={result['timeframe'],'1H','4H'} or any(w.get('status')!='READY' or w.get('identity')!=identity for w in windows.values())):
        raise ValueError('PAPER_WINDOW_IDENTITY_BLOCKED')
    instant=validate_session(identity,now)
    if args.get('timeframe')!=result['timeframe']:raise ValueError('PAPER_TIMEFRAME_MISMATCH')
    from calculation_window import execution_eligibility
    smart=hourly_receipt if hourly else manual if manual is not None else execution_eligibility(db,persisted,result['timeframe'],identity,instant,args['side'])
    if not hourly and manual is None and contract.get('smart_eligibility')!=smart:raise ValueError('PAPER_SMART_CONTEXT_CHANGED')
    execution_policy=POLICY
    if portfolio=='V2' and smart.get('execution_policy'):
        from paper_v2_30m import COSTS
        if smart['execution_policy']!=COSTS:raise ValueError('V2_EXECUTION_POLICY_CHANGED')
        execution_policy={**POLICY,**COSTS}
    state=ledger(db,portfolio);fee=money(execution_policy['commission']);half=Decimal(execution_policy['spread_bps'])/Decimal(20000)
    price=Decimal(str(b['close']))*(1+half if args['side']=='BUY' else 1-half);gross=money(price*q)
    if args['side']=='BUY' and gross+fee>Decimal(state['cash']):raise ValueError('PAPER_INSUFFICIENT_CASH')
    if args['side']=='SELL':
        held=next((p for p in state['positions'] if p['isin']==info['isin'] and p['identity']==identity),None)
        if held is None or Decimal(held['quantity'])<q:raise ValueError('PAPER_INSUFFICIENT_POSITION')
    if (time.time() if live_clock else now)>=args['_deadline']:raise ValueError('PAPER_EXPIRED')
    fill={'request_id':args['request_id'],'at':datetime.fromtimestamp(now,timezone.utc).isoformat(),'isin':info['isin'],'identity':identity,'timeframe':result['timeframe'],'side':args['side'],'quantity':str(q),'reference_price':str(b['close']),'bar_close_at':b['close_at'],'price':str(price),'gross':str(gross),'fee':str(fee),'spread_bps':POLICY['spread_bps'],'virtual':True,'input_revisions':token,'smart_eligibility':smart}
    fill['spread_bps']=execution_policy['spread_bps']
    if execution_identity!=identity:fill['execution_identity']=execution_identity
    if manual is not None:fill['manual_risk']=manual
    if hourly:fill['execution_quote_timeframe']=quote_result['timeframe']
    fill['initiator']=intent['initiator'] if intent else hourly_receipt.get('strategy_label','V1') if hourly else 'CHATGPT'
    if portfolio==ACCOUNT:
        db.execute('INSERT INTO paper_fills VALUES (?,?,?)',(args['request_id'],digest,json.dumps(fill,sort_keys=True)))
    else:
        fill['paper_portfolio']=portfolio
        db.execute('INSERT INTO paper_portfolio_fills VALUES (?,?,?,?)',
                   (args['request_id'],portfolio,digest,json.dumps(fill,sort_keys=True)))
    return {'status':'COMPLETED','fill':fill,'account':ledger(db,portfolio)}

def status(path, *, include_journal=False,portfolio=None):
    from pathlib import Path
    with closing(sqlite3.connect(Path(path).resolve().as_uri()+'?mode=ro',uri=True,timeout=.2)) as db:
        db.execute('BEGIN')
        state=mark_positions(db,ledger(db,portfolio))
        portfolio=state.get('account',ACCOUNT)
        split=portfolios_enabled(db)
        if split:
            balances={}
            for name in PORTFOLIOS:
                book=state if name==portfolio else ledger(db,name)
                balances[name]=dict(starting_cash=POLICY['starting_cash'],cash=book['cash'],
                    realized=book['realized'],fees=book['fees'],
                    open_positions=sum(Decimal(p['quantity'])>0 for p in book['positions']))
            state['portfolios']=balances
            legacy=ledger(db,ACCOUNT)
            state['legacy_portfolio']=dict(cash=legacy['cash'],fills=len(legacy['fills']),
                open_positions=sum(Decimal(p['quantity'])>0 for p in legacy['positions']))
        from paper_hourly import CONFIG,STATUS,read
        config=read(db,CONFIG,{})
        ready=independent_portfolios(db)
        if ready is not None:
            enabled=bool(config.get('enabled'))
            running=ready if enabled else ()
            for name,balance in state.get('portfolios',{}).items():
                balance['automation']='ACTIVE' if name in running else 'PAUSED' if name in ready else 'NOT_READY'
                if name not in ready and name in READINESS_BLOCKERS:
                    balance['readiness_blocker']=READINESS_BLOCKERS[name]
            state['automatic_portfolios']=list(running)
            profile=portfolio_config(config,portfolio)
            state['automation']={**profile,'enabled':portfolio in running,'global_enabled':enabled,
                'independent':True,'instruments':{isin:read(db,entry_status_key(portfolio,isin),{})
                    for isin in config.get('identities',{})}}
        else:
            state['automation']={**config,'instruments':{isin:read(db,STATUS+isin,{})
                    for isin in config.get('identities',{})}}
        if state['automation'].get('strategy_mode')=='V2':
            state['automation']['instruments']={
                isin:item for isin,item in state['automation']['instruments'].items()
                if item.get('candidate_timeframes')==[V2_TIMEFRAME]
                and set(item.get('timeframes',{}))<={V2_TIMEFRAME}}
        from paper_preparation import STATE
        state['automation']['preparation']=read(db,STATE,{})
        state['archived_fills']=len(operation_rows(db,portfolio,archived=True))
        if include_journal:
            from paper_journal import read as journal
            from paper_v5_context_study import summary as context_study
            state['journal']=journal(db,state.get('fills',[]),portfolio=portfolio,legacy=split)
            state['v5_context_study']=context_study(db)
        return state

def mark_positions(db,state,now=None):
    """Conservative EUR valuation from the same saved identity; no fallback quotes."""
    if state.get('status')!='ACTIVE':return state
    import pandas as pd
    from analytics import bar_ends
    live_clock=now is None
    now=time.time() if live_clock else now
    total=Decimal(0);complete=True
    for p in state['positions']:
        qty=Decimal(p['quantity'])
        if not qty:continue
        # Never retain a previous valuation when its current quote is rejected.
        for key in ('mark','mark_at','market_value','mark_context','mark_reason','mark_timeframe','mark_quality','archive_quality'):
            p.pop(key,None)
        try:
            ident=p['identity'];isin=p['isin']
            info=json.loads(db.execute('SELECT payload FROM instruments WHERE isin=?',(isin,)).fetchone()[0])
            tf=EXECUTION_TF
            rows=db.execute('SELECT ts,open,high,low,close,volume FROM series WHERE isin=? AND source=? AND venue=? AND currency=? AND symbol=? AND base_tf=? ORDER BY ts DESC LIMIT 4',(isin,ident['source'],ident['venue'],ident['currency'],ident['symbol'],tf)).fetchall()
            f=pd.DataFrame(rows,columns=['ts','open','high','low','close','volume']).set_index('ts').sort_index();f.index=pd.to_datetime(f.index);f.attrs={**ident,'timeframe':tf}
            candidates=[(end,opening,row) for (opening,row),end in zip(f.iterrows(),bar_ends(f))
                        if pd.notna(end) and end.timestamp()<=now] if not f.empty else []
            if not candidates:raise ValueError('PAPER_NO_CLOSED_BAR')
            end,opening,row=max(candidates,key=lambda candidate:candidate[0])
            if not 0<=now-end.timestamp()<=POLICY['max_bar_age_seconds']:raise ValueError('PAPER_STALE_QUOTE')
            # Valuation of an existing holding depends on this real quote, not
            # on an indicator warmup or the full archive's trading eligibility.
            # Revalidate that quote with the existing DQ engine; never mint a
            # trade permission or modify the saved archive report.
            import data_quality as dq
            f=pd.DataFrame([row.to_dict()],index=pd.DatetimeIndex([opening]));f.attrs={**ident,'timeframe':tf}
            quality=dq.audit(f,tf,ident,pd.Timestamp(now,unit='s',tz='UTC'))
            p.update(mark_quality=quality['state'],mark_timeframe=tf)
            if quality['state']!='READY':raise ValueError('PAPER_MARK_'+quality['state'])
            archive=db.execute('SELECT value FROM settings WHERE key=?',('data_quality:'+isin+':'+ident['symbol']+':'+tf,)).fetchone()
            p['archive_quality']=json.loads(archive[0]).get('state','UNKNOWN') if archive else 'UNKNOWN'
            bars=[{**row.to_dict(),'open_at':(opening.tz_localize(ident['timezone']) if opening.tzinfo is None else opening).isoformat(),
                   'closed':True,'close_at':end.isoformat()}]
            from eur_listings import stored_identity
            _,b=quote(info,dict(isin=isin,identity=ident,timeframe=tf,data_quality=quality,bars=bars),time.time() if live_clock else now,
                      stored_listing=stored_identity(db,info,ident,tf))
            mark=Decimal(str(b['close']));value=money(qty*mark)
            p.update(mark=str(mark),mark_at=b['close_at'],market_value=str(value),unrealized=str(money(value-Decimal(p['cost']))),
                     mark_status='READY',mark_context='CURRENT_CLOSED_BAR')
            total+=Decimal(p['unrealized'])
        except Exception as error:
            code=str(error)
            p.update(mark_status='DATA_BLOCKED',mark_reason=code if code.startswith('PAPER_') else 'VALUATION_UNAVAILABLE:'+type(error).__name__,unrealized=None)
            complete=False
    state['valuation_checked_at']=datetime.fromtimestamp(now,timezone.utc).isoformat()
    state['unrealized']=str(money(total)) if complete else None
    state['equity']=str(money(Decimal(state['cash'])+sum(Decimal(p.get('market_value','0')) for p in state['positions']))) if complete else None
    return state
