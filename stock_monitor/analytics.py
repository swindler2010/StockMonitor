"""Deterministic signals; also consumed by diagnostics and future clients."""
import math
import copy
import pandas as pd
import numpy as np

TIMEFRAMES={'5m':'5min','10m':'10min','15m':'15min','30m':'30min','1H':'1h','2H':'2h','3H':'3h','4H':'4h','1D':'1D','1W':'W-FRI','1M':'MS'}
RANK={'1m':1,'5m':5,'10m':10,'15m':15,'30m':30,'1H':60,'2H':120,'3H':180,'4H':240,'1D':1440,'1W':10080,'1M':43200}

def bar_ends(frame):
    """Conservative normal-session endpoints; no invented holiday candles."""
    tf=frame.attrs['timeframe'];index=frame.index
    from timeframes import endpoints
    if frame.attrs.get('venue') and frame.attrs.get('timezone'):
        try:return endpoints(frame)
        except ValueError:pass  # Unknown calendar stays quality INVALID; endpoints remain conservative.
    if index.tz is None:index=index.tz_localize(frame.attrs.get('timezone') or 'Europe/Berlin',ambiguous='NaT',nonexistent='NaT')
    ends=index+pd.offsets.MonthBegin(1) if tf=='1M' else index+pd.Timedelta(days=1) if tf=='1W' else index+pd.Timedelta(minutes=RANK[tf])
    close=frame.attrs.get('session_close')
    if tf in ('5m','10m','15m','30m','1H','2H','3H','4H') and close:
        import re
        if not re.fullmatch(r'(?:[01]\d|2[0-3]):[0-5]\d',close):raise ValueError('Invalid session closing time')
        hour,minute=map(int,close.split(':'))
        scheduled=index.normalize()+pd.Timedelta(hours=hour,minutes=minute)
        # Off-session observations are retained and use their full time bucket.
        ends=pd.DatetimeIndex([min(end,limit) if start<limit else end for start,end,limit in zip(index,ends,scheduled)])
    return ends.tz_convert('UTC')

def resample(frame, base, target, now=None):
    if RANK[target]<RANK[base]:return pd.DataFrame()
    if target==base:
        out=frame.copy();out.attrs['timeframe']=target
        return out
    if frame.empty:return frame.copy()
    from timeframes import MINUTES,aggregate
    if target in MINUTES and base in MINUTES:return aggregate(frame,base,target,now)
    agg={'open':'first','high':'max','low':'min','close':'last','volume':'sum'}
    if 'source' in frame:agg['source']=lambda x:'+'.join(sorted(set(x.dropna())))
    options={}
    anchor=frame.attrs.get('session_open')
    if target in ('30m','1H','4H') and anchor:
        import re
        if not re.fullmatch(r'(?:[01]\d|2[0-3]):[0-5]\d',anchor):raise ValueError('Invalid session opening time')
        hours,minutes=map(int,anchor.split(':'))
        options={'origin':'start_day','offset':pd.Timedelta(hours=hours,minutes=minutes)}
    if target=='1D' and base in MINUTES:
        from timeframes import daily_ohlcv
        out=daily_ohlcv(frame)
    elif options and not frame.empty:
        # Restart the anchor each local trading day so DST cannot shift buckets.
        out=pd.concat([part.resample(TIMEFRAMES[target],**options).agg(agg) for _,part in frame.groupby(frame.index.normalize())])
    else:out=frame.resample(TIMEFRAMES[target],**options).agg(agg)
    out=out.dropna(subset=['open','high','low','close'])
    out.attrs=frame.attrs.copy()
    out.attrs.pop('bar_ends',None)
    out.attrs['timeframe']=target
    if target in ('30m','1H','4H'):out.attrs['alignment']='session_open '+anchor if anchor else 'calendar grid; session anchor unknown'
    if (base=='1D' or base in MINUTES) and target in ('1D','1W','1M'):
        from timeframes import daily_provenance
        daily_provenance(frame,out,target,now,base=base)
    return out

def indicators(frame):
    x=_indicator_values(frame)
    x.attrs=copy.deepcopy(frame.attrs)
    return x

