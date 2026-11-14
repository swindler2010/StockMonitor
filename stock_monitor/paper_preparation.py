"""One sequential PAPER candidate on the existing hourly worker and CP queue.

Preparation is analysis only. Promotion requires a current closed-input proof,
existing owner risk receipts, benchmark and unchanged epoch. Closed-session
archive admission is separate from current quote permission to execute.
It neither creates a risk grant nor places an order.
"""
import copy
import json
import time

VERSION='sequential_ready_gettex_v1'
STATE='paper_auto_preparation'
RECEIPT='paper_auto_admission:'


def policy(db):
    from paper_hourly import CONFIG,read,authority
    cfg=read(db,CONFIG,{})
    p=cfg.get('automatic_admission',{})
    if (p.get('version')!=VERSION or p.get('enabled') is not True
            or p.get('approved') is not True or p.get('scope')!='registered_eur_stocks'
            or not p.get('prerequisites') or not cfg.get('identities')):
        raise ValueError('PAPER_PREPARATION_NOT_AUTHORIZED')
    # Reuse the original epoch/account/sizing/identity authority, including the
    # explicitly enabled pair. No alternate execution permission is minted.
    for isin in p['prerequisites']:authority(db,isin)
    return cfg,p


def write(db,key,value):
    db.execute('INSERT OR REPLACE INTO settings VALUES (?,?)',(key,json.dumps(value,ensure_ascii=False)))


def candidate_authority(db,isin,identity=None):
    from paper_hourly import read
    from paper_account import registered_instrument
    from eur_listings import stored_identity
    from data_quality import IDENTITY
    cfg,_=policy(db);pending=read(db,STATE,{})
    exact=pending.get('identity',{})
    if (pending.get('isin')!=isin or pending.get('epoch')!=cfg['epoch']
            or pending.get('state')=='ADMITTED' or isin in cfg['identities']
            or set(exact)!=set(IDENTITY) or exact.get('source')!='gettex_chart'
            or exact.get('venue')!='GETTEX' or exact.get('currency')!='EUR'
            or exact.get('timezone')!='Europe/Berlin'
            or (identity is not None and identity!=exact)):
        raise ValueError('PAPER_PREPARATION_IDENTITY_BLOCKED')
    info=registered_instrument(db,isin)
    if not stored_identity(db,info,exact,'15m'):
        raise ValueError('PAPER_PREPARATION_LISTING_UNCONFIRMED')
    return cfg,exact


def retry_inputs(db,isin,identity):
    """Cheap wake-up token, not an analytical or execution proof."""
    from paper_hourly import CONFIG
    scopes=['instrument:'+isin,'setting:'+CONFIG,'setting:paper_epoch',
        'setting:cp_manual_risk:'+isin,'setting:interval_evidence:'+isin]
    for base in ('5m','15m','1D'):
        scopes.extend(['market:'+isin+':'+base,
            'setting:data_quality:'+isin+':'+str(identity.get('symbol',''))+':'+base])
    # Source receipts update even without new candles. Only semantic input
    # revisions (market/DQ/policy) wake the calculation, not HTTP timestamps.
    rows=dict(db.execute('SELECT scope,revision FROM input_revisions WHERE scope IN ('+
        ','.join('?' for _ in scopes)+')',scopes))
    return {s:rows.get(s,0) for s in scopes}


