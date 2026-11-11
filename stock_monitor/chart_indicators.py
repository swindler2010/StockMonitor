"""Chart-only indicator settings; signal model parameters remain explicit."""
def _validate_macd_periods(fast, slow, signal):
    if any(isinstance(p, bool) or not isinstance(p, int) or not 1 <= p <= 500
           for p in (fast, slow, signal)) or fast >= slow:
        raise ValueError('MACD: периоды 1–500; быстрый период меньше медленного')


def macd_series(close, fast=12, slow=26, signal=9):
    _validate_macd_periods(fast, slow, signal)
    line = close.ewm(span=fast, adjust=False, min_periods=fast).mean() - close.ewm(
        span=slow, adjust=False, min_periods=slow).mean()
    average = line.ewm(span=signal, adjust=False, min_periods=signal).mean()
    return line, average, line-average


FIXED_COLUMNS=('ema10','ema30','rsi','atr','macd','macd_signal','volume_mean20')


def partial_indicators(frame, settings=None):
    """Worker-only visual calculations on the existing verified projection.

    The projection owns calendar/provenance exclusions. Never bridge its real
    breaks, borrow an accepted-risk interpolation, or publish a CORE decision.
    Recursive values are seeded per segment, not claimed archive-equivalent.
    """
    import copy
    import numpy as np
    import pandas as pd
    from analytics import indicators
    from data_quality import IDENTITY,FIELDS
    s=dict(ema_fast=10,ema_slow=30,sma=20,bb=20,bb_dev=2,rsi=14,atr=14,
           volume=20,macd_fast=12,macd_slow=26,macd_signal=9)
    s.update(settings or {})
    out=pd.DataFrame(frame,copy=True);meta=copy.deepcopy(frame.attrs)
    ui=('ema_fast','ema_slow','sma','bb_high','bb_low','vwap','rsi','atr',
        'volume_mean','relative_volume','macd','macd_signal','macd_hist')
    columns=FIXED_COLUMNS+tuple('chart_'+k for k in ui)
    for k in columns:out[k]=np.nan
    out['chart_segment']=-1
    status=dict(display_only=True,basis='SEGMENT_LOCAL_REAL_OHLCV',
                execution_allowed=False,segments=0,latest_segment_bars=0,
                fixed_minimum_bars=dict(ema10=10,ema30=30,rsi=15,atr=14,
                    macd=26,macd_signal=34,volume_mean20=21),blocked_reason=None)
    def finish():
        status['available_values']={k:int(out[k].notna().sum()) for k in FIXED_COLUMNS}
        status['latest_available']={k:bool(len(out) and pd.notna(out[k].iloc[-1])) for k in FIXED_COLUMNS}
        meta['partial_indicators']=status;out.attrs=meta
        return out
    if frame.empty:return finish()
    if not meta.get('partial_chart',{}).get('display_only'):
        status['blocked_reason']='NO_VERIFIED_DISPLAY_PROJECTION';return finish()
    proof=meta.get('listing_proof',{})
    isin=meta.get('isin') or proof.get('isin')
    if (any(not meta.get(k) for k in IDENTITY) or not isin or
            meta.get('currency')!='EUR' or any('+' in str(meta[k]) for k in IDENTITY)):
        status['blocked_reason']='IDENTITY_NOT_PROVEN';return finish()
    if proof and (proof.get('status')!='CONFIRMED' or proof.get('isin')!=isin or
                  any(proof.get(k)!=meta[k] for k in IDENTITY)):
        status['blocked_reason']='LISTING_PROOF_MISMATCH';return finish()
    expected=meta.get('data_quality',{}).get('identity',{})
    if any(expected.get(k) and expected[k]!=meta[k] for k in IDENTITY):
        status['blocked_reason']='IDENTITY_MISMATCH';return finish()
    if any(k in out and not out[k].eq(isin if k=='isin' else meta.get(k)).all() for k in (*IDENTITY,'isin')):
        status['blocked_reason']='MIXED_IDENTITY';return finish()
    if not isinstance(frame.index,pd.DatetimeIndex) or not frame.index.is_monotonic_increasing or frame.index.hasnans or frame.index.has_duplicates:
        status['blocked_reason']='INVALID_TIMESTAMPS';return finish()
    try:
        index=frame.index
        if index.tz is not None:
            if str(index.tz)!=meta['timezone']:raise ValueError('timezone')
            index=index.tz_localize(None)
        index.tz_localize(meta['timezone'],ambiguous='raise',nonexistent='raise')
        values=out[list(FIELDS)].astype(float)
    except (KeyError,ValueError,TypeError):
        status['blocked_reason']='INVALID_INPUT';return finish()
    valid=np.isfinite(values).all(axis=1)&(values[['open','high','low','close']]>0).all(axis=1)&(values.volume>=0)
    valid&=(values.high>=values[['open','low','close']].max(axis=1))&(values.low<=values[['open','high','close']].min(axis=1))
    rejected=list(meta.get('archive_invalid',{}).get('rejected_timestamps',[]))+list(meta.get('invalid_parent_provenance',{}))
    valid&=~frame.index.isin(pd.to_datetime(rejected))
    live=meta.get('live_candle',{})
    live_at=pd.Timestamp(live['timestamp']) if live.get('timestamp') else None
    live_ok=(live_at==frame.index[-1] and live.get('display_only') is True and
             live.get('analysis_mode')=='PRELIMINARY_ONLY' and
             all(live.get('identity',{}).get(k)==meta[k] for k in IDENTITY))
    if 'bar_complete' in out:
        complete=out.bar_complete.fillna(False).eq(True)
        valid &= complete | (out.index==live_at if live_ok else False)
    partial=meta['partial_chart']
    breaks=set(pd.to_datetime(partial.get('indicator_break_before',partial.get('break_before',[]))))
    starts=valid & (~valid.shift(1,fill_value=False) | frame.index.isin(breaks))
    ids=starts.cumsum().where(valid,-1)
    status['excluded_indicator_rows']=int((~valid).sum())
    status['live_provisional']=bool(live_ok and valid.iloc[-1])
    if valid.any():_validate_macd_periods(s['macd_fast'],s['macd_slow'],s['macd_signal'])
    for _,positions in out.loc[valid].groupby(ids.loc[valid],sort=False).indices.items():
        # groupby indices refer to the valid subset; map back without filling gaps.
        labels=out.index[valid][positions]
        part=pd.DataFrame(values.loc[labels],copy=True)
        status['segments']+=1;out.loc[labels,'chart_segment']=status['segments']
        if labels[-1]==out.index[-1]:
            status['latest_segment_bars']=len(part);status['latest_segment_start']=str(labels[0])
        # Short real segments still have VWAP; every other configured/fixed
        # series remains NaN until its own minimum history. Avoid constructing
        # dozens of rolling/EWM objects whose results are provably all empty.
        if len(part)<min(10,s['ema_fast'],s['ema_slow'],s['sma'],s['bb'],
                         s['rsi']+1,s['atr'],s['volume'],s['macd_fast']):
            pv=(part.high+part.low+part.close)/3*part.volume
            if meta.get('timeframe') in ('1D','1W'):
                vwap=pv.cumsum()/part.volume.cumsum().replace(0,np.nan)
            else:
                group=part.index.normalize()
                vwap=pv.groupby(group).cumsum()/part.volume.groupby(group).cumsum().replace(0,np.nan)
            out.loc[labels,'chart_vwap']=vwap.to_numpy()
            continue
        # Real values only: do not inherit analytical_close_series risk estimates.
        fixed=indicators(part)
        out.loc[labels,list(FIXED_COLUMNS)]=fixed[list(FIXED_COLUMNS)].to_numpy()
        close=part.close;computed={}
        for k in ('ema_fast','ema_slow'):computed[k]=close.ewm(span=s[k],min_periods=s[k],adjust=False).mean()
        computed['sma']=close.rolling(s['sma']).mean()
        mid=close.rolling(s['bb']).mean();std=close.rolling(s['bb']).std()
        computed['bb_high']=mid+s['bb_dev']*std;computed['bb_low']=mid-s['bb_dev']*std
        pv=(part.high+part.low+close)/3*part.volume
        if meta.get('timeframe') in ('1D','1W'):
            computed['vwap']=pv.cumsum()/part.volume.cumsum().replace(0,np.nan)
        else:
            group=part.index.normalize()
            computed['vwap']=pv.groupby(group).cumsum()/part.volume.groupby(group).cumsum().replace(0,np.nan)
        delta=close.diff();up=delta.clip(lower=0).ewm(alpha=1/s['rsi'],min_periods=s['rsi'],adjust=False).mean()
        down=(-delta.clip(upper=0)).ewm(alpha=1/s['rsi'],min_periods=s['rsi'],adjust=False).mean()
        rsi=100-100/(1+up/down.replace(0,np.nan));rsi[(down==0)&(up>0)]=100;rsi[(down==0)&(up==0)]=50
        computed['rsi']=rsi
        tr=pd.concat([part.high-part.low,(part.high-close.shift()).abs(),(part.low-close.shift()).abs()],axis=1).max(axis=1)
        computed['atr']=tr.ewm(alpha=1/s['atr'],min_periods=s['atr'],adjust=False).mean()
        computed['volume_mean']=part.volume.rolling(s['volume']).mean()
        computed['relative_volume']=part.volume/computed['volume_mean'].replace(0,np.nan)
        computed['macd'],computed['macd_signal'],computed['macd_hist']=macd_series(close,s['macd_fast'],s['macd_slow'],s['macd_signal'])
        for k,v in computed.items():out.loc[labels,'chart_'+k]=v.to_numpy()
    return finish()


