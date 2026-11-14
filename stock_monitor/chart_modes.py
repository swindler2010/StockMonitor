"""Price-only rendering; every mode shares the same candle-index coordinates."""
import numpy as np
from matplotlib.path import Path
from matplotlib.patches import PathPatch
MODES=('Candles','Line','Bars','Candles + Close line')

def draw_segments(ax,segments,**style):
    """One path with NaN breaks, rather than one Python Path per segment."""
    segments=np.asarray(segments,dtype=float)
    if not len(segments):return
    vertices=np.full((len(segments),3,2),np.nan)
    vertices[:,:2,:]=segments
    return ax.plot(vertices[:,:,0].ravel(),vertices[:,:,1].ravel(),solid_capstyle='butt',**style)[0]

def draw_prices(ax,frame,mode):
    import pandas as pd
    meta=frame.attrs
    frame=pd.DataFrame(frame,copy=False)
    if mode not in MODES:mode='Candles'
    x=np.arange(len(frame),dtype=float);o=frame.open.to_numpy(dtype=float);c=frame.close.to_numpy(dtype=float)
    low=frame.low.to_numpy();high=frame.high.to_numpy();up=c>=o
    if mode in ('Candles','Candles + Close line','Bars'):
        segments=np.stack((np.column_stack((x,low)),np.column_stack((x,high))),axis=1)
        if mode=='Bars':
            opening=np.stack((np.column_stack((x-.3,o)),np.column_stack((x,o))),axis=1)
            closing=np.stack((np.column_stack((x,c)),np.column_stack((x+.3,c))),axis=1)
            segments=np.stack((segments,opening,closing),axis=1).reshape(-1,2,2)
            mask=np.repeat(up,3)
        else:mask=up
        for selected,color in ((mask,'#22c55e'),(~mask,'#ef4444')):
            draw_segments(ax,segments[selected],color=color,lw=1.1)
    if mode in ('Candles','Candles + Close line'):
        lo=np.minimum(o,c);hi=np.maximum(np.maximum(o,c),lo+np.maximum(np.abs(lo)*.00005,.00001))
        bodies=np.stack((np.column_stack((x-.325,lo)),np.column_stack((x+.325,lo)),np.column_stack((x+.325,hi)),np.column_stack((x-.325,hi))),axis=1)
        # Separate closed subpaths preserve every body; two artists avoid N Path draws.
        for selected,color in ((up,'#22c55e'),(~up,'#ef4444')):
            b=bodies[selected]
            if not len(b):continue
            closed=np.concatenate((b,b[:,:1]),axis=1)
            codes=np.tile([Path.MOVETO,Path.LINETO,Path.LINETO,Path.LINETO,Path.CLOSEPOLY],len(b))
            path=Path(closed.reshape(-1,2),codes)
            path.should_simplify=False
            # add_patch walks every vertex in Python for limits; compute exact bounds once.
            ax.add_artist(PathPatch(path,transform=ax.transData,facecolor=color,edgecolor=color,linewidth=.5))
        if len(bodies):
            ax.update_datalim([[x[0]-.325,float(np.min(lo))],[x[-1]+.325,float(np.max(hi))]])
    breaks=meta.get('partial_chart',{}).get('break_before',[])
    boundaries=sorted({int(frame.index.searchsorted(value)) for value in breaks})
    boundaries=[i for i in boundaries if 0<i<len(frame)]
    if mode in ('Line','Candles + Close line'):
        # Separators belong to the drawing path, never to the OHLCV frame.
        px=np.insert(x,boundaries,np.nan);py=np.insert(c,boundaries,np.nan)
        ax.plot(px,py,color='#60a5fa',lw=1.8,label='Close')
    accepted=meta.get('partial_chart',{}).get('accepted_break_before',[])
    accepted_positions={int(frame.index.searchsorted(value)) for value in accepted}
    warnings=[i for i in boundaries if i not in accepted_positions]
    if warnings:
        # Long partial archives may have thousands of gaps. Two collections
        # preserve every separator without thousands of Tk/Matplotlib artists.
        from matplotlib.collections import PolyCollection,LineCollection
        positions=np.asarray(warnings,dtype=float)
        bands=[[(i-.65,0),(i-.35,0),(i-.35,1),(i-.65,1)] for i in positions]
        ax.add_collection(PolyCollection(bands,transform=ax.get_xaxis_transform(),
            facecolors='#f59e0b',edgecolors='none',alpha=.12,
            label='Разрыв / исключённые данные'),autolim=False)
        separators=[[(i-.5,0),(i-.5,1)] for i in positions]
        ax.add_collection(LineCollection(separators,transform=ax.get_xaxis_transform(),
            colors='#f59e0b',linewidths=.4,linestyles=':',alpha=.5),autolim=False)
    if meta.get('partial_chart'):
        ax.text(.99,.98,str(meta.get('data_quality',{}).get('state','UNKNOWN'))+' / PARTIAL — только отображение',ha='right',va='top',
                transform=ax.transAxes,color='#f59e0b',fontsize=8)
    live=meta.get('live_candle',{})
    if live.get('timestamp') in set(map(str,frame.index)):
        i=int(frame.index.get_loc(live['timestamp']))
        text='Живая свеча — предварительные данные' if live.get('status')=='FORMING' else 'Живая свеча — ожидаются данные'
        ax.plot([i],[c[i]],marker='o',markersize=4,color='#38bdf8',zorder=6)
        ax.text(.99,.91,text,ha='right',va='top',transform=ax.transAxes,color='#38bdf8',fontsize=8)
        from datetime import datetime
        from zoneinfo import ZoneInfo
        through=live.get('observed_through')
        if through:
            local=datetime.fromisoformat(through).astimezone(ZoneInfo(meta.get('timezone','Europe/Berlin')))
            ax.text(.99,.86,'Полученные данные до '+local.strftime('%d.%m %H:%M'),ha='right',va='top',
                    transform=ax.transAxes,color='#38bdf8',fontsize=7)
    ax.autoscale_view()


