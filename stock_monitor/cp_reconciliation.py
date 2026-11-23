"""Final review stage on CandlePilot's existing feed lane.

Metadata only: no TradingView scraping, prices, indicators, orders or new grants.
Native recovery remains responsible for OHLCV/DQ and retry limits. A visual
observation cannot certify market data or transfer evidence between timebases.
"""
import hashlib
import json
import platform
from pathlib import Path
from datetime import datetime, timezone

from cp_controls import read, write, digest

PREFIX = 'cp_reconciliation_job:'
PROGRESS = 'cp_reconciliation_progress:'
VERSION = 1
FINGERPRINT_VERSION = 4
INGEST_LIMIT = 4
CHECK_LIMIT = 4
SCAN_LIMIT = 64
BATCH_LIMIT = 4
REPAIR_RECHECK_VERSION = 2
OBSERVATIONS = ('ABSENT', 'ZERO_VOLUME', 'PRESENT', 'DIFFERENT', 'UNCONFIRMED')
SOURCE_POLICY = 'cp_gettex_source_policy'
FAILURES = {'EMPTY_RESPONSE', 'INCOMPLETE_RESPONSE', 'INVALID_RESPONSE',
            'AVAILABLE_FILES_NO_MATCH', 'SOURCE_LIMITED'}


def capability():
    return dict(mode='ASSISTED_ONLY', platform=platform.system(),
        desktop_controller=False, authenticated_session='UNKNOWN_TO_CANDLEPILOT',
        non_display_data_rights='NOT_CONFIRMED', automated_requests=False,
        indicator_comparison='NOT_VERIFIED: identical settings and licensed data required',
        reason='GUI-извлечение: LEGAL/BLOCKED без отдельных прав. Официальный MCP существует, но OAuth CP не подключён. Доступ Work не является доступом CP.',
        gui_automation='LEGAL/BLOCKED', official_mcp='AUTH_REQUIRED / ENTITLEMENT_UNCONFIRMED',
        official_mcp_url='https://mcp.tradingview.com/mcp',
        official_mcp_docs='https://www.tradingview.com/mcp/docs',
        terms_checked_at='2026-11-10', terms_url='https://www.tradingview.com/policies/')


def key(job):
    return PREFIX + job['isin'] + ':' + job['id']


def due_observation(db, infos, now=None):
    """Schedule saved human evidence, never authorize it or request TV data.

    Reuse the owner-priority feed slot; the normal turn still checks current
    identity, native rows and revisions. A short per-instrument interval gives
    other feeds time when a large review batch needs several bounded turns.
    """
    import pandas as pd
    clock=pd.Timestamp(now or datetime.now(timezone.utc))
    registered={i['isin'] for i in infos if i.get('kind')!='benchmark'}
    candidates=[]
    for k,raw in db.execute('SELECT key,value FROM settings WHERE key LIKE ?', (PROGRESS+'%',)):
        isin=k[len(PROGRESS):]
        if isin not in registered:continue
        progress=json.loads(raw)
        if not progress.get('counts',{}).get('RECHECK_QUEUED'):continue
        if progress.get('last_turn') and clock<pd.Timestamp(progress['last_turn'])+pd.Timedelta(minutes=5):continue
        for value, in db.execute('SELECT value FROM settings WHERE key LIKE ?', (PREFIX+isin+':%',)):
            job=json.loads(value);observation=job.get('observation') or {}
            if job.get('status')!='RECHECK_QUEUED' or not observation.get('identity_confirmed') or not observation.get('at'):continue
            if pd.Timestamp(job.get('next_check',observation['at']))>clock:continue
            observed=pd.Timestamp(observation['at'])
            # An old multi-part batch must yield after its bounded turn. If
            # feeds take longer than the five-minute spacing, sorting only by
            # observation time repeatedly selects that batch ahead of other
            # waiting instruments. Preserve exact observations and cooldowns;
            # rotate using the last actual reconciliation turn, not polling.
            serviced=max(observed,pd.Timestamp(progress['last_turn'])) if progress.get('last_turn') else observed
            candidates.append((serviced,observed,isin,job['id']))
    if not candidates:
        # A deployed repair may predate the dependent-scope wake-up. Read only
        # bounded receipt metadata here; the owned feed worker revalidates the
        # full source proof and performs the actual queue updates.
        from revision_watcher import RETIME_PREFIX
        rows=db.execute("""SELECT r.value FROM settings r LEFT JOIN settings p
          ON p.key=? || json_extract(r.value,'$.isin')
          WHERE r.key>=? AND r.key<? AND json_extract(r.value,'$.state')='APPLIED'
          AND json_extract(r.value,'$.mode') IN ('VERIFIED_NATIVE_CLOSING_TICK','VERIFIED_PRICE_CLOSING_TICK_PARTITION','VERIFIED_NATIVE_PRICE_SAMPLES')
          AND (COALESCE(json_extract(p.value,'$.native_repair_rechecks_version'),0)<>2 OR NOT EXISTS
            (SELECT 1 FROM json_each(COALESCE(json_extract(p.value,'$.native_repair_rechecks_seen'),'[]')) WHERE value=r.key))
          AND (json_extract(p.value,'$.last_turn') IS NULL OR julianday(json_extract(p.value,'$.last_turn'))<=julianday(?))
          ORDER BY json_extract(r.value,'$.created_at'),r.key LIMIT 8""",
          (PROGRESS,RETIME_PREFIX,RETIME_PREFIX+'\uffff',(clock-pd.Timedelta(minutes=5)).isoformat())).fetchall()
        for raw, in rows:
            receipt=json.loads(raw)
            if receipt['isin'] in registered:
                return dict(isin=receipt['isin'],job_id=receipt['job_id'],reason='NATIVE_REPAIR_FOLLOWUP')
        return None
    _,at,isin,jid=min(candidates)
    return dict(isin=isin,job_id=jid,observed_at=at.isoformat(),reason='SAVED_OBSERVATION_RECHECK')


def _save_job(db, job, previous=None):
    progress=read(db,PROGRESS+job['isin'],{})
    counts=progress.setdefault('counts',{})
    if previous:counts[previous['status']]=max(0,counts.get(previous['status'],0)-1)
    counts[job['status']]=counts.get(job['status'],0)+1
    write(db,key(job),job);write(db,PROGRESS+job['isin'],progress)


def _spec(isin, meta, tf, first, last):
    import pandas as pd
    from data_quality import IDENTITY, STEPS
    from timeframes import LABELS
    if tf not in (*LABELS, '1m') or meta.get('currency') != 'EUR' or meta.get('venue') != 'GETTEX':
        raise ValueError('RECONCILIATION_IDENTITY_OR_TF')
    if not all(meta.get(k) for k in IDENTITY) or meta['timezone'] != 'Europe/Berlin':
        raise ValueError('RECONCILIATION_IDENTITY_OR_TF')
    a, b = pd.Timestamp(first), pd.Timestamp(last)
    if a.tzinfo is not None or b.tzinfo is not None or a > b or b-a > pd.Timedelta(days=8):
        raise ValueError('RECONCILIATION_REQUIRES_BOUNDED_LOCAL_RANGE')
    if tf=='1W' and (a.dayofweek!=4 or b.dayofweek!=4 or a!=a.normalize() or b!=b.normalize()):
        raise ValueError('RECONCILIATION_WEEK_LABEL')
    if tf=='1M' and (a.day!=1 or b.day!=1 or a!=a.normalize() or b!=b.normalize()):
        raise ValueError('RECONCILIATION_MONTH_LABEL')
    start=a-pd.Timedelta(days=6) if tf=='1W' else a
    end = b + (STEPS[tf] if tf in STEPS else pd.Timedelta(days=1) if tf == '1W' else pd.DateOffset(months=1))
    spec = dict(isin=isin, identity={k:meta[k] for k in IDENTITY}, tf=tf,
                first=str(a), last=str(b), interval_start=str(start), end=str(end), session_basis='verified venue calendar',
                timezone=meta['timezone'])
    return dict(spec, id=digest(spec))


