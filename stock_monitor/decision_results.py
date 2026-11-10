"""Observable post-recommendation moves, never a broker P/L or causal verdict."""
import math
import pandas as pd

MINUTES={'5m':5,'10m':10,'15m':15,'30m':30,'1H':60,'2H':120,'3H':180,'4H':240,'1D':1440,'1W':1440,'1M':43200}


def recorded_frame(store, record):
    """Resolve the captured series, independent of today's watchlist/source choice."""
    identity=record.get('price_identity') or {}
    if any(not identity.get(k) for k in ('source','venue','currency','symbol','timeframe','timezone')):
        return pd.DataFrame()
    target=identity['timeframe']
    if target not in MINUTES:return pd.DataFrame()
    session=record.get('history_session') or {}
    if identity['source']=='gettex':
        if identity['venue']!='GETTEX' or identity['currency']!='EUR' or identity['symbol']!=record['isin']:return pd.DataFrame()
        base='5m' if target in ('5m','10m') else '15m'
        raw=store.load_official(record['isin'],base)
    elif identity['source']=='proxy':
        if target in ('30m','1H','4H') and not session.get('session_open') and session.get('alignment')!='calendar grid; session anchor unknown':
            return pd.DataFrame() # Do not guess an old recommendation's bucket grid.
        base='1D' if target in ('1D','1W','1M') else '5m' if target in ('5m','10m') else '15m'
        raw=store.load_series(record['isin'],base,'proxy',identity['symbol'],identity['venue'],identity['currency'])
    else:return pd.DataFrame()
    if raw.empty:return raw
    from analytics import resample
    raw.attrs.update(identity);raw.attrs.update(session)
    result=resample(raw,base,target);result.attrs.update(identity)
    return result


def scenario_outcome(record, frame, at, current):
    """Assess only the explicit immutable scenario, not general trading skill."""
    scenario=record.get('scenario')
    if not scenario:
        return {'status':'unspecified','reason':'При записи не были заданы цель, стоп и горизонт'}
    try:
        hours=scenario['horizon_hours'];target=float(scenario['target']);stop=float(scenario['stop'])
        price=float(record['price']);up=record['signal'] in ('BUY','ADD','HOLD')
        if type(hours) is not int or not 1<=hours<=8760 or not all(math.isfinite(v) and v>0 for v in (price,target,stop)):
            raise ValueError()
        if not (stop<price<target if up else target<price<stop):raise ValueError()
        due=at+pd.Timedelta(hours=hours)
        from analytics import bar_ends
        ends=bar_ends(frame)
        index=frame.index
        if index.tz is None:index=index.tz_localize(record['price_identity']['timezone'],ambiguous='NaT',nonexistent='NaT')
        starts=index.tz_convert('UTC')
        if not index.is_monotonic_increasing or index.has_duplicates:raise ValueError()
        # A candle straddling the observation or deadline cannot establish the
        # timing of a touch. Never use its full high/low as a post-signal fact.
        eligible=frame.loc[(starts>=at)&(ends<=min(current,due))&(frame.bar_complete if 'bar_complete' in frame else True)]
        if not {'high','low','close'}.issubset(eligible.columns):raise ValueError()
        if eligible.empty:
            return {'status':'unavailable' if current>=due else 'pending','reason':'Нет полных последующих свечей внутри горизонта','due_at':due.isoformat()}
        observed=[]
        for timestamp,row in eligible.iterrows():
            high,low,close=(float(row[k]) for k in ('high','low','close'))
            if not all(math.isfinite(v) and v>0 for v in (high,low,close)) or not low<=close<=high:raise ValueError()
            hit_target=high>=target if up else low<=target
            hit_stop=low<=stop if up else high>=stop
            if hit_target or hit_stop:
                status='ambiguous' if hit_target and hit_stop else 'target_reached' if hit_target else 'stop_reached'
                reasons={'ambiguous':'Цель и стоп внутри одной свечи: порядок неизвестен',
                         'target_reached':'Первое наблюдаемое касание в полных свечах — цель; пропуски и граничные свечи не оценены',
                         'stop_reached':'Первое наблюдаемое касание в полных свечах — стоп; пропуски и граничные свечи не оценены'}
                return {'status':status,'reason':reasons[status],'bar_at':str(timestamp),'due_at':due.isoformat(),
                        'scope':'observed complete candles only; gaps and partial boundary candles are not inferred',
                        'classification':'unassessed','not_trade_pnl':True}
            observed.append(timestamp)
        return {'status':'expired' if current>=due else 'pending',
                'reason':'В наблюдаемых полных свечах цель и стоп не достигнуты',
                'due_at':due.isoformat(),'observed_bars':len(observed),'classification':'unassessed',
                'scope':'observed complete candles only; missing history is not proof of no touch'}
    except (KeyError,TypeError,ValueError,OverflowError):
        return {'status':'unavailable','reason':'Некорректный сценарий или OHLC-история'}


