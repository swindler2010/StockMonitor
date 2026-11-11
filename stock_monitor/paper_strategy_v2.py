"""Pure shadow policy and two independent simulated books. No execution API.

V1 signals come from the unchanged CORE. All fills use the same next closed
execution-bar model, spread and fees. Stops take precedence when OHLC cannot
establish the order of intrabar touches. No synthetic market data is produced.
"""
import copy,math
from decimal import Decimal,ROUND_HALF_UP

VERSION='hourly_v2_shadow_1'
POLICY=dict(standard_risk=.01,early_risk=.005,total_risk=.02,allocation=.45,
            atr_multiple=2.,minimum_rr=2.,partial_fraction=.5,fee=1.,spread_bps=10.)

def money(x):return float(Decimal(str(x)).quantize(Decimal('.01'),rounding=ROUND_HALF_UP))
def finite(x):return isinstance(x,(int,float)) and math.isfinite(x)
def book():
    return dict(initial=10000.,cash=10000.,positions={},marks={},pending={},last_quote={},
                last_signal={},last_entry_signal={},peak=10000.,equity=10000.,max_drawdown=0.,closed=0,
                wins=0,losses=0,flat=0,gross_profit=0.,gross_loss=0.,fees=0.,signals={},rejections={},entry_refusals={})

def seed_error(context,name):
    proof=context.get('scope_initialization') or {}
    if not proof.get('stable'):raise ValueError('V2_SEED_PROOF_MISSING')
    if proof.get('mode')=='OWNER_ACCEPTED_DEMO_SENSITIVITY':
        value=proof.get('max_absolute_deltas',{}).get(name)
        if not finite(value) or value<0:raise ValueError('V2_SEED_BOUND_MISSING')
        return value
    # Existing producer proves exact terminal indicators AND boolean facts.
    if proof.get('basis') not in ('Exact terminal indicators and CORE facts with first in-scope seed omitted; no older data',
                                 'Exact terminal indicators and CORE facts with first approved warmup seed omitted'):
        raise ValueError('V2_SEED_PROOF_UNKNOWN')
    return 0.

def bounds(context,key):
    f=context['facts'];value=float(f[key]);atr=float(f['atr'])
    if key in ('histogram','histogram_delta'):
        error=(seed_error(context,'macd')+seed_error(context,'macd_signal'))*(2 if key.endswith('delta') else 1)
    elif key=='ema10_slope_atr':
        ae=seed_error(context,'atr')
        if atr<=ae:raise ValueError('V2_ATR_UNCERTAIN')
        error=(2*seed_error(context,'ema10')+abs(value)*ae)/(atr-ae)
    elif key in ('rsi','previous_rsi'):error=seed_error(context,'rsi')
    else:error=seed_error(context,key)
    return value-error,value+error

def above(context,key,threshold):
    lo,hi=bounds(context,key)
    if lo>threshold:return True
    if hi<=threshold:return False
    raise ValueError('V2_SEED_THRESHOLD_UNSTABLE:'+key)

