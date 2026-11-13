"""EUR-only paper account. No broker transport; quotes come from guarded analytics."""
import json, hashlib, math, sqlite3, time, re
from decimal import Decimal, ROUND_HALF_UP
from datetime import datetime, timezone

ALLOWED={'DE0005140008','DE0007236101','DE0007164600','DE0008404005','US0378331005'}
# Keep the original cohort only for already-authorized historical-risk policies
# and queue priority. Account participation now follows registered instruments;
# it does not authorize a trade or accept any historical defect.
UNIVERSE_VERSION='registered_eur_equities_v1'
ACCOUNT='five-eur-10000-v1'
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

def ledger(db):
    if not db.execute("SELECT 1 FROM sqlite_master WHERE name='paper_accounts'").fetchone():return {'status':'NOT_INITIALIZED','virtual':True}
    cash=money(POLICY['starting_cash']);realized=money(0);fees=money(0);positions={};fills=[]
    for (payload,) in db.execute('SELECT payload FROM paper_fills ORDER BY rowid'):
        e=json.loads(payload);qty=Decimal(e['quantity']);gross=Decimal(e['gross']);fee=Decimal(e['fee'])
        key=(e['isin'],json.dumps(e['identity'],sort_keys=True))
        p=positions.setdefault(key,{'isin':key[0],'identity':e['identity'],'quantity':Decimal(0),'cost':money(0)})
        p['timeframe']=e['timeframe']
        if e['side']=='BUY':p['quantity']+=qty;p['cost']+=gross+fee;cash-=gross+fee
        else:
            cost=p['cost']*qty/p['quantity'];p['quantity']-=qty;p['cost']-=cost;cash+=gross-fee;realized+=gross-fee-cost
        fees+=fee;fills.append(e)
    output=[]
    for p in positions.values():
        output.append({**p,'quantity':str(p['quantity']),'cost':str(money(p['cost'])),'unrealized':None if p['quantity'] else '0.00','mark_status':'REQUIRES_FRESH_READY_QUOTE' if p['quantity'] else 'FLAT'})
    flat=all(Decimal(p['quantity'])==0 for p in output)
    return {'status':'ACTIVE','virtual':True,'account':ACCOUNT,'policy':POLICY,'allowed_isins':registered_isins(db),
            'universe':{'version':UNIVERSE_VERSION,'automatic_new_instruments':True,'scope':'Registered stocks; verified EUR listing and all execution gates required'},
            'cash':str(cash),'realized':str(money(realized)),'fees':str(fees),'unrealized':'0.00' if flat else None,'equity':str(cash) if flat else None,'positions':output,'fills':fills,'currency':'EUR','broker_orders':False}

