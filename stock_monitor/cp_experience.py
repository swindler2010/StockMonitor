"""Reusable, evidence-backed recovery knowledge in the existing CandlePilot path.

The catalogue is data, never executable instructions. Experience orders existing
child TFs and reuses bounded negative local checks; all positive checks still run.
No network, mail, trading permission or OHLCV writes originate from this module.
"""
import hashlib
import json
import re
from functools import lru_cache
from pathlib import Path

POLICY = 1
IDENTITY = ('source', 'venue', 'currency', 'symbol', 'timezone')
TABLE = 'cp_recovery_experience'
LOCAL_MEMO_LIMIT = 64


@lru_cache(maxsize=1)
def recovery_policy_digest():
    """A deployment of a validator must invalidate old negative observations."""
    root=Path(__file__).parent
    return hashlib.sha256(b''.join((root/name).read_bytes() for name in
        ('cp_experience.py','data_quality.py','history_service.py','timeframes.py','intraday.py','eur_listings.py','gettex_chart.py','analytics.py'))).hexdigest()


def local_failure_token(store,info,meta,base,first,last):
    """Fingerprint exact candidate rows and their validation dependencies.

    Only used to avoid a repeated NEGATIVE local search, never to certify data.
    A fresh SQLite read transaction includes evidence, identity and nested lineage.
    """
    import pandas as pd
    from data_quality import STEPS
    from timeframes import MINUTES
    from history_service import parent_provenance_key
    from performance_cache import _digest,_closed_rows
    first=pd.Timestamp(first);end=pd.Timestamp(last)+STEPS[base]
    if end-first>pd.Timedelta(days=8):return None
    children=[tf for tf in ('1m','5m','15m','1H') if MINUTES[tf]<(1440 if base=='1D' else MINUTES[base])]
    isin=info['isin']
    result=[recovery_policy_digest(),scope(isin,meta,base),str(first),str(end),
            {k:meta.get(k) for k in ('intraday_blockers','classify_absence','listing_proof')}]
    with store.connect() as db:
        db.execute('BEGIN')
        def setting(key):
            row=db.execute('SELECT value FROM settings WHERE key=?',(key,)).fetchone()
            return json.loads(row[0]) if row else {}
        result.append(db.execute('SELECT payload FROM instruments WHERE isin=?',(isin,)).fetchone())
        result += [setting('history_identity:'+isin),setting('interval_evidence:'+isin)]
        for tf in [base,*children]:
            store.read_cache.depend('market:'+isin+':'+tf)
            args=[isin,meta['source'],tf]
            if meta['source']=='gettex':sql='SELECT ts,open,high,low,close,volume FROM candles WHERE isin=? AND source=? AND base_tf=?'
            else:
                sql='SELECT ts,open,high,low,close,volume FROM series WHERE isin=? AND source=? AND base_tf=? AND symbol=? AND venue=? AND currency=?'
                args += [meta['symbol'],meta['venue'],meta['currency']]
            result.append(db.execute(sql+' AND ts>=? AND ts<? ORDER BY ts',(*args,str(first),str(end))).fetchall())
            coverage=setting('history_coverage:'+isin+':'+meta['symbol']+':'+tf)
            result.append({k:coverage.get(k) for k in (*IDENTITY,'intraday_blockers')})
            if tf!=base:
                quality=setting('data_quality:'+isin+':'+meta['symbol']+':'+tf)
                result.append({k:quality.get(k) for k in ('state','issues','ranges')})
            chart=setting('gettex_chart:'+isin+':'+tf)
            result.append({k:chart.get(k) for k in ('state','identity','listing_proof')})
            proofs=setting(parent_provenance_key(isin,meta,tf))
            for stamp,proof in sorted(proofs.items()):
                if first<=pd.Timestamp(stamp)<end:
                    result.append((stamp,proof,[_digest(_closed_rows(db,s)) for s in proof['inputs']]))
    return _digest(result)


