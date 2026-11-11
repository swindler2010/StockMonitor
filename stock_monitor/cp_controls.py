"""Owner-reviewed candle risks and requests for the existing CP writer.

No market-data rewriting, signal generation, broker transport or second queue.
"""
import hashlib,json
from datetime import datetime,timezone

IDENTITY=('source','symbol','venue','currency','timezone')

def stamp():return datetime.now(timezone.utc).isoformat()
def digest(value):return hashlib.sha256(json.dumps(value,sort_keys=True,ensure_ascii=False,default=str).encode()).hexdigest()
def read(db,key,default=None):
    row=db.execute('SELECT value FROM settings WHERE key=?',(key,)).fetchone()
    return json.loads(row[0]) if row else default
def write(db,key,value):db.execute('INSERT OR REPLACE INTO settings VALUES (?,?)',(key,json.dumps(value,ensure_ascii=False,allow_nan=False)))
def repair_id(problem):return digest({k:problem.get(k) for k in ('isin','kind','identity','tf','range','report_key')})

def problems(db,isin):
    """Saved reports only. IDs bind the named defect, not unrelated new bars."""
    import data_quality as dq
    rows=[]
    def add(kind,meta,tf,state,reasons,span=None,key=None,count=None):
        identity={k:meta.get(k) for k in IDENTITY}
        if not identity['timezone']:identity['timezone']=dq.TIMEZONES.get(dq.CALENDARS.get(identity['venue']))
        item=dict(isin=isin,kind=kind,identity=identity,tf=tf,state=state,reasons=reasons,range=span,report_key=key)
        binding={k:v for k,v in item.items() if k!='reasons' or not span}
        # Ranged problems have a stable ID when a DIFFERENT gap is added.
        if kind=='context':binding['count']=count
        item['id']=digest(binding);rows.append(item)
    for key,payload in db.execute('SELECT key,value FROM settings WHERE key LIKE ?',('data_quality:'+isin+':%',)):
        q=json.loads(payload);state=q.get('state','UNKNOWN')
        if state=='READY':continue
        for span in q.get('ranges') or [None]:
            add('archive',q.get('identity',{}),q.get('base_tf',key.rsplit(':',1)[-1]),state,q.get('issues',[]),span,key)
    record=read(db,'cp_eligibility:'+isin,{})
    for tf,row in record.get('timeframes',{}).items():
        for reason in row.get('blockers',[]):
            add('context',row.get('identity',{}),tf,'ANALYSIS_BLOCKED',[reason],count=row.get('count'))
    return list({r['id']:r for r in rows}.values())

def view(db,isin):
    grants=read(db,'cp_manual_risk:'+isin,{'accepted':{}})
    accepted=effective_accepted(db,isin,grants)
    requests=read(db,'cp_repair_requests:'+isin,{})
    return [dict(p,accepted=p['id'] in accepted,accepted_by=accepted.get(p['id'],{}).get('actor'),
                 acceptance_evidence=accepted.get(p['id'],{}).get('evidence'),repair=requests.get(repair_id(p)),
                 historical_review=grants.get('historical_review',{}).get('results',{}).get(p['id'])) for p in problems(db,isin)]

def gap_price_estimate(db,problem,now=None,memo=None):
    """Read-only estimate for a saved missing range, never an OHLCV row.

    Called on one bounded health page inside its consistent read transaction.
    Uses nearest closes; a bad neighbour is not skipped to manufacture a value.
    Original reports, recovery, analytical inputs and execution stay untouched.
    """
    import pandas as pd
    import data_quality as dq
    from eur_listings import stored_identity
    from timeframes import endpoints
    clock=pd.Timestamp(now or stamp());memo={} if memo is None else memo
    def cached(key,load):
        if key not in memo:memo[key]=load()
        return memo[key]
    def absent(reason):return {'state':'UNAVAILABLE','reason':reason,'display_only':True}
    if problem.get('kind')!='archive' or not problem.get('range'):return absent('Нет точного исторического пропуска')
    meta=problem.get('identity',{});base=problem.get('tf');isin=problem.get('isin')
    if base not in ('5m','15m','1H','1D'):return absent('Нет подтверждённого самостоятельного базового ряда')
    if meta.get('currency')!='EUR' or not all(meta.get(k) for k in IDENTITY):return absent('Нужна точная EUR identity')
    report=cached(('report',problem['report_key']),lambda:read(db,problem['report_key'],{}))
    if (report.get('state') not in ('GAP','SOURCE_GAP','UNCONFIRMED') or report.get('identity')!=meta
            or problem['range'] not in report.get('ranges',[])):return absent('Пропуск изменился или исходные данные непригодны')
    row=cached(('info',isin),lambda:db.execute('SELECT payload FROM instruments WHERE isin=?',(isin,)).fetchone())
    if not row:return absent('Инструмент отсутствует')
    info=json.loads(row[0]);binding=tuple(meta[k] for k in IDENTITY)
    proof=cached(('proof',isin,binding,base),lambda:stored_identity(db,info,meta,base))
    if not proof:return absent('Листинг не подтверждён')
    try:
        begin,end=map(pd.Timestamp,problem['range'])
        if begin.tzinfo is not None or end.tzinfo is not None or begin>end or clock.tzinfo is None:return absent('Неподтверждённое время диапазона')
        if meta['source']=='gettex':
            table='candles';where='isin=? AND source=? AND base_tf=?';params=(isin,'gettex',base)
        else:
            table='series';where='isin=? AND source=? AND venue=? AND currency=? AND symbol=? AND base_tf=?'
            params=(isin,meta['source'],meta['venue'],'EUR',meta['symbol'],base)
        if db.execute('SELECT 1 FROM '+table+' WHERE '+where+' AND ts>=? AND ts<=? LIMIT 1',(*params,str(begin),str(end))).fetchone():
            return absent('В диапазоне уже есть строки; оценка не заменяет реальные или повреждённые OHLCV')
        columns='ts,open,high,low,close,volume'
        previous=db.execute('SELECT '+columns+' FROM '+table+' WHERE '+where+' AND ts<? ORDER BY ts DESC LIMIT 1',(*params,str(begin))).fetchone()
        following=db.execute('SELECT '+columns+' FROM '+table+' WHERE '+where+' AND ts>? ORDER BY ts LIMIT 1',(*params,str(end))).fetchone()
        if previous is None or following is None:return absent('Нет обеих соседних реальных свечей')
        neighbours=[]
        for row in (previous,following):
            key=('neighbour',binding,base,tuple(row))
            def validate():
                frame=pd.DataFrame([row[1:]],columns=dq.FIELDS,index=pd.to_datetime([row[0]]));frame.attrs={**meta,'timeframe':base}
                audit=dq.audit(frame,base,expected=meta,now=clock,check_stale=False)
                closed=endpoints(frame)[0]
                if audit['state']!='READY' or audit.get('non_candle_markers') or pd.isna(closed) or closed>clock:return None
                return {'timestamp':row[0],'close':float(row[4]),'closed_at':closed.isoformat()}
            checked=cached(key,validate)
            if checked is None:return absent('Соседняя свеча повреждена, не закрыта или не подтверждена календарём')
            neighbours.append(checked)
        value=neighbours[0]['close']/2+neighbours[1]['close']/2
        scopes=['instrument:'+isin,'market:'+isin+':'+base,'setting:'+problem['report_key'],'setting:history_identity:'+isin]
        if meta['source']=='gettex_chart':
            from gettex_chart import state_key
            scopes.append('setting:'+state_key(isin,base))
        token=cached(('revision',tuple(scopes)),lambda:_revisions(db,scopes))
        return {'state':'ESTIMATED','method':'MEAN_NEIGHBOUR_CLOSE','value':value,'currency':'EUR',
            'isin':isin,'identity':meta,'timeframe':base,'range':problem['range'],'neighbours':neighbours,
            'available_after':neighbours[1]['closed_at'],'evaluated_at':clock.isoformat(),'input_revisions':token,
            'display_only':True,'usable_for_execution':False,'usable_for_indicators':False,
            'label':'Оценка пропуска по соседним Close; не биржевая свеча и не подтверждение цены'}
    except (ValueError,TypeError,KeyError,OverflowError):return absent('Не удалось подтвердить соседние цены')

def gap_estimate_text(estimate):
    if not estimate or estimate.get('state')!='ESTIMATED':
        return 'Оценка средней недоступна: '+(estimate or {}).get('reason','Нет подтверждённых соседей')+'\n'
    a,b=estimate['neighbours']
    return (f"Оценка пропущенного участка: ({a['close']:g} + {b['close']:g}) / 2 = {estimate['value']:g} EUR.\n"
            f"Соседи: {a['timestamp']} и {b['timestamp']}. Доступна после {estimate['available_after']}.\n"
            'Это оценка Close, не настоящая OHLCV-свеча; DQ и цена исполнения не изменены.\n')

def accept(store,isin,reviewed,all_current=False,review_digest=None,initialization_policy=None,expected_revisions=None):
    """Local UI confirmation → atomic compare of the exact reviewed problem list."""
    from paper_account import registered_instrument,ACCOUNT
    ids={p['id'] for p in reviewed}
    if not ids and not review_digest:raise ValueError('Нет выбранных проблем')
    with store.connect() as db:
        db.execute('BEGIN IMMEDIATE');registered_instrument(db,isin)
        current={p['id']:p for p in problems(db,isin)}
        if expected_revisions and _revisions(db,expected_revisions)!=expected_revisions:
            raise ValueError('RISK_INPUT_REVISIONS_CHANGED')
        if initialization_policy is not None and not demo_initialization_policy(initialization_policy,isin):
            raise ValueError('DEMO_INITIALIZATION_POLICY_UNCONFIRMED')
        if all_current and review_digest:
            if digest(sorted(current))!=review_digest:raise ValueError('Список проблем изменился. Просмотрите его заново')
            ids=set(current)
        if not ids<=current.keys() or (all_current and ids!=current.keys()):
            raise ValueError('Список проблем изменился. Просмотрите его заново')
        old=read(db,'cp_manual_risk:'+isin,{'version':1,'account':ACCOUNT,'accepted':{},'events':[]})
        at=stamp()
        for key in sorted(ids):old['accepted'][key]=dict(problem=current[key],accepted_at=at,actor='owner_local_ui')
        if initialization_policy is not None:old['initialization_policy']=initialization_policy
        old.setdefault('events',[]).append(dict(action='ACCEPT_ALL_CURRENT' if all_current else 'ACCEPT_PROBLEM',at=at,ids=sorted(ids)))
        write(db,'cp_manual_risk:'+isin,old)
    return {'accepted':len(ids),'at':at}