def _indicator_values(frame):
    # Numeric pandas operations must not deep-copy the full DQ archive on every
    # column/rolling operation. Restore independent metadata exactly once.
    x=pd.DataFrame(frame,copy=True)
    from cp_controls import analytical_close_series
    prices=pd.Series(analytical_close_series(frame,values_only=True),copy=False)
    prices.attrs={}
    x['ema10']=prices.ewm(span=10,adjust=False,min_periods=10).mean(); x['ema30']=prices.ewm(span=30,adjust=False,min_periods=30).mean()
    delta=prices.diff(); up=delta.clip(lower=0).ewm(alpha=1/14,adjust=False,min_periods=14).mean()
    down=(-delta.clip(upper=0)).ewm(alpha=1/14,adjust=False,min_periods=14).mean()
    x['rsi']=100-100/(1+up/down.replace(0,np.nan))
    x.loc[((down==0)&(up>0)).reindex(x.index,fill_value=False),'rsi']=100; x.loc[((up==0)&(down==0)).reindex(x.index,fill_value=False),'rsi']=50
    tr=pd.concat([x.high-x.low,(x.high-x.close.shift()).abs(),(x.low-x.close.shift()).abs()],axis=1).max(axis=1)
    x['atr']=tr.ewm(alpha=1/14,adjust=False,min_periods=14).mean()
    x['volume_mean20']=x.volume.shift(1).rolling(20,min_periods=20).mean()
    macd=prices.ewm(span=12,adjust=False,min_periods=12).mean()-prices.ewm(span=26,adjust=False,min_periods=26).mean()
    x['macd']=macd
    x['macd_signal']=macd.ewm(span=9,adjust=False,min_periods=9).mean()
    return x


def _analyse_features(frame, info=None, now=None):
    enriched,result=_analyse_values(frame,info,now)
    enriched.attrs=copy.deepcopy(frame.attrs)
    return enriched,result


