"""V2-only official GETTEX execution provenance; no feed or archive writes."""
import pandas as pd


def identity(info):
    return dict(source='gettex',symbol=info['isin'],venue='GETTEX',currency='EUR',timezone='Europe/Berlin')


def new_slots(db,codes,previous,now):
    """Scheduling hint only; the shared executor still checks the actual row.

    A new eligible official close must not wait behind the archive's entire
    stale universe. Once evaluated, it loses this boost for that close.
    """
    if not codes:return set()
    local=pd.Timestamp(now,unit='s',tz='UTC').tz_convert('Europe/Berlin').tz_localize(None)
    upper=local.floor('15min')-pd.Timedelta(minutes=15)
    lower=local-pd.Timedelta(minutes=30)
    found=db.execute('SELECT isin,MAX(ts) FROM candles WHERE source=? AND base_tf=? '
        'AND ts>=? AND ts<=? AND isin IN ('+','.join('?' for _ in codes)+') GROUP BY isin',
        ('gettex','15m',str(lower),str(upper),*codes)).fetchall()
    boosted=set()
    for code,stamp in found:
        end=(pd.Timestamp(stamp)+pd.Timedelta(minutes=15)).tz_localize('Europe/Berlin')
        if end.minute%30==0 and previous.get(code,{}).get('at',0)<end.timestamp():boosted.add(code)
    return boosted


# Only instrument-specific admission failures can release the next candidate.
# Temporary database, revision or application errors leave the leader eligible.
V2_LOCAL_REJECTIONS = frozenset({
    'V2_CASH_OR_RISK', 'V2_REWARD_AFTER_COSTS', 'V2_PRICE_OR_QUOTE_CHANGED',
    'PAPER_STALE_QUOTE', 'PAPER_NO_CLOSED_BAR', 'PAPER_AUTO_IDENTITY_BLOCKED',
    'PAPER_NO_LISTING_REVISION', 'PAPER_AUTO_QUOTE_IDENTITY_BLOCKED',
    'PAPER_AUTO_IDENTITY_CHANGED',
})


def ranked_candidate(queue, previous, allowed=None):
    """Return the first untried candidate for this exact quote and model."""
    for code in queue['candidates']:
        if allowed is not None and code not in allowed:
            continue
        row = previous.get(code, {})
        if (row.get('v2_queue_key') == queue['key']
                and row.get('v2_attempted_isin') == code
                and row.get('v2_candidate_rejected') is True):
            continue
        return code
    return None


def candidate_rejected(report):
    if report.get('state') != 'BLOCKED' or not report.get('v2_attempted_isin'):
        return False
    blocker = report.get('execution_blocker') or report.get('quote_blocker')
    if blocker:
        return blocker.split(': ')[-1] in V2_LOCAL_REJECTIONS
    return report.get('core') in ('DATA BLOCKED', 'DATA_BLOCKED')


def ranked_slots(codes,previous,now):
    """Route an already ranked basket winner to its own full guarded CORE job."""
    ranked=set()
    allowed=set(codes)
    for status in previous.values():
        queue = status.get('v2_queue')
        if queue:
            stamp = pd.Timestamp(queue['quote_at']).timestamp()
            if 0 <= now-stamp <= 900:
                candidate = ranked_candidate(queue, previous, allowed)
                row = previous.get(candidate, {})
                waiting_for_leader = (row.get('v2_queue_key') == queue['key']
                                      and row.get('reason') == 'V2_HIGHER_RANKED_STOCK')
                if candidate and (waiting_for_leader or row.get('next_check', 0) <= now):
                    ranked.add(candidate)
            continue
        code=status.get('v2_ranked_isin');stamp=status.get('quote_at')
        if (code not in codes or status.get('reason')!='V2_HIGHER_RANKED_STOCK' or not stamp
                or status.get('v2_market_source')!='gettex'):continue
        close=pd.Timestamp(stamp).timestamp();at=status.get('at',0)
        if (0<=now-close<=900 and close<=at<=now and previous.get(code,{}).get('at',0)<at):ranked.add(code)
    return ranked


def rows(db,code,meta,start,end):
    if meta==identity({'isin':code}):
        return db.execute('SELECT ts,open,high,low,close,volume FROM candles WHERE '
            'isin=? AND source=? AND base_tf=? AND ts>=? AND ts<=? ORDER BY ts',
            (code,'gettex','15m',start,end)).fetchall()
    return db.execute('SELECT ts,open,high,low,close,volume FROM series WHERE '
        'isin=? AND source=? AND venue=? AND currency=? AND symbol=? AND base_tf=? '
        'AND ts>=? AND ts<=? ORDER BY ts',
        (code,meta['source'],meta['venue'],meta['currency'],meta['symbol'],'15m',start,end)).fetchall()