def revoke(store,isin):
    with store.connect() as db:
        db.execute('BEGIN IMMEDIATE');old=read(db,'cp_manual_risk:'+isin,{'version':1,'accepted':{},'events':[]})
        old['accepted']={};old.setdefault('events',[]).append(dict(action='REVOKE_ALL',at=stamp()))
        write(db,'cp_manual_risk:'+isin,old)
        policy=read(db,AUTO_POLICY,{})
        if policy.get('enabled'):
            policy['excluded_isins']=sorted(set(policy.get('excluded_isins',[]))|{isin})
            write(db,AUTO_POLICY,policy)
        tv=read(db,TV_POLICY,{})
        if tv.get('enabled'):
            tv['excluded_isins']=sorted(set(tv.get('excluded_isins',[]))|{isin})
            write(db,TV_POLICY,tv)
        reviewed=read(db,REVIEWED_PREFIX+isin,{})
        if reviewed.get('approved'):
            reviewed['approved']=False
            write(db,REVIEWED_PREFIX+isin,reviewed)


AUTO_POLICY='cp_auto_risk_policy'
AUTO_VERSION='proven_source_limit_demo_v1'
AUTO_ACTOR='candlepilot_owner_policy'
TV_POLICY='cp_tv_risk_policy'
TV_VERSION='owner_tv_matching_absence_demo_v1'
TV_ACTOR='candlepilot_owner_tv_policy'
TV_LABEL='Подобная проблема в TV; исторический риск принят владельцем'
TV_PRICE_MODEL='actual_closed_bars_only_v1'
REVIEWED_PREFIX='cp_reviewed_gap_policy:'
REVIEWED_VERSION='owner_scoped_actual_bars_v1'
REVIEWED_ACTOR='candlepilot_owner_scoped_review'
REVIEWED_LABEL='Демо: владелец принял конкретное историческое расхождение; расчёт по настоящим свечам'


def _reviewed_binding(db,info,problem):
    """Bind the reviewed neighbourhood, not unrelated newly arriving candles."""
    import pandas as pd
    from eur_listings import stored_identity
    p=problem;meta=p['identity'];base=p['tf'];a,b=map(pd.Timestamp,p['range'])
    if (p['kind']!='archive' or meta.get('currency')!='EUR' or meta.get('venue')!='GETTEX'
            or meta.get('source')!='gettex_chart' or base!='15m'
            or pd.isna(a) or pd.isna(b) or a.tzinfo is not None or b.tzinfo is not None
            or a>b or a.floor('15min')!=a or b.floor('15min')!=b):
        raise ValueError('REVIEW_SCOPE_UNSUPPORTED')
    proof=stored_identity(db,info,meta,base)
    if not proof:raise ValueError('REVIEW_IDENTITY_UNCONFIRMED')
    row=db.execute('SELECT value FROM settings WHERE key=?',(p['report_key'],)).fetchone()
    report=json.loads(row[0]) if row else {}
    if report.get('state') not in ('GAP','UNCONFIRMED','SOURCE_GAP') or report.get('identity')!=meta:
        raise ValueError('REVIEW_DQ_BLOCKED')
    rows=db.execute('SELECT ts,open,high,low,close,volume FROM series WHERE isin=? AND source=? AND symbol=? AND venue=? AND currency=? AND base_tf=? AND ts>=? AND ts<? ORDER BY ts',
        (info['isin'],meta['source'],meta['symbol'],meta['venue'],meta['currency'],base,str(a-pd.Timedelta(minutes=15)),str(b+pd.Timedelta(minutes=75)))).fetchall()
    # Bind the exact consecutive range already present in the saved report and
    # explicitly approved by its owner. A recovered row anywhere inside it
    # invalidates this receipt; never keep a blanket waiver over changed data.
    if not rows or any(a<=pd.Timestamp(r[0])<=b for r in rows):raise ValueError('REVIEW_NO_LONGER_MISSING')
    # For a missing final session bar the following real candle can be on the
    # next trading day. Include it even outside the short clock-time window.
    # Ordinary existing bindings are unchanged when it is already in rows.
    following=db.execute('SELECT ts,open,high,low,close,volume FROM series WHERE isin=? AND source=? AND symbol=? AND venue=? AND currency=? AND base_tf=? AND ts>? ORDER BY ts LIMIT 1',
        (info['isin'],meta['source'],meta['symbol'],meta['venue'],meta['currency'],base,str(b))).fetchone()
    if following and following[0] not in {r[0] for r in rows}:rows.append(following)
    return digest([info['isin'],meta,base,p['range'],rows])


def authorize_reviewed_gaps(store,isin,reviewed,evidence,expected_revisions):
    """Explicit owner authorization, exact saved scopes, no inferred grants."""
    from paper_account import ACCOUNT,registered_instrument
    if not reviewed or not evidence.get('sha256') or not evidence.get('owner_decision'):
        raise ValueError('REVIEW_AUTHORITY_REQUIRED')
    with store.connect() as db:
        db.execute('BEGIN IMMEDIATE');info=registered_instrument(db,isin)
        if not expected_revisions or _revisions(db,expected_revisions)!=expected_revisions:
            raise ValueError('RISK_INPUT_REVISIONS_CHANGED')
        current={p['id']:p for p in problems(db,isin)};entries={};identities=[]
        required={'instrument:'+isin,*('market:'+isin+':'+p['tf'] for p in reviewed),
                  *('setting:'+p['report_key'] for p in reviewed)}
        if not required<=set(expected_revisions):raise ValueError('REVIEW_DEPENDENCIES_REQUIRED')
        for p in reviewed:
            if current.get(p['id'])!=p:raise ValueError('REVIEW_PROBLEM_CHANGED')
            entries[p['id']]=dict(problem=p,native_binding=_reviewed_binding(db,info,p))
            identities.append(p['identity'])
        if any(v!=identities[0] for v in identities):raise ValueError('REVIEW_MIXED_IDENTITY')
        policy=dict(version=REVIEWED_VERSION,approved=True,account=ACCOUNT,isin=isin,
            identity=identities[0],entry_timeframes=['15m'],price_model=TV_PRICE_MODEL,
            entries=entries,evidence=evidence,authorized_at=stamp(),
            meaning='Owner risk, NOT NO_TRADES, factual READY, fabricated OHLCV or execution permission')
        write(db,REVIEWED_PREFIX+isin,policy)
    return {'authorized':len(entries),'version':REVIEWED_VERSION}


def reviewed_risk_turn(store,info,now=None):
    """Existing CP writer renews exact approved scopes or revokes changed ones."""
    import pandas as pd
    from paper_account import ACCOUNT
    isin=info['isin'];clock=pd.Timestamp(now or stamp())
    with store.connect() as db:
        db.execute('BEGIN IMMEDIATE');policy=read(db,REVIEWED_PREFIX+isin,{})
        if policy.get('version')!=REVIEWED_VERSION or not policy.get('approved') or policy.get('account')!=ACCOUNT:
            return {'state':'DISABLED'}
        info=read_instrument(db,isin)
        key='cp_manual_risk:'+isin;old=read(db,key,dict(version=1,account=ACCOUNT,accepted={},events=[]))
        previous=old['accepted'];accepted={k:v for k,v in previous.items() if v.get('actor')!=REVIEWED_ACTOR}
        current={p['id']:p for p in problems(db,isin)};outcomes={}
        for pid,entry in policy.get('entries',{}).items():
            p=current.get(pid)
            if p is None:outcomes[pid]='RESOLVED_OR_RECLASSIFIED';continue
            try:
                if p['identity']!=policy['identity'] or _reviewed_binding(db,info,p)!=entry['native_binding']:
                    raise ValueError('REVIEW_INPUT_CHANGED')
                scopes=['instrument:'+isin,'market:'+isin+':'+p['tf'],'setting:'+p['report_key'],
                    'setting:'+REVIEWED_PREFIX+isin,'setting:gettex_chart:'+isin+':'+p['tf']]
                accepted[pid]=dict(problem=p,actor=REVIEWED_ACTOR,
                    accepted_at=previous.get(pid,{}).get('accepted_at',clock.isoformat()),input_revisions=_revisions(db,scopes),
                    evidence=dict(status='OWNER_ACCEPTED_SCOPED_DISCREPANCY',price_model=TV_PRICE_MODEL,
                        policy_digest=digest(policy),native_binding=entry['native_binding'],label=REVIEWED_LABEL))
                outcomes[pid]='ACCEPTED_REAL_BARS_ONLY'
            except (ValueError,KeyError,TypeError) as error:outcomes[pid]=str(error)
        changed=accepted!=previous
        if changed:
            old['accepted']=accepted
            if set(accepted)!=set(previous):old.setdefault('events',[]).append(dict(action='OWNER_SCOPED_REVIEW',at=clock.isoformat(),ids=sorted(k for k in accepted if accepted[k]['actor']==REVIEWED_ACTOR)))
            write(db,key,old)
        result=dict(state='EVALUATED',outcomes=outcomes,accepted=sum(v['actor']==REVIEWED_ACTOR for v in accepted.values()),changed=changed)
        if changed:result['analysis_requested_at']=clock.isoformat()
        return result


def read_instrument(db,isin):
    row=db.execute('SELECT payload FROM instruments WHERE isin=?',(isin,)).fetchone()
    if not row:raise ValueError('UNKNOWN_INSTRUMENT')
    return json.loads(row[0])


def reviewed_entry_identity(store,info,tf):
    """Same explicitly reviewed provider throughout this demo calculation."""
    isin=info['isin'];key=REVIEWED_PREFIX+isin
    store.read_cache.depend('setting:'+key);store.read_cache.depend('setting:cp_manual_risk:'+isin)
    policy=store.setting(key,{})
    if tf not in policy.get('entry_timeframes',[]):return None
    grant=store.setting('cp_manual_risk:'+isin,{})
    for entry in grant.get('accepted',{}).values():
        if entry.get('actor')==REVIEWED_ACTOR:
            for scope in entry.get('input_revisions',{}):store.read_cache.depend(scope)
    with store.connect() as db:
        effective=effective_accepted(db,isin,grant)
    return policy.get('identity') if any(v.get('actor')==REVIEWED_ACTOR for v in effective.values()) else None


def _tv_enabled(db,isin):
    policy=read(db,TV_POLICY,{})
    return (policy.get('enabled') is True and policy.get('version')==TV_VERSION
            and policy.get('approved') is True and isin not in policy.get('excluded_isins',[]))


def _tv_native_binding(job,inputs,version=2):
    """Immutable reviewed rows/neighbours; refreshed evidence is checked anew."""
    from cp_reconciliation import _instrument_identity
    listing=dict(inputs['listing'])
    if isinstance(listing.get('listing_proof'),dict):
        listing['listing_proof']={k:v for k,v in listing['listing_proof'].items() if k!='verified_at'}
    return digest([job['isin'],job['identity'],job['tf'],job['first'],job['end'],
                   inputs['info'] if version==1 else _instrument_identity(inputs['info']),
                   listing,inputs['rows'],inputs['neighbors'],job['observation']])