def _analyse_values(frame, info=None, now=None):
    info=info or {}
    numeric=pd.DataFrame(frame,copy=False)
    # Arithmetic consumes values only. Pandas otherwise deep-copies the full
    # provenance/DQ tree for every column selection, clip and comparison.
    # _analyse_features restores independent metadata at the public boundary;
    # all safety decisions below still read the original frame.attrs.
    x=_indicator_values(frame)
    # Display may include forming candles; confirmed indicators may not.
    tf=frame.attrs.get('timeframe')
    now=pd.Timestamp.now(tz='UTC') if now is None else now
    if tf:
        closed=x.loc[(bar_ends(frame)<=now)&(x.bar_complete if 'bar_complete' in x else True)]
    else:
        # Missing timeframe metadata cannot prove that the latest bar is closed.
        closed=x.iloc[:-1]
    actual_closed=numeric.loc[closed.index].copy();actual_closed.attrs=frame.attrs
    closed=_indicator_values(actual_closed)
    for column in ('ema10','ema30','rsi','atr','volume_mean20','macd','macd_signal'):
        x.loc[closed.index,column]=closed[column]
    count=len(closed)
    sources=set(numeric['source'].astype(str)) if 'source' in frame else {'unknown'}
    mixed=len(sources)!=1 or any('+' in source for source in sources) or 'unknown' in sources
    result={'decision':'WAIT','reason':'Нужно не менее 35 закрытых свечей для EMA30, RSI, MACD и ATR.',
            'support':None,'resistance':None,'stop':None,'add':None,'sufficient_history':False,
            'bar_count':len(x),'closed_bar_count':count,'minimum_bars':35,'sources':sorted(sources),
            'excluded_open_bars':len(x)-count,'signal_at':str(closed.index[-1]) if count else None,
            'add_checks':{},'execution':'analytical scenario, not a trade'}
    values=closed[['open','high','low','close','volume']].to_numpy(dtype=float)
    valid=(np.isfinite(values).all() and (closed[['open','high','low','close']]>0).all().all()
           and (closed.volume>=0).all() and (closed.high>=closed[['open','close','low']].max(axis=1)).all()
           and (closed.low<=closed[['open','close','high']].min(axis=1)).all()
           and frame.index.is_monotonic_increasing and not frame.index.has_duplicates and not frame.index.hasnans)
    result['ohlcv_valid']=bool(valid)
    quality=frame.attrs.get('data_quality',{})
    result['data_quality']=quality
    from cp_controls import analysis_quality_allowed
    risk=frame.attrs.get('accepted_risk')
    if risk:
        from cp_controls import analytical_close_series
        result['accepted_risk']={**risk,'price_estimates_used':len(analytical_close_series(actual_closed,values_only=True))-len(actual_closed)}
    if quality and not analysis_quality_allowed(quality,risk):
        result['reason']='Качество данных: '+quality.get('state','INVALID')+'; сигналы отключены до проверки/восстановления.'
        if quality.get('stale_count'):result['reason']+=' Устаревший хвост: '+str(quality['stale_count'])+' ожидаемых интервалов; принятие исторических пропусков не обновляет цену.'
        if quality.get('state')=='INVALID':
            result['ohlcv_valid']=False
            x.loc[:,['ema10','ema30','rsi','atr','volume_mean20','macd','macd_signal']]=np.nan
        return x,result
    if not valid:
        result['reason']='В истории есть некорректные OHLCV или даты: индикаторы и сигналы отключены до исправления источника.'
        x.loc[:,['ema10','ema30','rsi','atr','volume_mean20','macd','macd_signal']]=np.nan
        return x,result
    if count<35 or mixed:
        if mixed:result['reason']='Источник неизвестен или смешан: торговая аналитика отключена.'
        return x,result
    last=closed.iloc[-1];prev=closed.iloc[-2];recent=closed.iloc[:-1].tail(20)
    if not all(math.isfinite(last[k]) for k in ('ema10','ema30','rsi','macd','macd_signal','atr','volume_mean20')):
        return x,result
    support=float(recent.low.min());resistance=float(recent.high.max())
    custom=info.get('levels') or {}
    support=float(custom.get('support') or support);resistance=float(custom.get('resistance') or resistance)
    stop=float(custom.get('stop') or max(.00001,min(support*.995,last.close-2*last.atr)))
    histogram=last.macd-last.macd_signal
    from trading_engine import facts
    result.update(support=support,resistance=resistance,stop=stop,sufficient_history=True,
                  reason='Индикаторы рассчитаны по закрытым свечам; допуск сигнала проверяет CORE.',
                  facts=facts(closed,support,resistance,stop))
    movement=(float(last.close)/float(prev.close)-1)*100 if prev.close>0 else None
    relative_volume=float(last.volume/last.volume_mean20) if last.volume_mean20>0 else None
    result['current_context']={
        'closed_at':str(closed.index[-1]),'change_pct':movement,'relative_volume':relative_volume,
        'rsi':float(last.rsi),'macd_histogram':float(histogram),
        'text':('Закрытая свеча '+('растущая' if last.close>last.open else 'снижающаяся' if last.close<last.open else 'без изменения')+
                (f'; к предыдущему закрытию {movement:+.2f}%' if movement is not None else '')+
                (f'; объём {relative_volume:.2f} среднего за 20 свечей' if relative_volume is not None else '; средний объём недоступен')+
                f'. RSI {last.rsi:.1f}; импульс MACD '+('положительный' if histogram>0 else 'отрицательный' if histogram<0 else 'нулевой')+'.')}
    return x,result


def _demo_eur_seed_equivalent(left, right):
    """Owner's demo seed precision, never an OHLCV/identity comparison.

    Preserve existing cent agreement and additionally allow <=0.001 EUR
    across a rounding boundary. Actual CORE predicates are still compared.
    Decimal text keeps the explicitly approved absolute boundary inclusive.
    """
    from decimal import Decimal
    from paper_account import money
    a,b=Decimal(str(left)),Decimal(str(right))
    return bool(a.is_finite() and b.is_finite() and
                (abs(a-b)<=Decimal('0.001') or money(a)==money(b)))