def local_recovery(info,store,meta,base,first,last,now,callback):
    """Reuse a failed local check for at most the existing initial 5-min wait.

    Uses ordinary CP settings, not another queue. No FAILED attempt is invented
    for provider waiting; remote retry/cooldown and publication guards stay put.
    """
    import pandas as pd
    clock=pd.Timestamp(now)
    context=scope(info['isin'],meta,base)
    key='cp_local_recovery:'+hashlib.sha256(context.encode()).hexdigest()
    slot=json.dumps([str(pd.Timestamp(first)),str(pd.Timestamp(last))])
    try:
        memo=store.setting(key,{})
        token=local_failure_token(store,info,meta,base,first,last)
        entry=memo.get(slot,{})
        if token and entry.get('token')==token and entry.get('checked_at') and entry.get('retry_at'):
            if pd.Timestamp(entry['checked_at'])<=clock<pd.Timestamp(entry['retry_at']):return None,True
    except Exception:
        # Optional optimization must never replace the original validator.
        return callback(),False
    value=callback()
    # A concurrent update cannot publish a stale negative observation.
    try:
        if value is None and token and local_failure_token(store,info,meta,base,first,last)==token:
            memo[slot]=dict(token=token,checked_at=clock.isoformat(),retry_at=(clock+pd.Timedelta(minutes=5)).isoformat(),
                            identity={k:meta.get(k) for k in IDENTITY},base=base,isin=info['isin'])
            memo=dict(sorted(memo.items(),key=lambda item:item[1]['checked_at'])[-LOCAL_MEMO_LIMIT:])
            store.set_setting(key,memo)
        elif slot in memo:
            del memo[slot];store.set_setting(key,memo)
    except Exception:
        pass  # Metadata failure cannot block validated recovery or imply READY.
    return value,False


@lru_cache(maxsize=1)
def catalogue():
    value = json.loads(Path(__file__).with_name('cp_experience_catalogue.json').read_text(encoding='utf-8'))
    if value.get('schema') != POLICY:
        raise ValueError('Unsupported CandlePilot experience catalogue')
    return value


def scope(isin, meta, base):
    return json.dumps([POLICY, isin, *[meta.get(k) for k in IDENTITY], base], ensure_ascii=False)


def exists(db):
    return bool(db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (TABLE,)).fetchone())


def install(db):
    db.execute('''CREATE TABLE IF NOT EXISTS cp_recovery_experience (
        event_id TEXT PRIMARY KEY, scope TEXT NOT NULL, isin TEXT NOT NULL,
        identity TEXT NOT NULL, base_tf TEXT NOT NULL, method TEXT NOT NULL,
        outcome TEXT NOT NULL, first_at TEXT NOT NULL, last_at TEXT NOT NULL,
        input_revisions TEXT NOT NULL, checked_at TEXT NOT NULL, reason TEXT NOT NULL)''')
    db.execute('CREATE INDEX IF NOT EXISTS cp_experience_scope ON cp_recovery_experience(scope, outcome, method)')


def record(db, info, meta, base, first, last, revisions, clock, method, outcome, reason=''):
    """SUCCESS must be called inside the same transaction as validated recovery.

    FAILED describes an attempted range, not a damaged candle. No historical
    counters are fabricated. Re-publication of the same attempt is idempotent.
    """
    if outcome not in ('SUCCESS', 'FAILED'):
        raise ValueError('Unsupported recovery experience outcome')
    if method not in ('provider_range', 'local_children', 'children:1m', 'children:5m', 'children:15m', 'children:1H'):
        raise ValueError('Unsupported recovery experience method')
    if not db.in_transaction:
        raise ValueError('Recovery experience requires an explicit transaction')
    identity = {k: meta.get(k) for k in IDENTITY}
    if not info.get('isin') or any(not v for v in identity.values()):
        raise ValueError('Recovery experience requires exact identity')
    install(db)
    token = json.dumps(revisions, sort_keys=True, ensure_ascii=False)
    context = scope(info['isin'], meta, base)
    event = hashlib.sha256(json.dumps([context, str(first), str(last), token, str(clock), method, outcome]).encode()).hexdigest()
    # Structured failures use the existing reason column; old events remain
    # readable. Only failure_evidence() creates these bounded, non-secret fields.
    detail = json.dumps(reason, ensure_ascii=False, sort_keys=True) if isinstance(reason, dict) else str(reason)[:300]
    if len(detail)>2000:raise ValueError('Recovery evidence exceeds bounded record')
    db.execute('INSERT OR IGNORE INTO cp_recovery_experience VALUES (?,?,?,?,?,?,?,?,?,?,?,?)',
               (event, context, info['isin'], json.dumps(identity), base, method, outcome,
                str(first), str(last), token, str(clock), detail))
    return event