def tv_risk_turn(store,info,now=None):
    """Owner policy on the existing feed; not a market-data certificate.

    Current exact-source ABSENT reviews cover only the named missing ranges.
    No TV prices, OHLCV writes or inferred NO_TRADES. Account universe and
    independent CORE, quote and execution gates remain intact.
    """
    import pandas as pd
    import cp_reconciliation as rv
    from paper_account import ACCOUNT
    clock=pd.Timestamp(now or stamp());isin=info['isin']
    if info.get('kind')=='benchmark':return {'state':'NOT_APPLICABLE'}
    with store.connect() as db:
        db.execute('BEGIN IMMEDIATE')
        if not _tv_enabled(db,isin):return {'state':'DISABLED'}
        key='cp_manual_risk:'+isin
        old=read(db,key,{'version':1,'account':ACCOUNT,'accepted':{},'events':[]})
        previous=old.get('accepted',{})
        jobs=[json.loads(v) for v, in db.execute('SELECT value FROM settings WHERE key LIKE ? AND json_extract(value,\'$.observation.result\')=\'ABSENT\'',(rv.PREFIX+isin+':%',))]
        eligible_jobs=[];rejected=[]
        for job in jobs:
            try:
                obs=job['observation'];inputs=rv._input(db,job)
                bound=_tv_native_binding(job,inputs)
                # Strict migration of a retained old receipt: reconstruct its
                # complete hash now. Never revive a stale market/identity row.
                bindings={bound,_tv_native_binding(job,inputs,version=1)}
                retained=any(v.get('actor')==TV_ACTOR and v.get('evidence',{}).get('native_binding') in bindings
                             and job['id'] in v.get('evidence',{}).get('job_ids',[]) for v in previous.values())
                if (not obs.get('identity_confirmed') or not obs.get('tv_symbol','').startswith('GETTEX:')
                        or not (rv._observation_current(job,inputs) or retained) or inputs['rows']):
                    rejected.append(dict(id=job['id'],reason='REVIEW_INPUT_CHANGED_OR_ROWS_PRESENT'));continue
                finding=rv._classify(job,inputs,clock)
                if (finding.get('state') not in ('GAP','UNCONFIRMED','SOURCE_TIMEBASE_UNCONFIRMED')
                        or finding.get('reason','').startswith('EXCHANGE_NOTICE_CONFLICT')):continue
                eligible_jobs.append(dict(job,_risk_native_binding=bound))
            except (ValueError,KeyError,TypeError):
                rejected.append(dict(id=job['id'],reason='REVIEW_IDENTITY_OR_INPUT_UNCONFIRMED'))
        if not jobs and not any(v.get('actor')==TV_ACTOR for v in previous.values()):return {'state':'NO_REVIEW'}
        accepted={k:v for k,v in previous.items() if v.get('actor')!=TV_ACTOR}
        current=problems(db,isin) if eligible_jobs else []
        for p in current:
            if p['id'] in accepted or p['kind']!='archive' or not p.get('range') or p['state'] not in ('GAP','SOURCE_GAP','UNCONFIRMED'):continue
            matches=[j for j in eligible_jobs if j['identity']==p['identity'] and j['tf']==p['tf'] and _covered([p['range']],[[j['first'],j['last']]])]
            if not matches:continue
            j=matches[0];meta=p['identity']
            scopes=['instrument:'+isin,'market:'+isin+':'+p['tf'],'setting:'+p['report_key'],
                    'setting:'+TV_POLICY,'setting:'+rv.key(j),'setting:interval_evidence:'+isin,
                    'setting:history_identity:'+isin,'setting:gettex_chart:'+isin+':'+p['tf']]
            accepted[p['id']]=dict(problem=p,actor=TV_ACTOR,accepted_at=previous.get(p['id'],{}).get('accepted_at',clock.isoformat()),
                input_revisions=_revisions(db,scopes),evidence=dict(status='OWNER_ACCEPTED_TV_ABSENCE',
                label=TV_LABEL,job_ids=[j['id']],tv_symbol=j['observation']['tv_symbol'],reviewed_at=j['observation']['at'],
                identity=meta,timeframe=p['tf'],range=p['range'],input_fingerprint=j['observation']['input_fingerprint'],
                native_binding=j['_risk_native_binding'],native_binding_version=2,
                price_model=TV_PRICE_MODEL,meaning='Accepted uncertainty, NOT NO_TRADES or DQ READY; actual bars only'))
        added=sorted(set(accepted)-set(previous));removed=sorted(set(previous)-set(accepted))
        changed=accepted!=previous
        if changed:
            old.update(account=ACCOUNT,accepted=accepted)
            if added or removed:old.setdefault('events',[]).append(dict(action='CP_OWNER_TV_POLICY',at=clock.isoformat(),accepted=added,revoked=removed))
            write(db,key,old)
            progress=read(db,rv.PROGRESS+isin,{})
            progress['analysis_requested_at']=clock.isoformat();write(db,rv.PROGRESS+isin,progress)
        result=dict(state='EVALUATED',policy=TV_VERSION,accepted=sum(v.get('actor')==TV_ACTOR for v in accepted.values()),
            added=len(added),revoked=len(removed),changed=changed,observations=len(jobs),current_observations=len(eligible_jobs),
            rejected=rejected,label=TV_LABEL,checked_at=clock.isoformat())
        diagnostic={k:v for k,v in result.items() if k not in ('added','revoked','changed','checked_at')}
        prior=read(db,'cp_tv_risk:'+isin,{})
        if any(prior.get(k)!=v for k,v in diagnostic.items()):write(db,'cp_tv_risk:'+isin,dict(diagnostic,checked_at=clock.isoformat()))
        if changed:result['analysis_requested_at']=clock.isoformat()
        return result


def _revisions(db,scopes):
    return {s:(db.execute('SELECT revision FROM input_revisions WHERE scope=?',(s,)).fetchone() or [0])[0] for s in scopes}


def effective_accepted(db,isin,grant):
    """Auto receipts expire on any consulted input change, even before CP runs."""
    policy=read(db,AUTO_POLICY,{})
    enabled=(policy.get('enabled') is True and policy.get('version')==AUTO_VERSION and
             isin not in policy.get('excluded_isins',[]))
    result={};validated={}
    for pid,entry in grant.get('accepted',{}).items():
        if entry.get('actor')==REVIEWED_ACTOR:
            policy=read(db,REVIEWED_PREFIX+isin,{})
            token=entry.get('input_revisions',{})
            if (policy.get('version')==REVIEWED_VERSION and policy.get('approved') is True
                    and pid in policy.get('entries',{}) and token and _revisions(db,token)==token
                    and all(entry.get('problem',{}).get(k)==policy['entries'][pid]['problem'].get(k)
                            for k in ('id','isin','kind','identity','tf','range','report_key','state'))
                    and entry.get('evidence',{}).get('policy_digest')==digest(policy)):
                result[pid]=entry
            continue
        if entry.get('actor')==TV_ACTOR:
            token=entry.get('input_revisions',{})
            if not _tv_enabled(db,isin) or not token or _revisions(db,token)!=token:continue
            result[pid]=entry
            continue # New explicit policy; never revive old historical grants.
        review=grant.get('historical_review',{})
        if review.get('version')==1 and review.get('state')=='COMPLETE':
            checked=review.get('results',{}).get(pid,{})
            token=checked.get('input_revisions',{})
            if checked.get('state')!='SOURCE_LIMITED' or not token or _revisions(db,token)!=token:continue
        if entry.get('actor')==AUTO_ACTOR:
            token=entry.get('input_revisions',{})
            if not enabled or not token:continue
            binding=tuple(sorted(token.items()))
            if binding not in validated:validated[binding]=_revisions(db,token)==token
            if not validated[binding]:continue
        result[pid]=entry
    return result


def automatic_turn(store,info,now=None):
    """Metadata-only owner policy at the existing serial feed boundary.

    Reuse existing SOURCE_LIMITED recovery evidence. Unknown GETTEX intervals,
    failures, damaged rows and CP's own generated blockers are not proof that
    recovery is impossible. This records accepted uncertainty, never READY.
    """
    import pandas as pd
    from paper_account import ALLOWED,ACCOUNT
    from eur_listings import stored_identity
    from timeframes import MINUTES
    clock=pd.Timestamp(now or stamp());isin=info['isin']
    if isin not in ALLOWED:return {'state':'NOT_APPLICABLE'}
    with store.connect() as db:
        db.execute('BEGIN IMMEDIATE');policy=read(db,AUTO_POLICY,{})
        if policy.get('enabled') is not True or policy.get('version')!=AUTO_VERSION or isin in policy.get('excluded_isins',[]):return {'state':'DISABLED'}
        row=db.execute('SELECT payload FROM instruments WHERE isin=?',(isin,)).fetchone()
        if not row:return {'state':'INSTRUMENT_REMOVED'}
        actual=json.loads(row[0]);current=problems(db,isin);reports={};eligible={};proofs={};tokens={};excluded={}
        for p in current:
            if p['kind']!='archive' or not p.get('range') or p['state'] not in ('GAP','SOURCE_GAP','UNCONFIRMED'):continue
            meta=p['identity'];base=p['tf'];key=p['report_key']
            if meta.get('source')!='proxy' or base not in ('5m','15m','1H'):continue
            if key not in reports:reports[key]=read(db,key,{})
            report=reports[key];limit=report.get('recovery_constraints',{})
            if (limit.get('status')!='SOURCE_LIMITED' or limit.get('identity')!=meta or
                limit.get('basis')!='existing Yahoo rolling intraday retention policy'):continue
            if key not in proofs:proofs[key]=stored_identity(db,actual,meta,base)
            if not proofs[key]:continue
            try:
                if key not in excluded:excluded[key]={tuple(span) for span in limit.get('excluded_ranges',[])}
                if tuple(p['range']) not in excluded[key]:continue
                cutoff=pd.Timestamp(limit['available_from']).tz_convert(meta['timezone']).tz_localize(None)
                from history_backfill import intraday_retention_start
                actual_cutoff=intraday_retention_start(base,clock).tz_convert(meta['timezone']).tz_localize(None)
                if cutoff>actual_cutoff:continue
                if pd.Timestamp(p['range'][1])>=cutoff:continue
            except (ValueError,TypeError,KeyError):continue
            children=[b for b in ('1m','5m','15m','1H') if MINUTES[b]<MINUTES[base]]
            # Existing local child rows may still make recovery possible. Let
            # ordinary bounded recovery try them; never preempt it with risk.
            right=str(pd.Timestamp(p['range'][1])+pd.Timedelta(minutes=MINUTES[base]))
            if any(db.execute('SELECT 1 FROM series WHERE isin=? AND source=? AND venue=? AND currency=? AND symbol=? AND base_tf=? AND ts>=? AND ts<? LIMIT 1',
                (isin,meta['source'],meta['venue'],meta['currency'],meta['symbol'],b,p['range'][0],right)).fetchone() for b in children):continue
            scopes=['instrument:'+isin,'market:'+isin+':'+base,'setting:'+key,'setting:'+AUTO_POLICY,
                    'setting:history_identity:'+isin,*('market:'+isin+':'+b for b in children)]
            if key not in tokens:tokens[key]=_revisions(db,scopes)
            eligible[p['id']]={'problem':p,'actor':AUTO_ACTOR,'input_revisions':tokens[key],
                'evidence':{'status':'SOURCE_LIMITED','basis':limit['basis'],'available_from':limit['available_from'],
                            'report_key':key,'checked_at':report.get('checked_at'),'range':p['range'],
                            'meaning':'Historical uncertainty accepted for demo only; not NO_TRADES or DQ READY'}}
        old=read(db,'cp_manual_risk:'+isin,{'version':1,'account':ACCOUNT,'accepted':{},'events':[]})
        accepted={k:v for k,v in old.get('accepted',{}).items() if v.get('actor')!=AUTO_ACTOR}
        for pid,entry in eligible.items():
            previous=old.get('accepted',{}).get(pid,{})
            if pid in accepted:continue # Never replace an owner's explicit review.
            entry['accepted_at']=previous.get('accepted_at',clock.isoformat())
            accepted[pid]=entry
        changed=accepted!=old.get('accepted',{})
        if changed:
            added=set(eligible)-set(old.get('auto_ids',[]));removed=set(old.get('auto_ids',[]))-set(eligible)
            old.update(account=ACCOUNT,accepted=accepted)
            if added or removed:
                old.setdefault('events',[]).append({'action':'CP_SOURCE_LIMIT_POLICY','at':clock.isoformat(),
                    'accepted':sorted(added),'revoked':sorted(removed)})
            old['auto_ids']=sorted(eligible);write(db,'cp_manual_risk:'+isin,old)
        result={'state':'EVALUATED','policy':AUTO_VERSION,'automatic_accepted':len(eligible),
                'unaccepted_problems':sum(p['id'] not in accepted for p in current),'changed':changed}
        status={k:v for k,v in result.items() if k!='changed'}
        prior=read(db,'cp_auto_risk:'+isin,{})
        if any(prior.get(k)!=v for k,v in status.items()):write(db,'cp_auto_risk:'+isin,dict(status,checked_at=clock.isoformat()))
        return result