def _input(db, job):
    """Exact local range and source proof. Unrelated new bars are not inputs."""
    import pandas as pd
    from eur_listings import stored_identity
    isin=job['isin'];meta=job['identity'];tf=job['tf']
    row=db.execute('SELECT payload FROM instruments WHERE isin=?',(isin,)).fetchone()
    if not row:raise ValueError('INSTRUMENT_REMOVED')
    info=json.loads(row[0]);listing=stored_identity(db,info,meta,tf)
    if not listing:raise ValueError('IDENTITY_UNCONFIRMED')
    if meta['source']=='gettex':
        table='candles';where='isin=? AND source=? AND base_tf=?';args=(isin,'gettex',tf)
    else:
        table='series';where='isin=? AND source=? AND venue=? AND currency=? AND symbol=? AND base_tf=?'
        args=(isin,meta['source'],meta['venue'],meta['currency'],meta['symbol'],tf)
    rows=db.execute('SELECT ts,open,high,low,close,volume FROM '+table+' WHERE '+where+
                    ' AND ts>=? AND ts<? ORDER BY ts LIMIT 1025',(*args,job['first'],job['end'])).fetchall()
    before=db.execute('SELECT ts,open,high,low,close,volume FROM '+table+' WHERE '+where+' AND ts<? ORDER BY ts DESC LIMIT 1',(*args,job['first'])).fetchone()
    after=db.execute('SELECT ts,open,high,low,close,volume FROM '+table+' WHERE '+where+' AND ts>=? ORDER BY ts LIMIT 1',(*args,job['end'])).fetchone()
    if len(rows)>1024:raise ValueError('RECONCILIATION_REQUIRES_BOUNDED_LOCAL_RANGE')
    proof=read(db,'interval_evidence:'+isin,{})
    relevant={k:v for k,v in proof.items() if k not in ('no_trades','trade_intervals','zero_quantity_intervals','incomplete_publication_intervals','reference','exchange_closures','closure_checked_at')}
    closures=[p for p in proof.get('exchange_closures',[]) if pd.Timestamp(p['start'])<pd.Timestamp(job['end']) and pd.Timestamp(p['end'])>pd.Timestamp(job['interval_start'])]
    if closures:relevant.update(exchange_closures=closures,closure_checked_at=proof.get('closure_checked_at'))
    for field in ('no_trades','trade_intervals','zero_quantity_intervals'):
        relevant[field]=[p for p in proof.get(field,[]) if pd.Timestamp(p[0])<pd.Timestamp(job['end']) and pd.Timestamp(p[1])>pd.Timestamp(job['interval_start'])]
    incomplete=[p for p in proof.get('incomplete_publication_intervals',[]) if pd.Timestamp(p[0])<pd.Timestamp(job['end']) and pd.Timestamp(p[1])>pd.Timestamp(job['interval_start'])]
    # New local evidence must not revoke reviews of independent old intervals.
    # Omit the absent key to retain the existing v4 hash outside this scope.
    if incomplete:relevant['incomplete_publication_intervals']=incomplete
    # Preserve the original authoritative reference, never a TV observation.
    relevant['reference']={k:v for k,v in proof.get('reference',{}).items()
                          if k.startswith('GETTEX:') and job['interval_start'][:10].replace('-','')<=k[7:]<=job['end'][:10].replace('-','')}
    report_key='data_quality:'+isin+':'+meta['symbol']+':'+tf
    report=read(db,report_key,{})
    result=dict(info=info,listing=listing,rows=rows,neighbors={'before':before,'after':after},proof=relevant,
                native_retry=report.get('next_retry'),report_key=report_key)
    from revision_watcher import RETIME_PREFIX,retime_current
    correction=read(db,RETIME_PREFIX+isin+':'+job['id'],{})
    if correction and retime_current(db,correction):
        result['official_retiming_empty']=[dict(first=correction['first'],end=correction['end'],
            authority={**proof['identity'],'absence_evidence':{**correction['source']['proof'],'checked_at':proof['checked_at']}})]
    # A mixed range can contain both real rows and a separately confirmed empty
    # child. Current primary-source receipts validate BOTH native5m/15m
    # windows. Visual observations alone never transfer between feeds/TF.
    if tf in ('5m','15m') and rows and len(rows)<int((pd.Timestamp(job['last'])-pd.Timestamp(job['first']))/pd.Timedelta(minutes=5 if tf=='5m' else 15))+1:
        prefix=RETIME_PREFIX+isin+':'
        siblings=db.execute("""SELECT value FROM settings WHERE key>=? AND key<? AND key<>?
          AND json_extract(value,'$.state')='APPLIED' AND json_extract(value,'$.mode') IN ('VERIFIED_NATIVE_EMPTY','VERIFIED_NATIVE_TRADE_PARTITION','VERIFIED_NATIVE_SESSION_TAIL','VERIFIED_NATIVE_POSITIVE_PRICE_EXTREME','VERIFIED_PRIMARY_WINDOW')
          AND json_extract(value,'$.first')<? AND json_extract(value,'$.end')>?
          AND json_extract(value,'$.identity.source')=? AND json_extract(value,'$.identity.symbol')=?
          AND json_extract(value,'$.identity.venue')=? AND json_extract(value,'$.identity.currency')=?
          AND json_extract(value,'$.identity.timezone')=? ORDER BY key LIMIT 8""",
          (prefix,prefix+'\uffff',RETIME_PREFIX+isin+':'+job['id'],job['end'],job['first'],
           *[meta[k] for k in ('source','symbol','venue','currency','timezone')])).fetchall()
        for raw, in siblings:
            sibling=json.loads(raw)
            if retime_current(db,sibling):
                result.setdefault('official_retiming_empty',[]).append(dict(first=sibling['first'],end=sibling['end'],
                    authority={**proof['identity'],'absence_evidence':{**sibling['source']['proof'],'checked_at':proof['checked_at']}}))
    result['fingerprint']=_fingerprint(result)
    if job.get('observation') and job['observation'].get('input_fingerprint')!=result['fingerprint']:
        from revision_watcher import reviewed_context_fingerprint
        context=reviewed_context_fingerprint(db,job,result)
        if context:result['review_context_fingerprint']=context
    return result


def _instrument_identity(info):
    # A visual market observation is independent of drawings, indicator toggles,
    # portfolio state and display names. _input still validates the current
    # listing against owner venue policy before this binding can be used.
    return {'isin':info['isin'], 'kind':info.get('kind','stock')}


def _fingerprint(inputs, proof=None, legacy=False, version=FINGERPRINT_VERSION):
    # Rechecking unchanged evidence is not a changed visual observation.
    # Keep the actual timestamp in proof: _classify/DQ still evaluate it now.
    # No quote age, reference, covered range, event or identity is discarded.
    listing=dict(inputs['listing'])
    if isinstance(listing.get('listing_proof'),dict):
        listing['listing_proof']={k:v for k,v in listing['listing_proof'].items() if k!='verified_at'}
    proof=dict(inputs['proof'] if proof is None else proof)
    if not legacy:proof.pop('checked_at',None)
    instrument=inputs['info'] if version<4 else _instrument_identity(inputs['info'])
    bound=[instrument,listing,inputs['rows'],inputs['neighbors']['before'],inputs['neighbors']['after'],proof]
    if inputs.get('official_retiming_empty'):
        bound.append(inputs['official_retiming_empty'])
    return digest(bound)


def _observation_current(job, inputs):
    observation=job.get('observation') or {}
    if not observation:return False
    if observation.get('input_fingerprint')==inputs['fingerprint']:return True
    if (observation.get('fingerprint_version')==FINGERPRINT_VERSION
            and inputs.get('review_context_fingerprint') is not None
            and inputs.get('review_context_fingerprint')==observation.get('input_fingerprint')):return True
    # Require the *old complete hash* before migration. Already stale reviews
    # cannot be revived merely by removing non-market fields from the binding.
    if (observation.get('fingerprint_version')==3 and
            _fingerprint(inputs,version=3)==observation.get('input_fingerprint')):return True
    # The GETTEX owner policy added this display/history selection field after
    # some v3 reviews. Reconstruct their COMPLETE old hash with that field
    # absent, never accept an arbitrary metadata or market-data difference.
    # _input already validates the current listing under the current lock.
    if observation.get('fingerprint_version')==3 and 'history_venue_lock' in inputs['info']:
        previous=dict(inputs,info={k:v for k,v in inputs['info'].items() if k!='history_venue_lock'})
        if _fingerprint(previous,version=3)==observation.get('input_fingerprint'):return True
    # Reconstruct the exact prior v2 hash, not a blanket migration. If any
    # native row, neighbor, instrument, source or proof content changed, no
    # previous check timestamp can make the old observation match.
    if observation.get('fingerprint_version',2)==2 and job.get('fingerprint_version',1)>=2:
        findings=[job.get('finding',{}),*[h.get('finding',{}) for h in job.get('history',[])]]
        times={inputs['proof'].get('checked_at'),observation.get('evidence_checked_at'),
               *(f.get('evidence_checked_at') for f in findings)}
        for at in times:
            if at is None:continue
            proof={**inputs['proof'],'checked_at':at}
            if _fingerprint(inputs,proof,legacy=True,version=3)==observation.get('input_fingerprint'):return True
    # One-time metadata migration from v1, whose fingerprint included volatile
    # listing verification time. Retain an old visual observation only against
    # its saved exact native rows/neighbours and unchanged authoritative proof.
    # Current listing/ISIN/venue/currency is independently revalidated by _input.
    return (job.get('fingerprint_version',1)==1 and
        observation.get('input_fingerprint')==job.get('input_fingerprint') and
        'native_rows' in job and digest(job['native_rows'])==digest(inputs['rows']) and
        digest(job.get('native_neighbors'))==digest(inputs['neighbors']) and
        job.get('finding',{}).get('evidence_digest')==digest(inputs['proof']))