def input_current(db,proof):
    from paper_hourly import digest
    code,meta,start,end,expected=proof
    return digest(rows(db,code,meta,start,end))==expected


def validate_inputs(db,result):
    if not all(input_current(db,proof) for proof in result.get('hourly',{}).get('v2_inputs',{}).values()):
        raise ValueError('V2_30M_MARKET_INPUT_CHANGED')


def fresh_quote(store,info,analysis_identity,now):
    """Choose one factual source. Never relabel official rows as archive rows."""
    import data_quality as dq
    from analytics import bar_ends
    from eur_listings import stored_identity
    from paper_account import quote
    meta=identity(info)
    with store.connect() as db:
        store.read_cache.depend('market:'+info['isin']+':15m')
        latest=db.execute('SELECT ts,open,high,low,close,volume FROM candles WHERE '
            'isin=? AND source=? AND base_tf=? ORDER BY ts DESC LIMIT 4',
            (info['isin'],'gettex','15m')).fetchall()
        candidates=[]
        for stamp,*values in latest:
            frame=pd.DataFrame([values],columns=dq.FIELDS,index=pd.DatetimeIndex([pd.Timestamp(stamp)]))
            frame.attrs=dict(meta,timeframe='15m')
            end=bar_ends(frame)[0]
            if pd.notna(end) and end<=now:candidates.append((end,frame))
        if candidates:
            end,frame=max(candidates,key=lambda c:c[0])
            try:
                bar={**{key:float(frame.iloc[0][key]) for key in dq.FIELDS},'closed':True,
                    'open_at':frame.index[0].tz_localize(meta['timezone']).isoformat(),'close_at':end.isoformat()}
                result=dict(isin=info['isin'],identity=meta,timeframe='15m',bars=[bar],
                    data_quality=dq.audit(frame,'15m',meta,now))
                quote(info,result,now.timestamp(),stored_listing=stored_identity(db,info,meta,'15m'))
                return result
            except (ValueError,TypeError):
                pass
    from paper_hourly import fresh_quote as archive_quote
    return archive_quote(store,info,analysis_identity,now)


def bound_identity(db,info,result):
    """Final exact-row check, restricted to the new autonomous V2 model."""
    q=result.get('execution_quote',{});origin=q.get('identity',{})
    analysis=result.get('identity',{})
    if origin==analysis:return origin
    from paper_account import result_portfolio
    from paper_hourly import held_position
    from paper_v2_30m import MODEL
    from eur_listings import stored_identity
    model=(result.get('execution_context',{}).get('trading_engine',{}).get('v2_30m') or {}).get('model')
    held=held_position(db,info['isin'],'V2') or {}
    if (result_portfolio(db,result)!='V2' or result.get('timeframe')!='30m' or result.get('discretionary')
            or MODEL not in (model,(held.get('auto_entry') or {}).get('strategy_model'))
            or origin!=identity(info) or q.get('isin')!=info['isin'] or q.get('timeframe')!='15m'
            or analysis.get('source')!='gettex_chart' or analysis.get('venue')!='GETTEX'
            or analysis.get('currency')!='EUR' or analysis.get('timezone')!='Europe/Berlin'
            or not stored_identity(db,info,analysis,'15m') or not stored_identity(db,info,origin,'15m')):
        raise ValueError('PAPER_AUTO_QUOTE_IDENTITY_BLOCKED')
    bars=q.get('bars') or []
    if len(bars)!=1:raise ValueError('V2_OFFICIAL_QUOTE_UNCONFIRMED')
    bar=bars[0]
    start=pd.Timestamp(bar.get('open_at'))
    if pd.isna(start) or start.tzinfo is None:raise ValueError('V2_OFFICIAL_QUOTE_UNCONFIRMED')
    if pd.Timestamp(bar.get('close_at'))!=start+pd.Timedelta(minutes=15):
        raise ValueError('V2_OFFICIAL_QUOTE_UNCONFIRMED')
    stamp=str(start.tz_convert('Europe/Berlin').tz_localize(None))
    saved=rows(db,info['isin'],origin,stamp,stamp)
    expected=tuple(bar.get(key) for key in ('open','high','low','close','volume'))
    if len(saved)!=1 or tuple(saved[0][1:])!=expected:
        raise ValueError('V2_OFFICIAL_QUOTE_CHANGED')
    return origin