def _original_review_receipt(review,pid,entry):
    """An old problem ID is not a newly issued, independently checked grant."""
    original=review.get('targets',{}).get(pid)
    return bool(original and all(original.get(k)==entry.get(k) for k in ('actor','accepted_at')))


def _problem_in_dependencies(problem,series):
    """Exclude only a proved non-overlap with every actual raw input range."""
    import pandas as pd
    related=[s['spec'] for s in series if problem['tf'] in (s['spec'].get('base_tf'),s['spec'].get('timeframe'))]
    if not related:return False
    if not problem.get('range'):return True
    try:
        zone=problem['identity']['timezone']
        def instant(value):
            ts=pd.Timestamp(value)
            return ts.tz_localize(zone) if ts.tzinfo is None else ts.tz_convert(zone)
        first,last=map(instant,problem['range'])
        if first>last:return True
        for spec in related:
            if not spec.get('from') or not spec.get('through'):return True
            if first<instant(spec['through']) and last>=instant(spec['from']):return True
        return False
    except (ValueError,KeyError,TypeError):return True


def manual_gate(db,isin,identity,entry_tf=None,dependency_proof=None):
    """Known historical risk only; execution must still validate the actual quote."""
    from paper_account import registered_instrument,ACCOUNT
    grant=read(db,'cp_manual_risk:'+isin,{})
    accepted=effective_accepted(db,isin,grant)
    review=grant.get('historical_review',{})
    if review.get('execution_suspended') and (not review.get('targets') or not accepted or any(
            e.get('actor') not in (TV_ACTOR,REVIEWED_ACTOR) or _original_review_receipt(review,pid,e) for pid,e in accepted.items())):
        raise ValueError('PAPER_HISTORICAL_REVIEW_PENDING')
    registered_instrument(db,isin)
    if grant.get('account')!=ACCOUNT or not grant.get('accepted'):
        raise ValueError('PAPER_MANUAL_NOT_GRANTED')
    if not all(identity.get(k) for k in IDENTITY) or identity.get('currency')!='EUR':raise ValueError('PAPER_MANUAL_IDENTITY_BLOCKED')
    if not any(g['problem'].get('identity')==identity for g in accepted.values()):raise ValueError('PAPER_MANUAL_LISTING_NOT_REVIEWED')
    required=None
    if dependency_proof is not None:
        from trading_engine import profile
        from performance_cache import validate_closed_inputs,InputRevisionChanged
        if entry_tf=='1H':
            from paper_hourly import authorize_hourly
            authorize_hourly(db,isin,identity)
        elif entry_tf not in ('5m','15m'):raise ValueError('PAPER_MANUAL_DEPENDENCY_SCOPE')
        own=[s for s in dependency_proof.get('series',[]) if s['spec'].get('isin')==isin]
        if (not set((entry_tf,*profile(entry_tf)))<={s['spec'].get('timeframe') for s in own if s.get('count',0)>0}
                or any(s['spec'].get('identity')!=identity for s in own)):
            raise ValueError('PAPER_MANUAL_DEPENDENCY_SCOPE')
        if 'instrument:'+isin not in dependency_proof.get('revisions',{}):raise ValueError('PAPER_MANUAL_DEPENDENCY_SCOPE')
        try:validate_closed_inputs(db,dependency_proof)
        except (InputRevisionChanged,ValueError,KeyError,TypeError) as error:raise ValueError('PAPER_MANUAL_DEPENDENCY_CHANGED') from error
        required={s['spec'][key] for s in own for key in ('base_tf','timeframe')}
    # A different provider's reserve is not an input of this calculation.
    # Generated CORE/context blockers are checked by the actual CORE below,
    # not granted by accepting CP's own diagnosis. Unknown identities block.
    current=[p for p in problems(db,isin) if p['kind']=='archive' and
             ((p['identity']==identity and (required is None or _problem_in_dependencies(p,own)))
              or not all(p['identity'].get(k) for k in IDENTITY))]
    if any(p['id'] not in accepted for p in current):raise ValueError('PAPER_MANUAL_NEW_OR_UNACCEPTED_PROBLEM')
    actual_only=all(accepted[p['id']].get('evidence',{}).get('price_model')==TV_PRICE_MODEL for p in current)
    result=dict(mode='MANUAL_RISK_ACCEPTED',isin=isin,identity=identity,account=ACCOUNT,
        calculation_context='owner_reviewed_actual_bars_v1' if actual_only else RISK_CONTEXT,
        price_model=TV_PRICE_MODEL if actual_only else PRICE_MODEL,
        grant_digest=digest(grant),problem_ids=sorted(p['id'] for p in current),
        label='Демо: исторический риск принят по решению владельца; это не сигнал CORE',automatic_signal=False)
    if required is not None:result.update(entry_timeframe=entry_tf,required_timeframes=sorted(required),dependency_basis='Actual verified CORE closed inputs; optional unrelated TF excluded')
    return result

def manual_context(store,isin,identity,entry_tf=None,dependency_proof=None):
    # Include permission and all consulted reports in the existing revision token.
    store.read_cache.depend('setting:cp_manual_risk:'+isin)
    store.read_cache.depend('setting:'+AUTO_POLICY)
    with store.connect() as db:
        if not read(db,'cp_manual_risk:'+isin,{}).get('accepted'):return None
        keys=[k for (k,) in db.execute('SELECT key FROM settings WHERE key LIKE ? OR key IN (?,?)',
            ('data_quality:'+isin+':%','cp_eligibility:'+isin,'cp_manual_risk:'+isin))]
        for key in [*keys,'cp_manual_risk:'+isin]:store.read_cache.depend('setting:'+key)
        for scope in {s for entry in read(db,'cp_manual_risk:'+isin,{}).get('accepted',{}).values() for s in entry.get('input_revisions',{})}:store.read_cache.depend(scope)
        try:return manual_gate(db,isin,identity,entry_tf,dependency_proof)
        except ValueError:return None


def begin_historical_review(store,isin,expected_digest):
    """Owner-requested review; keep the original receipts and block new risk fills."""
    with store.connect() as db:
        db.execute('BEGIN IMMEDIATE');key='cp_manual_risk:'+isin;grant=read(db,key,{})
        if digest(grant)!=expected_digest:raise ValueError('RISK_REVIEW_INPUT_CHANGED')
        if grant.get('historical_review',{}).get('version')==1:return grant['historical_review']
        review=dict(version=1,state='PENDING',execution_suspended=True,requested_at=stamp(),
                    targets=grant.get('accepted',{}).copy(),results={},groups={})
        grant['historical_review']=review
        grant.setdefault('events',[]).append(dict(action='OWNER_HISTORICAL_REVIEW',at=stamp(),count=len(review['targets'])))
        write(db,key,grant)
    return review