def _classify(job, inputs, now):
    import pandas as pd
    import data_quality as dq
    from timeframes import schedule
    meta=job['identity'];a,b=pd.Timestamp(job['interval_start']),pd.Timestamp(job['end'])
    state='UNCONFIRMED';reason='Нет достаточных собственных биржевых доказательств';market='UNCONFIRMED'
    rows=inputs['rows'];proof=inputs['proof']
    if rows:
        frame=pd.DataFrame([r[1:] for r in rows],index=pd.to_datetime([r[0] for r in rows]),columns=dq.FIELDS)
        frame.attrs={**meta,'timeframe':job['tf'],'classify_absence':True,
                     'absence_evidence':proof if proof.get('isin')==job['isin'] else {}}
        if inputs.get('official_retiming_empty'):
            frame.attrs['official_retiming_empty']=inputs['official_retiming_empty']
        check=dq.audit(frame,job['tf'],expected=meta,now=now,check_stale=False)
        expected=dq.expected_index(meta,job['tf'],job['first'],job['last'],now)
        absent=expected.difference(frame.index)
        confirmed=dq.covered_empty(frame.attrs,absent,job['tf'],now)
        complete=check['state']=='READY' and len(expected)>0 and not len(absent.difference(confirmed))
        observation=job.get('observation') or {}
        current=_observation_current(job,inputs)
        # Valid OHLCV geometry does not settle an observed price disagreement.
        # Keep the current exact-identity review open without importing TV prices
        # or changing native DQ. A newer native revision invalidates this review.
        different=current and observation.get('identity_confirmed') and observation.get('result')=='DIFFERENT'
        if different and check['state']!='INVALID':
            return dict(state='SOURCE_DISCREPANCY',discrepancy='TV_DIFFERENCE',
                reason='Свеча есть, но её значения отличаются от подтверждённой сверки TradingView; наличие строки не устраняет расхождение',
                native_dq=check['state'],market_state=market,can_affect_dq=False,
                native_complete=complete,
                confirmed_empty_intervals=len(confirmed),actual_candles=len(frame),
                observation_current=True,evidence_digest=digest(proof),evidence_checked_at=proof.get('checked_at'))
        result=dict(state='NATIVE_ROWS_CONFIRMED' if complete else 'NATIVE_ROWS_PRESENT' if check['state']!='INVALID' else 'INVALID',
                    reason='Штатный DQ обнаружил строки; окончательная полнота и допуск проверяются существующим pipeline',
                    native_dq=check['state'],market_state=market,can_affect_dq=False,
                    confirmed_empty_intervals=len(confirmed),actual_candles=len(frame),
                    observation_current=current)
        if result['state']=='NATIVE_ROWS_PRESENT':result.update(_publication_wait(job,inputs))
        return result
    # Identity alone omits TF; otherwise historical daily reviews incorrectly
    # take the narrower intraday calendar and fail before comparing evidence.
    sessions=schedule({**meta,'timeframe':job['tf']},a,b)
    lo=a.tz_localize(meta['timezone']).tz_convert('UTC');hi=b.tz_localize(meta['timezone']).tz_convert('UTC')
    if not any(r.open<hi and r.close>lo for _,r in sessions.iterrows()):
        state=market='MARKET_CLOSED';reason='Подтверждённый календарь: торговая сессия отсутствует'
    elif hi>now:
        state='PENDING';reason='Интервал ещё не завершён'
    elif hi+pd.Timedelta(minutes=15)>now:
        state='DELAYED';reason='Ожидание публикации; 15 минут — существующий запас, не измеренная задержка'
    else:
        source_meta=proof.get('identity',{})
        valid_market=(proof.get('isin')==job['isin'] and source_meta.get('source')=='gettex'
                      and source_meta.get('symbol')==job['isin'] and source_meta.get('venue')==meta['venue']
                      and source_meta.get('currency')=='EUR' and source_meta.get('timezone')==meta['timezone'])
        if valid_market:
            authority={**source_meta,'absence_evidence':proof}
            active=[(max(a,r.open.tz_convert(meta['timezone']).tz_localize(None)),
                     min(b,r.close.tz_convert(meta['timezone']).tz_localize(None)))
                    for _,r in sessions.iterrows() if r.open<hi and r.close>lo]
            if any(dq.known_trade(authority,x,y,now) for x,y in active):market='GAP'
            # A zero-quantity publication elsewhere in the archive is not
            # evidence about this interval. Keep half-open session boundaries;
            # overlapping unresolved records still forbid NO_TRADES.
            elif any(pd.Timestamp(x)<right and pd.Timestamp(y)>left
                     for left,right in active
                     for x,y in proof.get('zero_quantity_intervals',[])):
                market='ZERO_QUANTITY_UNCONFIRMED'
            elif active and all(dq.no_trades_proof(authority,x,y,now) for x,y in active):market='NO_TRADES'
        closure=dq.market_closed_proof({**meta,'absence_evidence':proof},a,b,now)
        if closure and not dq.closure_conflict({**meta,'absence_evidence':proof},a,b,now):
            state=market='MARKET_CLOSED';reason='Официальная остановка GETTEX: '+closure['start']+' — '+closure['end']+'; '+closure['reference']
        elif closure:
            state=market='UNCONFIRMED';reason='EXCHANGE_NOTICE_CONFLICT: биржевое уведомление противоречит опубликованным записям'
        elif market=='NO_TRADES':
            verified_local=any(pd.Timestamp(s['first'])<=a<b<=pd.Timestamp(s['end'])
                and dq.no_trades_proof(s['authority'],a,b,now)
                for s in inputs.get('official_retiming_empty',[]))
            if verified_local:
                state='NO_TRADES';reason='Полные публикации GETTEX и точное совпадение окружающего окна 5m/15m; локальная временная привязка перепроверена'
            elif all(source_meta.get(k)==meta.get(k) for k in dq.IDENTITY):
                state='NO_TRADES';reason='Полное собственное доказательство совпадает с source/identity/timebase'
            else:
                state='SOURCE_TIMEBASE_UNCONFIRMED';reason='Биржевые сделки отсутствуют, но временная семантика исходного ряда другая; доказательство не переносится'
        elif market=='GAP':state='GAP';reason='Официальный источник подтверждает сделки; свеча отсутствует'
        elif market=='ZERO_QUANTITY_UNCONFIRMED':reason='Есть нулевая запись; она не доказывает исполнение или отсутствие сделок'
    observation=job.get('observation') or {}
    visual=observation.get('result') if _observation_current(job,inputs) else None
    discrepancy=('TV_ROW_MISSING_IN_SM' if visual in ('PRESENT','ZERO_VOLUME') else
                 'TV_DIFFERENCE' if visual=='DIFFERENT' else 'MISSING_IN_BOTH' if visual=='ABSENT' else None)
    result=dict(state=state,reason=reason,market_state=market,discrepancy=discrepancy,
        can_affect_dq=state in ('NO_TRADES','MARKET_CLOSED'),
        traded_volume=0 if market=='NO_TRADES' else None,event_count=0 if market=='NO_TRADES' else None,
        zero_values_are='metadata only; no OHLCV row',
        evidence_digest=digest(proof),evidence_checked_at=proof.get('checked_at'),
        observation_current=bool(visual),indicator_timebase='Actual closed candles only; confirmed empty intervals add no observations or warmup')
    if state=='UNCONFIRMED':result.update(_publication_wait(job,inputs))
    return result


def _publication_wait(job,inputs):
    """A known partial official publication needs source recovery, not a click.

    This describes an existing GETTEX acquisition task. It supplies no absence
    proof, candle, waiver or new request; chart-vendor gaps remain independent.
    """
    import pandas as pd
    meta=job['identity'];proof=inputs['proof']
    if (meta.get('source')!='gettex' or meta.get('venue')!='GETTEX' or meta.get('currency')!='EUR'
            or meta.get('symbol')!=job['isin'] or meta.get('timezone')!='Europe/Berlin'
            or proof.get('isin')!=job['isin'] or proof.get('identity')!=meta
            or proof.get('basis')!='complete_published_trade_files'):return {}
    try:
        a,b=pd.Timestamp(job['interval_start']),pd.Timestamp(job['end'])
        if a.tzinfo is not None or b.tzinfo is not None or a>=b:return {}
        for first,last in proof.get('incomplete_publication_intervals',[]):
            left,right=pd.Timestamp(first),pd.Timestamp(last)
            if left.tzinfo is None and right.tzinfo is None and left<right and left<b and right>a:
                return dict(provider_wait=True,source_action='existing_gettex_acquisition',
                    reason='SOURCE_COVERAGE_UNCONFIRMED: неполная публикация GETTEX; штатная очередь ждёт подтверждённую полную пару MUNC/MUND, пустой ответ не доказывает отсутствие сделок')
    except (ValueError,TypeError):pass
    return {}


def _enqueue(db, spec, origin, now):
    previous=read(db,key(spec),{})
    if previous:return previous,False
    job=dict(spec,version=VERSION,fingerprint_version=FINGERPRINT_VERSION,created_at=now.isoformat(),status='QUEUED',
             origin=origin,next_check=now.isoformat(),checks=0,history=[],
             capability=capability(),review_required=True)
    _save_job(db,job)
    return job,True


def _queue_observed_recovery(db,job,inputs,finding,at):
    """A fresh PRESENT finding prioritizes native recovery, never TV import."""
    observation=job.get('observation') or {}
    if (observation.get('result')!='PRESENT' or not observation.get('identity_confirmed')
            or not _observation_current(job,inputs) or inputs['rows']
            or job['tf'] not in ('5m','15m','1H','1D')
            or finding.get('state') not in ('GAP','UNCONFIRMED','SOURCE_TIMEBASE_UNCONFIRMED')
            or finding.get('reason','').startswith('EXCHANGE_NOTICE_CONFLICT')):return
    from cp_controls import problems,_queue_repair,repair_id
    # Only the still-existing exact native DQ interval can be scheduled. Do
    # not expand an observation to a different source, timeframe or gap range.
    for problem in problems(db,job['isin']):
        if (problem['kind']=='archive' and problem['identity']==job['identity']
                and problem['tf']==job['tf'] and problem.get('range')==[job['first'],job['last']]
                and problem['state'] in ('GAP','SOURCE_GAP','UNCONFIRMED')):
            origin=dict(kind='CURRENT_TV_PRESENCE',job_id=job['id'],
                observation_digest=digest(observation),input_fingerprint=inputs['fingerprint'])
            request=_queue_repair(db,problem,at,origin)
            job['native_recovery_request']=dict(id=repair_id(problem),state=request['state'],
                requested_at=request['requested_at'],basis='Existing native recovery; cooldown and DQ unchanged')
            return


def _comparison(job, inputs, finding, clock):
    """Diagnostics of explicitly reviewed scopes, never a provider admission.

    PRESENT is not evidence of equal OHLCV. Neither free text nor a screenshot
    filename is parsed into prices. Broad ranges are not counted as one candle.
    """
    import pandas as pd
    observation=job.get('observation') or {}
    current=bool(observation.get('identity_confirmed') and _observation_current(job,inputs))
    closed=pd.Timestamp(job['end']).tz_localize(job['timezone']).tz_convert('UTC')<=clock
    usable=current and closed
    result=observation.get('result') if usable else None
    single=job['first']==job['last']
    present=any(pd.Timestamp(row[0])==pd.Timestamp(job['first']) for row in inputs['rows'])
    presence=((not present) if result=='ABSENT' else present if result in ('PRESENT','ZERO_VOLUME') else None) if single else None
    return dict(isin=job['isin'],identity=job['identity'],tf=job['tf'],reference='TradingView Desktop',
        scope_id=job['id'],scope=[job['first'],job['last']],single_interval=single,
        reference_checked_at=observation.get('at'),native_checked_at=clock.isoformat(),
        observation_current=current,closed=closed,observation_result=result,
        presence_match=presence,ohlcv_match=None,
        value_discrepancy=bool(result=='DIFFERENT'),native_rows=len(inputs['rows']),
        native_result=finding['state'],mode='DIAGNOSTIC_ONLY',
        limitation='Selected problem intervals, not a representative source-quality sample. OHLCV equality is UNKNOWN unless separately measured; presence is not NO_TRADES or trading permission.')


def _weekly_review_enabled(job, policy):
    meta=job.get('identity',{})
    return bool(policy and policy.get('version')==1 and policy.get('approved') is True
        and policy.get('source')=='gettex' and policy.get('review_days')==7
        and meta.get('source') in ('gettex','gettex_chart') and meta.get('venue')=='GETTEX'
        and meta.get('currency')=='EUR' and meta.get('timezone')=='Europe/Berlin')