def choose(db,now):
    """Persist one candidate and its retry time. Called off the UI thread."""
    import pandas as pd
    from candle_schedule import boundary
    from paper_hourly import read,STATUS
    from paper_account import registered_instrument
    cfg,p=policy(db);old=read(db,STATE,{})
    active=old.get('isin') if old.get('epoch')==cfg['epoch'] else None
    if active and active not in cfg['identities']:
        try:registered_instrument(db,active)
        except ValueError:active=None
    else:active=None
    if old.get('epoch')==cfg['epoch'] and old.get('next_check',0)>now:
        if not active or old.get('retry_inputs')==retry_inputs(db,active,old.get('identity',{})):return None
    if active is None:
        # Pair stability is a scheduling condition, never a permission proof.
        for isin in p['prerequisites']:
            s=read(db,STATUS+isin,{})
            closed=not boundary(cfg['identities'][isin],'15m',
                pd.Timestamp(now,unit='s',tz='UTC'),delay_seconds=0)['session_open']
            if not closed and (s.get('core')!='READY' or s.get('quote_blocker') or not s.get('quote_at')
                    or not 0<=now-s.get('at',0)<=180):return None
        candidates=[]
        for isin,payload in db.execute('SELECT isin,payload FROM instruments ORDER BY isin'):
            if isin in cfg['identities']:continue
            try:info=registered_instrument(db,isin)
            except ValueError:continue
            candidates.append(info)
        rank={isin:n for n,isin in enumerate(p.get('priority',[]))}
        info=min(candidates,key=lambda i:(rank.get(i['isin'],len(rank)),i['isin']),default=None)
        if info is None:return None
    else:info=registered_instrument(db,active)
    isin=info['isin'];saved=read(db,'gettex_chart:'+isin+':15m',{})
    item=dict(isin=isin,name=info.get('name',isin),epoch=cfg['epoch'],
        identity=saved.get('identity',{}),state='PREPARING',execution_permission=False,
        requested_at=old.get('requested_at',now) if active else now,
        retry_inputs=retry_inputs(db,isin,saved.get('identity',{})),
        at=now,next_check=now+300,reason='Проверка закрытых 1H/4H/1D; подключение отдельно от исполнения; сделок нет')
    write(db,STATE,item)
    return info


def closed_session_archive(db,info,identity,result,now):
    """Validate the saved final session bar at real now, never backdate a quote."""
    import pandas as pd
    import data_quality as dq
    from candle_schedule import boundary
    from analytics import bar_ends
    from performance_cache import primary_closed_series
    clock=pd.Timestamp(now,unit='s',tz='UTC')
    due=boundary(identity,'15m',clock,delay_seconds=0)
    if due['session_open'] or not due.get('close_at'):
        raise ValueError('PAPER_PREPARATION_REQUIRES_CLOSED_SESSION')
    end=pd.Timestamp(due['close_at'])
    # This is the final bar of the latest completed session, not a lunch break
    # or an arbitrary earlier bar which happens to be closed.
    daily=boundary(identity,'1D',clock,delay_seconds=0)
    if daily.get('close_at')!=due['close_at']:
        raise ValueError('PAPER_PREPARATION_SESSION_NOT_COMPLETE')
    token=result.get('_input_revisions',{})
    if not {'market:'+info['isin']+':15m',
            'setting:gettex_chart:'+info['isin']+':15m'}<=set(token):
        raise ValueError('PAPER_PREPARATION_REVISIONS_REQUIRED')
    row=db.execute('SELECT ts,open,high,low,close,volume FROM series WHERE '
        'isin=? AND source=? AND venue=? AND currency=? AND symbol=? AND base_tf=? '
        'ORDER BY ts DESC LIMIT 1',(info['isin'],identity['source'],identity['venue'],
            identity['currency'],identity['symbol'],'15m')).fetchone()
    if row is None:raise ValueError('PAPER_PREPARATION_SESSION_TAIL_MISSING')
    frame=pd.DataFrame([row[1:]],columns=dq.FIELDS,index=pd.DatetimeIndex([pd.Timestamp(row[0])]))
    frame.attrs=dict(identity,timeframe='15m')
    if bar_ends(frame)[0]!=end:
        raise ValueError('PAPER_PREPARATION_SESSION_TAIL_MISSING')
    quality=dq.audit(frame,'15m',identity,clock)
    if quality['state']!='READY':raise ValueError('PAPER_PREPARATION_TAIL_'+quality['state'])
    own=primary_closed_series(result['hourly']['proof'],info['isin'],identity)
    if not any(s['spec'].get('timeframe')=='1D' and pd.Timestamp(s['spec']['through'])==end for s in own):
        raise ValueError('PAPER_PREPARATION_DAILY_TAIL_MISSING')
    return dict(basis='CLOSED_SESSION_ARCHIVE',session_close=end.isoformat(),
                checked_at=clock.isoformat(),tail_at=str(row[0]),tail_quality=quality['state'],
                execution_blocker='PAPER_SESSION_CLOSED')