def failure_evidence(error, replacement=None, required=None):
    """Explain an attempted recovery, never infer NO_TRADES or permit trading.

    Do not persist arbitrary exception text: HTTP errors can contain tokens.
    Current DQ/revision checks still decide every subsequent attempt.
    """
    message=str(error);lower=message.lower()
    report=getattr(error,'report',{})
    if not isinstance(report,dict):report={}
    state=report.get('state') if report.get('state') in ('READY','GAP','UNCONFIRMED','INVALID','STALE','SOURCE_UNAVAILABLE') else 'UNKNOWN'
    status=getattr(error,'code',None)
    if type(status) is not int:status=getattr(getattr(error,'response',None),'status_code',None)
    if message in ('GETTEX_RECOVERY_COOLDOWN','GETTEX_RECOVERY_RATE_WAIT','Proxy source cooldown','GETTEX source cooldown'):
        code='WAITING_PROVIDER'
    elif lower.startswith(('recovery input changed','recovery lineage changed','input_changed','relevant_input_changed')):
        code='INPUT_CHANGED'
    elif message.startswith(('HISTORY_RESPONSE_IDENTITY_MISMATCH','EUR_HISTORY_IDENTITY_REQUIRED','GETTEX_RECOVERY_IDENTITY','EUR_HISTORY_ONLY')):
        code='IDENTITY_REJECTED'
    elif message.startswith(('PARENT_CHILD_CONFLICT','AUXILIARY_CHILD_CONFLICT')):code='DATA_CONFLICT'
    elif message.startswith(('SOURCE_LIMITED','AUX_1M_OUTSIDE_BOUNDED_RECENT_WINDOW')):code='SOURCE_LIMITED'
    elif message.startswith('В доступных файлах GETTEX нет сделок EUR для '):code='AVAILABLE_FILES_NO_MATCH'
    elif type(status) is int and 400<=status<=599:code='RATE_LIMIT_RESPONSE' if status==429 else 'HTTP_ERROR'
    elif state=='SOURCE_UNAVAILABLE':code='EMPTY_RESPONSE' if getattr(replacement,'empty',False) else 'SOURCE_UNAVAILABLE'
    elif state in ('GAP','UNCONFIRMED') or message.startswith('Источник не восстановил весь повреждённый участок'):code='INCOMPLETE_RESPONSE'
    elif state=='INVALID' or message=='GETTEX_RECOVERY_INVALID_RESPONSE':code='INVALID_RESPONSE'
    else:code='UNCLASSIFIED_FAILURE'
    cases={'WAITING_PROVIDER':'local_during_cooldown','INPUT_CHANGED':'revision_conflict',
           'IDENTITY_REJECTED':'bounded_same_source','DATA_CONFLICT':'revision_conflict',
           'SOURCE_LIMITED':'archive_limit','EMPTY_RESPONSE':'bounded_same_source',
           'SOURCE_UNAVAILABLE':'external_archive','INCOMPLETE_RESPONSE':'real_children',
           'INVALID_RESPONSE':'partial_archive','UNCLASSIFIED_FAILURE':'bounded_same_source',
           'AVAILABLE_FILES_NO_MATCH':'partial_evidence','RATE_LIMIT_RESPONSE':'local_during_cooldown','HTTP_ERROR':'external_archive'}
    descriptions={'WAITING_PROVIDER':'Ожидание действующего лимита поставщика; новый запрос не выполнен.',
        'INPUT_CHANGED':'Входные данные изменились; устаревший результат не записан.',
        'IDENTITY_REJECTED':'Не подтверждены инструмент, источник, площадка или EUR.',
        'DATA_CONFLICT':'Проверенные родительские и младшие данные противоречат друг другу.',
        'SOURCE_LIMITED':'Запрос находится за установленной границей доступного источника.',
        'EMPTY_RESPONSE':'Ответ не содержит пригодных закрытых свечей; отсутствие торгов не доказано.',
        'SOURCE_UNAVAILABLE':'Пригодные данные не получены; полнота архива неизвестна.',
        'INCOMPLETE_RESPONSE':'Вернулась только часть необходимого участка; оставшийся пропуск сохраняется.',
        'INVALID_RESPONSE':'Ответ не прошёл DQ; причины проверки указаны отдельно.',
        'UNCLASSIFIED_FAILURE':'Причина пока не классифицирована; тип ошибки сохранён без секретных данных.',
        'AVAILABLE_FILES_NO_MATCH':'В доступных файлах нет подходящих EUR-сделок; полнота файлов не доказана.',
        'RATE_LIMIT_RESPONSE':'Поставщик ответил HTTP 429; соблюдать существующий Retry-After.',
        'HTTP_ERROR':'Поставщик вернул HTTP-ошибку; это не доказательство отсутствия торгов.'}
    def count(value):
        try:return len(value) if value is not None else None
        except TypeError:return None
    detail=dict(version=1,code=code,error_type=type(error).__name__[:80],dq_state=state,
                returned_count=count(replacement),required_count=count(required),case_id=cases[code],
                explanation_ru=descriptions[code],
                inference='Observed attempt only; not proof of no trades, exhausted archive or trade readiness')
    if type(status) is int and 400<=status<=599:detail['http_status']=status
    prefixes={'Невозможные OHLC':'OHLCV','OHLCV:':'OHLCV','Свечи вне сессии':'SESSION_ALIGNMENT',
        'Календарь/timezone:':'CALENDAR','Timezone/DST/timestamps:':'TIMESTAMP',
        'Дубликаты timestamps':'DUPLICATES','Неправильный порядок timestamps':'TIMESTAMP_ORDER',
        'Не подтверждено ':'IDENTITY','Изменилась идентичность:':'IDENTITY','Смешение рядов:':'MIXED_IDENTITY',
        'PARENT_PROVENANCE_CHANGED:':'PROVENANCE','Неполные агрегированные свечи:':'INCOMPLETE_AGGREGATION',
        'Неподтверждённые пустые интервалы:':'UNCONFIRMED_INTERVAL','Пропущенные свечи:':'MISSING_INTERVAL'}
    detail['issue_codes']=sorted({code for issue in report.get('issues',[]) if isinstance(issue,str)
                                 for prefix,code in prefixes.items() if issue.startswith(prefix)})
    for k in ('missing_count','stale_count'):
        if type(report.get(k)) is int and report[k]>=0:detail[k]=report[k]
    if isinstance(getattr(replacement,'attrs',None),dict):
        digest=replacement.attrs.get('receipt_sha256')
        if isinstance(digest,str) and re.fullmatch('[0-9a-fA-F]{64}',digest):detail['response_sha256']=digest
    return detail