def proposal(engine):
    """Only validated closed 1H/4H/1D inputs; returns no trading permission."""
    if engine.get('status')!='READY':return dict(action='BLOCKED',reason='CORE_DATA_BLOCKED')
    if engine.get('demo_initialization') and not engine['demo_initialization'].get('consistent'):
        return dict(action='BLOCKED',reason='CORE_SEED_UNSTABLE')
    try:
        contexts=engine['timeframe_context'];h,m,d=(contexts[t] for t in ('1H','4H','1D'))
        f=h['facts'];price=float(f['price']);atr_hi=bounds(h,'atr')[1];support=float(f['support'])
        if not all(math.isfinite(v) and v>0 for v in (price,atr_hi,support)):raise ValueError('V2_RISK_INPUT_INVALID')
        # Existing accepted seed model bounds risk levels to one cent. Use the
        # adverse end of that bound for new V2 sizing/R:R decisions.
        quantum=.01 if h['scope_initialization'].get('mode')=='OWNER_ACCEPTED_DEMO_SENSITIVITY' else 0.
        stop=min((support-quantum)*.995,price-POLICY['atr_multiple']*atr_hi)
        if stop<=0 or stop>=price:raise ValueError('V2_STOP_INVALID')
        targets=[float(c['facts']['resistance'])-(.01 if c['scope_initialization'].get('mode')=='OWNER_ACCEPTED_DEMO_SENSITIVITY' else 0.)
                 for c in (h,m,d) if finite(c['facts'].get('resistance')) and c['facts']['resistance']>price]
        target=min(targets) if targets else None
        quality='UNAVAILABLE' if not f.get('volume_reliable') else 'ABOVE_AVERAGE' if f['relative_volume']>=1 else 'BELOW_AVERAGE'
        answer=dict(action='WAIT',reason='NO_ENTRY',stop=stop,target=target,atr=atr_hi,volume_quality=quality,
                    risk_fraction=POLICY['standard_risk'],seed_model='Existing terminal bounds; uncertain new thresholds blocked')
        if engine.get('signal')=='BUY':return dict(answer,action='BUY',mode='STANDARD',reason='V1_CONSERVATIVE_BUY')
        if engine.get('signal')=='SELL':return dict(answer,action='SELL',reason='V1_CONFIRMED_REVERSAL')
        # Boolean structure/regime flags are invariant in the existing seed proof.
        reversal=bool(f.get('breakdown') or f.get('bearish_retest')) and not above(h,'histogram',0) and not above(h,'rsi',45)
        if reversal:return dict(answer,action='SELL',reason='CLOSED_1H_STRUCTURE_REVERSAL')
        reasons=[]
        if not (f.get('breakout') or f.get('reclaim')):reasons.append('NO_CLOSED_STRUCTURE_TRIGGER')
        if not (above(h,'histogram',0) and above(h,'histogram_delta',0) and above(h,'rsi',50)):reasons.append('MOMENTUM_NOT_CONFIRMED')
        improving=above(m,'histogram_delta',0) and above(m,'ema10_slope_atr',0)
        if not improving:reasons.append('4H_NOT_IMPROVING')
        daily_bear=(d['facts'].get('regime')=='BEARISH' and not above(d,'histogram',0)
                    and not above(d,'histogram_delta',0) and not above(d,'rsi',45))
        if daily_bear:reasons.append('1D_STRONG_BEARISH_IMPULSE')
        if f.get('extended'):reasons.append('EXTENDED_OVER_3_ATR')
        if target is None or (target-price)/(price-stop)<POLICY['minimum_rr']:reasons.append('STRUCTURAL_RR_BELOW_2')
        return dict(answer,action='WAIT' if reasons else 'BUY',mode='EARLY',risk_fraction=POLICY['early_risk'],
                    reason=';'.join(reasons) if reasons else 'EARLY_CLOSED_1H_CONFIRMED')
    except (KeyError,TypeError,ValueError,ZeroDivisionError) as error:
        return dict(action='BLOCKED',reason=str(error))

def open_risk(state):
    return sum(max(0.,p['entry']-p['stop']*(1-POLICY['spread_bps']/20000))*p['qty']+POLICY['fee'] for p in state['positions'].values())

