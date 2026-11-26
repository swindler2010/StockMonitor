"""Bounded, listing-exact data for the workspace, outside CORE/PAPER."""
from contextlib import closing, contextmanager
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from decimal import Decimal, ROUND_HALF_UP
import csv
import json
import sqlite3
from zoneinfo import ZoneInfo
from uuid import uuid4

import numpy as np
import pandas as pd

from analytics import resample
from data_quality import FIELDS, IDENTITY, expected_index
from gettex_chart import SOURCE as CHART, confirmed_state, state_key
from gettex_sessions import warning
from timeframes import LABELS, MINUTES, endpoints


BERLIN = ZoneInfo('Europe/Berlin')
SOURCES = {'gettex':'Сделки GETTEX','gettex_chart':'LSEG / GETTEX'}
TF = ('1m',*LABELS)
BOOKS = ('V1','V2','V3','V4','V5')
DEFAULT_LAYOUT = ('15m','1H','4H','1D')
LAYOUT_KEY = 'intraday_workspace_layout'
ALERT_KEY = 'intraday_workspace_alerts'
TRAINING_KEY = 'intraday_workspace_training'
COLS = ('ts', *FIELDS)
WHERE = ','.join(COLS)


@contextmanager
def read(path):
    with closing(sqlite3.connect(Path(path).resolve().as_uri()+'?mode=ro',uri=True,timeout=.2)) as db:
        db.execute('PRAGMA query_only=ON')
        db.execute('BEGIN')
        yield db


def setting(db,key,default=None):
    row=db.execute('SELECT value FROM settings WHERE key=?',(key,)).fetchone()
    return json.loads(row[0]) if row else default


def identity(db,isin,source,base):
    if source=='gettex':
        return dict(source=source,venue='GETTEX',currency='EUR',symbol=isin,timezone='Europe/Berlin')
    if source!=CHART:
        raise ValueError('Неизвестный источник свечей')
    saved=setting(db,state_key(isin,base),{})
    return saved['identity'] if confirmed_state({'isin':isin},saved) else None


def base_for(source,tf):
    if tf in ('1W','1M','1D'):
        return '1D' if source==CHART else '15m'
    return tf if tf in ('1m','5m','15m') else '5m' if tf=='10m' else '15m'


def statement(isin,source,base,meta,select):
    if source==CHART:
        return (f'SELECT {select} FROM series WHERE isin=? AND source=? AND venue=? AND currency=? AND symbol=? AND base_tf=?',
                (isin,source,meta['venue'],meta['currency'],meta['symbol'],base))
    return (f'SELECT {select} FROM candles WHERE isin=? AND source=? AND base_tf=?',(isin,source,base))


def base_frame(db,isin,source,base,start,end):
    meta=identity(db,isin,source,base)
    if not meta or source=='gettex' and base=='1m':
        return pd.DataFrame()
    query,params=statement(isin,source,base,meta,WHERE)
    rows=db.execute(query+' AND ts>=? AND ts<? ORDER BY ts',(*params,str(start),str(end))).fetchall()
    frame=pd.DataFrame(rows,columns=COLS).set_index('ts')
    frame.index=pd.DatetimeIndex(frame.index)
    recorded=setting(db,state_key(isin,base),{}) if source==CHART else {}
    frame.attrs={**meta,'timeframe':base,
                 'rejected':recorded.get('archive_invalid',{}).get('rejected_timestamps',[])}
    return frame


def clean_frame(raw,base,now):
    """Reject bad values, venue-offsession observations and still-open bars."""
    if raw.empty:
        return raw
    numbers=raw[list(FIELDS)].to_numpy(dtype=float)
    op,hi,lo,cl,vol=numbers.T
    ok=np.isfinite(numbers).all(axis=1)&(lo>0)&(hi>=op)&(hi>=cl)&(lo<=op)&(lo<=cl)&(vol>=0)
    grid=expected_index(raw.attrs,base,raw.index[0],raw.index[-1],now)
    rejected=pd.DatetimeIndex(raw.attrs.get('rejected',[]))
    closed=ok&raw.index.isin(grid)&~raw.index.isin(rejected)
    filtered=raw.loc[closed].copy()
    filtered.attrs={**raw.attrs,'excluded':int((~closed).sum())}
    return filtered


def project(raw,base,tf,now):
    if raw.empty:
        return raw
    frame=raw if tf==base else resample(raw,base,tf,now)
    if frame.empty:
        return frame
    meta=frame.attrs
    ends=endpoints(frame)
    mask=ends<=now
    if 'bar_complete' in frame:
        mask &= frame.bar_complete.fillna(False).to_numpy()
    frame=frame.loc[mask,list(FIELDS)].copy()
    frame.attrs={**{k:raw.attrs[k] for k in IDENTITY},'timeframe':tf,
                 'excluded':raw.attrs.get('excluded',0)+int((~mask).sum()),
                 'ends':tuple(ends[mask])}
    grid=expected_index(frame.attrs,tf,frame.index[0],frame.index[-1],now) if not frame.empty and tf not in ('1W','1M') else None
    if grid is not None:
        idx=grid.get_indexer(frame.index)
        gaps=np.r_[False,np.diff(idx)!=1]
    else:
        gaps=np.zeros(len(frame),dtype=bool)
    frame.attrs['breaks']=tuple(gaps)
    # Visual EMA follows the archived data in each continuous segment.
    frame['ema']=frame.close.groupby(np.cumsum(gaps)).transform(lambda s:s.ewm(span=20,min_periods=20,adjust=False).mean())
    return frame


