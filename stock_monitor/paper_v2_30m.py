"""Frozen 30m V2 adapter for the existing PAPER/CORE execution path.

The research feature predicates live in paper_v2_30m_features. No feed, broker,
training, account reset or inferred candle repair is performed here.
"""
import hashlib
import json
import math
from types import SimpleNamespace
import numpy as np
import pandas as pd
import paper_v2_30m_features as f

MODEL = 'v2_frozen_forward_watch_1'
MODEL_KEY = 'paper_v2_30m_model'
ADMISSION_KEY = 'paper_v2_30m_admission'
COSTS = {'commission':'2.00', 'spread_bps':'20'}
POLICY = dict(early_risk=.005, total_risk=.02, allocation=.45, minimum_rr=2.)
_CACHE = {}


def canonical(value):
    return json.dumps(value,sort_keys=True,separators=(',',':'),allow_nan=False)


def validate_model(model, now):
    if not isinstance(model,dict) or model.get('schema')!=1 or model.get('model')!=MODEL:
        raise ValueError('V2_30M_MODEL_MISSING')
    unsigned={k:v for k,v in model.items() if k!='artifact_sha256'}
    if hashlib.sha256(canonical(unsigned).encode()).hexdigest()!=model.get('artifact_sha256'):
        raise ValueError('V2_30M_MODEL_CHANGED')
    week=pd.Timestamp(now).tz_convert('Europe/Berlin').tz_localize(None).to_period('W-SUN').start_time.date().isoformat()
    if model.get('week_start')!=week or not model.get('training_end','9999')<week:
        raise ValueError('V2_30M_MODEL_WEEK_EXPIRED')
    if (model.get('features')!=list(f.FEATURES) or model.get('interim')!=list(f.INTERIM)
            or model.get('entries')!=list(f.ENTRIES) or len(model.get('models',[]))!=2
            or model.get('fee')!=2. or model.get('half_spread')!=.001):
        raise ValueError('V2_30M_MODEL_CONTRACT_CHANGED')
    return model['artifact_sha256']


def authority(db, now):
    from paper_hourly import read
    model=read(db,MODEL_KEY,{})
    digest=validate_model(model,now)
    permit=read(db,ADMISSION_KEY,{})
    if (permit.get('model')!=MODEL or permit.get('artifact_sha256')!=digest
            or permit.get('enabled') is not True or permit.get('e2e_pass') is not True):
        raise ValueError('V2_30M_E2E_NOT_ACCEPTED')
    return model


def activate(db, model, e2e, now):
    """Explicit owner task #24; never called by a calculation or on import."""
    from paper_account import AUTO_PORTFOLIOS_SETTING,automatic_portfolios
    from paper_hourly import read
    digest=validate_model(model,now)
    if e2e.get('passed') is not True or e2e.get('model')!=MODEL or e2e.get('artifact_sha256')!=digest:
        raise ValueError('V2_30M_E2E_NOT_ACCEPTED')
    policy=read(db,AUTO_PORTFOLIOS_SETTING,{})
    ready=automatic_portfolios(policy)
    if ready is None: raise ValueError('V2_30M_INDEPENDENT_ACCOUNTS_REQUIRED')
    for key,value in [(MODEL_KEY,model),(ADMISSION_KEY,dict(enabled=True,model=MODEL,
            artifact_sha256=digest,e2e_pass=True,accepted_at=pd.Timestamp(now).isoformat()))]:
        db.execute('INSERT OR REPLACE INTO settings VALUES (?,?)',(key,json.dumps(value)))
    policy=dict(policy,ready=list(dict.fromkeys((*ready,'V2'))))
    db.execute('UPDATE settings SET value=? WHERE key=?',(json.dumps(policy),AUTO_PORTFOLIOS_SETTING))