def _demo_seed_trials(closed, info, now, enriched, result):
    """Owner-authorized sensitivity model, not proof of missing market prices.

    Test every seed omission up to 10% (at most 20), retain >=35 real bars.
    Currency indicators/risk levels keep approved cent precision, with the
    owner's additional <=0.001 EUR noise tolerance across rounding boundaries.
    RSI keeps hundredths; volume stays exact. CORE decisions remain separate.
    No change to the strict full/bounded contexts without the explicit receipt.
    """
    from cp_controls import initialization_for,analysis_quality_allowed
    from paper_account import money
    risk=closed.attrs.get('accepted_risk',{});policy=initialization_for(closed)
    if policy is None:return None
    if not analysis_quality_allowed(result.get('data_quality',{}),risk):return None
    limit=min(20,len(closed)//10,len(closed)-35)
    if limit<1:return None
    columns=['ema10','ema30','rsi','macd','macd_signal','atr','volume_mean20']
    terminal=pd.DataFrame(enriched,copy=False).loc[closed.index,columns].tail(2)
    trials=[];deltas={k:0. for k in columns}
    for omit in range(1,limit+1):
        reference,trial=_analyse_features(closed.iloc[omit:].copy(),info,now)
        if not trial.get('sufficient_history') or not trial.get('ohlcv_valid'):return None
        other=pd.DataFrame(reference,copy=False)[columns].tail(2)
        if not terminal.index.equals(other.index):return None
        if not all(math.isfinite(float(v)) for v in other.to_numpy().flat):return None
        for name in columns:
            deltas[name]=max(deltas[name],float((terminal[name]-other[name]).abs().max()))
            if name=='volume_mean20':
                if not terminal[name].equals(other[name]):return None
            elif name=='rsi':
                if any(money(a)!=money(b) for a,b in zip(terminal[name],other[name])):return None
            elif any(not _demo_eur_seed_equivalent(a,b) for a,b in zip(terminal[name],other[name])):return None
        for name in ('support','resistance','stop'):
            if not _demo_eur_seed_equivalent(result[name],trial[name]):return None
        a,b=result['facts'],trial['facts']
        if set(a)!=set(b):return None
        if any(a[k]!=b[k] for k in a if isinstance(a[k],(str,bool)) or a[k] is None):return None
        # Keep only current CORE inputs; estimated-price lists remain in the
        # original context and are never copied into published trial receipts.
        trials.append({k:trial[k] for k in ('sufficient_history','ohlcv_valid','data_quality','facts','reason','closed_bar_count','signal_at','accepted_risk') if k in trial})
    return trials,dict(policy=policy['version'],mode='OWNER_ACCEPTED_DEMO_SENSITIVITY',
        seed_omissions=list(range(1,limit+1)),max_absolute_deltas=deltas,
        price_risk_quantum_eur='0.01',rsi_quantum='0.01',closed_bars=len(closed),
        absolute_tolerance_eur='0.001',comparison='cent_agreement_or_absolute_eur_noise_v2',
        limitation='Bounded seed sensitivity only; original DQ and any explicitly accepted historical gaps retained; no market-data certification')


def _safe_features(frame, info, now, prove_scope=False, demo_initialization=False):
    """Unverified input produces a structured blocked result, never an order."""
    try:
        if frame.empty or not frame.index.is_unique or not frame.index.is_monotonic_increasing or frame.index.hasnans:
            raise ValueError('Пустая история, дубликаты или некорректный порядок timestamps')
        enriched,result=_analyse_features(frame, info, now)
        if frame.attrs.get('warmup_context'):result['warmup_context']=frame.attrs['warmup_context']
        if prove_scope and frame.attrs.get('analysis_scope'):
            if not result.get('sufficient_history') or not result.get('ohlcv_valid'):
                result['scope_initialization']={'stable':False,'checked':False,'basis':'Input quality/warmup gate failed before recursive comparison'}
                return enriched,result
            # Both initializations stay INSIDE the owner's five-year scope.
            # Exact terminal values AND CORE facts: no tuned tolerance or vote
            # override. A short recursive history stays explicitly blocked.
            clock=pd.Timestamp.now(tz='UTC') if now is None else pd.Timestamp(now)
            closed=frame.loc[bar_ends(frame)<=clock].copy()
            stable=False
            from cp_controls import analysis_quality_allowed
            if len(closed)>35 and result.get('sufficient_history') and result.get('ohlcv_valid') and analysis_quality_allowed(result.get('data_quality',{}),result.get('accepted_risk')):
                variant=closed.iloc[1:].copy()
                reference,trial=_analyse_features(variant,info,clock)
                columns=['ema10','ema30','rsi','macd','macd_signal','atr','volume_mean20']
                terminal=pd.DataFrame(enriched,copy=False).loc[enriched.index.isin(closed.index),columns].tail(2)
                stable=(trial.get('sufficient_history') and terminal.equals(pd.DataFrame(reference,copy=False)[columns].tail(2))
                        and result.get('facts')==trial.get('facts'))
            result['scope_initialization']={'stable':bool(stable),'closed_bars':len(closed),
                'basis':('Exact terminal indicators and CORE facts with first approved warmup seed omitted' if frame.attrs.get('warmup_context') else 'Exact terminal indicators and CORE facts with first in-scope seed omitted; no older data')}
            if not stable and demo_initialization:
                proof=_demo_seed_trials(closed,info,clock,enriched,result)
                if proof:
                    trials,receipt=proof
                    result['_demo_seed_trials']=trials
                    result['scope_initialization']={**receipt,'stable':True,'exact':False,
                        'basis':'Owner-approved demo model; actual CORE output equivalence still required'}
                    stable=True
            if not stable:result.update(sufficient_history=False,reason='INSUFFICIENT_RECURSIVE_WARMUP_5Y')
        return enriched,result
    except (ValueError, TypeError, KeyError, AttributeError, OverflowError) as error:
        x=frame.copy()
        for name in ('ema10','ema30','rsi','atr','volume_mean20','macd','macd_signal'):x[name]=np.nan
        return x,dict(decision='WAIT',reason=str(error),sufficient_history=False,ohlcv_valid=False,
            data_quality={'state':'INVALID','issues':[str(error)]},closed_bar_count=0,bar_count=len(frame),
            support=None,resistance=None,stop=None,add=None,signal_at=None)


def analyse(frame, info=None, now=None):
    """Compatibility entry point; missing MTF evidence cannot authorize a signal."""
    from trading_engine import evaluate
    info=info or {}; tf=frame.attrs.get('timeframe') or '1D'
    enriched,result=_safe_features(frame,info,now)
    result.update(evaluate({tf:dict(result)},tf,info.get('position')))
    return enriched,result


def analyse_multiframe(frames, selected_tf, info=None, benchmark=None, now=None,required_tf=None):
    """Core API: supplied OHLCV only, no fetches, persistence or UI operations.

    All TF must carry READY quality, matching listing identity and timeframe.
    Caller supplies existing Relative Performance context; no fallback is made.
    """
    from trading_engine import evaluate, profile
    required=profile(selected_tf)
    if required_tf is not None:
        from trading_context import plan
        required=tuple(plan(selected_tf,required_tf)['required_tf'])
    info=info or {}; contexts={}; reference=None
    clock=pd.Timestamp.now(tz='UTC') if now is None else pd.Timestamp(now)
    for tf in dict.fromkeys((selected_tf,*required)):
        frame=frames.get(tf)
        if frame is None:continue
        calc=dict(info);meta=frame.attrs
        identity=tuple(meta.get(k) for k in ('source','symbol','currency','venue','timezone'))
        # Unattributed manual levels cannot override market levels.
        if any(not info.get('levels_'+k) or info['levels_'+k]!=meta.get(k) for k in ('source','symbol','currency','venue')):
            calc['levels']={}
        from cp_controls import initialization_for
        policy=initialization_for(frame)
        permitted=selected_tf in (policy.get('analysis_timeframes',policy['entry_timeframes']) if policy else ())
        _,result=_safe_features(frame,calc,clock,prove_scope=True,demo_initialization=permitted)
        if reference is None:reference=identity
        if identity!=reference or not all(identity) or meta.get('timeframe')!=tf:
            result.update(data_quality={'state':'INVALID'},reason='Идентичность/TF не подтверждены или различаются')
        for key,value in info.get('expected_identity',{}).items():
            if meta.get(key)!=value:result.update(data_quality={'state':'INVALID'},reason='Несовпадение ожидаемой идентичности: '+key)
        contexts[tf]=result
    answer=evaluate(contexts,selected_tf,info.get('position'),benchmark,required=required)
    for tf,context in answer.get('timeframe_context',{}).items():
        if contexts.get(tf,{}).get('scope_initialization'):
            context['scope_initialization']=contexts[tf]['scope_initialization']
        if contexts.get(tf,{}).get('warmup_context'):
            context['warmup_context']=contexts[tf]['warmup_context']
    sensitive={tf:c for tf,c in contexts.items() if c.get('_demo_seed_trials')}
    if sensitive:
        from itertools import product
        versions={initialization_for(frames[tf])['version'] for tf in sensitive}
        policy_version=next(iter(versions)) if len(versions)==1 else None
        checked=0;consistent=answer['status']=='READY' and policy_version is not None
        total=math.prod(len(c['_demo_seed_trials'])+1 for c in sensitive.values())
        proof_status=('NOT_RUN_INPUT_BLOCKED' if answer['status']!='READY' else
                      'NOT_RUN_POLICY_MISMATCH' if policy_version is None else
                      'NOT_RUN_SCENARIO_LIMIT' if total>500 else 'CONFIRMED')
        keys=('status','signal','decision','score','confidence','entry_blockers','blockers','bullish_reasons','bearish_reasons','position_state','warnings')
        if total>500:consistent=False
        if consistent:
            for choices in product(*([c,*c['_demo_seed_trials']] for c in sensitive.values())):
                trial_contexts={**contexts,**dict(zip(sensitive,choices))}
                trial=evaluate(trial_contexts,selected_tf,info.get('position'),benchmark,required=required)
                checked+=1
                if any(trial.get(k)!=answer.get(k) for k in keys):
                    consistent=False;proof_status='UNSTABLE';break
        proof={tf:c['scope_initialization'] for tf,c in sensitive.items()}
        answer['demo_initialization']={'policy':policy_version,'consistent':consistent,
            'status':proof_status,'scenarios_checked':checked,'scenarios_total':total,
            'timeframes':proof,'automatic_permission':False}
        if not consistent:
            answer.update(status='DATA BLOCKED',signal='WAIT',decision='WAIT',confidence=0)
            # A blocked dependency prevents comparison; it is not evidence of
            # an unstable seed. Preserve the actual input blockers and require
            # a completed consistent proof at execution as before.
            code={'UNSTABLE':'DEMO_SEED_SIGNAL_OR_RISK_UNSTABLE',
                  'NOT_RUN_POLICY_MISMATCH':'DEMO_SEED_POLICY_MISMATCH',
                  'NOT_RUN_SCENARIO_LIMIT':'DEMO_SEED_SCENARIO_LIMIT'}.get(proof_status)
            if code:answer['blockers'].append(code)
            answer['reason']='; '.join(answer['blockers'])
        else:
            answer['calculation_context']=policy_version
            answer['warnings'].append(('Только демо: принят риск пропусков GETTEX; ' if answer.get('accepted_risks') else 'Отдельный демоконтекст без принятых пропусков: ')+
                'устойчивость CORE проверена по нескольким начальным срезам: прежняя точность до евроцента, дополнительно допуск 0,001 € на границе округления; сигнал и риск неизменны. Это не точный полноархивный расчёт.')
    for tf,context in answer.get('timeframe_context',{}).items():
        if context.get('warmup_context'):answer['warnings'].append(tf+': '+context['warmup_context']['label'])
    return answer


def diagnostics(snapshot):
    issues=[]
    for stock in snapshot.get('stocks',[]):
        label=stock['name']
        if not stock.get('candles'):
            issues.append(f'{label}: свечи отсутствуют.');continue
        if stock.get('age_minutes',0)>60:
            issues.append(f'{label}: последней свече {stock["age_minutes"]:.0f} мин. Возможны закрытый рынок или задержка источника.')
        if stock.get('count',0)<30:issues.append(f'{label}: меньше 30 свечей; короткая история.')
        source=stock['candles'][-1].get('source','unknown')
        if source!='gettex':issues.append(f'{label}: последняя свеча {source}, это не чистый GETTEX.')
        if stock.get('decision')=='STOP':issues.append(f'{label}: достигнут сохранённый стоп.')
        if stock.get('error'):issues.append(f'{label}: {stock["error"]}')
    return issues or ['Локальная проверка завершена: явных проблем в доступном снимке не найдено.']