def compact_number(value,_=None):
    for scale,suffix in ((1e9,'B'),(1e6,'M'),(1e3,'k')):
        if abs(value)>=scale:return f'{value/scale:.3g}{suffix}'
    return f'{value:.3g}'


def draw_volume(ax,frame,valid=None):
    """Two filled paths, exact observed volumes; no N-patch rendering overhead."""
    x=np.arange(len(frame),dtype=float);values=frame.volume.to_numpy(dtype=float)
    ok=np.isfinite(values)&(values>=0)
    if valid is not None:ok&=valid
    up=frame.close.to_numpy()>=frame.open.to_numpy()
    for mask,color in ((ok&up,'#34d399'),(ok&~up,'#fb7185')):
        xx=x[mask];vv=values[mask]
        if not len(xx):continue
        bodies=np.stack((np.column_stack((xx-.4,np.zeros(len(xx)))),np.column_stack((xx+.4,np.zeros(len(xx)))),
                         np.column_stack((xx+.4,vv)),np.column_stack((xx-.4,vv)),np.column_stack((xx-.4,np.zeros(len(xx))))),axis=1)
        path=Path(bodies.reshape(-1,2),np.tile([1,2,2,2,79],len(xx)));path.should_simplify=False
        ax.add_artist(PathPatch(path,transform=ax.transData,facecolor=color,edgecolor=color,lw=.35,alpha=.95))
    if ok.any():ax.update_datalim([[x[ok][0]-.4,0],[x[ok][-1]+.4,float(values[ok].max())]])
    ax.autoscale_view()


def fit_volume(ax,frame,xlim):
    from matplotlib.ticker import FuncFormatter,MaxNLocator
    portion=frame.iloc[max(0,int(np.ceil(xlim[0]))):max(0,min(len(frame),int(np.floor(xlim[1]))+1))]
    if 'chart_segment' in portion:portion=portion.loc[portion.chart_segment>0]
    values=portion.volume.to_numpy(dtype=float);values=values[np.isfinite(values)&(values>=0)]
    maximum=float(values.max()) if len(values) else 0
    # Keep the optional volume-average overlay visible as well.
    for line in ax.lines:
        x=np.asarray(line.get_xdata(),dtype=float);y=np.asarray(line.get_ydata(),dtype=float)
        eligible=np.isfinite(y)&(x>=xlim[0])&(x<=xlim[1])
        if eligible.any():maximum=max(maximum,float(y[eligible].max()))
    ax.set_ylim(0,maximum*1.12 if maximum else 1)
    ax.yaxis.set_major_locator(MaxNLocator(3))
    ax.yaxis.set_major_formatter(FuncFormatter(compact_number))