def _source_disposition(job, finding, policy):
    """Owner acceptance of a visual difference, never an OHLCV/DQ override."""
    meta=job.get('identity',{})
    if (not _weekly_review_enabled(job,policy) or meta.get('source')!='gettex'
        or meta.get('symbol')!=job.get('isin') or finding.get('state')!='SOURCE_DISCREPANCY'
        or finding.get('native_dq')!='READY' or finding.get('native_complete') is not True
        or finding.get('observation_current') is not True):return None
    return dict(state='OWNER_ACCEPTED_SOURCE_DIFFERENCE',severity='WARNING',
        blocks_native_analysis=False,review_days=7,responsible='Work №1',
        label='Источник GETTEX принят владельцем; расхождение с TV отмечено для еженедельной сверки Work №1.',
        policy_digest=digest(policy),candles_changed=False,trading_permission=False,
        limits='Only the TV disagreement is advisory. Native DQ, identity, warmup, CORE, freshness and risk remain mandatory.')


def _monthly_review(job, finding, clock, policy=None):
    """Checkpoint on the existing bounded native recheck, not another timer.

    A calendar month requests a fresh source check, not a new trading grant or
    automatic expiry of unchanged owner evidence. External TV revalidation is
    explicitly unperformed until another assisted observation is supplied.
    """
    import pandas as pd
    observation=job.get('observation') or {}
    if not observation.get('at'):return None
    weekly=_weekly_review_enabled(job,policy)
    offset=pd.DateOffset(weeks=1) if weekly else pd.DateOffset(months=1)
    observed=pd.Timestamp(observation['at']);external_due=observed+offset
    prior=job.get('monthly_review') or {}
    if (prior.get('reference_checked_at')!=observation['at']
        or (prior.get('cadence_days')==7)!=weekly):prior={}
    result=dict(prior,reference_checked_at=observation['at'],external_due_at=external_due.isoformat(),
        external_state='ASSISTED_RECHECK_REQUIRED' if clock>=external_due else 'NOT_DUE',
        policy=('weekly Work №1 review' if weekly else 'calendar month')+'; native checks reuse existing CP lane; no evidence or grant extension')
    if weekly:result.update(cadence_days=7,responsible='Work №1')
    due=pd.Timestamp(prior.get('next_native_check_at',external_due))
    result.setdefault('next_native_check_at',due.isoformat())
    if clock>=due:
        result.update(last_native_check_at=clock.isoformat(),native_result=finding['state'],
            native_checks=prior.get('native_checks',0)+1,
            next_native_check_at=(clock+offset).isoformat())
    return result


def _comparison_summary(comparisons):
    """Latest unique interval receipt per source/ISIN/TF; no double counting."""
    groups={}
    for item in comparisons:
        if not item:continue
        k=digest([item['isin'],item['identity'],item['tf']])
        group=groups.setdefault(k,dict(isin=item['isin'],identity=item['identity'],tf=item['tf'],scopes={}))
        group['scopes'][item['scope_id']]=item
    result=[]
    for group in groups.values():
        rows=list(group.pop('scopes').values());current=[r for r in rows if r['observation_current'] and r['closed']]
        known=[r for r in current if r['presence_match'] is not None]
        equal=sum(r['presence_match'] for r in known)
        result.append(dict(group,reviewed_scopes=len(rows),current_closed_scopes=len(current),
            last_native_checked_at=max(r['native_checked_at'] for r in rows),
            presence_compared=len(known),presence_equal=equal,
            presence_agreement_percent=100*equal/len(known) if known else None,
            value_discrepancy_scopes=sum(r['value_discrepancy'] for r in current),
            ohlcv_compared=0,ohlcv_agreement_percent=None,
            mode='DIAGNOSTIC_ONLY',scope='As of saved checks on selected problem scopes; not all candles or source certification'))
    return sorted(result,key=lambda x:(x['isin'],x['tf'],x['identity']['source']))


def _seed_official_empty_checks(db,info,clock,progress,limit):
    """Bounded saved-evidence discovery, not a grant or another download queue."""
    import pandas as pd
    import data_quality as dq
    from revision_watcher import RETIME_POLICY,RETIME_VERSION,empty_native_span
    policy=read(db,RETIME_POLICY,{})
    if limit<=0 or policy.get('approved') is not True or policy.get('version')!=RETIME_VERSION:return []
    isin=info['isin'];meta=read(db,'gettex_chart:'+isin+':15m',{}).get('identity',{})
    if meta.get('source')!='gettex_chart' or meta.get('venue')!='GETTEX' or meta.get('currency')!='EUR':return []
    report=read(db,'data_quality:'+isin+':'+meta.get('symbol','')+':15m',{})
    if report.get('identity')!=meta or report.get('state') not in ('UNCONFIRMED','GAP','SOURCE_GAP','STALE'):return []
    reported=list(report.get('ranges',[]))
    if report.get('stale_range'):reported.append(report['stale_range'])
    ranges=sorted({r[0] for r in reported if len(r)==2 and r[0]==r[1]},reverse=True)
    cursor=progress.get('official_empty_cursor');ordered=ranges if cursor is None else [x for x in ranges if x<cursor]+[x for x in ranges if x>=cursor]
    proof=read(db,'interval_evidence:'+isin,{})
    if proof.get('isin')!=isin:return []
    authority={**proof.get('identity',{}),'absence_evidence':proof};queued=[]
    for first in ordered[:limit]:
        progress['official_empty_cursor']=first
        try:
            a=pd.Timestamp(first)
            if not dq.no_trades_proof(authority,a,a+pd.Timedelta(minutes=15),clock):continue
            spec=_spec(isin,meta,'15m',first,first);inputs=_input(db,spec)
            if not empty_native_span(spec,inputs):continue
            if _classify(spec,inputs,clock)['state']=='NO_TRADES':continue
        except (ValueError,KeyError,TypeError) as error:
            progress['official_empty_wait']=dict(first=first,reason=str(error),at=clock.isoformat());continue
        job,_=_enqueue(db,spec,dict(kind='saved_official_empty_evidence'),clock)
        # One new algorithm check; hourly/monthly retry remains the existing
        # mechanism. A poll, unchanged source or restart cannot reset it.
        if job.get('official_empty_algorithm')==3:continue
        # Saved-source discovery uses ordinary capacity, not the two reserved
        # slots for explicit human observations.
        old=dict(job);job.update(official_empty_algorithm=3,status='QUEUED',next_check=clock.isoformat())
        _save_job(db,job,old);queued.append((key(job),json.dumps(job,ensure_ascii=False)))
    return queued


def _queue_repaired_scopes(db,receipt_key,receipt,clock,limit=16):
    """Local input change, not a provider retry or a refreshed TV observation."""
    changed={}
    for base,rows in receipt['source']['rows'].items():
        old={r[0]:list(r[1:]) for r in receipt['before'][base]}
        changed[base]={r[0] for r in rows if old.get(r[0])!=list(r[1:])}
        changed[base].update(m['old_at'] for m in receipt.get('relocations',[]) if m['base_tf']==base)
    if receipt.get('mode')=='VERIFIED_NATIVE_EMPTY':
        import pandas as pd
        target=read(db,PREFIX+receipt['isin']+':'+receipt['job_id'],{})
        if target.get('tf') in ('5m','15m'):
            changed.setdefault(target['tf'],set()).update(str(t) for t in pd.date_range(target['first'],target['end'],freq=target['tf'].replace('m','min'),inclusive='left'))
    if not any(changed.values()):return dict(queued=[],complete=True)
    prefix=PREFIX+receipt['isin']+':'
    # Projection is bounded to this exact listing and the repaired window.
    candidates=db.execute("""SELECT key,value FROM settings WHERE key>=? AND key<?
      AND json_extract(value,'$.status') IN ('AWAITING_SOURCE','AWAITING_USER','RESOLVED_NATIVE')
      AND json_extract(value,'$.identity.source')=? AND json_extract(value,'$.identity.symbol')=?
      AND json_extract(value,'$.identity.venue')=? AND json_extract(value,'$.identity.currency')=?
      AND json_extract(value,'$.identity.timezone')=?
      AND ((json_extract(value,'$.first')<? AND json_extract(value,'$.end')>?)
       OR (json_extract(value,'$.observation.result') IN ('ABSENT','DIFFERENT') AND
        ((json_extract(value,'$.native_neighbors.before[0]')>=? AND json_extract(value,'$.native_neighbors.before[0]')<?)
         OR (json_extract(value,'$.native_neighbors.after[0]')>=? AND json_extract(value,'$.native_neighbors.after[0]')<?))))
      ORDER BY key LIMIT ?""",(prefix,prefix+'\uffff',*[receipt['identity'][k] for k in ('source','symbol','venue','currency','timezone')],
        receipt['end'],receipt['first'],receipt['first'],receipt['end'],receipt['first'],receipt['end'],limit+1)).fetchall()
    queued=[]
    for _,raw in candidates[:limit]:
        job=json.loads(raw)
        if job['id']==receipt['job_id']:continue
        slots=changed.get(job['tf'],set());obs=job.get('observation') or {}
        direct=any(job['first']<=at<job['end'] for at in slots)
        neighbours=job.get('native_neighbors') or {}
        adjacent=(obs.get('result') in ('ABSENT','DIFFERENT') and any((neighbours.get(k) or [None])[0] in slots for k in ('before','after')))
        if not (direct or adjacent):continue
        if _input(db,job)['fingerprint']==job.get('input_fingerprint'):continue
        old=dict(job);job.update(status='RECHECK_QUEUED' if obs else 'QUEUED',next_check=clock.isoformat(),
            recheck_due_to_native_repair=dict(receipt_key=receipt_key,at=clock.isoformat(),version=REPAIR_RECHECK_VERSION,
                reason='LOCAL_EMPTY_PROOF_CONFIRMED' if receipt.get('mode')=='VERIFIED_NATIVE_EMPTY' else 'CURRENT_NATIVE_INPUT_CHANGED'))
        # Preserve the original visual binding and source cooldown. The next
        # ordinary turn must classify the newly changed inputs independently.
        _save_job(db,job,old);queued.append(job['id'])
    return dict(queued=queued,complete=len(candidates)<=limit)