def sized(cash, price, volume, stop, target):
    half,fee=.001,2.
    distance=price-stop*(1-half)
    risk=cash*POLICY['early_risk']
    budget=cash*POLICY['allocation']
    if not all(math.isfinite(float(v)) for v in (cash,price,volume,stop,target)) or distance<=0 or risk<=2*fee:
        return 0,'V2_CASH_OR_RISK'
    quantity=min(10,int(volume),int((budget-fee)//price),int((risk-2*fee)//distance))
    while quantity>0 and (round(price*quantity,2)+fee>budget or distance*quantity+2*fee>risk):
        quantity-=1
    if quantity<1: return 0,'V2_CASH_OR_RISK'
    if (target*(1-half)-price)*quantity-2*fee<POLICY['minimum_rr']*(distance*quantity+2*fee):
        return 0,'V2_REWARD_AFTER_COSTS'
    return quantity,None


def environment():
    base=SimpleNamespace(MAX_SHARES=10,POLICY=POLICY)
    return SimpleNamespace(Q=pd.Timedelta(minutes=15),FEE=2.,HALF_SPREAD=.001,
        CASH=10000.,HALT_DAYS=('2026-10-26',),base=base)


def grid(start,end):
    from timeframes import schedule
    parts=[]
    for session in schedule({'venue':'GETTEX','timezone':'Europe/Berlin'},start,end).itertuples():
        stamps=pd.date_range(session.open+pd.Timedelta(minutes=15),session.close,freq='15min')
        parts.append(pd.DataFrame({'day':session.Index.date().isoformat(),'session_close':session.close,
            'minutes':np.arange(1,len(stamps)+1)*15},index=stamps))
    return pd.concat(parts) if parts else pd.DataFrame()


def forecast(model,number,code,entry,exit_rule,values):
    fit=model['models'][number].get(exit_rule)
    if not fit: return None,0
    x=np.asarray(values,dtype=float)
    center,scale,coeff=(np.asarray(fit[key],dtype=float) for key in ('center','scale','coeff'))
    if x.shape!=center.shape or not np.isfinite(x).all(): return None,0
    design=f.basis(np.clip((x[None,:]-center)/scale,-4,4))
    score=float((design@coeff)[0])
    history=next((np.asarray(g['values'],dtype=float) for g in fit['groups']
        if g['isin']==code and g['entry']==entry),None)
    if history is None or not len(history): return score,0
    query=x[[2,16,10]]
    strength=1/(1+np.abs(history[:,:3]-query).sum(axis=1))
    score+=float(np.clip(np.dot(strength,history[:,3])/(3.+strength.sum()),-14.,14.))
    return score,len(history)


def candidates(tapes,g,when,model):
    """Same frozen watch predicates; prices after `when` cannot enter inputs."""
    s=environment()
    signal=when-2*s.Q
    waits={code:'V2_NO_COMPLETE_SIGNAL_CANDLE' for code in tapes}
    if signal not in g.index or g.at[signal,'day']!=g.at[when,'day'] or g.at[signal,'minutes']<150:
        return [],{code:'V2_WARMUP_OR_SESSION_BOUNDARY' for code in tapes}
    frame=f.sample_features(s,g,tapes)
    if frame.empty: return [],waits
    interim=f.interim_features(s,g,tapes,frame,when)
    enriched=f.enrich(tapes,frame,interim)
    room,_,_=f.enough_room(s,g,tapes,frame)
    floors=enriched.floor_margin.ge(0)
    conditions={
        'pullback':floors.to_numpy(),
        'trend_room':(floors & enriched.own_120m.ge(0) & (room>=0)).to_numpy(),
        'recovery':(floors & enriched.drift_90.lt(0) & enriched.interim_move.ge(.18)).to_numpy(),
        'continuation':(enriched.own_120m.gt(0) & frame.relative_30m.ge(0)
            & frame.close_location.ge(.55) & enriched.interim_move.ge(-.15)).to_numpy()}
    patterns,_,_=f.entry_patterns(s,g,tapes,frame,when)
    conditions['false_break']=patterns[2][f.Policy('false_break','any','step30')]
    loc=g.index.get_loc(when)
    prior=loc-1
    quarter=pd.DataFrame({code:tape.close for code,tape in tapes.items()},index=g.index)
    returns=np.log(quarter/quarter.shift())
    offers=[]
    for pos in np.flatnonzero(frame.index==signal):
        row=frame.iloc[pos];code=row['isin'];tape=tapes[code]
        waits[code]='V2_NO_QUALIFYING_SETUP'
        q,volume=tape.close[loc],tape.volume[loc]
        atr,reference=float(row.atr),float(row.reference)
        peers=returns.iloc[loc].drop(labels=code).dropna()
        if len(peers)<12 or not np.isfinite(q) or not volume>0: continue
        first=(tape.close[prior]-reference)/atr
        relative=(q-tape.close[prior]-tape.close[prior]*peers.median())/atr
        if not np.isfinite(first) or not np.isfinite(relative) or first>0 and relative<-.50:
            waits[code]='V2_RELATIVE_REVERSAL_OR_PEERS';continue
        for entry,mask in conditions.items():
            if not mask[pos]:continue
            anchor=reference-(0. if entry=='continuation' else .30 if entry=='false_break' else .65)*atr
            if not anchor+.05*atr<=q<=anchor+.65*atr:
                waits[code]='V2_PRICE_OUTSIDE_DELAYED_ENTRY';continue
            distance=max(1.15*atr,.0025*q);stop,target=q-distance,q+3.75*distance
            qty,why=sized(10000.,q*1.001,volume,stop,target)
            if not qty: waits[code]=why;continue
            values=np.r_[row.loc[list(f.FEATURES)].to_numpy(dtype=float),interim[pos],
                (q-reference)/atr,np.log1p(volume),(4./(qty*q)+.002)/(atr/q),
                [float(entry==x) for x in f.ENTRIES],[float(entry==x) for x in f.PATTERNS]]
            for exit_rule in f.EXITS:
                if when+f.EXITS[exit_rule]*s.Q>g.at[when,'session_close']:
                    waits[code]='V2_NOT_ENOUGH_SESSION_FOR_EXIT';continue
                score,seen=forecast(model,int(entry=='false_break'),code,entry,exit_rule,values)
                if seen<2: waits[code]='V2_INSUFFICIENT_PERSONAL_HISTORY';continue
                if score is None or score<4.: waits[code]='V2_EXPECTED_NET_BELOW_4_EUR';continue
                offers.append(dict(isin=code,score=score,setup=entry,exit_rule=exit_rule,
                    atr=atr,anchor=anchor,stop=stop,target=target,quote=float(q),
                    quote_end=when.isoformat(),signal_closed=signal.isoformat(),
                    personal_days=seen,model=MODEL,artifact_sha256=model['artifact_sha256']))
    return sorted(offers,key=lambda o:(-o['score'],o['isin'],o['setup']+'/any/'+o['exit_rule'])),waits


def context(store,info,identity,now,execution_quote):
    from eur_listings import stored_identity
    import paper_v2_quotes as sources
    cache=store.read_cache
    with store.connect() as db:
        for key in (MODEL_KEY,ADMISSION_KEY): cache.depend('setting:'+key)
        model=authority(db,now)
        if not execution_quote or not execution_quote.get('bars'):
            return {'action':'WAIT','reason':'V2_EXECUTION_QUOTE_UNAVAILABLE'}
        when=pd.Timestamp(execution_quote['bars'][-1]['close_at'])
        if when.tz_convert('Europe/Berlin').minute%30:
            return {'action':'WAIT','reason':'V2_WAIT_NEXT_30M_EXECUTION_SLOT'}
        if not pd.Timedelta(0)<=now-when<=pd.Timedelta(minutes=15):
            return {'action':'BLOCKED','reason':'PAPER_STALE_QUOTE'}
        g=grid((when-pd.Timedelta(days=8)).date().isoformat(),when.date().isoformat())
        g=g.loc[g.index<=when]
        if g.empty or when not in g.index: return {'action':'WAIT','reason':'V2_SESSION_GRID_UNAVAILABLE'}
        instruments=[json.loads(row[0]) for row in db.execute('SELECT payload FROM instruments ORDER BY isin')]
        official=execution_quote.get('identity',{}).get('source')=='gettex'
        tapes={};raws={};peers={}
        for item in instruments:
            from paper_account import instrument_allowed
            if not instrument_allowed(item):continue
            code=item['isin'];key='gettex_chart:'+code+':15m'
            for scope in ('instrument:'+code,'setting:'+key,'market:'+code+':15m'): cache.depend(scope)
            saved=db.execute('SELECT value FROM settings WHERE key=?',(key,)).fetchone()
            meta=sources.identity(item) if official else json.loads(saved[0]).get('identity',{}) if saved else {}
            if (meta.get('source') not in ('gettex','gettex_chart') or meta.get('venue')!='GETTEX' or meta.get('currency')!='EUR'
                    or meta.get('timezone')!='Europe/Berlin' or not stored_identity(db,item,meta,'15m')): continue
            start=str((g.index[0]-pd.Timedelta(minutes=15)).tz_convert('Europe/Berlin').tz_localize(None))
            end=str((when-pd.Timedelta(minutes=15)).tz_convert('Europe/Berlin').tz_localize(None))
            rows=sources.rows(db,code,meta,start,end)
            from paper_hourly import digest
            peers['market:'+code+':15m']=(code,meta,start,end,digest(rows))
            if not rows:continue
            raws[code]=rows
        fingerprint=digest([model['artifact_sha256'],when.isoformat(),instruments,
                            [(key,proof[-1]) for key,proof in peers.items()]])
        cached=_CACHE.get(fingerprint)
        if cached is not None:
            offers,waits=cached
        else:
            offers=waits=None
        for code,rows in (() if cached is not None else raws.items()):
            raw=pd.DataFrame(rows,columns=['ts','open','high','low','close','volume']).set_index('ts')
            raw=raw.apply(pd.to_numeric,errors='coerce')
            raw.index=pd.DatetimeIndex(raw.index).tz_localize('Europe/Berlin',ambiguous='NaT',nonexistent='NaT')+pd.Timedelta(minutes=15)
            raw=raw.loc[~raw.index.isna() & raw.index.isin(g.index)]
            valid=np.isfinite(raw).all(axis=1)&raw[['open','high','low','close']].gt(0).all(axis=1)&raw.volume.ge(0)
            valid&=raw.high.ge(raw[['open','low','close']].max(axis=1))&raw.low.le(raw[['open','high','close']].min(axis=1))
            if not valid.any():continue
            native,bars=f.half_hours(raw.loc[valid],g)
            tapes[code]=SimpleNamespace(close=native.close.to_numpy(),volume=native.volume.to_numpy(),bars=bars)
        if cached is None:
            offers,waits=candidates(tapes,g,when,model) if len(tapes)>=13 else ([],{})
            _CACHE.clear();_CACHE[fingerprint]=(offers,waits)
        shared=dict(_v2_inputs=peers,cohort=[item['isin'] for item in instruments],
                    market_source='gettex' if official else 'gettex_chart')
        if not offers:return dict(shared,action='WAIT',reason=waits.get(info['isin'],'V2_PEER_OR_PRICE_COVERAGE_INSUFFICIENT'))
        best=offers[0]
        if best['isin']!=info['isin']:return dict(shared,action='WAIT',reason='V2_HIGHER_RANKED_STOCK',ranked_isin=best['isin'])
        return dict(best,**shared,action='BUY',reason='V2_30M_FROZEN_ENTRY',hold_minutes=f.EXITS[best['exit_rule']]*15)


def validate_context(db,engine,now):
    packet=engine.get('v2_30m') or {}
    if packet.get('action')!='BUY':return
    model=authority(db,pd.Timestamp(now,unit='s',tz='UTC'))
    if packet.get('artifact_sha256')!=model['artifact_sha256']:raise ValueError('V2_30M_MODEL_CHANGED')
    signal,price=pd.Timestamp(packet.get('signal_closed')),pd.Timestamp(packet.get('quote_end'))
    if (pd.isna(signal) or pd.isna(price) or signal.tzinfo is None or price.tzinfo is None
            or price-signal!=pd.Timedelta(minutes=30) or price.minute%30
            or price.second or price.microsecond):raise ValueError('V2_30M_SIGNAL_CLOCK_CHANGED')
    codes=[row[0] for row in db.execute('SELECT isin FROM instruments ORDER BY isin')]
    if codes!=packet.get('cohort'):raise ValueError('V2_30M_REGISTERED_UNIVERSE_CHANGED')


def decision(engine,timeframe):
    if timeframe!='30m':return dict(action='WAIT',reason='V2_30M_ONLY')
    return dict(engine.get('v2_30m') or dict(action='WAIT',reason='V2_30M_RESEARCH_ONLY'))


def screen(store,info,identity,now):
    """Cheap rejection only. A possible BUY must still run the full CORE path."""
    from paper_v2_quotes import fresh_quote
    quote={}
    try:
        quote['execution_quote']=fresh_quote(store,info,identity,now)
        packet=context(store,info,identity,now,quote['execution_quote'])
    except ValueError as error:
        packet=dict(action='BLOCKED',reason=str(error))
        quote['quote_blocker']=str(error)
    if packet.get('action')=='BUY':return None
    peers=packet.pop('_v2_inputs',{})
    return dict(isin=info['isin'],identity=identity,timeframe='30m',_v2_entry_screen=True,
        _v2_inputs=peers,execution_context=dict(trading_engine=dict(status='NOT_EVALUATED',
            signal='WAIT',timestamp=packet.get('signal_closed'),reason=packet['reason'],v2_30m=packet)),**quote)


def size(db,info,result,signal,now):
    from paper_account import ledger
    state=ledger(db,'V2')
    if any(float(p['quantity'])>0 for p in state['positions']):raise ValueError('V2_ONE_POSITION_PER_PORTFOLIO')
    if signal.get('model')!=MODEL:raise ValueError('V2_30M_MODEL_MISSING')
    model=authority(db,pd.Timestamp(now,unit='s',tz='UTC'))
    if signal.get('artifact_sha256')!=model['artifact_sha256']:raise ValueError('V2_30M_MODEL_CHANGED')
    # Only realized exits count towards the frozen daily profit lock.
    costs={};net=0.;today=pd.Timestamp(now,unit='s',tz='UTC').tz_convert('Europe/Berlin').date()
    for fill in state['fills']:
        code=fill['isin'];gross,fee=float(fill['gross']),float(fill['fee'])
        if fill['side']=='BUY':costs[code]=gross+fee
        elif code in costs:
            if pd.Timestamp(fill['at']).tz_convert('Europe/Berlin').date()==today:net+=gross-fee-costs[code]
            costs.pop(code,None)
    if net>0:raise ValueError('V2_DAILY_NET_PROFIT_LOCK')
    bar=result['execution_quote']['bars'][-1]
    quote=float(bar['close']);atr=signal['atr'];anchor=signal['anchor']
    if pd.Timestamp(bar['close_at'])!=pd.Timestamp(signal['quote_end']) or not anchor+.05*atr<=quote<=anchor+.65*atr:
        raise ValueError('V2_PRICE_OR_QUOTE_CHANGED')
    distance=max(1.15*atr,.0025*quote);stop,target=quote-distance,quote+3.75*distance
    qty,why=sized(float(state['cash']),quote*1.001,float(bar['volume']),stop,target)
    if not qty:raise ValueError(why)
    return dict(quantity=qty,mode=MODEL,stop=str(stop),target=str(target),
        estimated_risk=str((quote*1.001-stop*.999)*qty+4.),risk_is_estimate=True,
        execution_policy=COSTS.copy())


def protection_plan(result,signal,size,now):
    from paper_protection import STRATEGY_VERSION,POLICY as protection
    return dict(version=STRATEGY_VERSION,strategy_mode='V2',model=MODEL,
        identity=result['identity'].copy(),signal_tf='30m',quote_tf=result['execution_quote']['timeframe'],
        stop=size['stop'],target=size['target'],entry=str(signal['quote']*1.001),
        initial_quote=str(signal['quote']),atr=str(signal['atr']),exit_rule=signal['exit_rule'],
        planned_quantity=str(size['quantity']),armed_at=now,estimated_stop_risk=size['estimated_risk'],
        risk_is_estimate=True,trigger=protection['trigger'],fill=protection['fill'])


def protection_trigger(db,position,entry,plan,bar,price):
    from decimal import Decimal
    from paper_hourly import read
    from paper_protection import management_key
    if plan.get('exit_rule') not in f.EXITS:raise ValueError('V2_PROTECTION_RULE_CHANGED')
    last=read(db,management_key(entry['request_id']),{})
    stamp=pd.Timestamp(bar['close_at'])
    if last and stamp<pd.Timestamp(last['quote_at']):raise ValueError('PAPER_PROTECTION_OLDER_QUOTE')
    stop=Decimal(last.get('stop',plan['stop']))
    if last.get('quote_at')==bar['close_at']:stop=Decimal(last['prior_stop'])
    qty=Decimal(position['quantity']);atr=Decimal(plan['atr']);quote=Decimal(plan['initial_quote'])
    elapsed=(stamp-pd.Timestamp(entry['bar_close_at']))/pd.Timedelta(minutes=15)
    reason=('STOP_LOSS' if price<=stop else 'TAKE_PROFIT' if price>=Decimal(plan['target'])
        else 'V2_TIME_EXIT' if elapsed>=f.EXITS[plan['exit_rule']]
        else 'V2_FIRST_QUARTER_FAILED' if plan['exit_rule']=='step30' and elapsed>=1 and price<=quote-Decimal('.30')*atr
        else None)
    if reason:return reason,qty,False,None
    highest=max(price,Decimal(last.get('highest',quote)))
    breakeven=(Decimal(position['cost'])+Decimal('2'))/(qty*Decimal('.999'))
    updated=max(stop,breakeven,highest-Decimal('.9')*atr) if plan['exit_rule']=='lock75' and highest>=breakeven+Decimal('.30')*atr else stop
    management=(dict(quote_at=bar['close_at'],stop=str(updated),prior_stop=str(stop),highest=str(highest))
        if last.get('quote_at')!=bar['close_at'] and (updated>stop or highest>Decimal(last.get('highest',quote))) else None)
    return None,qty,False,management
