"""One deterministic analytical policy. No orders, network, SQLite or UI access.

Weights express evidence, not calibrated probabilities. Three trend votes have
equal weight; timing/structure on the selected chart supplies the entry trigger.
"""
import math

VERSION = '1'
WEIGHTS = {'trend': 2, 'momentum': 2, 'rsi': 1, 'structure': 3,
           'volume': 1, 'benchmark': 1}
RULES = {'buy_score': 7, 'sell_score': -7, 'volume_confirmation': 1.2,
         'near_level_atr': .5, 'break_margin_atr': .1, 'max_extension_atr': 3,
         'min_room_atr': 1, 'range_slope_atr': .05}


def profile(tf):
    """Explicit horizons, never switched because one source is unavailable."""
    if tf in ('1W', '1M'): return ('1D', '1W', '1M')
    if tf == '1D': return ('4H', '1D', '1W')
    if tf == '4H': return ('1H', '4H', '1D')
    return (tf, '4H', '1D')


def facts(closed, support, resistance, stop):
    a, b = closed.iloc[-1], closed.iloc[-2]
    atr = float(a.atr)
    scale = max(atr, float(a.close) * 1e-8)
    fast_slope = float(a.ema10-b.ema10)/scale
    slow_slope = float(a.ema30-b.ema30)/scale
    bullish = a.close > a.ema10 > a.ema30 and fast_slope > 0 and slow_slope > 0
    bearish = a.close < a.ema10 < a.ema30 and fast_slope < 0 and slow_slope < 0
    regime = ('BULLISH' if bullish else 'BEARISH' if bearish else
              'RANGE' if abs(slow_slope) < RULES['range_slope_atr'] else 'TRANSITION')
    hist = float(a.macd-a.macd_signal); old_hist = float(b.macd-b.macd_signal)
    avg = float(a.volume_mean20)
    reliable_volume = math.isfinite(avg) and avg > 0 and (closed.volume.tail(20) > 0).sum() >= 16
    rv = float(a.volume/avg) if reliable_volume else None
    margin = RULES['break_margin_atr']*scale
    # Levels exclude the current bar. Retests use a frozen earlier level, so
    # yesterday's breakout high cannot move its own retest target.
    prior = closed.iloc[:-2].tail(20)
    old_resistance = float(prior.high.max()); old_support = float(prior.low.min())
    breakout = a.close > resistance + margin and b.close <= resistance + margin
    breakdown = a.close < support - margin and b.close >= support - margin
    retest = b.close > old_resistance+margin and a.low <= old_resistance+.5*scale and a.close > old_resistance+margin
    bearish_retest = b.close < old_support-margin and a.high >= old_support-.5*scale and a.close < old_support-margin
    reclaim = b.close < b.ema10 and a.close > a.ema10 and a.close > b.high and a.close > a.open
    return dict(regime=regime, price=float(a.close), ema10=float(a.ema10), ema30=float(a.ema30),
        ema10_slope_atr=fast_slope, ema30_slope_atr=slow_slope,
        ema_cross=('UP' if b.ema10<=b.ema30 and a.ema10>a.ema30 else 'DOWN' if b.ema10>=b.ema30 and a.ema10<a.ema30 else 'NONE'),
        rsi=float(a.rsi), previous_rsi=float(b.rsi), macd=float(a.macd), macd_signal=float(a.macd_signal),
        histogram=hist, histogram_delta=hist-old_hist, atr=atr,
        relative_volume=rv, volume_reliable=bool(reliable_volume), support=support, resistance=resistance,
        breakout=bool(breakout), breakdown=bool(breakdown), retest=bool(retest), bearish_retest=bool(bearish_retest),
        reclaim=bool(reclaim), near_resistance=bool(0<=resistance-a.close<=RULES['near_level_atr']*scale),
        near_support=bool(0<=a.close-support<=RULES['near_level_atr']*scale),
        extended=bool(a.close-a.ema10>RULES['max_extension_atr']*scale),
        room=bool(resistance-a.close>=RULES['min_room_atr']*scale),
        closed_below_stop=bool(a.close<=stop), candle_up=bool(a.close>a.open),
        two_below_slow=bool(a.close<a.ema30 and b.close<b.ema30))