def _resume_repaired_scopes(db,isin,clock):
    """Backfill pending follow-ups for the two newly deployed repair modes."""
    from revision_watcher import RETIME_PREFIX,retime_current
    p=read(db,PROGRESS+isin,{});seen=list(p.get('native_repair_rechecks_seen',[])) if p.get('native_repair_rechecks_version')==REPAIR_RECHECK_VERSION else [];prefix=RETIME_PREFIX+isin+':'
    rows=db.execute("""SELECT key,value FROM settings WHERE key>=? AND key<?
      AND json_extract(value,'$.state')='APPLIED'
      AND json_extract(value,'$.mode') IN ('VERIFIED_NATIVE_CLOSING_TICK','VERIFIED_PRICE_CLOSING_TICK_PARTITION','VERIFIED_NATIVE_PRICE_SAMPLES')
      ORDER BY json_extract(value,'$.created_at') DESC LIMIT 8""",(prefix,prefix+'\uffff')).fetchall()
    count=0
    for key,raw in rows:
        if key in seen:continue
        if count>=2:break
        receipt=json.loads(raw);count+=1
        if retime_current(db,receipt,clock):result=_queue_repaired_scopes(db,key,receipt,clock)
        else:result=dict(queued=[],complete=True,reason='REPAIR_PROOF_CHANGED')
        if result['complete']:seen.append(key)
        p=read(db,PROGRESS+isin,{})
        p.update(native_repair_rechecks_version=REPAIR_RECHECK_VERSION,native_repair_rechecks_seen=seen[-64:],last_native_repair_rechecks=dict(receipt_key=key,at=clock.isoformat(),**result))
        write(db,PROGRESS+isin,p)


NATIVE_EMPTY_ALGORITHM=8
NATIVE_EMPTY_LIMIT=8


def _native_empty_pending(db,isin,limit):
    prefix=PREFIX+isin+':'
    rows=db.execute("""SELECT key,value FROM settings WHERE key>=? AND key<?
      AND (json_extract(value,'$.tf')='5m' OR (json_extract(value,'$.tf')='15m' AND json_extract(value,'$.observation.result') IN ('ABSENT','PRESENT','DIFFERENT','ZERO_VOLUME')))
      AND julianday(json_extract(value,'$.last'))>=julianday(json_extract(value,'$.first'))
      AND julianday(json_extract(value,'$.last'))-julianday(json_extract(value,'$.first'))<30.0/1440
      AND json_extract(value,'$.identity.source')='gettex_chart'
      AND json_extract(value,'$.identity.venue')='GETTEX'
      AND json_extract(value,'$.identity.currency')='EUR'
      AND ((json_extract(value,'$.status') IN ('RECHECK_QUEUED','QUEUED')
        AND julianday(COALESCE(json_extract(value,'$.next_check'),json_extract(value,'$.created_at')))<=julianday('now')
        AND (json_extract(value,'$.observation.result') IN ('ABSENT','PRESENT','DIFFERENT','ZERO_VOLUME')
          OR COALESCE(json_extract(value,'$.native_empty_algorithm'),0)>0))
       OR (json_extract(value,'$.status') IN ('AWAITING_USER','AWAITING_SOURCE')
        AND ((json_extract(value,'$.finding.state') IN ('SOURCE_TIMEBASE_UNCONFIRMED','GAP','NATIVE_ROWS_PRESENT')
        AND (COALESCE(json_extract(value,'$.native_empty_algorithm'),0)<?
          OR (json_extract(value,'$.tf')='5m' AND json_extract(value,'$.observation.result') IN ('PRESENT','DIFFERENT')
            AND julianday(COALESCE(json_extract(value,'$.next_check'),json_extract(value,'$.created_at')))<=julianday('now')
            AND COALESCE(json_extract(value,'$.native_empty_algorithm'),0)<12)))
        OR (json_extract(value,'$.tf')='5m' AND json_extract(value,'$.first')=json_extract(value,'$.last')
          AND json_extract(value,'$.finding.state')='SOURCE_DISCREPANCY'
          AND json_extract(value,'$.observation.result')='DIFFERENT'
          AND julianday(COALESCE(json_extract(value,'$.next_check'),json_extract(value,'$.created_at')))<=julianday('now')
          AND COALESCE(json_extract(value,'$.native_empty_algorithm'),0)<12)
        OR (json_extract(value,'$.tf')='5m' AND json_extract(value,'$.observation') IS NULL
          AND json_extract(value,'$.finding.state') IN ('SOURCE_TIMEBASE_UNCONFIRMED','GAP')
          AND julianday(COALESCE(json_extract(value,'$.next_check'),json_extract(value,'$.created_at')))<=julianday('now')
          AND COALESCE(json_extract(value,'$.automatic_zero_algorithm'),0)<1)
        OR (json_extract(value,'$.tf')='15m' AND json_extract(value,'$.first')=json_extract(value,'$.last')
          AND json_extract(value,'$.finding.state')='SOURCE_DISCREPANCY'
          AND json_extract(value,'$.observation.result')='DIFFERENT'
          AND julianday(COALESCE(json_extract(value,'$.next_check'),json_extract(value,'$.created_at')))<=julianday('now')
          AND (COALESCE(json_extract(value,'$.current_difference_algorithm'),0)<1
            OR (COALESCE(json_extract(value,'$.current_difference_algorithm'),0)<2
              AND json_extract(value,'$.official_timebase_repair.reason')='RETIME_OTHER_CURRENT_TV_REFERENCE_CONFLICT'))))))
      ORDER BY CASE json_extract(value,'$.status') WHEN 'RECHECK_QUEUED' THEN 0 WHEN 'QUEUED' THEN 1 ELSE 2 END,json_extract(value,'$.first') DESC,key LIMIT ?""",
      (prefix,prefix+'\uffff',NATIVE_EMPTY_ALGORITHM,limit)).fetchall()
    room=limit-len(rows)
    if room<=0:return rows
    # Extract the small range list once, not the full source ledger per job.
    # The later writer still validates the complete, current proof.
    saved=db.execute("SELECT json_extract(value,'$.isin','$.basis','$.identity','$.incomplete_publication_intervals') FROM settings WHERE key=?",('interval_evidence:'+isin,)).fetchone()
    if not saved:return rows
    try:
        subject,basis,identity,intervals=json.loads(saved[0])
        if (subject!=isin or basis!='complete_published_trade_files' or identity!=dict(source='gettex',venue='GETTEX',currency='EUR',symbol=isin,timezone='Europe/Berlin') or not intervals):return rows
    except (ValueError,TypeError):return rows
    # One cached reclassification of a known partial official pair must not
    # wait behind the entire archive scan. Existing visual reviews keep their
    # reserved precedence. This SQL nominates only; _input/final guards still
    # validate the exact listing, source proof and native window on the writer.
    rows+=db.execute("""SELECT j.key,j.value FROM settings j WHERE j.key>=? AND j.key<?
      AND json_extract(j.value,'$.identity.source')='gettex'
      AND json_extract(j.value,'$.identity.venue')='GETTEX'
      AND json_extract(j.value,'$.identity.currency')='EUR'
      AND json_extract(j.value,'$.identity.symbol')=?
      AND json_extract(j.value,'$.tf') IN ('5m','15m')
      AND json_extract(j.value,'$.observation') IS NULL
      AND json_extract(j.value,'$.status') IN ('QUEUED','AWAITING_USER','AWAITING_SOURCE')
      AND COALESCE(json_extract(j.value,'$.source_publication_wait_algorithm'),0)<1
      AND julianday(COALESCE(json_extract(j.value,'$.next_check'),json_extract(j.value,'$.created_at')))<=julianday('now')
      AND julianday(json_extract(j.value,'$.end'))>julianday(json_extract(j.value,'$.interval_start'))
      AND julianday(json_extract(j.value,'$.end'))-julianday(json_extract(j.value,'$.interval_start'))<=30.0/1440
      AND EXISTS (SELECT 1 FROM json_each(?) r
        WHERE json_extract(r.value,'$[0]')<json_extract(j.value,'$.end')
        AND json_extract(r.value,'$[1]')>json_extract(j.value,'$.interval_start'))
      ORDER BY json_extract(j.value,'$.created_at'),j.key LIMIT ?""",
      (prefix,prefix+'\uffff',isin,json.dumps(intervals),room)).fetchall()
    return rows


def native_empty_batches(db,infos,limit=8):
    """Rotate saved 5m evidence on the existing writer, without any download."""
    from revision_watcher import RETIME_POLICY,RETIME_VERSION
    policy=read(db,RETIME_POLICY,{});source=read(db,SOURCE_POLICY,{})
    if (policy.get('approved') is not True or policy.get('version')!=RETIME_VERSION
        or source.get('approved') is not True or source.get('version')!=1
        or source.get('scope')!='registered_stocks' or source.get('exclusive_venue')!='GETTEX'):return []
    candidates=[]
    for info in infos:
        if info.get('kind')=='benchmark':continue
        if _native_empty_pending(db,info['isin'],1):
            candidates.append((read(db,PROGRESS+info['isin'],{}).get('native_empty_batch_at',''),info))
    return [{**i,'_native_empty_only':True} for _,i in sorted(candidates,key=lambda x:(x[0],x[1]['isin']))[:limit]]


