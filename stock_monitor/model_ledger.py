"""Explicit model quantities only. Never interpret recommendations as broker fills.

Replay uses the next observed complete candle's open, preserving source identity.
Reports are recomputed from the archive; no capital, fees, slippage or FX is assumed.
"""
import math
import pandas as pd
from analytics import bar_ends

IDENTITY=('source','venue','currency','symbol','timeframe','timezone')


def _state(events, cutoff, mark):
    quantity=cost=realized=cash=0.0
    accepted=[];rejected=[]
    for event in events:
        if event['at']>=cutoff:continue
        qty=event['quantity'];price=event['price']
        if event['signal'] in ('BUY','ADD'):
            quantity+=qty;cost+=qty*price;cash-=qty*price
        else:
            if qty>quantity+1e-9:
                rejected.append(event['id']);continue
            average=cost/quantity if quantity else 0
            realized+=qty*(price-average);cash+=qty*price
            quantity-=qty;cost-=qty*average
            if abs(quantity)<1e-9:quantity=cost=0.0
        accepted.append(event['id'])
        if not all(math.isfinite(v) for v in (quantity,cost,realized,cash)):raise ValueError('Model arithmetic overflow')
    unrealized=quantity*mark-cost if mark is not None else 0.0 if quantity==0 else None
    if unrealized is not None and not math.isfinite(unrealized):raise ValueError('Model mark overflow')
    return {'quantity':quantity,'cost':cost,'realized':realized,'unrealized':unrealized,
            'pnl':cash+quantity*mark if mark is not None else cash if quantity==0 else None,
            'accepted':accepted,'rejected':rejected}


def report(records, frame_for_record, now=None):
    current=pd.Timestamp(now or pd.Timestamp.now(tz='UTC'))
    if current.tzinfo is None:raise ValueError('Timezone-aware current time required')
    current=current.tz_convert('UTC')
    midnight=current.tz_convert('Europe/Berlin').normalize().tz_convert('UTC')
    groups={};unavailable=[]
    for record in records:
        if 'model_quantity' not in record:continue
        identity=record.get('price_identity',{})
        try:
            qty=float(record['model_quantity'])
            if isinstance(record['model_quantity'],bool) or not math.isfinite(qty) or qty<=0:raise ValueError()
            if record['signal'] not in ('BUY','ADD','SELL','REDUCE','STOP'):raise ValueError()
            if any(not identity.get(k) for k in IDENTITY):raise ValueError()
            key=(record['isin'],*(identity[k] for k in IDENTITY))
            group=groups.setdefault(key,{'identity':identity,'isin':record['isin'],'records':[]})
            group['records'].append(record)
        except (KeyError,TypeError,ValueError):
            unavailable.append({'id':record.get('id'),'reason':'Некорректная модельная запись'})
    rows=[];totals={}
    for group in groups.values():
        records=group['records']
        try:
            records=sorted(records,key=lambda r:(pd.Timestamp(r['at']),r['id']))
            frame=frame_for_record(records[0])
            identity=group['identity']
            if frame is None or frame.empty or any(frame.attrs.get(k)!=identity[k] for k in IDENTITY):raise ValueError()
            if 'source' in frame and set(frame.source.astype(str))!={identity['source']}:raise ValueError()
            if not frame.index.is_monotonic_increasing or frame.index.has_duplicates:raise ValueError()
            index=frame.index
            if index.tz is None:index=index.tz_localize(identity['timezone'],ambiguous='NaT',nonexistent='NaT')
            starts=index.tz_convert('UTC');ends=bar_ends(frame)
            events=[];pending=[]
            for record in records:
                at=pd.Timestamp(record['at'])
                if at.tzinfo is None:raise ValueError()
                candidates=[i for i in range(len(frame)) if starts[i]>=at and ends[i]<=current]
                if not candidates:
                    pending.append(record['id']);continue
                pos=candidates[0];price=float(frame.iloc[pos]['open'])
                if not math.isfinite(price) or price<=0:raise ValueError()
                events.append({'id':record['id'],'at':starts[pos],'price':price,
                               'signal':record['signal'],'quantity':float(record['model_quantity'])})
            events.sort(key=lambda e:e['at'])
            def mark(cutoff):
                candidates=frame.loc[ends<=cutoff]
                if candidates.empty:return None
                value=float(candidates.iloc[-1]['close'])
                if not math.isfinite(value) or value<=0:raise ValueError()
                return value
            latest=mark(current);previous=mark(midnight)
            state=_state(events,current,latest);before=_state(events,midnight,previous)
            daily=state['pnl']-before['pnl'] if state['pnl'] is not None and before['pnl'] is not None else None
            row={'isin':group['isin'],'identity':identity,**state,'daily_pnl':daily,
                 'mark':latest,'pending':pending,'fills':[{**e,'at':e['at'].isoformat()} for e in events if e['id'] in state['accepted']]}
            rows.append(row)
            total=totals.setdefault(identity['currency'],{'pnl':0.0,'daily_pnl':0.0,'realized':0.0,'unrealized':0.0,'complete':True})
            for key in ('pnl','daily_pnl','realized','unrealized'):
                if row[key] is None:total['complete']=False;total[key]=None
                elif total[key] is not None:total[key]+=row[key]
            if pending or state['rejected']:total['complete']=False
        except (KeyError,TypeError,ValueError,OverflowError):
            unavailable.extend({'id':r['id'],'reason':'Нет сопоставимой полноценной истории для модельного учёта'} for r in records)
    return {'positions':rows,'by_currency':totals,'unavailable':unavailable,
            'complete':not unavailable and all(t['complete'] for t in totals.values()),
            'policy':'Только явно заданное модельное количество. Вход/выход по открытию следующей доступной полной свечи, пересчёт по архиву. Без комиссий, проскальзывания, дивидендов, корпоративных действий, FX и операций у брокера. Разные источники и интервалы — отдельные модельные позиции. Стоп и цель сценария не создают автоматические сделки. Итоги при пропусках неполные.'}