def panels(path,isin,source,intervals,day,lookback,now=None):
    if source not in SOURCES or not intervals or any(tf not in TF for tf in intervals):
        raise ValueError('Некорректный источник или интервал')
    now=pd.Timestamp(now or datetime.now(timezone.utc))
    end=pd.Timestamp(date.fromisoformat(day))+pd.Timedelta(days=1)
    start=end-pd.Timedelta(days=lookback)
    from paper_v1_news import context
    with read(path) as db:
        row=db.execute('SELECT payload FROM instruments WHERE isin=?',(isin,)).fetchone()
        if row is None:
            raise ValueError('Инструмент удалён из списка')
        info=json.loads(row[0])
        bases={base:base_frame(db,isin,source,base,start,end)
               for base in {base_for(source,tf) for tf in intervals}}
        background=context(db,info,now.to_pydatetime())
    bases={key:clean_frame(frame,key,now) for key,frame in bases.items()}
    output={tf:project(bases[base_for(source,tf)],base_for(source,tf),tf,now) for tf in intervals}
    return {'frames':output,'events':events_from(background),'isin':isin,'source':source}


def events_from(background):
    issuer=background.get('company_events',{})
    dated=[*background['calendar']['events'],*issuer.get('dates',[]),*issuer.get('filings',[])]
    events=[(item.get('starts_at') or item['date'],item, bool(item.get('starts_at')))
            for item in dated if not item.get('stale')]
    for article in background.get('company',{}).get('articles',[]):
        if article.get('events'):
            events.append((article['published'],article,True))
    return events


def coverage(path,source,tf,book,now=None):
    if source not in SOURCES or tf not in TF or book not in BOOKS:
        raise ValueError('Некорректный фильтр')
    now=pd.Timestamp(now or datetime.now(timezone.utc))
    base=base_for(source,tf)
    local=now.tz_convert(BERLIN)
    from freshness_view import session_at
    market=session_at('GETTEX',now.isoformat())
    until=local.tz_localize(None)
    since=until.normalize()-pd.Timedelta(days=7)
    identity_template=dict(venue='GETTEX',timezone='Europe/Berlin')
    try:
        grid=expected_index(identity_template,base,since,until,now)
    except ValueError:
        grid=pd.DatetimeIndex([])
    records=[]
    with read(path) as db:
        instruments=[json.loads(p) for p, in db.execute('SELECT payload FROM instruments')]
        for info in instruments:
            if info.get('kind','stock')!='stock':
                continue
            isin=info['isin']
            meta=identity(db,isin,source,base)
            first=last=None
            count=0
            data_state='Нет данных'
            if meta:
                sql,params=statement(isin,source,base,meta,'min(ts),max(ts),count(*)')
                first,last,count=db.execute(sql,params).fetchone()
                if count:
                    data_state='Архив'
            age=None
            if last and base in MINUTES:
                age=max(0,(local-pd.Timestamp(last).tz_localize(BERLIN)).total_seconds()/60)
                data_state='Рынок закрыт' if market=='MARKET_CLOSED' else ('Задержка' if age>MINUTES[base]+15 else 'Есть свежие записи')
                if market=='UNKNOWN':
                    data_state='Нет подтверждения сессии'
            chart_state=setting(db,state_key(isin,base),{}) if source==CHART else {}
            dq=setting(db,f"data_quality:{isin}:{meta['symbol']}:{base}",{}) if meta else {}
            quality=chart_state.get('raw_archive_quality') or chart_state.get('quality') or dq
            if isinstance(quality,str):
                quality={'state':quality}
            elif not isinstance(quality,dict):
                quality={}
            missing=None
            if first and len(grid):
                expected=grid[grid>=pd.Timestamp(first)]
                stmt,params=statement(isin,source,base,meta,'ts')
                present=db.execute(stmt+' AND ts>=? AND ts<=?',(*params,str(since),str(until))).fetchall()
                observed=pd.DatetimeIndex([ts for ts, in present])
                missing=len(expected.difference(observed))
            report=setting(db,'paper_auto_status:'+book+':'+isin,{})
            checks=report.get('timeframes',{})
            check=checks.get(tf,{})
            reason=check.get('execution_blocker') or check.get('quote_blocker') or check.get('reason') or ''
            source_reason=' | '.join(str(issue) for issue in quality.get('issues',[])[:2])
            inputs=report.get('trend_watch') or {}
            signal_at=check.get('at')
            fresh_signal=isinstance(signal_at,(int,float)) and 0<=now.timestamp()-signal_at<=900
            records.append(dict(isin=isin,name=info['name'],base=base,first=first,last=last,count=count,
                age=age,state=data_state,source_status=chart_state.get('state'),missing=missing,
                archive_quality=quality.get('state'),source_reason=source_reason,
                signal=check.get('signal','—'),core=check.get('core','—'),reason=reason,
                signal_fresh=fresh_signal,trend=inputs.get('state','') if isinstance(inputs,dict) else '',
                volume=last_volume(db,isin,source,base,meta,last) if count else None))
    return records