def historical_review_turn(store,info,now=None):
    """One unchanged-data-aware group in the existing CP feed writer, no HTTP.

    Reuse DQ and its evidence. Missing API rows never prove irrecoverability.
    The normal archive recovery turn owns requests, cooldown and OHLCV writes.
    """
    import pandas as pd
    import data_quality as dq
    from eur_listings import stored_identity
    from collections import Counter
    isin=info['isin'];key='cp_manual_risk:'+isin;clock=pd.Timestamp(now or stamp())
    with store.connect() as db:
        db.execute('BEGIN');grant=read(db,key,{})
        review=grant.get('historical_review',{})
        if review.get('version')!=1:return {'state':'NOT_REQUESTED'}
        if _review_receipts_retired(grant):
            # This was a review of OLD authorizations, not a second archive
            # recovery loop. Once all targets were examined and every original
            # receipt revoked, new quotes cannot reopen those permissions.
            # Ordinary DQ/recovery and current independently issued grants
            # remain revision-bound and continue through their normal queues.
            if not review.get('receipts_retired') or review.get('execution_suspended') or review.get('state')!='COMPLETE':
                db.execute('ROLLBACK');db.execute('BEGIN IMMEDIATE')
                current=read(db,key,{})
                if digest(current)!=digest(grant):raise ValueError('RISK_REVIEW_INPUT_CHANGED')
                review=current['historical_review']
                review.update(state='COMPLETE',execution_suspended=False,receipts_retired=True,
                    retired_at=clock.isoformat(),retirement_basis='All original receipts reviewed and revoked; current DQ/recovery remain active')
                write(db,key,current)
                return dict(state='COMPLETE',reviewed=len(review['results']),total=len(review['targets']),
                            summary=review.get('summary',{}),revoked=0,receipts_retired=True)
            return dict(state='UNCHANGED',reviewed=len(review['results']),summary=review.get('summary',{}),receipts_retired=True)
        original=digest(grant);groups={}
        proven_auto={pid for pid,e in effective_accepted(db,isin,grant).items() if e.get('actor')==AUTO_ACTOR}
        for pid,entry in review.get('targets',{}).items():
            p=entry['problem'];g=digest([p.get('identity'),p.get('tf'),p.get('report_key')])
            groups.setdefault(g,[]).append((pid,p))
        pending=[]
        for g,items in groups.items():
            p=items[0][1];base=p['tf'];meta=p['identity'];report_key=p['report_key']
            from history_service import parent_provenance_key
            from timeframes import MINUTES
            scopes=['instrument:'+isin,'market:'+isin+':'+base,'setting:'+report_key,'setting:'+AUTO_POLICY,
                    'setting:interval_evidence:'+isin,'setting:history_identity:'+isin,
                    'setting:gettex_chart:'+isin+':'+base,'setting:'+parent_provenance_key(isin,meta,base),
                    *('market:'+isin+':'+tf for tf in ('1m','5m','15m','1H') if MINUTES[tf]<(1440 if base=='1D' else MINUTES.get(base,0)))]
            for pid,_ in items:scopes.extend(grant.get('accepted',{}).get(pid,{}).get('input_revisions',{}))
            token=_revisions(db,scopes);old=review.get('groups',{}).get(g,{})
            retry_due=any(r.get('state')=='WAITING' and r.get('next_retry') and pd.Timestamp(r['next_retry'])<=clock
                          for pid,_ in items for r in [review.get('results',{}).get(pid,{})])
            if old.get('input_revisions')!=token or retry_due:pending.append((bool(old),old.get('checked_at',''),g,items,token))
        if not pending:return {'state':'UNCHANGED','reviewed':len(review.get('results',{}))}
        _,_,group,items,token=min(pending,key=lambda x:x[:3]);p=items[0][1];meta=p['identity'];base=p['tf']
        actual=json.loads(db.execute('SELECT payload FROM instruments WHERE isin=?',(isin,)).fetchone()[0])
        proof=stored_identity(db,actual,meta,base);saved=read(db,p['report_key'],{})
    frame=store.load_official(isin,base) if meta['source']=='gettex' else store.load_series(isin,base,meta['source'],meta['symbol'],meta['venue'],meta['currency'])
    frame.attrs.update(proof or meta);dq.attach_interval_evidence(store,frame,isin)
    report=dq.audit(frame,base,meta,clock,check_stale=False)
    outcomes={};bad_spans=[tuple(map(pd.Timestamp,s)) for s in report.get('ranges',[])]
    for pid,p in items:
        state='UNCONFIRMED';reason='Нет доказательства полноты или невосстановимости источника';expected_count=None
        try:
            if not proof or meta.get('currency')!='EUR':raise ValueError('IDENTITY_UNCONFIRMED')
            if not p.get('range'):raise ValueError('NO_EXACT_RANGE')
            a,b=map(pd.Timestamp,p['range'])
            if a>b:raise ValueError('TIMESTAMPS_INVALID')
            grid=dq.expected_index(meta,base,a,b,clock)
            expected_count=len(grid);available=grid.intersection(frame.index)
            missing=grid.difference(frame.index)
            empty=dq.covered_empty(frame.attrs,missing,base,clock) if len(missing) else pd.DatetimeIndex([])
            overlapping=any(x<=b and y>=a for x,y in bad_spans)
            global_invalid=report['state']=='INVALID' and not report.get('repairable')
            if global_invalid:state='INVALID';reason='; '.join(report.get('issues',[]))
            elif not len(grid):
                if len(dq.expected_index(meta,'1m',a.floor('min'),b.ceil('min'),clock)):raise ValueError('TIMESTAMP_ALIGNMENT_UNCONFIRMED')
                state='MARKET_CLOSED';reason='Подтверждённый календарь: свечи не ожидались'
            elif not len(missing) and not overlapping:state='RESOLVED';reason='Все ожидаемые реальные OHLCV присутствуют и прошли штатный DQ'
            elif len(empty)==len(missing) and not overlapping:state='NO_TRADES';reason='Полное identity-bound доказательство отсутствия сделок'
            elif report['state']=='INVALID' and overlapping:state='INVALID';reason='Штатный DQ отклонил данные в этом диапазоне'
            else:
                limit=saved.get('recovery_constraints',{})
                # The only existing proven exhausted archive policy is Yahoo
                # rolling retention. Do not invent an equivalent GETTEX claim.
                if (pid in proven_auto and meta['source']=='proxy' and limit.get('identity')==meta and
                    limit.get('status')=='SOURCE_LIMITED' and
                    limit.get('basis')=='existing Yahoo rolling intraday retention policy' and
                    p['range'] in limit.get('excluded_ranges',[])):
                    state='SOURCE_LIMITED';reason=limit['basis']
                elif saved.get('next_retry') and pd.Timestamp(saved['next_retry'])>clock:
                    state='WAITING';reason='Штатное восстановление ожидает cooldown / Retry-After'
                elif any(pd.Timestamp(x)<=b and pd.Timestamp(y)>=a for x,y in report.get('interval_classification',{}).get('confirmed_gap_ranges',[])):
                    state='GAP';reason='Имеются сделки или другие подтверждённые основания ожидать данные'
        except (ValueError,TypeError,KeyError) as error:state='UNCONFIRMED';reason=str(error)
        outcomes[pid]=dict(state=state,reason=reason,range=p.get('range'),identity=meta,timeframe=base,
            expected_intervals=expected_count,checked_at=clock.isoformat(),input_revisions=token,
            next_retry=saved.get('next_retry'),report_key=p['report_key'],factual_dq=report['state'])
    with store.connect() as db:
        db.execute('BEGIN IMMEDIATE');current=read(db,key,{})
        if digest(current)!=original or _revisions(db,token)!=token:raise ValueError('RISK_REVIEW_INPUT_CHANGED')
        review=current['historical_review'];review['results'].update(outcomes)
        review['groups'][group]=dict(input_revisions=token,checked_at=clock.isoformat(),quality=report,
            next_retry=saved.get('next_retry') if any(x['state']=='WAITING' for x in outcomes.values()) else None)
        complete=(len(review['results'])==len(review['targets']) and
                  all(_revisions(db,x['input_revisions'])==x['input_revisions'] for x in review['groups'].values()))
        review.update(state='COMPLETE' if complete else 'RUNNING',execution_suspended=not complete,
                      checked_at=clock.isoformat(),summary=dict(Counter(x['state'] for x in review['results'].values())))
        # Keep the original receipt in targets/events, never delete its evidence.
        remove={pid for pid,x in outcomes.items() if x['state'] in ('RESOLVED','MARKET_CLOSED','NO_TRADES')}
        if complete:remove|={pid for pid,x in review['results'].items() if x['state']!='SOURCE_LIMITED'}
        revoked=sorted(pid for pid in remove & current.get('accepted',{}).keys()
                       if _original_review_receipt(review,pid,current['accepted'][pid]))
        for pid in revoked:current['accepted'].pop(pid)
        if revoked:current.setdefault('events',[]).append(dict(action='CP_EVIDENCE_REVIEW_REVOKE',at=clock.isoformat(),ids=revoked))
        write(db,key,current)
    return dict(state=review['state'],reviewed=len(review['results']),total=len(review['targets']),summary=review['summary'],revoked=len(revoked))


def historical_review_due(store,infos,now=None):
    """Continue one incomplete metadata review on the existing serial feed lane."""
    import pandas as pd
    pending=[];clock=pd.Timestamp(now or stamp())
    with store.connect() as db:
        for info in infos:
            grant=read(db,'cp_manual_risk:'+info['isin'],{})
            review=grant.get('historical_review',{})
            if _review_receipts_retired(grant) and review.get('receipts_retired'):continue
            retry_due=any(r.get('state')=='WAITING' and r.get('next_retry') and pd.Timestamp(r['next_retry'])<=clock for r in review.get('results',{}).values())
            if review.get('state') in ('PENDING','RUNNING') or retry_due:
                pending.append((review.get('checked_at',''),info))
    return [dict(min(pending,key=lambda x:x[0])[1],_review_only=True)] if pending else []


def _review_receipts_retired(grant):
    """No count-only or flag-only shortcut; inspect exact reviewed target IDs."""
    review=grant.get('historical_review',{})
    targets=review.get('targets',{})
    results=review.get('results',{})
    return bool(review.get('version')==1 and targets and set(targets)<=set(results)
        and all(results[k].get('state') for k in targets)
        and not any(_original_review_receipt(review,pid,entry)
                    for pid,entry in grant.get('accepted',{}).items()))


RISK_CONTEXT='owner_accepted_estimated_close_v2'
PRICE_MODEL='mean_neighbour_close_v1'
DEMO_INITIALIZATION='owner_apple_gettex_seed_stability_v1'
DEMO_ALL_INITIALIZATION='owner_gettex_all_stocks_seed_stability_v1'
DEMO_ALL_POLICY='cp_demo_initialization_policy'


def initialization_policy_for(store,info,identity=None):
    """Explicit owner policy, resolved against a current confirmed listing.

    No gap grant, prices, preferences or execution permission are created here.
    The global setting is a revision dependency even when approval is absent.
    """
    from paper_account import ACCOUNT,instrument_allowed
    from timeframes import LABELS
    key='cp_manual_risk:'+info['isin']
    store.read_cache.depend('setting:'+key)
    store.read_cache.depend('setting:'+DEMO_ALL_POLICY)
    old=store.setting(key,{}).get('initialization_policy')
    legacy=old if demo_initialization_policy(old,info['isin'],identity) else None
    authority=store.setting(DEMO_ALL_POLICY,{})
    if not (instrument_allowed(info) and authority.get('version')==DEMO_ALL_INITIALIZATION
            and authority.get('approved') is True and authority.get('account')==ACCOUNT
            and authority.get('scope')=='registered_stocks' and authority.get('older_verified_warmup') is True):return legacy
    from gettex_chart import state_key,confirmed_state
    saved_key=state_key(info['isin'],'1D');store.read_cache.depend('setting:'+saved_key)
    saved=store.setting(saved_key,{})
    if not confirmed_state(info,saved):return legacy
    exact=saved['identity']
    if identity is not None and identity!=exact:return legacy
    # The later explicit all-stock demo policy includes Apple. Returning its
    # legacy analysis-only receipt first permanently bypassed the existing
    # 15m execution integration. Never switch an older owner's pinned identity.
    if legacy and legacy['identity']!=exact:return legacy
    policy=dict(version=DEMO_ALL_INITIALIZATION,isin=info['isin'],identity=exact,
        account=ACCOUNT,approved=True,entry_timeframes=['15m'],analysis_timeframes=list(LABELS),
        older_verified_warmup=True,authority=DEMO_ALL_POLICY)
    return policy if demo_initialization_policy(policy,info['isin'],identity) else None