def segmented_line(frame, values):
    """Insert plot-only NaNs; retain every real first value of each segment."""
    import numpy as np
    x=np.arange(len(frame),dtype=float);y=np.asarray(values,dtype=float).copy()
    ids=frame.chart_segment.to_numpy();y[ids<0]=np.nan
    edges=np.flatnonzero(ids[1:]!=ids[:-1])+1
    return np.insert(x,edges,np.nan),np.insert(y,edges,np.nan)


def draw_partial(app, full, visible, s, muted, blue):
    """Only bind already prepared series on Tk, never calculate indicators here."""
    import numpy as np
    import pandas as pd
    from chart_modes import draw_segments
    # Plot geometry uses already computed numeric columns; it must not copy
    # the entire provenance archive for every panel/column/segment.
    visible=pd.DataFrame(visible,copy=False)
    def line(ax,column,**kwargs):
        x,y=segmented_line(visible,visible[column]);artist,=ax.plot(x,y,**kwargs)
        # The first warmed-up value (often today's RSI) is a single point.
        # A line alone hides it; mark isolated values without joining gaps.
        finite=np.isfinite(y)
        isolated=finite&~np.r_[False,finite[:-1]]&~np.r_[finite[1:],False]
        if isolated.any():ax.plot(x[isolated],y[isolated],linestyle='None',marker='.',markersize=4,color=artist.get_color())
    for enabled,column,label in (
        ('EMA','ema_fast','EMA '+str(s['ema_fast'])),('EMA','ema_slow','EMA '+str(s['ema_slow'])),
        ('SMA','sma','SMA '+str(s['sma'])),('Bollinger','bb_high','Боллинджер верх'),
        ('Bollinger','bb_low','Боллинджер низ'),('VWAP','vwap','VWAP (по участкам)')):
        if s[enabled]:line(app.ax_price,'chart_'+column,label=label,lw=1)
    x=np.arange(len(visible));valid=visible.chart_segment.to_numpy()>0
    def bars(ax,values,alpha=1):
        values=np.asarray(values,dtype=float);ok=valid&np.isfinite(values)
        draw_segments(ax,np.stack((np.column_stack((x[ok],np.zeros(ok.sum()))),np.column_stack((x[ok],values[ok]))),axis=1),color=blue,lw=1,alpha=alpha)
        ax.autoscale_view()
    for name,ax in app.panels:
        ax.set_ylabel(name,color=muted,fontsize=8)
        if name=='Объём':
            bars(ax,visible.volume)
            if s['Средний объём']:line(ax,'chart_volume_mean',color='#f59e0b')
        elif name=='Относительный объём':line(ax,'chart_relative_volume',color=blue)
        elif name=='RSI':
            line(ax,'chart_rsi');ax.axhline(70,ls=':',color=muted);ax.axhline(30,ls=':',color=muted);ax.set_ylim(0,100)
        elif name=='MACD':
            ax.set_ylabel(f"MACD {s['macd_fast']}/{s['macd_slow']}/{s['macd_signal']}",color=muted,fontsize=8)
            line(ax,'chart_macd');line(ax,'chart_macd_signal');bars(ax,visible.chart_macd_hist,alpha=.4)
        elif name=='ATR':line(ax,'chart_atr')
        cols={'RSI':['chart_rsi'],'MACD':['chart_macd','chart_macd_signal'],'ATR':['chart_atr'],
              'Относительный объём':['chart_relative_volume']}.get(name,[])
        if cols and not visible[cols].notna().any().any():
            ax.text(.5,.5,'Недостаточен прогрев непрерывного участка',transform=ax.transAxes,ha='center',color=muted,fontsize=8)