def admit(db,info,result,now):
    """Within BEGIN IMMEDIATE. Recheck inputs; add authority, never a fill."""
    from paper_hourly import CONFIG,read,validate_analysis,compact,digest
    from paper_account import quote
    from eur_listings import stored_identity
    cfg,identity=candidate_authority(db,info['isin'],result.get('identity'))
    if (result.get('isin')!=info['isin'] or result.get('timeframe')!='1H'
            or result.get('execution_context',{}).get('isin')!=info['isin']):
        raise ValueError('PAPER_PREPARATION_CONTEXT_CHANGED')
    if result.get('hourly',{}).get('preparation_only') is not True:
        raise ValueError('PAPER_PREPARATION_PROOF_REQUIRED')
    token=result.get('_input_revisions',{})
    required={'instrument:'+info['isin'],'setting:'+CONFIG,'setting:paper_epoch','setting:'+STATE}
    if not required<=set(token) or not any(k.startswith('market:'+info['isin']+':') for k in token):
        raise ValueError('PAPER_PREPARATION_REVISIONS_REQUIRED')
    for key,revision in token.items():
        current=db.execute('SELECT revision FROM input_revisions WHERE scope=?',(key,)).fetchone()
        if (current[0] if current else 0)!=revision:raise ValueError('PAPER_STALE_INPUTS')
    validate_analysis(db,info,result,now,cfg)
    admission=dict(basis='CURRENT_QUOTE')
    if result.get('quote_blocker')=='PAPER_STALE_QUOTE':
        admission=closed_session_archive(db,info,identity,result,now)
    else:
        if result.get('quote_blocker'):raise ValueError(result['quote_blocker'])
        q=result.get('execution_quote',{})
        if q.get('identity')!=identity or q.get('isin')!=info['isin']:
            raise ValueError('PAPER_AUTO_QUOTE_IDENTITY_BLOCKED')
        if 'setting:gettex_chart:'+info['isin']+':'+str(q.get('timeframe')) not in token:
            raise ValueError('PAPER_NO_LISTING_REVISION')
        quote(info,q,now,stored_listing=stored_identity(db,info,identity,q.get('timeframe')))
    report={**read(db,STATE,{}),**compact(result), 'state':'ADMITTED','at':now,
        'reason':'Данные проверены; деморежим подключён автоматически. Исполнение проверяется заново.',
        'admission':admission,
        'execution_permission':False,'proof_digest':digest(result['hourly']['proof']),
        'input_revisions':token,'next_check':now+300}
    cfg=copy.deepcopy(cfg);cfg['identities'][info['isin']]=identity
    cfg.setdefault('names',{})[info['isin']]=info.get('name',info['isin'])
    write(db,CONFIG,cfg);write(db,RECEIPT+info['isin'],report);write(db,STATE,report)
    return report


def maintenance(db):
    """The candidate gets CP's existing bounded maintenance slot, not orders."""
    from paper_hourly import read
    pending=read(db,STATE,{})
    try:
        _,identity=candidate_authority(db,pending.get('isin'))
        return pending['isin'],identity
    except (ValueError,KeyError):return None


def turn(app):
    """Run on the existing trading lane, at most one candidate per turn."""
    from paper_hourly import read,compact
    if app.closing or app.user_dirty:return
    with app.store.connect() as db:
        db.execute('BEGIN IMMEDIATE')
        try:info=choose(db,time.time())
        except ValueError:return
    if info is None:return
    result=None
    try:
        values=dict(watchlist=[copy.deepcopy(info)],drafts={},feed_errors={},symbol=info['name'],
            timeframe='1H',period='Max',ai_config=copy.deepcopy(app.ai_config),_paper_prepare=True,
            instrument_revisions=app.store.read_cache.token_for(['instrument:'+info['isin']]))
        result=app.compute_trading.calculate('paper_hourly',values)
        if app.closing or app.user_dirty:raise ValueError('PAPER_APP_NOT_READY')
        with app.store.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            return admit(db,info,result,time.time())
    except Exception as error:
        with app.store.connect() as db:
            db.execute('BEGIN IMMEDIATE');old=read(db,STATE,{})
            if old.get('isin')!=info['isin'] or old.get('state')=='ADMITTED':return
            old.update(compact(result) if result else {})
            if result:old['analysis_reason']=result.get('execution_context',{}).get('trading_engine',{}).get('reason')
            old.update(state='WAITING',at=time.time(),next_check=time.time()+300,
                execution_permission=False,reason=str(error),
                message='Допуск пока закрыт; CandlePilot продолжает штатное восстановление данных')
            write(db,STATE,old)
        return old
