"""Confirmed pivot zones, recalculated using only supplied same-market history."""
import numpy as np
import pandas as pd

def zones(frame,limit=3):
    if len(frame)<15:return []
    f=frame.dropna(subset=['high','low','close']).iloc[:-1].tail(100000)
    if len(f)<14:return []
    current=float(f.close.iloc[-1]);width=max(float((f.high-f.low).tail(30).median())*.35,abs(current)*.002)
    if not np.isfinite(width) or width<=0:return []
    candidates=[]
    for field,side in [('low','support'),('high','resistance')]:
        values=f[field].to_numpy()
        closes=f.close.to_numpy()
        # For a run of three closes, the largest (support) or smallest
        # (resistance) must cross the boundary. Suffix extrema make this O(N).
        runs=np.array([closes[j:j+3].max() if side=='support' else closes[j:j+3].min() for j in range(len(f)-2)])
        suffix=(np.minimum.accumulate(runs[::-1]) if side=='support' else np.maximum.accumulate(runs[::-1]))[::-1]
        for i in range(3,len(f)-3):
            chunk=values[i-3:i+4]
            if values[i]!=(min(chunk) if side=='support' else max(chunk)):continue
            p=float(values[i])
            # Three consecutive closes beyond the zone invalidate the old role.
            if (suffix[i+1]<p-width if side=='support' else suffix[i+1]>p+width):continue
            if (side=='support' and p>=current) or (side=='resistance' and p<=current):continue
            candidates.append((p,side,f.index[i]))
    result=[]
    for side in ('support','resistance'):
        groups=[]
        for price,kind,at in sorted(candidates):
            if kind!=side:continue
            if groups and price-groups[-1]['high']<=width:
                groups[-1]['high']=price;groups[-1]['touches']+=1;groups[-1]['last']=max(groups[-1]['last'],at);groups[-1]['first']=min(groups[-1]['first'],at)
            else:groups.append(dict(low=price,high=price,side=side,touches=1,first=at,last=at))
        for g in groups:
            g['historical']=(f.index[-1]-g['first'])>=pd.Timedelta(days=90)
            g['distance']=abs((g['low']+g['high'])/2-current)
        near=sorted(groups,key=lambda g:g['distance'])[:1]
        strong=sorted([g for g in groups if g not in near],key=lambda g:(g['touches'],g['historical']),reverse=True)
        for g in (near+strong)[:limit]:
            g['low']-=width/2;g['high']+=width/2;result.append(g)
    return result