def evaluate(contexts, selected_tf, position=None, benchmark=None, required=None):
    from cp_controls import analysis_quality_allowed, risk_summary, RISK_CONTEXT
    required=tuple(required or profile(selected_tf)); benchmark=benchmark or {}
    bull=[]; bear=[]; blockers=[]; warnings=[]; score=0
    def vote(side, code, text, weight):
        nonlocal score
        (bull if side>0 else bear).append(dict(code=code, text=text, weight=weight))
        score += side*weight
    for tf in required:
        c=contexts.get(tf,{})
        if not c.get('sufficient_history') or not analysis_quality_allowed(c.get('data_quality',{}),c.get('accepted_risk')) or not c.get('ohlcv_valid') or not c.get('facts'):
            blockers.append(f'{tf}: DATA BLOCKED / '+c.get('data_quality',{}).get('state','UNVERIFIED')+'; '+c.get('reason','Нет проверенной истории'))
    selected=contexts.get(selected_tf,{})
    if selected_tf not in required and (not selected.get('sufficient_history') or not analysis_quality_allowed(selected.get('data_quality',{}),selected.get('accepted_risk'))):
        blockers.append(selected_tf+': DATA BLOCKED / нет проверенного timing')
    for tf in required:
        f=contexts.get(tf,{}).get('facts',{})
        if f.get('regime')=='BULLISH':vote(1,'trend:'+tf,tf+': цена > EMA10 > EMA30, обе EMA растут',WEIGHTS['trend'])
        if f.get('regime')=='BEARISH':vote(-1,'trend:'+tf,tf+': цена < EMA10 < EMA30, обе EMA снижаются',WEIGHTS['trend'])
    f=selected.get('facts',{}); higher=contexts.get(required[-1],{}).get('facts',{})
    medium=contexts.get(required[1],{}).get('facts',{})
    if f:
        if f['histogram']>0 and f['histogram_delta']>0:vote(1,'macd','MACD выше signal, histogram усиливается',WEIGHTS['momentum'])
        elif f['histogram']<0 and f['histogram_delta']<0:vote(-1,'macd','MACD ниже signal, отрицательный histogram усиливается',WEIGHTS['momentum'])
        elif f['histogram_delta']<0:vote(-1,'macd_weakening','Импульс MACD ослабевает',1)
        elif f['histogram_delta']>0:vote(1,'macd_recovering','Импульс MACD восстанавливается',1)
        if 50<=f['rsi']<=70:vote(1,'rsi','RSI поддерживает положительный импульс',WEIGHTS['rsi'])
        elif f['rsi']<45:vote(-1,'rsi','RSI ниже 45; перепроданность сама не подтверждает разворот',WEIGHTS['rsi'])
        elif f['rsi']>70:warnings.append('RSI > 70: сильный импульс, сам по себе не SELL')
        if f['breakout'] or f['retest'] or f['reclaim']:vote(1,'structure','Подтверждённая закрытием структура: '+('breakout' if f['breakout'] else 'retest' if f['retest'] else 'reclaim'),WEIGHTS['structure'])
        if f['breakdown'] or f['bearish_retest']:vote(-1,'structure','Слом поддержки / bearish retest на закрытии',WEIGHTS['structure'])
        if f['near_resistance']:vote(-1,'resistance','Сопротивление ближе 0.5 ATR',WEIGHTS['structure'])
        if not f['volume_reliable']:warnings.append('Объём отсутствует или ненадёжен: не используется; BUY не подтверждён')
        elif f['relative_volume']>=RULES['volume_confirmation']:
            vote(1 if f['candle_up'] else -1,'volume',f"Относительный объём {f['relative_volume']:.2f} × предыдущих 20 свечей",WEIGHTS['volume'])
    valid_benchmark=benchmark.get('quality')=='READY' and benchmark.get('state') in ('OUTPERFORMING','NEUTRAL','UNDERPERFORMING')
    if valid_benchmark and benchmark['state']!='NEUTRAL':
        vote(1 if benchmark['state']=='OUTPERFORMING' else -1,'benchmark',str(benchmark.get('benchmark_name','Benchmark'))+': '+benchmark['state'],WEIGHTS['benchmark'])
    if not valid_benchmark:warnings.append('Benchmark недоступен/не проверен: исключён из оценки, без подмены')
    try:
        qty=float((position or {}).get('quantity'))
        position_state='HELD' if math.isfinite(qty) and qty>0 else 'NONE' if qty==0 else 'UNKNOWN'
    except (TypeError,ValueError):position_state='UNKNOWN'
    signal='WAIT';entry_blockers=[]
    if f:
        if f['regime']=='BEARISH':entry_blockers.append('Timing TF сохраняет bearish структуру')
        if higher.get('regime')!='BULLISH' or medium.get('regime')!='BULLISH':entry_blockers.append('Средний и старший TF не согласованы bullish')
        if not (f['breakout'] or f['retest'] or f['reclaim']):entry_blockers.append('Нет подтверждённого breakout/retest/reclaim')
        if not (f['histogram']>0 and f['histogram_delta']>0 and f['rsi']>=50):entry_blockers.append('Momentum входа не подтверждён')
        if not f['volume_reliable'] or f['relative_volume']<RULES['volume_confirmation']:entry_blockers.append('Вход не подтверждён объёмом ≥ 1.2 × предыдущих 20 свечей')
        if f['extended']:entry_blockers.append('Цена дальше 3 ATR от EMA10')
        if not (f['breakout'] or f['retest'] or f['room']):entry_blockers.append('До сопротивления меньше 1 ATR')
        if not blockers:
            sell=(score<=RULES['sell_score'] and f['two_below_slow'] and f['histogram']<0) or ((f['breakdown'] or f['bearish_retest'] or f['closed_below_stop']) and f['histogram']<0 and f['rsi']<45 and f['volume_reliable'] and f['relative_volume']>=RULES['volume_confirmation'])
            if sell:signal='SELL'
            elif not entry_blockers and score>=RULES['buy_score']:signal='BUY'
            elif position_state=='HELD' and f['regime'] in ('BULLISH','RANGE') and higher.get('regime')!='BEARISH' and medium.get('regime')!='BEARISH' and not f['breakdown'] and not f['closed_below_stop'] and score>=0:signal='HOLD'
    maximum=3*WEIGHTS['trend']+sum(WEIGHTS[k] for k in ('momentum','rsi','structure','volume','benchmark'))
    strength=round(min(100,100*abs(score)/maximum)) if not blockers else 0
    accepted={tf:risk_summary(contexts[tf]['accepted_risk']) for tf in dict.fromkeys((*required,selected_tf)) if contexts.get(tf,{}).get('accepted_risk')}
    if accepted:warnings.append('Принят риск оценочных Close: EMA/RSI/MACD используют средние соседей; ATR, объём, уровни и цена исполнения — реальные. Архив DQ не исправлен.')
    return dict(signal=signal,decision=signal,score=score,confidence=strength,
        calculation_context=RISK_CONTEXT if accepted else 'standard',accepted_risks=accepted,
        confidence_meaning='Сила направленных подтверждений, не вероятность прибыли',
        bullish_reasons=bull,bearish_reasons=bear,blockers=blockers,
        entry_blockers=entry_blockers,warnings=warnings,position_state=position_state,
        timeframe_context={tf:{k:contexts.get(tf,{}).get(k) for k in ('signal_at','facts','data_quality','closed_bar_count')} | ({'accepted_risk':accepted[tf]} if tf in accepted else {}) for tf in dict.fromkeys((*required,selected_tf))},
        benchmark_context=benchmark,timestamp=selected.get('signal_at'),rules_version=VERSION,
        status='DATA BLOCKED' if blockers else 'READY',
        reason='; '.join(blockers or ([r['text'] for r in (bear if signal=='SELL' else bull)][:3]+entry_blockers[:2]+warnings[:1])) or 'Нет достаточных направленных подтверждений')