def demo_initialization_policy(policy,isin,identity=None):
    """Explicit owner's separate demo policy; never inferred from gap grants."""
    from paper_account import ACCOUNT
    expected=dict(source='gettex_chart',symbol='AAPL.GTX',venue='GETTEX',currency='EUR',timezone='Europe/Berlin')
    from timeframes import LABELS
    if isinstance(policy,dict) and policy.get('version')==DEMO_ALL_INITIALIZATION:
        meta=policy.get('identity',{})
        return bool(isin and policy.get('isin')==isin and policy.get('account')==ACCOUNT
            and policy.get('approved') is True and policy.get('authority')==DEMO_ALL_POLICY
            and meta.get('source')=='gettex_chart' and meta.get('venue')=='GETTEX'
            and meta.get('currency')=='EUR' and meta.get('timezone')=='Europe/Berlin'
            and isinstance(meta.get('symbol'),str) and meta['symbol'].endswith('.GTX')
            and set(meta)==set(IDENTITY) and (identity is None or meta==identity)
            and policy.get('entry_timeframes')==['15m'] and policy.get('analysis_timeframes')==list(LABELS)
            and policy.get('older_verified_warmup') is True)
    return bool(isinstance(policy,dict) and policy.get('version')==DEMO_INITIALIZATION
        and isin==policy.get('isin')=='US0378331005' and policy.get('account')==ACCOUNT
        and policy.get('identity')==expected and (identity is None or identity==expected)
        and policy.get('entry_timeframes')==['5m','15m'] and policy.get('approved') is True
        and ('analysis_timeframes' not in policy or policy['analysis_timeframes']==list(LABELS)))

def initialization_for(frame):
    """Same owner model for clean inputs as for explicitly accepted gaps.

    This receipt changes seed comparison only. It never accepts a DQ fault,
    authorizes execution on a new TF, or changes the underlying OHLCV.
    """
    policy=frame.attrs.get('initialization_policy') or frame.attrs.get('accepted_risk',{}).get('initialization_policy')
    identity={k:frame.attrs.get(k) for k in IDENTITY}
    isin=frame.attrs.get('isin') or frame.attrs.get('accepted_risk',{}).get('isin') or frame.attrs.get('listing_proof',{}).get('isin')
    return policy if demo_initialization_policy(policy,isin,identity) else None

def _covered(ranges, approved):
    """Inclusive missing-bar ranges; never extend an owner's reviewed interval."""
    import pandas as pd
    from bisect import bisect_right
    spans=sorted((pd.Timestamp(a),pd.Timestamp(b)) for a,b in approved)
    starts=[];ends=[]
    for a,b in spans:
        starts.append(a);ends.append(max(b,ends[-1]) if ends else b)
    for x,y in ranges:
        a,b=pd.Timestamp(x),pd.Timestamp(y);i=bisect_right(starts,a)-1
        if i<0 or a>b or b>ends[i]:return False
    return True

def attach_tv_absence(store, frame, isin, base):
    """Current owner receipts, separate from exchange-proven NO_TRADES.

    No OHLCV or DQ writes. Track even rejected receipts so cached display and
    analysis cannot outlive a changed observation, policy, identity or revision.
    """
    frame.attrs.pop('tv_accepted_absence',None)
    key='cp_manual_risk:'+isin
    for scope in ('setting:'+key,'setting:'+TV_POLICY):store.read_cache.depend(scope)
    grant=store.setting(key,{})
    for entry in grant.get('accepted',{}).values():
        if entry.get('actor')==TV_ACTOR:
            for scope in entry.get('input_revisions',{}):store.read_cache.depend(scope)
    identity={k:frame.attrs.get(k) for k in IDENTITY}
    if identity.get('venue')!='GETTEX' or identity.get('currency')!='EUR':return frame
    with store.connect() as db:accepted=effective_accepted(db,isin,grant)
    ranges=[];ids=[]
    for pid,entry in accepted.items():
        p=entry.get('problem',{});e=entry.get('evidence',{})
        if (entry.get('actor')==TV_ACTOR and p.get('isin')==isin and p.get('kind')=='archive'
                and p.get('identity')==identity and p.get('tf')==base and p.get('range')
                and p.get('state') in ('GAP','UNCONFIRMED','SOURCE_GAP')
                and e.get('status')=='OWNER_ACCEPTED_TV_ABSENCE' and e.get('price_model')==TV_PRICE_MODEL):
            ranges.append(p['range']);ids.append(pid)
    if ranges:
        frame.attrs['tv_accepted_absence']=dict(version=TV_VERSION,isin=isin,identity=identity,
            base=base,ranges=ranges,problem_ids_digest=digest(sorted(ids)),grant_digest=digest(grant),
            label=TV_LABEL,price_model=TV_PRICE_MODEL)
    return frame


def tv_absent_slots(meta, stamps, base, now):
    """Only exact closed slots accepted under the current TV owner policy."""
    import pandas as pd
    import data_quality as dq
    proof=meta.get('tv_accepted_absence',{});stamps=pd.DatetimeIndex(stamps)
    empty=pd.DatetimeIndex([])
    if (stamps.empty or proof.get('version')!=TV_VERSION or proof.get('base')!=base
            or proof.get('price_model')!=TV_PRICE_MODEL
            or proof.get('identity')!={k:meta.get(k) for k in IDENTITY}):return empty
    try:
        closed=dq.expected_index(meta,base,stamps.min(),stamps.max(),now).intersection(stamps)
        ranges=proof.get('ranges',[])
        if not ranges:return empty
        # Inclusive union of the SAME approved ranges. Avoid scanning every
        # timestamp and allocating a pandas union for every receipt, for each
        # aggregate bucket. No new absence proof or calendar assumption.
        import numpy as np
        parsed=dq._proof_ranges(tuple(map(tuple,ranges)))
        starts=pd.DatetimeIndex([a for a,b in parsed])
        ends=pd.DatetimeIndex([b for a,b in parsed])
        left=closed.searchsorted(starts,side='left')
        right=closed.searchsorted(ends,side='right')
        valid=(left<right)&~starts.isna()&~ends.isna()
        counts=np.zeros(len(closed)+1,dtype=np.int64)
        np.add.at(counts,left[valid],1);np.add.at(counts,right[valid],-1)
        return closed[np.cumsum(counts[:-1])>0]
    except (ValueError,KeyError,TypeError):return empty


def analysis_quality_allowed(report, risk=None):
    """A separate owner decision, NOT a change to the factual DQ state."""
    if report.get('state')=='READY':return True
    return bool(risk and risk.get('context')==RISK_CONTEXT and risk.get('approved')
        and report.get('state') in ('GAP','UNCONFIRMED','SOURCE_GAP')
        and not report.get('stale_count')
        and report.get('identity')==risk.get('identity')
        and report.get('base_tf')==risk.get('timeframe')
        and ((report.get('missing_count',0)>0 and report.get('ranges')
              and _covered(report['ranges'],risk.get('ranges',[])))
             or (report.get('missing_count')==0 and not report.get('ranges')
                 and risk.get('tv_complete_aggregate') is True
                 and risk.get('price_model')==TV_PRICE_MODEL
                 and risk.get('source_missing_count',0)>0)))

def accepted_gap_input(store,info,frame,base,now):
    """Read the existing grant and validate ONLY genuine missing base bars.

    Called in the existing analysis worker before the existing aggregation.
    No data/DQ writes, no grant inferred from CP's own generated blockers.
    """
    import data_quality as dq
    from eur_listings import confirmed_listing
    from paper_account import instrument_allowed,ACCOUNT
    key='cp_manual_risk:'+info['isin'];store.read_cache.depend('setting:'+key)
    store.read_cache.depend('setting:'+AUTO_POLICY)
    store.read_cache.depend('setting:'+TV_POLICY)
    grant=store.setting(key,{})
    if grant.get('account',ACCOUNT)!=ACCOUNT or frame.empty:return frame
    out=frame.copy();out.attrs.pop('accepted_gap_input',None);out.attrs.pop('accepted_risk',None)
    out.attrs.pop('initialization_policy',None)
    attach_tv_absence(store,out,info['isin'],base)
    meta=out.attrs;identity={k:meta.get(k) for k in IDENTITY}
    if identity.get('currency')!='EUR' or not confirmed_listing(info,meta):return out
    inherited=meta.get('data_quality',{})
    if inherited.get('state') not in ('READY','GAP','UNCONFIRMED','SOURCE_GAP','STALE'):return out
    from timeframes import MINUTES
    report=dq.audit(frame,base,expected=identity,now=now,
        check_stale=not bool(meta.get('closed_parent_input') and base in MINUTES))
    if report['state'] not in ('READY','GAP','UNCONFIRMED') or report.get('stale_count'):return out
    policy=initialization_policy_for(store,info,identity)
    # A verified clean daily archive has no gap grant to carry this policy.
    # Keep it separate; analysis_quality_allowed still checks factual DQ.
    if demo_initialization_policy(policy,info['isin'],identity):
        out.attrs['initialization_policy']=policy
    with store.connect() as db:accepted=effective_accepted(db,info['isin'],grant)
    approved=[];ids=[];tv_ids=[];reviewed_ids=[]
    for pid,entry in accepted.items():
        if not instrument_allowed(info):continue
        for scope in entry.get('input_revisions',{}):store.read_cache.depend(scope)
        p=entry.get('problem',{})
        if (p.get('isin')==info['isin'] and p.get('kind')=='archive' and p.get('tf')==base
                and p.get('identity')==identity and p.get('state') in ('GAP','UNCONFIRMED','SOURCE_GAP') and p.get('range')):
            approved.append(p['range']);ids.append(pid)
            if entry.get('actor') in (TV_ACTOR,REVIEWED_ACTOR):tv_ids.append(pid)
            if entry.get('actor')==REVIEWED_ACTOR:reviewed_ids.append(pid)
    if not approved or not _covered(report.get('ranges',[]),approved):return out
    if inherited.get('state')=='SOURCE_GAP' and not _covered(inherited.get('ranges',[]),approved):return out
    out.attrs['accepted_gap_input']=dict(context=RISK_CONTEXT,approved=True,isin=info['isin'],
        identity=identity,base=base,grant_digest=digest(grant),accepted_problem_count=len(ids),problem_ids_digest=digest(sorted(ids)),
        source_ranges=report.get('ranges',[]),source_missing_count=report.get('missing_count',0))
    if tv_ids:
        # Do not mix the earlier estimated-close policy with this actual-bar model.
        if len(tv_ids)!=len(ids):out.attrs.pop('accepted_gap_input',None);return out
        out.attrs['accepted_gap_input'].update(price_model=TV_PRICE_MODEL,label=TV_LABEL)
        if reviewed_ids:out.attrs['accepted_gap_input'].update(label=REVIEWED_LABEL,reviewed_discrepancy_count=len(reviewed_ids))
    if demo_initialization_policy(policy,info['isin'],identity):
        out.attrs['accepted_gap_input']['initialization_policy']=policy
    return out

def risk_summary(risk):
    return {k:v for k,v in (risk or {}).items() if k not in ('ranges','source_ranges','excluded_incomplete','price_estimates')}