def turn(store, info, now=None, native_empty_only=False):
    """At most four recovery events and four checks per existing feed turn."""
    import pandas as pd
    if info.get('kind')=='benchmark':return {'state':'NOT_APPLICABLE'}
    clock=pd.Timestamp(now or datetime.now(timezone.utc));isin=info['isin'];created=checked=0;segments=0
    from gettex_history import attach_known_interruptions
    closure_updated=False if native_empty_only else attach_known_interruptions(info,store,clock)
    with store.connect() as db:
        db.execute('BEGIN IMMEDIATE')
        if not native_empty_only:_resume_repaired_scopes(db,isin,clock)
        progress=read(db,PROGRESS+isin,{})
        exists=db.execute("SELECT 1 FROM sqlite_master WHERE name='cp_recovery_experience'").fetchone()
        events=db.execute('SELECT rowid,event_id,identity,base_tf,method,outcome,first_at,last_at,reason FROM cp_recovery_experience WHERE isin=? AND rowid>? ORDER BY rowid LIMIT ?',
                          (isin,progress.get('cursor',0),INGEST_LIMIT)).fetchall() if exists and not native_empty_only else []
        for rowid,event,raw,tf,method,outcome,first,last,raw_reason in events:
            try:
                reason=json.loads(raw_reason);meta=json.loads(raw)
                if not isinstance(reason,dict) or not isinstance(meta,dict):raise ValueError('INVALID_RECOVERY_EVIDENCE')
                if method=='provider_range' and outcome=='FAILED' and reason.get('code') in FAILURES:
                    from data_quality import STEPS
                    step=STEPS.get(tf,pd.DateOffset(weeks=1) if tf=='1W' else pd.DateOffset(months=1))
                    a=pd.Timestamp(progress.get('next_range_first',first));b=pd.Timestamp(last)
                    while a<=b and segments<INGEST_LIMIT:
                        end=min(b,a+min(pd.Timedelta(days=8),step*1000)) if tf in STEPS else a
                        if tf in STEPS:end=a+((end-a)//step)*step
                        spec=_spec(isin,meta,tf,str(a),str(end))
                        _input(db,spec) # exact stored listing; no web entitlement inferred
                        _,added=_enqueue(db,spec,dict(kind='existing_bounded_recovery',event_id=event,
                            code=reason['code'],local_recomposition='preceded provider_range in existing repair',
                            limits='Configured licensed/native workflow attempted; not proof that every external archive is exhausted'),clock)
                        created+=int(added);segments+=1;a=end+step
                    if a<=b:
                        progress['next_range_first']=str(a);break
                progress['cursor']=rowid;progress.pop('next_range_first',None)
                if segments>=INGEST_LIMIT:break
            except (ValueError,TypeError,KeyError) as error:
                progress['cursor']=rowid;progress.pop('next_range_first',None)
                progress['last_ineligible_event']={'event_id':event,'reason':str(error),'at':clock.isoformat()}
        if native_empty_only:
            seeded=[]
            for _,raw in _native_empty_pending(db,isin,NATIVE_EMPTY_LIMIT):
                job=json.loads(raw);old=dict(job)
                job.update(native_empty_algorithm=12 if job['tf']=='5m' and (job.get('observation') or {}).get('result') in ('PRESENT','DIFFERENT') else NATIVE_EMPTY_ALGORITHM,positive_price_algorithm=1,current_difference_algorithm=2 if job['tf']=='15m' else 1,status='RECHECK_QUEUED' if job['status']=='RECHECK_QUEUED' else 'QUEUED',next_check=clock.isoformat())
                if job['tf']=='5m' and not job.get('observation'):job['automatic_zero_algorithm']=1
                _save_job(db,job,old);seeded.append((key(job),json.dumps(job,ensure_ascii=False)))
            progress['native_empty_batch_at']=clock.isoformat()
        else:seeded=_seed_official_empty_checks(db,info,clock,progress,max(0,INGEST_LIMIT-segments))
        # Rotate a bounded page. New short-TF jobs cannot monopolize checks.
        cursor=progress.get('scan_cursor','');pattern=PREFIX+isin+':%'
        page=seeded if native_empty_only else db.execute('SELECT key,value FROM settings WHERE key LIKE ? AND key>? ORDER BY key LIMIT ?', (pattern,cursor,SCAN_LIMIT)).fetchall()
        if not page and not native_empty_only:page=db.execute('SELECT key,value FROM settings WHERE key LIKE ? ORDER BY key LIMIT ?', (pattern,SCAN_LIMIT)).fetchall()
        # Explicit reviews must reach their reserved slots even outside this
        # rotating page. Keep ordinary progress independent and the same limit.
        explicit=db.execute("""SELECT key,value FROM settings WHERE key LIKE ?
            AND json_extract(value,'$.status')='RECHECK_QUEUED'
            AND julianday(COALESCE(json_extract(value,'$.next_check'),json_extract(value,'$.created_at')))<=julianday(?)
            ORDER BY COALESCE(json_extract(value,'$.checked_at'),''),json_extract(value,'$.created_at'),key LIMIT 2""",
            (pattern,clock.isoformat())).fetchall() if not native_empty_only else []
        jobs=[json.loads(v) for v in dict(page+seeded+explicit).values()]
        due=sorted((j for j in jobs if closure_updated or j.get('fingerprint_version',1)<FINGERPRINT_VERSION or pd.Timestamp(j.get('next_check',j['created_at']))<=clock),key=lambda j:(j.get('checked_at',''),j['created_at'],j['id']))
        # Reserve two slots for explicit observations, two for ordinary work.
        manual=[j for j in due if j['status']=='RECHECK_QUEUED'][:2]
        ordinary=[j for j in due if j['status']!='RECHECK_QUEUED']
        remaining=[j for j in due if j not in manual and j not in ordinary]
        due=(manual+ordinary+remaining)[:NATIVE_EMPTY_LIMIT if native_empty_only else CHECK_LIMIT]
        progress['counts']=read(db,PROGRESS+isin,{}).get('counts',{})
        if page and not native_empty_only:progress['scan_cursor']=page[-1][0]
        progress.update(last_ingested_at=clock.isoformat(),created_total=progress.get('created_total',0)+created)
        if closure_updated:progress['analysis_requested_at']=clock.isoformat()
        write(db,PROGRESS+isin,progress)
    for prior in due:
        try:
            with store.connect() as db:
                db.execute('BEGIN');inputs=_input(db,prior);source_policy=read(db,SOURCE_POLICY,{})
            result=_classify(prior,inputs,clock)
            from revision_watcher import retime_plan,publish_retime
            correction=None;correction_wait=None
            if (result['state'] in ('GAP','SOURCE_TIMEBASE_UNCONFIRMED','SOURCE_DISCREPANCY')
                    or (result.get('market_state')=='ZERO_QUANTITY_UNCONFIRMED'
                        and prior.get('observation',{}).get('result')=='ZERO_VOLUME')):
                try:
                    with store.connect() as db:
                        db.execute('BEGIN');correction=retime_plan(db,prior,inputs,clock)
                except (ValueError,KeyError,TypeError) as error:correction_wait=str(error)
            with store.connect() as db:
                db.execute('BEGIN IMMEDIATE');current=read(db,key(prior))
                if (digest(current)!=digest(prior) or _input(db,prior)['fingerprint']!=inputs['fingerprint']
                    or read(db,SOURCE_POLICY,{})!=source_policy):
                    continue # don't overwrite newer observation or changed source rows
                changed=current.get('finding')!=result
                if changed:
                    current['history']=(current.get('history',[])+[dict(at=clock.isoformat(),finding=result)])[-20:]
                old=dict(current)
                if correction:
                    current['official_timebase_repair']=publish_retime(db,current,inputs,correction,clock)
                    if (correction.get('mode')=='VERIFIED_NATIVE_EMPTY'
                            or correction.get('reviewed_tf')=='5m' and correction.get('mode') in ('VERIFIED_NATIVE_TRADE_PARTITION','VERIFIED_NATIVE_SESSION_TAIL','VERIFIED_NATIVE_POSITIVE_PRICE_EXTREME')):
                        # Finish the current proof on this same writer transaction
                        # instead of adding a second queued pass.
                        inputs=_input(db,current);result=_classify(current,inputs,clock)
                        allowed=('NO_TRADES',) if correction.get('mode')=='VERIFIED_NATIVE_EMPTY' else ('NO_TRADES','NATIVE_ROWS_CONFIRMED')
                        if result['state'] not in allowed:raise ValueError('RETIME_EMPTY_PUBLICATION_RECHECK_FAILED')
                    else:
                        current.update(status='RECHECK_QUEUED',next_check=clock.isoformat(),checked_at=clock.isoformat())
                        _save_job(db,current,old);checked+=1
                        continue
                if correction_wait:current['official_timebase_repair']=dict(state='WAITING_EVIDENCE',reason=correction_wait)
                if (_observation_current(current,inputs) and current['observation']['input_fingerprint']!=inputs['fingerprint']
                        and not inputs.get('review_context_fingerprint')):
                    current['observation']={**current['observation'],'previous_input_fingerprint':current['observation']['input_fingerprint'],
                        'input_fingerprint':inputs['fingerprint'],'binding_updated_at':clock.isoformat(),
                        'fingerprint_version':FINGERPRINT_VERSION,
                        'binding_reason':'v4: exact native rows/neighbours/validated listing/proof; unrelated chart and portfolio settings are not market evidence'}
                current.update(finding=result,input_fingerprint=inputs['fingerprint'],checked_at=clock.isoformat(),
                    fingerprint_version=FINGERPRINT_VERSION,
                    native_rows=inputs['rows'],native_neighbors=current.get('native_neighbors') if inputs.get('review_context_fingerprint') else inputs['neighbors'],
                    checks=current.get('checks',0)+1,next_check=(clock+pd.Timedelta(hours=1)).isoformat(),
                    native_retry=inputs['native_retry'],capability=capability(),
                    status='RESOLVED_NATIVE' if result['can_affect_dq'] or result['state']=='NATIVE_ROWS_CONFIRMED' else 'AWAITING_SOURCE' if current.get('observation') or result.get('provider_wait') is True else 'AWAITING_USER',
                    review_required=not (result['can_affect_dq'] or result['state']=='NATIVE_ROWS_CONFIRMED' or result.get('observation_current',False) or result.get('provider_wait') is True))
                if native_empty_only and current['identity']['source']=='gettex':
                    current['source_publication_wait_algorithm']=1
                _queue_observed_recovery(db,current,inputs,result,clock.isoformat())
                current['source_disposition']=_source_disposition(current,result,source_policy)
                if current['source_disposition']:
                    current.update(status='REVIEW_WEEKLY',review_required=False)
                if current.get('observation'):
                    current['source_comparison']=_comparison(current,inputs,result,clock)
                    current['monthly_review']=_monthly_review(current,result,clock,source_policy)
                _save_job(db,current,old);checked+=1
        except (ValueError,TypeError,KeyError) as error:
            with store.connect() as db:
                db.execute('BEGIN IMMEDIATE');current=read(db,key(prior),{})
                if digest(current)==digest(prior):
                    old=dict(current);current.update(status='BLOCKED',reason=str(error),next_check=(clock+pd.Timedelta(hours=1)).isoformat(),checked_at=clock.isoformat(),fingerprint_version=FINGERPRINT_VERSION)
                    _save_job(db,current,old)
    with store.connect() as db:
        db.execute('BEGIN IMMEDIATE');progress=read(db,PROGRESS+isin,{})
        progress.update(last_turn=clock.isoformat(),last_created=created,last_checked=checked,
            checked_total=progress.get('checked_total',0)+checked,mode='ASSISTED_ONLY')
        current_jobs=[read(db,k,{}) for k,_ in page]
        update_suspicious(progress,current_jobs,clock.isoformat())
        previous=progress.get('review_batch')
        retained=[read(db,PREFIX+isin+':'+i['id'],{}) for i in (previous or {}).get('items',[])]
        stable=bool(retained) and pd.Timestamp(previous['valid_until'])>clock and all(
            j.get('review_required') and review_binding(j)==item['binding'] for j,item in zip(retained,previous['items']))
        # Keep an open packet stable; otherwise follow the existing rotating page.
        progress['review_batch']=previous if stable else prepare_batch(current_jobs,clock)
        requested=progress.get('analysis_requested_at') if not progress.get('counts',{}).get('RECHECK_QUEUED',0) else None
        write(db,PROGRESS+isin,progress)
    return dict(state='PROCESSED' if created or checked else 'IDLE',created=created,checked=checked,
                mode='ASSISTED_ONLY',analysis_requested_at=requested,
                observations_checked=sum(bool(j.get('observation')) for j in due))


def review_binding(job):
    # Routine queue timestamps do not invalidate an unchanged human review.
    return digest([job['id'],job.get('input_fingerprint'),job.get('observation')])


def prepare_batch(jobs, now, previous=None):
    """Small persisted instruction packet; never infers a TV ticker or result."""
    import pandas as pd
    suggestions={j['observation']['tv_symbol'] for j in jobs if j.get('observation',{}).get('identity_confirmed')}
    symbol=next(iter(suggestions)) if len(suggestions)==1 else ''
    selected=sorted((j for j in jobs if j.get('review_required') and j.get('input_fingerprint') and j['status'] not in ('RESOLVED_NATIVE','BLOCKED','QUEUED','RECHECK_QUEUED')),
        key=lambda j:(j['created_at'],j['id']))[:BATCH_LIMIT]
    items=[dict(id=j['id'],binding=review_binding(j),isin=j['isin'],identity=j['identity'],tf=j['tf'],
        start=j['interval_start'],end=j['end'],timezone=j['timezone'],
        utc_start=pd.Timestamp(j['interval_start']).tz_localize(j['timezone']).tz_convert('UTC').isoformat(),
        utc_end=pd.Timestamp(j['end']).tz_localize(j['timezone']).tz_convert('UTC').isoformat(),
        reason=j.get('finding',{}).get('reason'),tv_symbol=symbol,
        native_neighbors=j.get('native_neighbors'),input_fingerprint=j['input_fingerprint']) for j in selected]
    signature=digest(items)
    if previous and previous.get('signature')==signature and pd.Timestamp(previous['valid_until'])>now:return previous
    return dict(signature=signature,prepared_at=now.isoformat(),valid_until=(now+pd.Timedelta(hours=2)).isoformat(),
        items=items,confirmation='Each selected interval must be visually checked; no default absence or trading grant',
        navigation='TradingView: confirm GETTEX/EUR and Europe/Berlin; choose TF and Go to date (Alt+G). The link opens a symbol only, not a verified range.')


def review_url(symbol):
    import re
    from urllib.parse import quote
    if not re.fullmatch(r'GETTEX:[A-Z0-9._-]{1,30}',symbol or ''):raise ValueError('REVIEW_IDENTITY_CONFIRMATION_REQUIRED')
    return 'https://www.tradingview.com/chart/?symbol='+quote(symbol,safe='')


def _evidence_files(evidence):
    if len(evidence)>4:raise ValueError('REVIEW_TOO_LARGE')
    files=[]
    for item in evidence:
        path=Path(item).resolve()
        if path.suffix.lower() not in ('.png','.jpg','.jpeg') or not path.is_file() or path.stat().st_size>20_000_000:
            raise ValueError('SCREENSHOT_FILE_INVALID')
        files.append(dict(path=str(path),sha256=hashlib.sha256(path.read_bytes()).hexdigest()))
    return files


def _record_observation(db,job,inputs,result,symbol,note,files,at):
    old=dict(job)
    if job.get('observation'):job['observation_history']=(job.get('observation_history',[])+[job['observation']])[-20:]
    job['observation']=dict(result=result,tv_symbol=symbol,identity_confirmed=True,at=at,
        actor='human_assisted',note=note,evidence=files,input_fingerprint=inputs['fingerprint'],
        fingerprint_version=FINGERPRINT_VERSION,evidence_checked_at=inputs['proof'].get('checked_at'),
        use='diagnostic only; no market-data import')
    job.update(status='RECHECK_QUEUED',next_check=at,native_rows=inputs['rows'],native_neighbors=inputs['neighbors'])
    _save_job(db,job,old)
    progress=read(db,PROGRESS+job['isin'],{})
    progress['analysis_requested_at']=at
    write(db,PROGRESS+job['isin'],progress)


def observe_batch(store,isin,packet,answers,submission_id,confirmed=False):
    """One atomic confirmation of explicit answers; blank rows remain pending."""
    import pandas as pd
    if not confirmed or not answers or len(answers)>BATCH_LIMIT or not isinstance(submission_id,str) or not 8<=len(submission_id)<=100:
        raise ValueError('BATCH_CONFIRMATION_REQUIRED')
    if len({a['id'] for a in answers})!=len(answers):raise ValueError('BATCH_DUPLICATE_ROW')
    packet_digest=digest([packet,answers]);prepared=[]
    for answer in answers:
        review_url(answer.get('tv_symbol'))
        if answer.get('result') not in OBSERVATIONS or len(answer.get('note',''))>1500:raise ValueError('REVIEW_TOO_LARGE_OR_UNKNOWN_RESULT')
        prepared.append((answer,_evidence_files(answer.get('evidence',[]))))
    now=pd.Timestamp.now(tz='UTC')
    with store.connect() as db:
        db.execute('BEGIN IMMEDIATE');p=read(db,PROGRESS+isin,{})
        for receipt in p.get('batch_receipts',[]):
            if receipt['submission_id']==submission_id:
                if receipt['digest']!=packet_digest:raise ValueError('BATCH_SUBMISSION_CONFLICT')
                return {**receipt,'duplicate':True}
        if pd.Timestamp(packet['valid_until'])<=now:raise ValueError('BATCH_EXPIRED')
        if digest(packet)!=digest(p.get('review_batch')) or packet.get('signature')!=digest(packet.get('items',[])):
            raise ValueError('BATCH_CHANGED')
        known={i['id']:i for i in packet.get('items',[])};checked=[]
        for answer,files in prepared:
            item=known.get(answer['id']);job=read(db,PREFIX+isin+':'+answer['id'])
            if not item or item.get('isin')!=isin or not job or review_binding(job)!=item.get('binding'):
                raise ValueError('REVIEW_JOB_CHANGED')
            inputs=_input(db,job)
            if inputs['fingerprint']!=item.get('input_fingerprint'):raise ValueError('REVIEW_INPUT_CHANGED')
            checked.append((job,inputs,answer,files))
        for job,inputs,answer,files in checked:
            _record_observation(db,job,inputs,answer['result'],answer['tv_symbol'],answer.get('note',''),files,now.isoformat())
        p=read(db,PROGRESS+isin,{})
        receipt=dict(submission_id=submission_id,digest=packet_digest,at=now.isoformat(),recorded=len(checked),state='RECHECK_QUEUED',candles_written=0,grants_changed=False)
        p['batch_receipts']=(p.get('batch_receipts',[])+[receipt])[-20:]
        p.pop('review_batch',None);write(db,PROGRESS+isin,p)
    return receipt


def complete_analysis_request(store,isin,requested,result):
    """Receipt of existing eligibility pipeline, not a new admission mechanism."""
    if not requested or (result or {}).get('deferred'):return
    with store.connect() as db:
        db.execute('BEGIN IMMEDIATE');p=read(db,PROGRESS+isin,{})
        if p.get('analysis_requested_at')!=requested:return
        p.pop('analysis_requested_at',None)
        p['analysis_receipt']=dict(requested_at=requested,processed_at=datetime.now(timezone.utc).isoformat(),
            assessment_checked_at=(result or {}).get('checked_at'),error=(result or {}).get('error'),
            basis='Existing native feed/DQ and eligibility_turn; unchanged inputs reuse the existing calculation; current gates apply on every read')
        write(db,PROGRESS+isin,p)


def observe(store, job_id, isin, expected_digest, result, tv_symbol, confirmed, note='', evidence=None):
    """Human observation only; cannot supply OHLCV, NO_TRADES or an admission."""
    import re
    if result not in OBSERVATIONS or not confirmed or not re.fullmatch(r'GETTEX:[A-Z0-9._-]{1,30}',tv_symbol or ''):
        raise ValueError('REVIEW_IDENTITY_CONFIRMATION_REQUIRED')
    if len(note)>1500:raise ValueError('REVIEW_TOO_LARGE')
    files=_evidence_files(evidence or [])
    with store.connect() as db:
        db.execute('BEGIN IMMEDIATE');k=PREFIX+isin+':'+job_id;job=read(db,k)
        if not job or digest(job)!=expected_digest:raise ValueError('REVIEW_JOB_CHANGED')
        inputs=_input(db,job)
        if job.get('input_fingerprint')!=inputs['fingerprint']:raise ValueError('REVIEW_INPUT_CHANGED')
        _record_observation(db,job,inputs,result,tv_symbol,note,files,datetime.now(timezone.utc).isoformat())
    return {'state':'RECHECK_QUEUED','id':job_id,'grants_changed':False,'candles_written':0}


def confirm_primary_window(store,spec,native_reference,tv_symbol,screenshots,matched_fields,confirmed=False,note=''):
    """Record exact manual agreement with one captured independent parent.

    Metadata only. No caller-supplied OHLCV, candle write or trading grant.
    The existing feed writer must independently revalidate the complete tape.
    """
    import pandas as pd
    from revision_watcher import _official_window,_native,IDENTITY,RETIME_POLICY,RETIME_VERSION
    from eur_listings import stored_identity
    fields=['timestamp','open','high','low','close','volume']
    if confirmed is not True or matched_fields!=fields or len(note)>1500:raise ValueError('EXACT_PRIMARY_PARENT_CONFIRMATION_REQUIRED')
    review_url(tv_symbol)
    spec=_spec(spec['isin'],spec['identity'],spec['tf'],spec['first'],spec['last'])
    if spec['tf']!='15m' or spec['first']!=spec['last'] or spec['identity'].get('source')!='gettex_chart' or spec['identity'].get('venue')!='GETTEX' or spec['identity'].get('currency')!='EUR':raise ValueError('SINGLE_GETTEX_EUR_PARENT_REQUIRED')
    path=Path(native_reference).resolve()
    if not path.is_file() or path.suffix.lower()!='.json' or path.stat().st_size>10_000_000:raise ValueError('NATIVE_DIAGNOSTIC_REFERENCE_REQUIRED')
    packet=json.loads(path.read_text(encoding='utf-8'));records=packet.get('records',[])
    if len(records)!=1 or records[0].get('spec')!=spec:raise ValueError('NATIVE_REFERENCE_SCOPE_REQUIRED')
    frozen=records[0]['native']
    if (_fingerprint(frozen)!=frozen.get('fingerprint') or digest(packet.get('native_window'))!=packet.get('native_window_digest')
            or digest(packet.get('source'))!=packet.get('source_digest')):raise ValueError('PRIMARY_REFERENCE_INVALID')
    files=_evidence_files(screenshots)
    if not files:raise ValueError('EXACT_PRIMARY_PARENT_SCREENSHOT_REQUIRED')
    now=pd.Timestamp.now(tz='UTC')
    with store.connect() as db:
        db.execute('BEGIN IMMEDIATE');inputs=_input(db,spec);sp=read(db,SOURCE_POLICY,{});policy=read(db,RETIME_POLICY,{})
        if not (sp.get('approved') is True and sp.get('version')==1 and sp.get('scope')=='registered_stocks' and sp.get('exclusive_venue')=='GETTEX' and policy.get('approved') is True and policy.get('version')==RETIME_VERSION):raise ValueError('PRIMARY_SOURCE_POLICY_REQUIRED')
        native={b:_native(db,spec['isin'],spec['identity'],b,spec['first'],spec['end']) for b in ('5m','15m')}
        if not stored_identity(db,inputs['info'],spec['identity'],'5m'):raise ValueError('PRIMARY_CHILD_IDENTITY_UNCONFIRMED')
        if inputs['fingerprint']!=frozen['fingerprint'] or digest(native)!=packet['native_window_digest']:raise ValueError('PRIMARY_NATIVE_INPUT_CHANGED')
        source=_official_window(db,spec['isin'],spec['first'],spec['end'],now,allow_zero_rows=True)
        if digest(source)!=packet['source_digest']:raise ValueError('PRIMARY_SOURCE_INPUT_CHANGED')
        job,_=_enqueue(db,spec,dict(kind='exact_primary_parent_visual_confirmation',native_reference=str(path)),now)
        _record_observation(db,job,inputs,'DIFFERENT',tv_symbol,note,files,now.isoformat())
        prior=dict(job);job['observation']={**job['observation'],'primary_window_confirmation':dict(version=1,first=spec['first'],end=spec['end'],matched_fields=fields,
            source_digest=packet['source_digest'],native_window_digest=packet['native_window_digest'],native_input_fingerprint=inputs['fingerprint'],
            meaning='Exact manual closed15m TV parent matches independently captured complete GETTEX source; no TV values imported')}
        _save_job(db,job,prior)
    return dict(state='RECHECK_QUEUED',id=spec['id'],candles_written=0,grants_changed=False)


def register_prior_review(store, spec, native_reference, result, tv_symbol, screenshots, note=''):
    """Reuse a completed bounded human check; no repeating Desktop navigation.

    The native receipt and screenshots remain auditable local references. This
    only queues a finding; the ordinary feed turn must evaluate current inputs.
    """
    import pandas as pd
    path=Path(native_reference).resolve()
    if not path.is_file() or path.suffix.lower()!='.json' or path.stat().st_size>10_000_000:
        raise ValueError('NATIVE_DIAGNOSTIC_REFERENCE_REQUIRED')
    spec=_spec(spec['isin'],spec['identity'],spec['tf'],spec['first'],spec['last'])
    # A file's existence/hash is not evidence that today's rows are the rows
    # checked visually. Bind to exactly one captured native receipt, before
    # creating any job; later acquisition must never rebase an old observation.
    raw=path.read_bytes()
    try:
        packet=json.loads(raw)
        if 'records' in packet:captured=packet['records']
        elif 'spec' in packet and 'native' in packet:captured=[packet]
        elif len(packet.get('specs',[]))==len(packet.get('inputs',[])):
            captured=[dict(spec=s,native=n) for s,n in zip(packet.get('specs',[]),packet.get('inputs',[]))]
        else:raise ValueError('NATIVE_REFERENCE_SCOPE_REQUIRED')
        records=[r for r in captured if r['spec']==spec]
        if len(records)!=1:raise ValueError('NATIVE_REFERENCE_SCOPE_REQUIRED')
        native=records[0]['native'];expected=native['fingerprint']
        reference_version=FINGERPRINT_VERSION if _fingerprint(native)==expected else 3
        if _fingerprint(native,version=reference_version)!=expected:raise ValueError('NATIVE_REFERENCE_FINGERPRINT_INVALID')
    except (KeyError,TypeError,AttributeError,ValueError) as error:
        raise ValueError('NATIVE_DIAGNOSTIC_REFERENCE_INVALID') from error
    review_url(tv_symbol)
    if result not in OBSERVATIONS or len(note)>1500:raise ValueError('REVIEW_TOO_LARGE_OR_UNKNOWN_RESULT')
    files=_evidence_files(screenshots)
    with store.connect() as db:
        db.execute('BEGIN IMMEDIATE');inputs=_input(db,spec)
        if _fingerprint(inputs,version=reference_version)!=expected:raise ValueError('REVIEW_INPUT_CHANGED')
        job,added=_enqueue(db,spec,dict(kind='prior_human_diagnosis',native_reference=str(path),
            native_sha256=hashlib.sha256(raw).hexdigest()),pd.Timestamp.now(tz='UTC'))
        if job.get('observation'):return {'state':'ALREADY_RECORDED','id':job['id']}
        old=dict(job);job['input_fingerprint']=inputs['fingerprint'];job['fingerprint_version']=FINGERPRINT_VERSION;_save_job(db,job,old)
        _record_observation(db,job,inputs,result,tv_symbol,note,files,datetime.now(timezone.utc).isoformat())
    return {'state':'RECHECK_QUEUED','id':job['id'],'grants_changed':False,'candles_written':0}


def suspicious_finding(job):
    """Evidence-based attention only; never DQ, NO_TRADES or permission."""
    finding=job.get('finding',{});state=finding.get('state')
    meta=job.get('identity',{})
    if meta.get('source') not in ('gettex','gettex_chart') or meta.get('venue')!='GETTEX':return None
    if state=='INVALID':return 'INVALID_NATIVE_DATA'
    if state=='GAP' and finding.get('market_state')=='GAP':return 'CONFIRMED_TRADES_WITHOUT_NATIVE_BAR'
    if state=='SOURCE_DISCREPANCY' and finding.get('material_difference') is True:
        return 'CONFIRMED_MATERIAL_SOURCE_DIFFERENCE'
    # Missing rows, source limits, delays, ordinary zero volume and uncertain
    # observations are not the most suspicious anomalies. A matching TV hole
    # remains uncertainty/accepted history, never fabricated exchange evidence.
    return None


def update_suspicious(progress,jobs,checked_at=None):
    if checked_at is not None:
        if progress.get('suspicious_version')!=2:
            progress['suspicious']={};progress['suspicious_version']=2
        jobs=[j for j in jobs if j.get('checked_at')==checked_at]
    saved=dict(progress.get('suspicious',{}))
    for job in jobs:
        reason=suspicious_finding(job);identifier=job.get('id')
        if not identifier:continue
        if not reason:saved.pop(identifier,None);continue
        saved[identifier]={k:job.get(k) for k in ('id','isin','identity','tf','first','last','end','checked_at','input_fingerprint')}
        saved[identifier].update(state=reason,reason=job.get('finding',{}).get('reason'),severity='HIGH')
    progress['suspicious_truncated']=len(saved)>256
    progress['suspicious']=dict(sorted(saved.items(),key=lambda item:str(item[1].get('checked_at') or ''),reverse=True)[:256])
    progress['suspicious_note']='Confirmed severe anomalies only; other DQ/jobs preserved separately. Cross-exchange gaps are context, not GETTEX evidence.'


def collect(db, isin=None, page=0):
    from collections import Counter
    # Read-only; no job creation, source calls or reclassification on UI/MCP reads.
    progress={k[len(PROGRESS):]:json.loads(v) for k,v in db.execute('SELECT key,value FROM settings WHERE key LIKE ?', (PROGRESS+'%',))}
    rows=[];counts=Counter();page=max(0,int(page))
    for i,p in progress.items():
        if not isin or i==isin:counts.update(p.get('counts',{}))
    total=sum(counts.values());page=min(page,max(0,(total-1)//100))
    pattern=PREFIX+isin+':%' if isin else PREFIX+'%'
    rows=[json.loads(raw) for (raw,) in db.execute('SELECT value FROM settings WHERE key LIKE ? ORDER BY key LIMIT 100 OFFSET ?', (pattern,page*100))]
    batches={i:p['review_batch'] for i,p in progress.items() if p.get('review_batch') and (not isin or i==isin)}
    # Compact saved metadata only. No market queries, audit or provider traffic
    # from the UI/MCP read path; old jobs enter statistics on their normal turn.
    comparisons=[json.loads(v) for v, in db.execute("SELECT json_extract(value,'$.source_comparison') FROM settings WHERE key LIKE ? AND json_type(value,'$.source_comparison')='object'",(pattern,))]
    return dict(version=VERSION,capability=capability(),progress=progress,review_batches=batches,counts=dict(counts),
                total=total,rows=rows,page=page,pages=max(1,(total+99)//100),
                source_comparisons=_comparison_summary(comparisons),
                suspicious=[row for i,p in progress.items() if (not isin or i==isin) and p.get('suspicious_version')==2 for row in p.get('suspicious',{}).values()][:100],
                suspicious_count=sum(len(p.get('suspicious',{})) for i,p in progress.items() if (not isin or i==isin) and p.get('suspicious_version')==2),
                suspicious_reviewed_stocks=sum(p.get('suspicious_version')==2 for i,p in progress.items() if not isin or i==isin),
                suspicious_scope='Saved severe findings from ordinary CP turns; not a new audit or DQ override',
                source_admission=read(db,SOURCE_POLICY,{}),
                scope='Diagnostic jobs, not repaired candles or trading permissions',recovered_candles=0)