def last_volume(db,isin,source,base,meta,last):
    sql,params=statement(isin,source,base,meta,'volume')
    values=db.execute(sql+' AND ts<=? ORDER BY ts DESC LIMIT 21',(*params,last)).fetchall()
    previous=[v for v, in values[1:] if v is not None and v>0]
    current=values[0][0] if values else None
    return round(current/(sum(previous)/len(previous)),2) if previous and current is not None else None


def export_frame(path,isin,frame):
    """Only the explicitly chosen local CSV, with venue-aware bar timestamps."""
    with Path(path).open('w',encoding='utf-8-sig',newline='') as f:
        writer=csv.writer(f)
        writer.writerow(('isin','source','venue','currency','symbol','timeframe','open_berlin','close_utc',*FIELDS))
        prefix=(isin,*(frame.attrs[k] for k in ('source','venue','currency','symbol','timeframe')))
        for at,end,row in zip(frame.index,frame.attrs['ends'],frame[list(FIELDS)].itertuples(index=False,name=None)):
            writer.writerow((*prefix,at.tz_localize(BERLIN).isoformat(),end.isoformat(),*row))


def money(value):
    return Decimal(str(value)).quantize(Decimal('.01'),rounding=ROUND_HALF_UP)


class Replay:
    """Independent long-only training book. The next *observed* open fills."""
    def __init__(self,frame,day,delay=0):
        from paper_account import POLICY
        if frame.empty or frame.attrs.get('timeframe') not in MINUTES:
            raise ValueError('Выберите внутридневной график с закрытыми свечами')
        self.frame=frame
        if type(delay) is not int or delay not in (0,5,15):
            raise ValueError('Выберите задержку 0, 5 или 15 минут')
        self.delay=pd.Timedelta(minutes=delay)
        self.id=uuid4().hex
        self.day=day
        self.cursor=int(frame.index.searchsorted(pd.Timestamp(day)))
        if self.cursor>=len(frame) or str(frame.index[self.cursor].date())!=day:
            raise ValueError('На выбранную дату нет закрытой свечи')
        self.cash=Decimal(POLICY['starting_cash'])
        self.half=Decimal(POLICY['spread_bps'])/20000
        self.fee=Decimal(POLICY['commission'])
        self.quantity=0
        self.pending=None
        self.history=[]

    @property
    def clock(self):
        return self.frame.attrs['ends'][self.cursor]+self.delay

    @property
    def last_visible_close(self):
        return self.frame.attrs['ends'][self.cursor]

    def order(self,side,quantity):
        if (self.pending or side not in ('BUY','SELL') or type(quantity) is not int
                or quantity<=0 or side=='SELL' and quantity>self.quantity):
            raise ValueError('Проверьте количество и наличие ожидающей заявки')
        next_open=self.frame.index[-1].tz_localize(BERLIN)
        if self.cursor+1>=len(self.frame) or next_open<self.clock:
            raise ValueError('Следующая свеча отсутствует')
        self.pending=(side,quantity,self.clock)

    def step(self):
        if self.cursor+1>=len(self.frame):
            return False
        self.cursor+=1
        if self.pending:
            side,qty,requested=self.pending
            row=self.frame.iloc[self.cursor]
            opening=self.frame.index[self.cursor]
            timestamp=opening.isoformat()
            if self.frame.attrs['breaks'][self.cursor]:
                self.pending=None
                self.history.append((timestamp,side,qty,'—','Пропуск в истории'))
            elif opening.tz_localize(BERLIN)>=requested:
                self.pending=None
                sign=1 if side=='BUY' else -1
                price=money(Decimal(str(row.open))*(1+sign*self.half))
                debit=price*qty*sign+self.fee
                if debit<=self.cash:
                    self.cash-=debit
                    self.quantity+=sign*qty
                    self.history.append((timestamp,side,qty,str(price),'Исполнено'))
                else:
                    self.history.append((timestamp,side,qty,str(price),'Недостаточно средств'))
        return True


def save_training(store,record):
    with store.connect() as db:
        db.execute('BEGIN IMMEDIATE')
        rows=setting(db,TRAINING_KEY,[])
        rows=[r for r in rows if r['id']!=record['id']]
        db.execute('INSERT OR REPLACE INTO settings VALUES (?,?)',
                   (TRAINING_KEY,json.dumps([*rows[-29:],record],ensure_ascii=False,allow_nan=False)))