def size(state,reference,decision,variant):
    fee=POLICY['fee'];half=POLICY['spread_bps']/20000;entry=reference*(1+half)
    if not finite(entry) or entry<=0:raise ValueError('PRICE_INVALID')
    allocation=money(state['cash']*POLICY['allocation'])
    if variant=='V1':
        # Existing cash_volatility_v1 formula, independently tested against
        # paper_hourly.order_size. No new stop/order is added to V1.
        risk=money(state['cash']*.01)
        distance=max(2*decision['atr'],reference-decision['support']*.995)+2*reference*half
    else:
        stop=decision['stop']
        if not 0<stop<reference:raise ValueError('STOP_NOT_BELOW_FILL')
        distance=entry-stop*(1-half)
        risk=min(money(state['equity']*decision['risk_fraction']),money(state['equity']*POLICY['total_risk'])-open_risk(state))
        if decision.get('mode')=='EARLY':
            target=decision.get('target')
            if not target or (target*(1-half)-entry)/distance<2:raise ValueError('FILL_RR_BELOW_2_AFTER_SPREAD')
    qty=min(int((min(state['cash'],allocation)-fee)//entry),int((risk-2*fee)//distance)) if distance>0 else 0
    while qty>0 and (money(entry*qty)+fee>min(state['cash'],allocation) or distance*qty+2*fee>risk):qty-=1
    if qty<1:raise ValueError('RISK_OR_CASH_BELOW_ONE_SHARE')
    # Fees per share can reduce a seemingly adequate gross 2:1 ratio.
    if variant=='V2' and decision.get('mode')=='EARLY' and ((decision['target']*(1-half)-entry)*qty-2*fee)/(distance*qty+2*fee)<2:
        raise ValueError('FILL_RR_BELOW_2_AFTER_COSTS')
    return qty,entry,dict(risk_budget=risk,estimated_loss=distance*qty+2*fee,allocation=allocation)

def mark(state,isin,reference,at):
    state['marks'][isin]={'price':reference,'at':at}
    state['equity']=money(state['cash']+sum(p['qty']*state['marks'][i]['price'] for i,p in state['positions'].items()))
    state['peak']=max(state['peak'],state['equity']);state['max_drawdown']=max(state['max_drawdown'],(state['peak']-state['equity'])/state['peak'])

def sell(state,isin,qty,reference,at,reason,events):
    p=state['positions'][isin];fee=POLICY['fee'];price=reference*(1-POLICY['spread_bps']/20000)
    allocated=p['cost']*qty/p['qty'];net=money(price*qty)-fee;profit=net-allocated
    state['cash']=money(state['cash']+net);state['fees']+=fee;p['cost']-=allocated;p['qty']-=qty;p['realized']+=profit
    events.append(dict(kind='FILL',side='SELL',isin=isin,at=at,quantity=qty,price=price,commission=fee,net_pnl=money(profit),reason=reason))
    if not p['qty']:
        pnl=money(p['realized']);state['closed']+=1
        state['wins' if pnl>0 else 'losses' if pnl<0 else 'flat']+=1
        state['gross_profit' if pnl>0 else 'gross_loss']+=abs(pnl)
        events.append(dict(kind='CLOSED_TRADE',isin=isin,at=at,net_pnl=pnl));del state['positions'][isin]

def advance(state,variant,event,*,smart_exit=True):
    """One validated observation shared by V1/V2. Mutates only shadow state.

    The signal is considered after executing this bar: entries/reversals cannot
    use the signal candle's own price/high/low. New stops apply next bar only.
    """
    isin=event['isin'];q=event['quote'];at=q['close_at'];events=[]
    if state['last_quote'].get(isin, '')>=at:return events
    if isin in state['positions'] and state['last_quote'].get(isin,'')<q['open_at']:
        import pandas as pd
        from timeframes import schedule
        left=pd.Timestamp(state['last_quote'][isin]);right=pd.Timestamp(q['open_at'])
        sessions=schedule(event['identity'],left.tz_convert('Europe/Berlin').tz_localize(None).normalize(),right.tz_convert('Europe/Berlin').tz_localize(None).normalize())
        if any(max(left,r.open)<min(right,r.close) for _,r in sessions.iterrows()):
            raise ValueError('SHADOW_MISSED_EXECUTION_BARS_REPLAY_REQUIRED')
    if not q.get('closed') or q['open_at']>=at or at>event['available_at']:raise ValueError('UNCLOSED_EXECUTION_BAR')
    vals=[q.get(k) for k in ('open','high','low','close','volume')]
    if (not all(finite(v) and v>0 for v in vals) or q['high']<max(q['open'],q['close'],q['low'])
        or q['low']>min(q['open'],q['close'])):raise ValueError('BAD_EXECUTION_OHLCV')
    mark(state,isin,q['close'],at)
    pending=state['pending'].get(isin);p=state['positions'].get(isin)
    if pending and event.get('revoke_pending'):
        state['pending'].pop(isin,None);pending=None
        events.append(dict(kind='REJECT',isin=isin,at=at,reason='PRIOR_CLOSED_INPUTS_CHANGED'))
    fingerprint=event.get('decision_fingerprints',{}).get(variant,event['decision_fingerprint'])
    if p and variant=='V2' and smart_exit:
        if q['low']<=p['stop']:
            sell(state,isin,p['qty'],min(q['open'],p['stop']),at,'STOP_OR_GAP_STOP',events);p=None
        elif not p['partial'] and q['high']>=p['entry']+2*p['r']:
            if p['qty']>=2:
                sell(state,isin,max(1,int(p['qty']*POLICY['partial_fraction'])),p['entry']+2*p['r'],at,'PARTIAL_2R',events)
            else:events.append(dict(kind='MANAGEMENT',isin=isin,at=at,reason='ONE_SHARE_NO_PARTIAL_TRAILING_ARMED'))
            p['partial']=True
        if p and q['high']>=p['entry']+p['r']:
            # Remaining basis includes its allocated entry fee; future exit
            # spread + one commission are included. Applies only next bar.
            breakeven=(p['cost']/p['qty']+POLICY['fee']/p['qty'])/(1-POLICY['spread_bps']/20000)
            p['stop']=max(p['stop'],breakeven)
    if pending and q['open_at']>=pending['available_at'] and at>pending['signal_close']:
        state['pending'].pop(isin,None)
        if event['signal_at']!=pending['signal_at'] or fingerprint!=pending['fingerprint']:
            events.append(dict(kind='REJECT',at=at,isin=isin,reason='SIGNAL_OR_INPUTS_CHANGED'))
        elif pending['decision']['action']=='SELL':
            if p:sell(state,isin,p['qty'],q['close'],at,'CLOSED_1H_REVERSAL',events);p=None
        elif not p and not events:
            decision=pending['decision']
            try:
                if state['last_entry_signal'].get(isin)==pending['signal_at']:raise ValueError('ONE_ENTRY_PER_SIGNAL_BAR')
                from datetime import datetime
                if any(not 0<=(datetime.fromisoformat(event['available_at'])-datetime.fromisoformat(state['marks'][i]['at'])).total_seconds()<=900
                       for i in state['positions']):raise ValueError('PORTFOLIO_MARK_STALE')
                qty,price,risk=size(state,q['close'],decision,variant)
                fee=POLICY['fee'];cost=money(price*qty)+fee;state['cash']=money(state['cash']-cost);state['fees']+=fee
                stop=decision['stop'];state['positions'][isin]=dict(qty=qty,entry=price,cost=cost,stop=stop,initial_stop=stop,
                    r=price-stop,partial=False,realized=0.,entered_at=at,mode=decision.get('mode','STANDARD'))
                state['last_entry_signal'][isin]=pending['signal_at']
                events.append(dict(kind='FILL',side='BUY',isin=isin,at=at,quantity=qty,price=price,commission=fee,reason=decision['reason'],risk=risk))
            except ValueError as error:events.append(dict(kind='REJECT',isin=isin,at=at,reason=str(error)))
    decision=event['v1'] if variant=='V1' else event['v2']
    signature=event['signal_at']+':'+fingerprint
    if signature!=state['last_signal'].get(isin):
        state['last_signal'][isin]=signature;state['signals'][decision['action']]=state['signals'].get(decision['action'],0)+1
        events.append(dict(kind='SIGNAL',isin=isin,at=at,signal_at=event['signal_at'],decision=decision))
        if decision['action'] in ('WAIT','BLOCKED'):
            counts=state.setdefault('entry_refusals',{});reason=decision['reason'];counts[reason]=counts.get(reason,0)+1
        if decision['action'] in ('BUY','SELL'):
            state['pending'][isin]=dict(decision=copy.deepcopy(decision),signal_at=event['signal_at'],signal_close=event['signal_close'],
                available_at=event['available_at'],fingerprint=fingerprint)
    p=state['positions'].get(isin)
    if variant=='V2' and smart_exit and p and p['partial'] and decision.get('atr'):
        p['stop']=max(p['stop'],event['hourly_price']-2*decision['atr'])
    for e in events:
        e['observed_at']=event['available_at'];e['bar_close_at']=at
        if e['kind']=='REJECT':state['rejections'][e['reason']]=state['rejections'].get(e['reason'],0)+1
    state['last_quote'][isin]=at;mark(state,isin,q['close'],at)
    return events

def metrics(state):
    losses=state['gross_loss'];closed=state['closed']
    return dict(initial_equity=state['initial'],equity=state['equity'],cash=state['cash'],net_profit=money(state['equity']-state['initial']),
        commissions=state['fees'],max_drawdown_pct=100*state['max_drawdown'],closed_trades=closed,wins=state['wins'],losses=state['losses'],
        profit_factor=state['gross_profit']/losses if losses else None,
        profit_factor_status='DEFINED' if losses else 'NO_LOSING_CLOSED_TRADES',
        expectancy=(state['gross_profit']-losses)/closed if closed else None,open_positions=len(state['positions']),
        marks={i:state['marks'].get(i) for i in state['positions']},
        signals=state['signals'],rejections=state['rejections'],entry_refusals=state.get('entry_refusals',{}))