def accepted_close_estimates(frame,report,now,leading=None):
    """Supplemental close observations only; never manufacture OHLCV rows."""
    import pandas as pd
    import data_quality as dq
    from analytics import bar_ends
    if frame.empty:return []
    tf=frame.attrs['timeframe'];ends=bar_ends(frame);out=[]
    for a,b in report.get('ranges',[]):
        for at in dq.expected_index(frame.attrs,tf,a,b,now):
            if at in frame.index:raise ValueError('ESTIMATE_CANNOT_REPLACE_REAL_ROW')
            pos=frame.index.searchsorted(at)
            # No predecessor exists before the first genuine calculation bar.
            # Do not extrapolate a seed or reject every later reviewed gap.
            # Keep the factual DQ/ranges and disclose this unestimated boundary;
            # actual-bar warmup and the unchanged recursive proof still apply.
            if pos==0 and at<frame.index[0]:
                if leading is not None:leading.append(str(at))
                continue
            if pos==0 or pos==len(frame) or ends[pos]>now:raise ValueError('ESTIMATE_CLOSED_NEIGHBOURS_REQUIRED')
            previous,following=frame.index[pos-1],frame.index[pos]
            p,n=float(frame.close.iloc[pos-1]),float(frame.close.iloc[pos])
            out.append(dict(timestamp=str(at),value=p/2+n/2,previous_at=str(previous),previous_close=p,
                next_at=str(following),next_close=n,available_after=ends[pos].isoformat()))
    return out

def analytical_close_series(frame,*,values_only=False):
    """Explicit accepted-risk model for close-only indicators at REAL bar ends.

    Estimate references must be inside this calculation window and closed.
    Thus a shorter window cannot import a seed or a future neighbour silently.
    ATR, volume, levels, warmup counts and execution keep genuine observations.
    """
    import pandas as pd,math,copy
    risk=frame.attrs.get('accepted_risk',{})
    # Metadata is still read from the original frame for every safety check.
    # Internal arithmetic needs values only; public callers retain an isolated
    # metadata copy without pandas copying the whole archive twice per column.
    prices=pd.DataFrame(frame,copy=False).close.copy()
    if not values_only:prices.attrs=copy.deepcopy(frame.attrs)
    if not risk.get('price_estimates'):return prices
    identity={k:frame.attrs.get(k) for k in IDENTITY}
    if (risk.get('context')!=RISK_CONTEXT or not risk.get('approved') or risk.get('price_model')!=PRICE_MODEL
        or risk.get('identity')!=identity or identity.get('currency')!='EUR' or risk.get('timeframe')!=frame.attrs.get('timeframe')):
        raise ValueError('ESTIMATE_CONTEXT_UNCONFIRMED')
    if not analysis_quality_allowed(frame.attrs.get('data_quality',{}),risk):raise ValueError('ESTIMATE_DQ_BLOCKED')
    from analytics import bar_ends
    ends=bar_ends(frame);asof=pd.Timestamp(risk['checked_at']);extra={}
    for e in risk['price_estimates']:
        t,a,b=map(pd.Timestamp,(e['timestamp'],e['previous_at'],e['next_at']))
        # Tail/window/recursive-seed slices must contain BOTH true inputs.
        if a not in prices.index or b not in prices.index:continue
        pos=prices.index.searchsorted(t)
        if t in prices.index or pos==0 or pos==len(prices) or prices.index[pos-1]!=a or prices.index[pos]!=b:
            raise ValueError('ESTIMATE_NEIGHBOUR_BINDING_CHANGED')
        p,n=float(prices.loc[a]),float(prices.loc[b]);mean=p/2+n/2
        if (not all(math.isfinite(v) and v>0 for v in (p,n,mean)) or p!=e['previous_close'] or n!=e['next_close']
            or mean!=e['value'] or pd.Timestamp(e['available_after'])!=ends[pos] or ends[pos]>asof):
            raise ValueError('ESTIMATE_INPUT_CHANGED')
        if t in extra:raise ValueError('ESTIMATE_DUPLICATE')
        extra[t]=mean
    return pd.concat([prices,pd.Series(extra,dtype=float)]).sort_index() if extra else prices

def accepted_gap_output(frame,tf,now):
    """Use only complete real aggregates; preserve gaps and their original DQ."""
    import data_quality as dq
    import pandas as pd
    proof=frame.attrs.get('accepted_gap_input')
    if not proof or frame.empty:return frame
    out=frame.copy();out.attrs.pop('accepted_risk',None)
    if {k:out.attrs.get(k) for k in IDENTITY}!=proof['identity']:return out
    if out.attrs.get('data_quality',{}).get('state') in ('INVALID','SOURCE_UNAVAILABLE','RECOVERING'):return out
    # Existing intraday aggregation already omits damaged buckets. Do not
    # invent their missing high/low/volume or promote a partial bucket.
    if 'bar_complete' in out and not out.bar_complete.fillna(False).all():return out
    report=dq.audit(out,tf,expected=proof['identity'],now=now)
    if report['state'] not in ('READY','GAP','UNCONFIRMED') or report.get('stale_count'):return out
    if tf!=proof['base'] and out.attrs.get('aggregation_base')!=proof['base']:return out
    leading=[];actual_only=proof.get('price_model')==TV_PRICE_MODEL
    try:estimates=[] if actual_only else accepted_close_estimates(out,report,now,leading=leading)
    except (ValueError,KeyError,TypeError):return out
    # The output was just built by the existing aggregator from the verified
    # base input. Narrower calculation windows may waive only these gaps.
    out.attrs['accepted_risk']={**proof,'timeframe':tf,'ranges':report.get('ranges',[]),
        'missing_count':report.get('missing_count',0),'checked_at':pd.Timestamp(now).isoformat(),
        'excluded_incomplete':out.attrs.get('data_quality',{}).get('excluded_incomplete',[]),
        'price_model':PRICE_MODEL,'price_estimates':estimates,'price_estimate_count':len(estimates),
        'unestimated_leading_intervals':leading,
        'initialization_basis':'First genuine bar; no preceding price invented; strict recursive proof unchanged',
        'price_model_scope':'EMA/RSI/MACD: real+estimated Close; ATR/volume/levels/warmup/execution: real bars only',
        'label':'Принятый риск: средние Close в EMA/RSI/MACD; настоящие OHLCV и исходный DQ сохранены'}
    if actual_only:
        out.attrs['accepted_risk'].update(price_model=TV_PRICE_MODEL,price_model_scope='All indicators, warmup and execution use real closed bars only',label=proof.get('label',TV_LABEL))
        from timeframes import endpoints
        excluded=out.attrs['accepted_risk']['excluded_incomplete']
        labels=pd.DataFrame(index=pd.to_datetime(excluded));labels.attrs={**out.attrs,'timeframe':tf}
        labels.attrs.pop('bar_ends',None)
        closed_exclusions=bool(excluded and (endpoints(labels)<=pd.Timestamp(now)).any())
        out.attrs['accepted_risk']['tv_complete_aggregate']=bool(tf!=proof['base']
            and out.attrs.get('tv_aggregation_used') and not closed_exclusions)
    return out

def _queue_repair(db,problem,at,origin=None):
    """Same durable repair request for UI and a current reviewed discrepancy."""
    key='cp_repair_requests:'+problem['isin'];requests=read(db,key,{})
    target=repair_id(problem);prior=requests.get(target,{})
    if prior.get('state') in ('QUEUED','RUNNING','WAITING'):return prior
    # A diagnostic recheck is not another owner retry. Reuse the terminal
    # receipt for the same observation instead of looping provider requests.
    if origin is not None and prior.get('origin')==origin:return prior
    item=dict(problem=problem,state='QUEUED',requested_at=at,attempts=0)
    if origin is not None:item['origin']=origin
    requests[target]=item;write(db,key,requests)
    return item


def request_repair(store,problem):
    isin=problem['isin']
    with store.connect() as db:
        db.execute('BEGIN IMMEDIATE');current={p['id']:p for p in problems(db,isin)}
        if problem['id'] not in current:raise ValueError('Проблема изменилась. Обновите список')
        return _queue_repair(db,current[problem['id']],stamp())

def _automatic_request(item):
    return (item.get('origin') or {}).get('kind')=='AUTOMATIC_DEPENDENCY'


def _automatic_repair_enabled(info,policy):
    return (info.get('kind','stock')=='stock' and policy.get('approved') is True
        and policy.get('version')==1 and policy.get('scope')=='registered_stocks'
        and policy.get('exclusive_venue')=='GETTEX')