def measured_reason(reason, observations):
    """Bounded measured work; CANDIDATE is not a committed successful repair."""
    import math
    grouped={}
    for o in observations:
        if o.get('tf') not in ('1m','5m','15m','1H') or o.get('result') not in ('REJECTED','CANDIDATE','ERROR'):continue
        if not isinstance(o.get('elapsed_ms'),(float,int)) or not math.isfinite(o['elapsed_ms']) or o['elapsed_ms']<0:continue
        if type(o.get('parents')) is not int or o['parents']<1:continue
        prev=grouped.get(o['tf'],{})
        grouped[o['tf']]=dict(tf=o['tf'],result=o['result'],parents=o['parents'],elapsed_ms=round(prev.get('elapsed_ms',0)+o['elapsed_ms'],3))
    if not grouped:return reason
    result=dict(reason) if isinstance(reason,dict) else dict(version=1,code='COMMITTED_RECOVERY',explanation_ru='OHLCV и происхождение сохранены атомарно; торговый допуск проверяется отдельно.')
    result['method_observations']=list(grouped.values())
    if len(json.dumps(result,ensure_ascii=False,sort_keys=True))>2000:return reason
    return result


def method_stats(db,info,meta,base,now):
    """Recent empirical method results, never market-data confidence."""
    import pandas as pd
    import math
    from statistics import median
    if not exists(db):return {}
    clock=pd.Timestamp(now);samples={}
    for method,outcome,at,reason in db.execute(
        'SELECT method,outcome,checked_at,reason FROM cp_recovery_experience WHERE scope=? ORDER BY checked_at DESC LIMIT 200',
        (scope(info['isin'],meta,base),)):
        try:
            if not clock-pd.Timedelta(days=30)<=pd.Timestamp(at)<=clock:continue
        except (ValueError,TypeError):continue
        try:detail=json.loads(reason)
        except (ValueError,TypeError):continue
        if not isinstance(detail,dict):continue
        observations=detail.get('method_observations',[])
        if not isinstance(observations,list):continue
        for o in observations[:4]:
            if not isinstance(o,dict) or o.get('tf') not in ('1m','5m','15m','1H'):continue
            if type(o.get('parents')) is not int or o['parents']<1:continue
            if type(o.get('elapsed_ms')) not in (float,int) or not math.isfinite(o['elapsed_ms']) or o['elapsed_ms']<0:continue
            if o.get('result') not in ('CANDIDATE','REJECTED'):continue
            tf=o['tf'];committed=outcome=='SUCCESS' and method=='children:'+tf and o['result']=='CANDIDATE'
            if not committed and o['result']!='REJECTED':continue
            items=samples.setdefault(tf,[])
            if len(items)<20:items.append((int(committed),o['elapsed_ms']/o['parents']))
    return {tf:dict(attempts=len(rows),successes=sum(x[0] for x in rows),median_ms_per_parent=median(x[1] for x in rows)) for tf,rows in samples.items()}