def volume_profile_rows(frame, analytical_ohlcv_valid=False):
    """Visual volume from validated rows, independent of trading admission."""
    if analytical_ohlcv_valid:return frame
    status=frame.attrs.get('partial_indicators',{})
    if (not frame.attrs.get('partial_chart',{}).get('display_only')
            or status.get('basis')!='SEGMENT_LOCAL_REAL_OHLCV'
            or status.get('blocked_reason') or 'chart_segment' not in frame):
        return None
    # Worker-produced segment membership already validates real OHLCV,
    # identity, timestamps and parent provenance, including a marked live tail.
    return frame.loc[frame.chart_segment.gt(0)]


def volume_profile(frame, bins=24):
    """Approximation: distribute each candle's volume uniformly across its range.

    OHLCV cannot recover traded volume at each price. Never label this as ticks.
    """
    import numpy as np
    if not isinstance(bins, int) or not 2 <= bins <= 100:
        raise ValueError('Volume Profile: 2–100 уровней')
    values=frame[['low','high','volume']].to_numpy(dtype=float)
    if not len(values):return np.array([]),np.array([]),np.array([])
    if not np.isfinite(values).all() or (values[:,2]<0).any() or (values[:,1]<values[:,0]).any():
        raise ValueError('Некорректные свечи для Volume Profile')
    low=values[:,0].min();high=values[:,1].max()
    if high==low:
        delta=max(abs(low)*.0001,.0001);low-=delta;high+=delta
    edges=np.linspace(low,high,bins+1);volumes=np.zeros(bins)
    # Bounded vector batches preserve the same OHLCV approximation; avoid a
    # Python loop per historical candle on every viewport update.
    for start in range(0,len(values),2048):
        part=values[start:start+2048];flat=part[:,1]==part[:,0]
        if flat.any():
            points=part[flat];indices=np.clip(np.searchsorted(edges,points[:,0],side='right')-1,0,bins-1)
            np.add.at(volumes,indices,points[:,2])
        ranges=part[~flat]
        if len(ranges):
            overlap=np.maximum(0,np.minimum(edges[1:][None,:],ranges[:,1,None])-np.maximum(edges[:-1][None,:],ranges[:,0,None]))
            volumes+=(overlap*(ranges[:,2]/(ranges[:,1]-ranges[:,0]))[:,None]).sum(axis=0)
    return (edges[:-1]+edges[1:])/2,volumes,np.diff(edges)
