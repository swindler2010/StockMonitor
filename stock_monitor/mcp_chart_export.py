"""Capture existing desktop indicator output, without Tk or duplicate formulas."""
import math
import numpy as np
from types import SimpleNamespace


class PlotCapture:
    def __init__(self,name,count):self.name=name;self.count=count;self.series={};self.number=0
    def plot(self,x,y,**style):
        self.number+=1
        names={'MACD':['MACD','MACD signal','MACD histogram'],'Объём':['Volume','Volume mean']}
        label=style.get('label') or (names[self.name][self.number-1] if self.name in names else self.name)
        values=[None]*self.count
        x=np.asarray(x);y=np.asarray(y)
        if style.get('solid_capstyle')=='butt':x=x.reshape(-1,3)[:,1];y=y.reshape(-1,3)[:,1]
        for a,b in zip(x,y):
            if np.isfinite(a) and 0<=int(a)<self.count:values[int(a)]=float(b) if np.isfinite(b) else None
        self.series[label]=values
        return [None]
    def set_ylabel(self,*args,**kwargs):pass
    def axhline(self,*args,**kwargs):pass
    def set_ylim(self,*args,**kwargs):pass
    def autoscale_view(self,*args,**kwargs):pass


def export(frame,settings,timeframe):
    from desktop014 import Desktop014
    from chart_indicators import volume_profile
    primary=PlotCapture('price',len(frame))
    panels=[(name,PlotCapture(name,len(frame))) for name in ('Объём','Относительный объём','RSI','MACD','ATR') if settings.get(name)]
    host=SimpleNamespace(ax_price=primary,panels=panels,timeframe=SimpleNamespace(get=lambda:timeframe))
    Desktop014.draw_indicators(host,frame,frame,settings)
    series=dict(primary.series)
    for name,axis in panels:series.update(axis.series)
    profile={'status':'DISABLED'}
    if settings.get('Volume Profile'):
        try:
            prices,volumes,widths=volume_profile(frame)
            profile={'status':'AVAILABLE','scope':'FULL_REQUESTED_HISTORY_NOT_UI_VIEWPORT','approximation':'Existing OHLCV volume_profile; not tick-by-price volume',
                     'bins':[dict(price=float(p),volume=float(v),width=float(w)) for p,v,w in zip(prices,volumes,widths)]}
        except (ValueError,TypeError):profile={'status':'UNAVAILABLE','reason':'INVALID_PROFILE_INPUT'}
    return series,profile