def quote(info,result,now,accepted_historical_risk=False,stored_listing=None):
    if not instrument_allowed(info) or result.get('isin')!=info['isin']:raise ValueError('PAPER_INSTRUMENT_BLOCKED')
    identity=result.get('identity',{})
    if identity.get('currency')!='EUR' or result.get('timeframe') not in ('5m','15m'):raise ValueError('PAPER_EUR_INTRADAY_REQUIRED')
    from eur_listings import confirmed_listing
    meta={k:identity.get(k) for k in ('source','symbol','venue','currency','timezone')}
    if stored_listing and all(stored_listing.get(k)==identity.get(k) for k in ('source','symbol','venue','currency','timezone')):
        meta=stored_listing
    confirmed=confirmed_listing(info,meta)
    if identity.get('source')=='gettex_chart' and not stored_listing:confirmed=False
    if not confirmed or identity.get('venue') not in ('GER','FRA','GETTEX'):raise ValueError('PAPER_IDENTITY_BLOCKED')
    quality=result.get('data_quality',{})
    if quality.get('state')!='READY' and not accepted_historical_risk:
        detail='; '.join(str(issue) for issue in quality.get('issues', []))
        state=quality.get('state') or 'UNKNOWN'
        raise ValueError(f"PAPER_DATA_BLOCKED ({result['timeframe']}, {state})" + (f": {detail}" if detail else ""))
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
    if accepted_historical_risk:
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
    digest=hashlib.sha256(json.dumps({k:args.get(k) for k in ('request_id','isin','side','quantity','timeframe')},sort_keys=True).encode()).hexdigest()
    prior=db.execute('SELECT digest,payload FROM paper_fills WHERE id=?',(args['request_id'],)).fetchone()
    archived=False
    if not prior and db.execute("SELECT 1 FROM sqlite_master WHERE name='paper_fill_archive'").fetchone():
        prior=db.execute('SELECT digest,payload FROM paper_fill_archive WHERE id=?',(args['request_id'],)).fetchone()
        archived=bool(prior)
    if prior:
        if prior[0]!=digest:raise ValueError('PAPER_REQUEST_REUSED')
        return {'status':'COMPLETED','fill':json.loads(prior[1]),'idempotent':True,'archived':archived}
    token=result.get('_input_revisions')
    if not token or 'instrument:'+info['isin'] not in token or not any(k.startswith('market:'+info['isin']+':') for k in token):raise ValueError('PAPER_NO_INPUT_REVISIONS')
    if db.execute("SELECT 1 FROM settings WHERE key='paper_epoch'").fetchone() and 'setting:paper_epoch' not in token:
        raise ValueError('PAPER_ACCOUNT_RESET')
    for scope,rev in token.items():
        row=db.execute('SELECT revision FROM input_revisions WHERE scope=?',(scope,)).fetchone()
        if (row[0] if row else 0)!=rev:raise ValueError('PAPER_STALE_INPUTS')
    persisted=registered_instrument(db,info['isin'])
    contract=result.get('execution_context') or {}
    hourly=result.get('timeframe') in ('1H','4H')
    hourly_receipt=None
    if hourly:
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
    listing=stored_identity(db,persisted,result.get('identity',{}),quote_result['timeframe'])
    identity,b=quote(persisted,quote_result,now,accepted_historical_risk=manual is not None,stored_listing=listing)
    if (not hourly and contract.get('status')!='READY' and manual is None) or contract.get('isin')!=info['isin']:
        raise ValueError('PAPER_WINDOW_BLOCKED')
    if manual is None and contract.get('trading_engine',{}).get('status')!='READY':raise ValueError('PAPER_ENGINE_DATA_BLOCKED')
    checked=datetime.fromisoformat(contract.get('checked_at',''))
    if checked.tzinfo is None or not 0<=now-checked.timestamp()<=60:raise ValueError('PAPER_STALE_WINDOW')
    windows=contract.get('windows',{})
    if not hourly and manual is None and (set(windows)!={result['timeframe'],'1H','4H'} or any(w.get('status')!='READY' or w.get('identity')!=identity for w in windows.values())):
        raise ValueError('PAPER_WINDOW_IDENTITY_BLOCKED')
    from timeframes import schedule
    import pandas as pd
    instant=pd.Timestamp(now,unit='s',tz='UTC');local=instant.tz_convert(identity['timezone']).tz_localize(None)
    sessions=schedule(identity,local.normalize(),local.normalize())
    if not any(r.open<=instant<r.close and not (pd.notna(r.break_start) and r.break_start<=instant<r.break_end) for _,r in sessions.iterrows()):
        raise ValueError('PAPER_SESSION_CLOSED')
    if args.get('timeframe')!=result['timeframe']:raise ValueError('PAPER_TIMEFRAME_MISMATCH')
    from calculation_window import execution_eligibility
    smart=hourly_receipt if hourly else manual if manual is not None else execution_eligibility(db,persisted,result['timeframe'],identity,instant,args['side'])
    if not hourly and manual is None and contract.get('smart_eligibility')!=smart:raise ValueError('PAPER_SMART_CONTEXT_CHANGED')
    state=ledger(db);fee=money(POLICY['commission']);half=Decimal(POLICY['spread_bps'])/Decimal(20000)
    price=Decimal(str(b['close']))*(1+half if args['side']=='BUY' else 1-half);gross=money(price*q)
    if args['side']=='BUY' and gross+fee>Decimal(state['cash']):raise ValueError('PAPER_INSUFFICIENT_CASH')
    if args['side']=='SELL':
        held=next((p for p in state['positions'] if p['isin']==info['isin'] and p['identity']==identity),None)
        if held is None or Decimal(held['quantity'])<q:raise ValueError('PAPER_INSUFFICIENT_POSITION')
    if (time.time() if live_clock else now)>=args['_deadline']:raise ValueError('PAPER_EXPIRED')
    fill={'request_id':args['request_id'],'at':datetime.fromtimestamp(now,timezone.utc).isoformat(),'isin':info['isin'],'identity':identity,'timeframe':result['timeframe'],'side':args['side'],'quantity':str(q),'reference_price':str(b['close']),'bar_close_at':b['close_at'],'price':str(price),'gross':str(gross),'fee':str(fee),'spread_bps':POLICY['spread_bps'],'virtual':True,'input_revisions':token,'smart_eligibility':smart}
    if manual is not None:fill['manual_risk']=manual
    if hourly:fill['execution_quote_timeframe']=quote_result['timeframe']
    db.execute('INSERT INTO paper_fills VALUES (?,?,?)',(args['request_id'],digest,json.dumps(fill,sort_keys=True)))
    return {'status':'COMPLETED','fill':fill,'account':ledger(db)}

def status(path):
    from pathlib import Path
    with sqlite3.connect(Path(path).resolve().as_uri()+'?mode=ro',uri=True,timeout=.2) as db:
        db.execute('BEGIN')
        state=mark_positions(db,ledger(db))
        from paper_hourly import CONFIG,STATUS,read
        config=read(db,CONFIG,{})
        state['automation']={**config,'instruments':{isin:read(db,STATUS+isin,{}) for isin in config.get('identities',{})}}
        from paper_preparation import STATE
        state['automation']['preparation']=read(db,STATE,{})
        state['archived_fills']=db.execute('SELECT COUNT(*) FROM paper_fill_archive').fetchone()[0] if db.execute("SELECT 1 FROM sqlite_master WHERE name='paper_fill_archive'").fetchone() else 0
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
            candidates=[]
            for tf in ('5m','15m'):
                rows=db.execute('SELECT ts,open,high,low,close,volume FROM series WHERE isin=? AND source=? AND venue=? AND currency=? AND symbol=? AND base_tf=? ORDER BY ts DESC LIMIT 4',(isin,ident['source'],ident['venue'],ident['currency'],ident['symbol'],tf)).fetchall()
                if not rows:continue
                f=pd.DataFrame(rows,columns=['ts','open','high','low','close','volume']).set_index('ts').sort_index();f.index=pd.to_datetime(f.index);f.attrs={**ident,'timeframe':tf}
                for (opening,row),end in zip(f.iterrows(),bar_ends(f)):
                    if pd.notna(end) and end.timestamp()<=now:candidates.append((end,tf,opening,row))
            if not candidates:raise ValueError('PAPER_NO_CLOSED_BAR')
            end,tf,opening,row=max(candidates,key=lambda candidate:candidate[0])
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