def evaluate(record, frame, now=None):
    result={'status':'unavailable','reason':'Нет сопоставимых данных','horizon':'24 часа после записи; первое доступное закрытие',
            'execution':'independent price scenario; not a trade', 'return_pct':None}
    identity=record.get('price_identity') or {}
    keys=('source','venue','currency','symbol','timeframe','timezone')
    if any(not identity.get(k) for k in keys) or any(identity[k]!=frame.attrs.get(k) for k in keys):
        return {**result,'reason':'Идентичность истории не совпадает или не была сохранена'}
    try:
        price=float(record['price'])
        if not math.isfinite(price) or price<=0:raise ValueError()
        at=pd.Timestamp(record['at'])
        if at.tzinfo is None:raise ValueError()
        at=at.tz_convert('UTC')
        current=pd.Timestamp(now or pd.Timestamp.now(tz='UTC'))
        if current.tzinfo is None:raise ValueError()
        result['scenario']=scenario_outcome(record,frame,at,current)
        index=frame.index
        if not index.is_monotonic_increasing or index.has_duplicates:raise ValueError()
        if index.tz is None:index=index.tz_localize(identity['timezone'],ambiguous='NaT',nonexistent='NaT')
        from analytics import bar_ends
        ends=bar_ends(frame)
        due=at+pd.Timedelta(hours=24)
        candidates=frame.loc[(ends>=due)&(ends<=current)&(index.tz_convert('UTC')>=at)&(frame.bar_complete if 'bar_complete' in frame else True)]
        if candidates.empty:return {**result,'status':'pending','reason':'Ещё нет закрытой свечи после горизонта оценки','due_at':due.isoformat()}
        candle=candidates.iloc[0];ts=candidates.index[0]
        value=float(candle.close)
        if not math.isfinite(value) or value<=0:raise ValueError()
        delta=(value-price)/price*100
        # Exit signals measure the decline avoided, not a fictional short trade.
        direction=1 if record['signal'] in ('BUY','ADD','HOLD') else -1
        aligned=delta*direction
        return {**result,'status':'evaluated','reason':'Сравнение цен одного ряда; исходная цена могла быть архивной',
                'price_at':record.get('price_at'),'evaluated_bar_at':str(ts),'due_at':due.isoformat(),
                'price_after':value,'currency':identity['currency'],'return_pct':delta,
                'scenario_direction_pct':aligned,'direction_result':'favorable' if aligned>0 else 'unfavorable' if aligned<0 else 'unchanged',
                'quality_verdict':'not inferred; requires scenario review'}
    except (KeyError,TypeError,ValueError,OverflowError):
        return {**result,'reason':'Недостаточные или некорректные время/цена/интервал'}


def summary(records, today=None):
    day=today or pd.Timestamp.now(tz='Europe/Berlin').date()
    def collect(items):
        evaluated=[r['outcome'] for r in items if r.get('outcome',{}).get('status')=='evaluated']
        return {'recorded':len(items),'evaluated':len(evaluated),
                'favorable':sum(o['direction_result']=='favorable' for o in evaluated),
                'unfavorable':sum(o['direction_result']=='unfavorable' for o in evaluated),
                'mean_scenario_direction_pct':sum(o['scenario_direction_pct'] for o in evaluated)/len(evaluated) if evaluated else None}
    daily=[r for r in records if pd.Timestamp(r['at']).tz_convert('Europe/Berlin').date()==day]
    return {'today':collect(daily),'loaded_total':collect(records),'note':'Среднее независимых сценариев, не накопленная доходность портфеля; комиссии и сделки отсутствуют.'}