def child_order(store, info, meta, base, candidates, now=None):
    """Prefer previously COMMITTED successful children for this exact listing.

    This does not certify the new range. Caller must repeat all current guards.
    A success on another venue/currency/source/ISIN/parent TF cannot leak here.
    """
    import pandas as pd
    with store.connect() as db:
        if not exists(db):
            return tuple(candidates)
        successes = dict(db.execute("SELECT method,count(*) FROM cp_recovery_experience WHERE scope=? AND outcome='SUCCESS' GROUP BY method",
                                    (scope(info['isin'], meta, base),)).fetchall())
        stats=method_stats(db,info,meta,base,now or pd.Timestamp.now(tz='UTC'))
    order = {tf: i for i, tf in enumerate(candidates)}
    baseline=sorted(candidates, key=lambda tf: (-successes.get('children:' + tf, 0), order[tf]))
    # Only reorder adequately measured methods among their existing positions.
    # Sparse/legacy/unknown evidence retains its prior deterministic fallback.
    eligible=[tf for tf in baseline if stats.get(tf,{}).get('attempts',0)>=3 and stats[tf]['successes']>0]
    ranked=iter(sorted(eligible,key=lambda tf:(-stats[tf]['successes']/stats[tf]['attempts'],stats[tf]['median_ms_per_parent'],order[tf])))
    return tuple(next(ranked) if tf in eligible else tf for tf in baseline)


def plan(info, meta, base, report):
    """Explain the existing safe recovery path using reviewed case identifiers."""
    state = report.get('state', 'UNKNOWN')
    matched = [c for c in catalogue()['cases']
               if state in c['states'] and (not c.get('sources') or meta.get('source') in c['sources'])]
    return dict(policy=POLICY, identity={k: meta.get(k) for k in IDENTITY}, isin=info['isin'], timeframe=base,
                case_ids=[c['id'] for c in matched],
                first_action='Проверить совместимые сохранённые младшие свечи; прошлые успешные TF — первыми, с новой проверкой DQ.',
                next_action='Штатное восстановление того же источника после cooldown; при нехватке доказательств сохранить блокировку.',
                external_action='Контакты и варианты архива — в «Опыт CP». Письма и платный доступ требуют разрешения владельца.',
                confidence='Прошлый успех — порядок проверки, не вероятность успеха и не разрешение на торговлю.')


def read(db):
    """Read-only dashboard/MCP projection; no DDL, calculations or downloads."""
    catalog = catalogue()
    groups = []
    recent = []
    if exists(db):
        groups = [dict(method=m, outcome=o, attempts=n) for m, o, n in db.execute(
            'SELECT method,outcome,count(*) FROM cp_recovery_experience GROUP BY method,outcome')]
        for row in db.execute('SELECT event_id,isin,identity,base_tf,method,outcome,first_at,last_at,checked_at,reason,input_revisions FROM cp_recovery_experience ORDER BY checked_at DESC,event_id DESC LIMIT 100'):
            event, isin, identity, tf, method, outcome, first, last, at, reason, revisions = row
            detail=None
            if reason.startswith('{'):
                try:
                    parsed=json.loads(reason)
                    if parsed.get('version')==1 and parsed.get('code'):detail=parsed;reason=parsed['code']
                except (ValueError,TypeError,AttributeError):pass
            recent.append(dict(id=event, isin=isin, identity=json.loads(identity), tf=tf, method=method,
                               outcome=outcome, first=first, last=last, checked_at=at, reason=reason,
                               failure_evidence=detail if outcome=='FAILED' else None,
                               success_evidence=detail if outcome=='SUCCESS' else None,
                               method_observations=(detail or {}).get('method_observations',[]),
                               input_revisions=json.loads(revisions)))
    return dict(schema=POLICY, cases=catalog['cases'], source_notes=catalog['sources'],
                evidence_index=catalog['evidence_index'], attempts=groups, recent=recent,
                historical_attempt_total='UNKNOWN', probability=None,
                note='Числа — новые фактические попытки восстановления диапазонов, не свечи и не вероятность. Старые PASS и найденные сайты не считаются успешным восстановлением.')


def display_rows(knowledge):
    """Small pure projection reused by the existing Health tree."""
    rows = []
    for case in knowledge['cases']:
        rows.append(dict(id=case['id'], isin='Все подходящие', source=', '.join(case.get('sources') or ['любой подтверждённый']),
                         venue='по identity', currency='EUR', symbol=case['title'], tf='по условиям',
                         state=case['status'], name=case['title'], experience=case))
    for event in knowledge['recent']:
        rows.append(dict(id=event['id'], isin=event['isin'], **event['identity'], tf=event['tf'],
                         state=event['outcome'], name=event['method'], experience=event))
    return rows