def automatic_repair_turn(store,info,now=None):
    """Plan one bounded native repair from saved dependencies, never a permit.

    Same durable requests as the UI. Candidate inputs are priority hints only;
    the writer revalidates current identity/DQ before touching market data.
    """
    import pandas as pd
    from data_quality import STEPS
    from cp_reconciliation import _spec,_input
    isin=info['isin'];clock=pd.Timestamp(now or stamp());key='cp_repair_plan:'+isin
    policy=store.setting('cp_gettex_source_policy',{})
    if not _automatic_repair_enabled(info,policy):return {'state':'NOT_ENABLED'}
    with store.connect() as db:
        db.execute('BEGIN IMMEDIATE')
        if not db.execute('SELECT 1 FROM instruments WHERE isin=?',(isin,)).fetchone():return {'state':'REMOVED'}
        requests=read(db,'cp_repair_requests:'+isin,{})
        active=[x for x in requests.values() if _automatic_request(x) and x.get('state') in ('QUEUED','RUNNING','WAITING')]
        if active:return dict(state='PENDING',request_id=repair_id(active[0]['problem']))
        assessment=read(db,'cp_eligibility:'+isin,{})
        if assessment.get('isin')!=isin:return {'state':'AWAITING_ASSESSMENT'}
        specs=[]
        for candidate in assessment.get('candidates',[]):
            if not candidate.get('analysis_ready'):
                specs.extend(x.get('spec',{}) for x in candidate.get('closed_inputs',{}).get('series',[]))
        for tf,row in assessment.get('timeframes',{}).items():
            # Optional short TFs do not become mandatory through this planner.
            # A real 5m dependency of a higher TF is still included above/below.
            if tf not in ('5m','10m') and not row.get('analysis_ready') and row.get('blockers'):
                specs.append(row.get('_closed_input') or {})
        scopes={}
        for spec in specs:
            meta=spec.get('identity') or {};base=spec.get('base_tf')
            if (spec.get('isin')!=isin or meta.get('source') not in ('gettex','gettex_chart')
                    or meta.get('venue')!='GETTEX' or meta.get('currency')!='EUR'
                    or meta.get('timezone')!='Europe/Berlin' or base not in ('5m','15m','1H','1D')):continue
            try:
                start=pd.Timestamp(spec['from']);end=pd.Timestamp(spec['through'])
                if start.tzinfo is not None:start=start.tz_convert(meta['timezone']).tz_localize(None)
                if end.tzinfo is None:continue
                end=min(end,clock).tz_convert(meta['timezone']).tz_localize(None)
                if pd.isna(start) or pd.isna(end) or start>end:continue
                scopes.setdefault((digest(meta),base),set()).add((start,end))
            except (KeyError,TypeError,ValueError):continue
        candidates={}
        for problem in problems(db,isin):
            if (problem['kind']!='archive' or not problem.get('range')
                    or problem['state'] not in ('GAP','SOURCE_GAP','UNCONFIRMED','INVALID','SOURCE_UNAVAILABLE')):continue
            meta=problem['identity'];base=problem['tf']
            for first,last in scopes.get((digest(meta),base),[]):
                a,b=map(pd.Timestamp,problem['range']);a=max(a,first);b=min(b,last)
                if a>b:continue
                # One date per request keeps fingerprints and native retries
                # bounded even when a saved missing range spans months.
                for day in pd.date_range(a.normalize(),b.normalize(),freq='D'):
                    left=max(a,day);right=min(b,day+pd.Timedelta(days=1)-STEPS[base])
                    if left>right:continue
                    part=dict(problem,range=[str(left),str(right)])
                    part['id']=digest(part);candidates[repair_id(part)]=part
        progress=read(db,key,{})
        # Timestamp cursor survives restart; no fixed first bad interval loop.
        ordered=sorted(candidates,key=lambda k:(candidates[k]['range'][0],k),reverse=True)
        cursor=progress.get('cursor')
        if cursor in ordered:
            index=ordered.index(cursor)+1;ordered=ordered[index:]+ordered[:index]
        result=dict(state='NO_ADDRESSABLE_DEPENDENCY' if not ordered else 'CHECKED',checked=0,candidates=len(ordered))
        for target in ordered[:4]:
            problem=candidates[target];progress['cursor']=target;result['checked']+=1
            try:
                job=_spec(isin,problem['identity'],problem['tf'],*problem['range'])
                inputs=_input(db,job)
            except (ValueError,TypeError,KeyError) as error:
                result['reason']=str(error);continue
            origin=dict(kind='AUTOMATIC_DEPENDENCY',version=1,fingerprint=inputs['fingerprint'])
            prior=requests.get(target,{})
            if prior.get('origin')==origin:
                deadline=prior.get('recheck_after')
                if not deadline or clock<pd.Timestamp(deadline):continue
                origin['retry_after']=deadline
            # retry_after records the last bounded retry epoch, not each poll.
            elif (_automatic_request(prior) and prior['origin'].get('fingerprint')==origin['fingerprint']):
                deadline=prior.get('recheck_after')
                if not deadline or clock<pd.Timestamp(deadline):continue
                origin['retry_after']=deadline
            item=_queue_repair(db,problem,clock.isoformat(),origin)
            result.update(state=item['state'],request_id=target,range=problem['range'],identity=problem['identity'],base=problem['tf'])
            break
        progress.update(result,last_turn=clock.isoformat());write(db,key,progress)
        return result


def due_request(db,isin=None,now=None,include_automatic=True):
    import pandas as pd
    clock=pd.Timestamp(now or stamp());pending=[]
    for key,value in db.execute('SELECT key,value FROM settings WHERE key LIKE ?',('cp_repair_requests:'+(isin or '')+'%',)):
        for item in json.loads(value).values():
            if not include_automatic and _automatic_request(item):continue
            if item.get('state') not in ('QUEUED','RUNNING','WAITING'):continue
            if item.get('next_retry') and clock<pd.Timestamp(item['next_retry']):continue
            pending.append(item)
    # Rotate actual recovery attempts, not creation times. Otherwise the first
    # empty range repeatedly renews the series cooldown and starves every later
    # request. A cooldown-only visit is not an attempt. Legacy receipts already
    # contain attempts/started_at, so no queue migration or reset is necessary.
    def order(item):
        attempted=item.get('last_attempt_at') or item.get('legacy_rotation_at') or (item.get('started_at') if item.get('attempts',0)>0 else None)
        return _automatic_request(item),pd.Timestamp(attempted or item['requested_at']),pd.Timestamp(item['requested_at'])
    return min(pending,key=order) if pending else None

def service_request(info,store,recover_gettex,now=None):
    """One bounded owner-priority turn on the SAME serial feed writer."""
    import pandas as pd
    import data_quality as dq
    import history_service
    from eur_listings import confirmed_listing
    clock=pd.Timestamp(now or stamp());isin=info['isin'];key='cp_repair_requests:'+isin
    with store.connect() as db:
        db.execute('BEGIN IMMEDIATE');item=due_request(db,isin,clock)
        if item is None:return None
        if _automatic_request(item):
            if not _automatic_repair_enabled(info,read(db,'cp_gettex_source_policy',{})):return None
            plan=read(db,'cp_repair_plan:'+isin,{})
            if plan.pop('yield_regular',False):
                write(db,'cp_repair_plan:'+isin,plan)
                return None
            plan['yield_regular']=True;write(db,'cp_repair_plan:'+isin,plan)
        requests=read(db,key,{});p=item['problem'];item=dict(item)
        if not item.get('last_attempt_at') and item.get('attempts',0)>0:
            # Preserve one stable legacy ordering seed before started_at is
            # reused for a cooldown-only visit. It is NOT a measured attempt
            # time: old receipts did not distinguish attempts from waiting.
            item.setdefault('legacy_rotation_at',item.get('started_at') or item['requested_at'])
        item.update(state='RUNNING',started_at=clock.isoformat())
        target=repair_id(p);requests[target]=item;write(db,key,requests)
    try:
        with store.connect() as db:current=problems(db,isin)
        matches=any(repair_id(q)==target for q in current)
        if _automatic_request(item):
            # Automatically split daily ranges remain bound to a CURRENT saved
            # defect of this exact source. No arbitrary queued range is enough.
            matches=any(q.get('range') and all(q.get(k)==p.get(k) for k in ('kind','identity','tf','report_key'))
                and pd.Timestamp(q['range'][0])<=pd.Timestamp(p['range'][0])<=pd.Timestamp(p['range'][1])<=pd.Timestamp(q['range'][1]) for q in current)
        if not matches:out=dict(state='SUPERSEDED',reason='Диапазон или identity изменились; старая задача не выполняется. Это не подтверждение исправления')
        elif p['kind']=='context':
            from calculation_window import eligibility_turn
            assessment=eligibility_turn(store,info) or {}
            unit=assessment.get('timeframes',{}).get(p['tf'],{})
            remaining=unit.get('blockers',[])
            out=dict(state='BLOCKED' if remaining or assessment.get('error') else 'RECHECKED',
                reason='; '.join(remaining) or assessment.get('error') or 'Штатная оценка обновлена; это не разрешение сделки')
            if assessment.get('deferred'):
                out=dict(state='WAITING',next_retry=(clock+pd.Timedelta(minutes=5)).isoformat(),reason='Оценка поставлена в существующую очередь расчётов')
        elif not p.get('range') or p['tf'] not in ('5m','15m','1H','1D'):
            out=dict(state='BLOCKED',reason='Нет адресного повреждённого диапазона для recovery. CORE/прогрев нельзя исправить вымышленными свечами')
        else:
            from eur_listings import stored_identity
            with store.connect() as db:meta=stored_identity(db,info,p['identity'],p['tf'])
            if not meta:raise ValueError('Источник и листинг не подтверждены')
            previous=store.setting(dq.key(info,meta['symbol'],p['tf']),{})
            delays=[store.setting('cp_source_retry:'+meta['source']),previous.get('next_retry'),previous.get('raw_retry_after')]
            if meta['source']=='gettex_chart':
                from gettex_chart import recovery_wait
                delays.append(recovery_wait(store,info,meta,p['tf'],clock))
            future=[pd.Timestamp(t) for t in delays if t and pd.Timestamp(t)>clock]
            # Validate available local data even during provider cooldown. The
            # bounded budget covers legacy/owner requests as well as new ones.
            capped=item.get('attempts',0)>=3
            if meta['source']=='gettex':adapter=lambda symbol,interval,start,end=None:recover_gettex(symbol,start,end,p['tf'])
            elif meta['source']=='gettex_chart':
                from gettex_chart import recovery_download
                adapter=lambda symbol,interval,start,end=None:recovery_download(store,info,meta,p['tf'],start,end,clock)
            elif meta['source']=='proxy':adapter=lambda symbol,interval,start,end=None:history_service.download(symbol,interval,start,end,store=store,expected=meta)
            else:raise ValueError('Для этого источника нет подтверждённого адресного recovery')
            attempted=False
            def callback(symbol,interval,start,end=None):
                nonlocal attempted
                attempted=True
                return adapter(symbol,interval,start,end=end)
            try:
                report=dq.repair(info,store,meta['symbol'],p['tf'],meta,callback,clock,max_ranges=1,target_range=p['range'],
                    local_only=capped or bool(future),network_not_before=max(future).isoformat() if future else None)
            finally:
                if attempted:
                    item['last_attempt_at']=clock.isoformat();item['attempts']=item.get('attempts',0)+1
            if report.get('recovery_provider_attempted') and not attempted:
                item['last_attempt_at']=clock.isoformat();item['attempts']=item.get('attempts',0)+1
            a,b=map(pd.Timestamp,p['range']);remaining=any(pd.Timestamp(x)<=b and pd.Timestamp(y)>=a for x,y in report.get('ranges',[]))
            if not remaining and report.get('state') not in ('INVALID','SOURCE_UNAVAILABLE','RECOVERING'):
                out=dict(state='COMPLETED',reason='Выбранный диапазон больше не имеет этой DQ-проблемы; остальные проверки сохранены')
            elif report.get('next_retry') or future:
                retries=[pd.Timestamp(t) for t in [report.get('next_retry'),*future] if t]
                out=dict(state='WAITING',next_retry=max(retries).isoformat(),reason=report.get('recovery_error') or report.get('state'))
            else:out=dict(state='BLOCKED',reason=report.get('recovery_constraints',{}).get('status') or report.get('state','UNCONFIRMED'))
            out['quality']=report.get('state')
        item.update(out,finished_at=stamp())
    except Exception as error:item.update(state='BLOCKED',reason=str(error),finished_at=stamp())
    if _automatic_request(item) or item.get('attempts',0)>=3:
        if item.get('state')=='WAITING' and item.get('attempts',0)>=3:
            item.update(state='BLOCKED',reason='Три адресные попытки без исправления; новая попытка при новых доказательствах или плановой перепроверке. '+item.get('reason',''))
        if item.get('state')=='BLOCKED':
            item['recheck_after']=max(clock+pd.Timedelta(days=7),pd.Timestamp(item.get('next_retry') or clock)).isoformat()
    with store.connect() as db:
        db.execute('BEGIN IMMEDIATE');requests=read(db,key,{})
        requests[target]=item;write(db,key,requests)
    return item
