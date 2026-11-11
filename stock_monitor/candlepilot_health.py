from i18n import tr
from localized_widgets import CanonicalCombobox
"""Read-only saved CandlePilot diagnostics. Never audits or fetches OHLCV."""
import json,sqlite3,time
from contextlib import closing
from pathlib import Path
from collections import Counter
from datetime import datetime,timezone,timedelta
FIELDS=('source','venue','currency','symbol')

def diagnostic_scopes(rows,infos,policy):
    """Describe saved report relevance without changing DQ or granting readiness."""
    from eur_listings import owner_history_info,venue_allowed
    groups={k:dict(series=0,problems=0,states={}) for k in ('current_scope','other_venue_archive','removed_instrument','unknown_identity')}
    for row in rows:
        if row['isin'] not in infos:scope='removed_instrument'
        elif not all(row.get(k) for k in FIELDS):scope='unknown_identity'
        elif venue_allowed(owner_history_info(infos[row['isin']],policy),row):scope='current_scope'
        else:scope='other_venue_archive'
        row['diagnostic_scope']=scope
        unit=groups[scope];unit['series']+=1;unit['problems']+=row['state']!='READY'
        unit['states'][row['state']]=unit['states'].get(row['state'],0)+1
    return dict(groups=groups,meaning='Policy scope only; not current DQ validation or trading permission. Historical errors and rows preserved.')

def diagnostic_scope_text(summary):
    groups=summary.get('groups',{})
    return 'Проблемные серии: текущая область {0}; архив других площадок {1}; удалённые инструменты {2}; источник не установлен {3}. Это не торговый допуск.'.format(
        *(groups.get(k,{}).get('problems',0) for k in ('current_scope','other_venue_archive','removed_instrument','unknown_identity')))+\
        '\nСнято в историю отчётов удалённых инструментов: {0}. Это очистка диагностики, не исправление свечей.'.format(summary.get('retired_reports',0))

def retired_diagnostic_count(db):
    return db.execute("SELECT count(*) FROM settings WHERE key LIKE 'cp_retired_diagnostic:%'").fetchone()[0]

REVIEW_LABELS={
    'AWAITING_USER':'Нужна внешняя сверка; CP продолжает проверку источника',
    'AWAITING_SOURCE':'Ожидаются данные или подтверждение поставщика',
    'RESOLVED_NATIVE':'Закрыто по данным источника',
    'REVIEW_WEEKLY':'GETTEX принят; расхождение с TV — на еженедельную сверку',
    'QUEUED':'В очереди CP','RECHECK_QUEUED':'Сохранённая сверка ожидает проверки CP',
    'BLOCKED':'Техническая блокировка проверки'}

def review_comparison_text(row):
    accepted=(row.get('source_disposition') or {}).get('label','')
    prefix=accepted+'\n' if accepted else ''
    comparison=row.get('source_comparison') or {}
    if not comparison:return prefix+'Статистика этой сверки ещё не подготовлена обычной очередью CP.\n'
    presence=comparison.get('presence_match')
    text=prefix+'Совпадение наличия свечи с TV: '+('да' if presence is True else 'нет' if presence is False else 'не измерено')+'. OHLCV: совпадение не измерено.\n'
    text+='Сохранённая проверка: '+str(comparison.get('native_checked_at'))+'. Это диагностика выбранного интервала, не общий рейтинг источника.\n'
    monthly=row.get('monthly_review') or {}
    if monthly:
        text+=('Еженедельная сверка Work №1: '+str(monthly.get('external_due_at')) if monthly.get('cadence_days')==7
               else 'Ежемесячная проверка данных CP: '+str(monthly.get('next_native_check_at')))+'.\n'
        if monthly.get('external_state')=='ASSISTED_RECHECK_REQUIRED':
            text+='Наступил срок повторной внешней сверки TV; она ещё не выполнена. Сам срок не отменяет неизменившееся принятое доказательство.\n'
    return text

def readiness_text(summary):
    return tr('Smart Eligibility: проверено {p0}/{p1} акций; оценки актуальны: {p2}. Контекст графика: актуален {p3}, прошёл последнюю оценку {p4}.\nАнализ: {p5}; сигнал: {p6}; демоторговля: {p7}. Это допуски расчётных контекстов, не количество акций со свечами в архиве.\n',
        p0=summary['assessed'],p1=summary['stocks'],p2=summary['current'],p3=summary['chart_ready'],
        p4=summary['chart_ready_at_last_assessment'],p5=summary['analysis_ready'],p6=summary['signal_ready'],p7=summary['trade_ready'])

def chart_assessment_text(row):
    if row['chart_ready']:return tr('актуален')
    if row.get('chart_ready_at_last_assessment'):return tr('прежняя оценка; требуется актуализация')
    return tr('не подтверждён')

def eligibility_reason(code):
    labels={'IDENTITY_UNCONFIRMED':'Не подтверждён EUR-листинг: источник, площадка или валюта',
        'LISTING_UNCONFIRMED':'Листинг не подтверждён для этого ISIN','IDENTITY_MISMATCH':'Источник или листинг отличается от разрешённого',
        'DERIVED_PROVENANCE_NOT_PROVEN':'Не доказана полнота исходных свечей производного TF',
        'ARCHIVE_ERROR_SCOPE_NOT_PROVEN':'Не доказано, что ошибка архива находится вне расчётных зависимостей',
        'RECURSIVE_INITIALIZATION_SENSITIVE':'Ограничение истории изменяет индикаторы или факты CORE',
        'INDICATOR_WARMUP_OR_OHLCV':'Недостаточный прогрев индикаторов либо повреждены OHLCV',
        'NO_USABLE_CANDLES':'Нет пригодных свечей в расчётном контексте; причина исходного DQ сохранена, если известна',
        'TIMESTAMPS_INVALID':'Повторяющиеся либо неупорядоченные временные метки'}
    labels['INSUFFICIENT_RECURSIVE_WARMUP_5Y']='В пределах пяти лет не подтверждена устойчивость прогрева рекурсивных индикаторов'
    labels['DEMO_SEED_SIGNAL_OR_RISK_UNSTABLE']='Проверка прогрева выявила изменение сигнала или риска'
    labels['DEMO_SEED_POLICY_MISMATCH']='Не совпадают разрешённые расчётные контексты; проверка прогрева не выполнена'
    labels['DEMO_SEED_SCENARIO_LIMIT']='Проверка прогрева не завершена: превышен допустимый объём комбинаций'
    if code in labels:return labels[code]
    states={'UNCONFIRMED':'неподтверждённые интервалы','GAP':'пропущенные свечи','STALE':'устаревшие данные','INVALID':'повреждённые данные','SOURCE_GAP':'неполные данные поставщика','SOURCE_UNAVAILABLE':'данные недоступны'}
    for prefix,text in [('WINDOW_','В расчётном окне: '),('RECURSIVE_REFERENCE_','В истории, необходимой для проверки индикаторов: ')]:
        if code.startswith(prefix):return text+states.get(code[len(prefix):],code[len(prefix):])
    return code

def delta(rows,previous):
    old={r['key']:r['state'] for r in previous};new={r['key']:r['state'] for r in rows}
    shared=old.keys()&new.keys()
    return dict(resolved=sum(old[k]!='READY' and new[k]=='READY' for k in shared),new_problems=sum(old[k]=='READY' and new[k]!='READY' for k in shared),changed=sum(old[k]!=new[k] for k in shared),added=len(new.keys()-old.keys()),removed=len(old.keys()-new.keys()),scope='saved report transitions, not independently verified repairs')

def proof_slots(proofs):
    slots=set();unsupported=0
    for ident,a,b in proofs:
        try:
            start=datetime.fromisoformat(a);end=datetime.fromisoformat(b)
            seconds=(end-start).total_seconds()
            if seconds<=0 or seconds%300 or seconds>86400 or start.minute%5 or start.second:raise ValueError()
            while start<end:
                slots.add((ident,start.isoformat()));start+=timedelta(minutes=5)
        except (ValueError,TypeError):unsupported+=1
    return len(slots),unsupported

def collect(path,snapshot=None,problem_isin=None,problem_page=0):
    start=time.perf_counter();path=Path(path)
    with closing(sqlite3.connect(path.resolve().as_uri()+'?mode=ro',uri=True)) as db:
        db.execute('PRAGMA query_only=ON');db.execute('BEGIN')
        import cp_experience
        experience=cp_experience.read(db)
        import cp_reconciliation
        reconciliation=cp_reconciliation.collect(db,problem_isin,problem_page)
        reconciliation['status_explanations']=REVIEW_LABELS.copy()
        reconciliation['awaiting_user_meaning']='Задания без внешнего подтверждения. CP продолжает ограниченную проверку собственных источников по next_check; это не число потерянных свечей и не остановка всей очереди.'
        infos={k:json.loads(v) for k,v in db.execute('SELECT isin,payload FROM instruments')}
        policy_row=db.execute("SELECT value FROM settings WHERE key='cp_gettex_source_policy'").fetchone()
        diagnostic_policy=json.loads(policy_row[0]) if policy_row else {}
        retired_reports=retired_diagnostic_count(db)
        prefixes=('history_archive_progress:','data_quality:','interval_evidence:','cp_schedule:','cp_revision:','cp_eligibility:')
        settings={k:json.loads(v) for k,v in db.execute('SELECT key,value FROM settings WHERE '+ ' OR '.join('key LIKE ?' for _ in prefixes),[p+'%' for p in prefixes])}
        rows={};archive=set();success=[];no_trades=set()
        def add(isin,meta,tf,q,origin):
            key=json.dumps([isin,*[meta.get(k) for k in FIELDS],tf],ensure_ascii=False)
            row=dict(key=key,isin=isin,name=infos.get(isin,{}).get('name',isin),**{k:meta.get(k) for k in FIELDS},tf=tf,state=q.get('state','UNCONFIRMED'),issues=q.get('issues',[]),warnings=q.get('warnings',[]),checked_at=q.get('checked_at'),origin=origin)
            prior=rows.get(key)
            if prior is None or str(row['checked_at'] or '')>=str(prior['checked_at'] or ''):rows[key]=row
            return key
        for key,value in settings.items():
            if key.startswith('history_archive_progress:'):
                isin=key.split(':',1)[1]
                for unit,report in value.get('series',{}).items():
                    source,venue,currency,symbol,tf=json.loads(unit)
                    archive.add(add(isin,dict(source=source,venue=venue,currency=currency,symbol=symbol),tf,report.get('quality',{}),'archive'))
            elif key.startswith('data_quality:'):
                isin=key.split(':')[1];meta=value.get('identity',{})
                add(isin,meta,value.get('base_tf',key.rsplit(':',1)[-1]),value,'saved_dq')
            elif key.startswith('interval_evidence:') and value.get('basis')=='complete_published_trade_files':
                ident=json.dumps([key.split(':',1)[1],value.get('identity')],sort_keys=True)
                for a,b in value.get('no_trades',[]):no_trades.add((ident,a,b))
            elif key.startswith('cp_schedule:') and value.get('outcome')=='CHECKED':success.append(value.get('finished_at',''))
            elif key.startswith('cp_revision:') and value.get('state') in ('UPDATED','UNCHANGED'):success.append(value.get('detected_at',value.get('checked_at','')))
        exists=db.execute("SELECT 1 FROM sqlite_master WHERE name='candle_revision_history'").fetchone()
        changes=db.execute('SELECT count(*) FROM (SELECT DISTINCT isin,identity,base_tf,ts FROM candle_revision_history)').fetchone()[0] if exists else 0
        events=db.execute('SELECT count(*) FROM candle_revision_history').fetchone()[0] if exists else 0
        recovery_exists=db.execute("SELECT 1 FROM sqlite_master WHERE name='candle_recoveries'").fetchone()
        recovered=db.execute('SELECT count(*) FROM candle_recoveries').fetchone()[0] if recovery_exists else 0
        token=(snapshot or {}).get('_input_revisions',{})
        current=bool(token) and all((db.execute('SELECT revision FROM input_revisions WHERE scope=?',(k,)).fetchone() or [0])[0]==v for k,v in token.items())
        historical_valid=False;historical_error=None
        if (snapshot or {}).get('analysis_snapshot'):
            from performance_cache import validate_closed_inputs,InputRevisionChanged
            try:
                validate_closed_inputs(db,(snapshot or {}).get('_closed_inputs'))
                historical_valid=True
            except (InputRevisionChanged,ValueError,KeyError,TypeError) as error:historical_error=str(error)
        from candle_impact import summary as impact_summary
        from calculation_window import eligibility_state,candidate_state
        from analytics import TIMEFRAMES
        eligibility={};eligibility_rows=[];input_memo={}
        for isin,info in infos.items():
            if info.get('kind')=='benchmark':continue
            saved=settings.get('cp_eligibility:'+isin,{})
            status=eligibility_state(db,saved,datetime.now(timezone.utc))
            valid=status=='CURRENT'
            candidates=[dict(c,current_state=candidate_state(db,saved,c,datetime.now(timezone.utc),input_memo)) for c in saved.get('candidates',[])]
            # A closed historical analysis can survive unrelated revisions;
            # execution permission cannot. Use the same read-only transaction gate.
            from calculation_window import execution_eligibility
            for candidate in candidates:
                if candidate.get('trade_ready'):
                    try:execution_eligibility(db,info,candidate['timeframe'],candidate.get('execution_identity'),datetime.now(timezone.utc))
                    except (ValueError,KeyError,TypeError) as error:
                        candidate['trade_ready']=False
                        candidate['trade_blockers']=[*candidate.get('trade_blockers',[]),str(error)]
            live_candidates=[c for c in candidates if c['current_state']=='CURRENT']
            eligibility[isin]=dict(state=status,checked_at=saved.get('checked_at'),expires_at=saved.get('expires_at'),
                analysis_scope=saved.get('analysis_scope'),
                error=saved.get('error'),context=saved.get('context'),candidates=live_candidates,
                historical_candidates=[c for c in candidates if c.get('closed_inputs') and c['current_state']=='EXPIRED'],
                primary_entry_timeframe=saved.get('primary_entry_timeframe','15m'),optional_timeframes=saved.get('optional_timeframes',[]),
                current_basis='Each candidate validates its own required closed inputs; execution also checks its own current revisions and price age')
            for tf in TIMEFRAMES:
                unit=saved.get('timeframes',{}).get(tf,{})
                candidates=[c for c in eligibility[isin]['candidates'] if c.get('timeframe')==tf]
                row_valid=valid or bool(candidates)
                full=any(c.get('context')=='full_closed_reference_v1' for c in candidates)
                approved=next((c for c in candidates if c.get('owner_reviewed_analysis')),None)
                shown=unit.get('full_reference',{}) if full else unit
                if approved:
                    shown=(approved.get('result') or {}).get('timeframe_context',{}).get(tf,{})
                eligibility_rows.append(dict(isin=isin,name=info.get('name',isin),tf=tf,state='CURRENT' if row_valid else status,record_state=status,
                    identity=approved['identity'] if approved else unit.get('identity',{}),count=shown.get('count',shown.get('closed_bar_count')),first=shown.get('first'),last=shown.get('last'),
                    analysis_scope=saved.get('analysis_scope'),
                    context=approved['context'] if approved else 'full_closed_reference_v1' if full else unit.get('context'),bounded_blockers=unit.get('blockers',[]),
                    chart_ready=row_valid and bool(unit.get('chart_ready')),
                    chart_ready_at_last_assessment=bool(unit.get('chart_ready')),
                    chart_assessed_at=saved.get('checked_at'),
                    analysis_ready=any(c.get('analysis_ready') for c in candidates),
                    signal_ready=any(c.get('signal_ready') for c in candidates),
                    trade_ready=any(c.get('trade_ready') for c in candidates),
                    blockers=([b for c in candidates for b in c.get('blockers',[])+((c.get('result') or {}).get('blockers',[]))] if full or approved else unit.get('blockers',[])) if row_valid else ['Оценка ожидается' if status=='UNKNOWN' else 'Допуск отозван: '+status],
                    candidates=candidates,archive_state=unit.get('archive_state'),window_quality=unit.get('window_quality',{}),
                    quality_impact=unit.get('quality_impact'),impact_summary=('Стандартный контекст без принятия риска: ' if approved else '')+impact_summary(unit),analytical_only=bool(unit.get('analytical_only'))))
        from cp_controls import view,digest,gap_price_estimate
        # Potentially 100k archived ranges: only page the requested instrument,
        # never rebuild/insert every problem in Tk or send it in every MCP read.
        problems=[];review=None
        if problem_isin in infos:
            all_problems=view(db,problem_isin)
            page=max(0,min(int(problem_page),max(0,(len(all_problems)-1)//150)))
            review=dict(isin=problem_isin,count=len(all_problems),page=page,pages=max(1,(len(all_problems)+149)//150),digest=digest(sorted(p['id'] for p in all_problems)))
            problems=[dict(p,name=infos[problem_isin].get('name',problem_isin)) for p in all_problems[page*150:(page+1)*150]]
            estimate_memo={}
            for p in problems:p['price_estimate']=gap_price_estimate(db,p,memo=estimate_memo)
        problem_summary={isin:dict(archive_ranges=sum(max(1,len(v.get('ranges',[]))) for k,v in settings.items() if k.startswith('data_quality:'+isin+':') and v.get('state')!='READY'),
            note='Saved problem ranges; not unique damaged candles') for isin,i in infos.items() if i.get('kind')!='benchmark'}
        owner_controls={}
        for key,payload in db.execute("SELECT key,value FROM settings WHERE key LIKE 'cp_manual_risk:%' OR key LIKE 'cp_repair_requests:%'"):
            isin=key.split(':',1)[1];value=json.loads(payload);unit=owner_controls.setdefault(isin,{})
            if key.startswith('cp_manual_risk:'):
                unit['reviewed_problems']=len(value.get('accepted',{}));unit['last_owner_action']=(value.get('events') or [None])[-1]
                unit['permission_note']='Historical receipt only; current problems and executable quote rechecked at order time'
                from cp_controls import TV_ACTOR,TV_LABEL,TV_PRICE_MODEL,effective_accepted
                tv={k:v for k,v in value.get('accepted',{}).items() if v.get('actor')==TV_ACTOR}
                if tv:
                    active_tv=effective_accepted(db,isin,{'accepted':tv})
                    unit['tv_risk']=dict(label=TV_LABEL,reviewed_ranges=len(tv),active_ranges=len(active_tv),
                        price_model=TV_PRICE_MODEL,meaning='Conditional historical risk only; not DQ READY or executable permission',
                        ranges=[dict(id=k,active=k in active_tv,**v['evidence']) for k,v in tv.items()])
                permission_review=value.get('historical_review',{})
                if permission_review:
                    unit['historical_review']={k:permission_review.get(k) for k in ('state','execution_suspended','checked_at','requested_at','summary')}
                    unit['historical_review'].update(total=len(permission_review.get('targets',{})),reviewed=len(permission_review.get('results',{})),
                        next_retry=min((x['next_retry'] for x in permission_review.get('results',{}).values() if x.get('next_retry') and x.get('state')=='WAITING'),default=None))
            else:unit['repair_requests']=[dict(target=k,state=v.get('state'),reason=v.get('reason'),next_retry=v.get('next_retry'),requested_at=v.get('requested_at')) for k,v in value.items()]
        core={}
        for isin in infos:
            stock=next((x for x in (snapshot or {}).get('stocks',[]) if x.get('isin')==isin),{})
            engine=stock.get('trading_engine') or {}
            state='UNKNOWN' if not engine else 'STALE' if not current or not (snapshot or {}).get('ready') else ('BLOCKED' if engine.get('blockers') else 'READY' if engine.get('status')=='READY' else 'UNKNOWN')
            stock_historical_valid=historical_valid and stock.get('input_state')!='INVALIDATED'
            stock_historical_error=historical_error
            if (snapshot or {}).get('analysis_snapshot') and not historical_valid and stock.get('_closed_inputs'):
                try:validate_closed_inputs(db,stock['_closed_inputs'],input_memo);stock_historical_valid=True
                except (InputRevisionChanged,ValueError,KeyError,TypeError) as error:stock_historical_error=str(error)
            if engine and (snapshot or {}).get('analysis_snapshot'):state='HISTORICAL' if stock_historical_valid else 'STALE'
            core[isin]={'state':state,'reasons':engine.get('blockers',[]),'entry_blockers':engine.get('entry_blockers',[]),'engine_status':engine.get('status','UNKNOWN'),'signal':engine.get('signal','UNKNOWN'),'timeframe':stock.get('timeframe'),'basis':'Published Trading Engine CORE input gate; not trade permission; identity/TF scope of published snapshot','input_revisions_current':current}
            if (snapshot or {}).get('analysis_snapshot'):
                core[isin].update(closed_inputs_unchanged=stock_historical_valid,historical_invalid_reason=stock_historical_error if not stock_historical_valid else None,
                    as_of=(snapshot or {})['analysis_snapshot'].get('captured_at'),execution_permission=False)

    ordered=sorted(rows.values(),key=lambda r:(r['name'],r['source'] or '',r['venue'] or '',r['symbol'] or '',r['tf']))
    scope_summary=diagnostic_scopes(ordered,infos,diagnostic_policy)
    scope_summary['retired_reports']=retired_reports
    slots,unsupported=proof_slots(no_trades)
    result=dict(core=core,recovered_since_tracking=recovered,recovery_historical_total='UNKNOWN',no_trades_confirmed_5m_intervals=slots,no_trades_unquantified_ranges=unsupported,schema=1,generated_at=datetime.now(timezone.utc).isoformat(),instruments=len(infos),archive_series=len(archive),saved_dq_series=len(rows),states=dict(Counter(r['state'] for r in ordered)),rows=ordered,revision_changed_unique_candles=changes,revision_events=events,recovered_unique_candles=None,recovery_reason='Only new proven recovery writes tracked; historical total UNKNOWN; incoming raw rejection repairs not counted before persistence',no_trades_proof_ranges=len(no_trades),no_trades_unit='unique exact identity/start/end evidence ranges, NOT candles; different-sized overlapping ranges not merged',last_success=max(success,default=None),scope='Saved diagnostics only; READY reports are not current trading permission')
    tf_rows=[]
    result['diagnostic_scopes']=scope_summary
    groups={tuple(r.get(k) for k in ('isin',*FIELDS)) for r in ordered}
    for group in sorted(groups,key=str):
        found={r['tf']:r for r in ordered if tuple(r.get(k) for k in ('isin',*FIELDS))==group}
        for tf in ('5m','10m','15m','30m','1H','2H','3H','4H','1D','1W','1M'):
            if tf in found:tf_rows.append(found[tf]);continue
            meta=dict(zip(('isin',*FIELDS),group))
            tf_rows.append(dict(**meta,name=infos.get(meta['isin'],{}).get('name',meta['isin']),key=json.dumps([*group,tf]),tf=tf,state='UNKNOWN',issues=['No saved DQ report; no calculation requested'],origin='absent_report',checked_at=None))
    result['display_rows']=tf_rows
    result['experience']=experience
    result['reconciliation']=reconciliation
    result['eligibility']=eligibility
    result['eligibility_rows']=eligibility_rows
    result['problems']=problems
    result['problem_review']=review
    result['problem_summary']=problem_summary
    result['owner_controls']=owner_controls
    result['owner_controls_version']=1
    result['manual_risk_note']='Accepted missing bars can enter a separate owner-risk CORE context. DQ stays factual; invalid data, warmup and stale quotes remain blocked.'
    result['eligibility_summary']=dict(stocks=len(eligibility),assessed=sum(bool(v.get('checked_at')) for v in eligibility.values()),
        current=sum(v['state']=='CURRENT' for v in eligibility.values()),
        assessment_states=dict(Counter(v['state'] for v in eligibility.values())),
        chart_ready_basis='Unique stocks with an eligible calculation-window chart context; NOT archive availability. Last assessment is historical evidence, never current permission.',
        **{k:len({r['isin'] for r in eligibility_rows if r[k]}) for k in ('chart_ready','chart_ready_at_last_assessment','analysis_ready','signal_ready','trade_ready')})
    baseline=path.parent/'candlepilot-health-checkpoint.json'
    try:
        previous=json.loads(baseline.read_text(encoding='utf-8'));result['delta']=delta(ordered,previous['rows']);result['baseline_at']=previous['generated_at']
    except (OSError,ValueError,KeyError):result['delta']=None;result['baseline_at']=None
    result['read_seconds']=round(time.perf_counter()-start,4)
    return result

def show(app):
    import tkinter as tk
    from tkinter import ttk, messagebox
    prior = getattr(app, '_cp_health_window', None)
    if prior is not None and prior.winfo_exists():
        prior.lift()
        return
    window = tk.Toplevel(app.root)
    app._cp_health_window = window
    window.title(tr('CandlePilot Health — центр управления'))
    window.geometry('1120x700')
    header = ttk.Label(window, text=tr('Чтение сохранённой диагностики…'), wraplength=1080)
    header.pack(fill='x', padx=8, pady=8)
    bar = ttk.Frame(window)
    bar.pack(fill='x')
    search = tk.StringVar()
    ttk.Label(bar, text=tr('ISIN / источник / площадка / TF:')).pack(side='left')
    ttk.Entry(bar, textvariable=search, width=45).pack(side='left')
    columns = ('isin', 'source', 'venue', 'currency', 'symbol', 'tf', 'state')
    mode = tk.StringVar(value='Допуск CP')
    tree = ttk.Treeview(window, columns=columns, show='headings', height=15)
    for col in columns:
        tree.heading(col, text=col)
        tree.column(col, width=145 if col == 'isin' else 110)
    tree.pack(fill='both', expand=True, padx=8)
    details = tk.Text(window, height=11, wrap='word')
    details.pack(fill='both', padx=8, pady=8)
    data = {}
    shown = {}
    busy = [False]
    page = [0]
    stock_names = {i.get('name', i['isin']): i['isin'] for i in app.watchlist if i.get('kind') != 'benchmark'}
    stock = tk.StringVar(value=next(iter(stock_names), ''))

    def filter_rows(*_):
        picked = tree.selection()
        old = shown.get(picked[0], {}) if picked else {}
        selected_key = tuple((old.get(k) for k in ('isin', 'tf', 'source', 'venue', 'symbol', 'currency', 'id'))) if old else None
        tree.delete(*tree.get_children())
        shown.clear()
        query = search.get().casefold()
        rows = data.get('display_rows', [])
        if mode.get() == 'Допуск CP':
            rows = [{**r, **r.get('identity', {}), 'eligibility_state': r.get('state'), 'state': tr('Контекст графика: ') + chart_assessment_text(r) + tr(' / Анализ ') + (tr('да') if r['analysis_ready'] else tr('нет')) + tr(' / Сигнал ') + (tr('да') if r['signal_ready'] else tr('нет')) + tr(' / Торговля ') + (tr('да') if r['trade_ready'] else tr('нет'))} for r in data.get('eligibility_rows', [])]
        elif mode.get() == 'Проблемы свечей':
            rows = [{**r, **r['identity'], 'state': r['state'] + (tr(' · риск принят') if r['accepted'] else '')} for r in data.get('problems', [])]
        elif mode.get() == 'Опыт CP':
            from cp_experience import display_rows
            rows = display_rows(data.get('experience', {'cases': [], 'recent': []}))
        elif mode.get() == 'Сверка TradingView':
            from cp_controls import digest
            rows=[{**j,**j['identity'],'state':(REVIEW_LABELS['REVIEW_WEEKLY'] if j.get('source_disposition') and j['status']=='REVIEW_WEEKLY'
                   else j.get('finding',{}).get('state',j['status'])),
                   'name':j['interval_start']+' — '+j['end'],'review_digest':digest(j)} for j in data.get('reconciliation',{}).get('rows',[])]
        experience_mode = mode.get() == 'Опыт CP'
        tree.configure(displaycolumns=('symbol', 'isin', 'source', 'tf', 'state') if experience_mode else columns)
        for col in columns:
            tree.heading(col, text=({'symbol': 'Решение / попытка', 'isin': 'Область / ISIN', 'source': 'Источник', 'tf': 'TF', 'state': 'Результат'}.get(col, col) if experience_mode else col))
            tree.column(col, width=(380 if col == 'symbol' else 150) if experience_mode else (145 if col == 'isin' else 110))
        if not experience_mode:
            tree.column('state', width=330 if mode.get() != 'DQ' else 110)
        for button in control_buttons:
            button.configure(state='disabled' if experience_mode or mode.get()=='Сверка TradingView' else 'normal')
        for row in rows:
            if query and query not in ' '.join((str(row.get(k, '')) for k in (*columns, 'name'))).casefold():
                continue
            i = tree.insert('', 'end', values=[row.get(k) for k in columns])
            shown[i] = row
            if selected_key == tuple((row.get(k) for k in ('isin', 'tf', 'source', 'venue', 'symbol', 'currency', 'id'))):
                tree.selection_set(i)
        if tree.selection():
            selected()
        elif selected_key:
            details.delete('1.0', 'end')

    def selected(_=None):
        selected = tree.selection()
        if not selected:
            return
        row = shown[selected[0]]
        bridge = getattr(app, 'bridge', None)
        snapshot = getattr(bridge, 'state', {})
        if mode.get() == 'Сверка TradingView':
            from cp_reconciliation import capability
            details.delete('1.0','end')
            details.insert('1.0','Заключительная сверка с участием пользователя. CP сам обрабатывает очередь; TradingView открывает и проверяет человек.\n'
                +row['isin']+' · '+row['tf']+' · '+row['interval_start']+' — '+row['end']+' · '+row['timezone']+'\n'
                +row.get('finding',{}).get('reason','Ожидает обработки')+'\n'
                +REVIEW_LABELS.get(row.get('status'),row.get('status',''))+'\n'
                +'Визуальное отсутствие не доказывает NO_TRADES. Цены TradingView не импортируются. Допуски и сделки здесь не создаются.\n'
                +'Повторная проверка CP: '+str(row.get('next_check'))+'; native recovery: '+str(row.get('native_retry'))+'\n\n'
                +review_comparison_text(row)+'\n'
                +json.dumps(row,ensure_ascii=False,indent=2)+'\n'+json.dumps(capability(),ensure_ascii=False,indent=2))
            return
        if mode.get() == 'Опыт CP':
            details.delete('1.0', 'end')
            knowledge=data.get('experience',{})
            case=row.get('experience') or {}
            readable=(case.get('title','')+'\n'+'\n'.join(case.get('steps',[]))+'\nОграничения: '+str(case.get('limits',''))+'\n\n') if case.get('title') else ''
            details.insert('1.0',readable+'Опыт CandlePilot: сначала проверенные способы, затем доступные альтернативы.\n'
                           +knowledge.get('note','')+'\n\n'+json.dumps(row.get('experience'),ensure_ascii=False,indent=2)
                           +'\n\nИсточники и контакты:\n'+json.dumps(knowledge.get('source_notes',[]),ensure_ascii=False,indent=2))
            return
        if mode.get() == 'Проблемы свечей':
            from cp_controls import gap_estimate_text
            repair = row.get('repair') or {}
            span = row.get('range') or [tr('Не определено'), tr('Не определено')]
            details.delete('1.0', 'end')
            details.insert('1.0', tr('{p0} · {p1} · {p2}\nПериод: {p3} — {p4} ({p5})\nПричина: ', p0=row['name'], p1=row['isin'], p2=row['tf'], p3=span[0], p4=span[1], p5=row['identity'].get('timezone')) + '; '.join((tr(eligibility_reason(x)) for x in row.get('reasons', []))) + tr('\nРиск: {p0}. Исходный DQ сохранён; принятые пропуски допускаются в отдельный расчёт CORE.\nCP: {p1} · {p2}\nПовтор не раньше: {p3}\n{p4} CORE сохраняет identity и остальные проверки. Для ручной демооперации нужна свежая достоверная цена.\n\nТехнические подробности:\n', p0=tr('принят владельцем') if row.get('accepted') else tr('не принят'), p1=repair.get('state', tr('Заявка не отправлена')), p2=repair.get('reason', ''), p3=repair.get('next_retry', '—'), p4=('Подобная проблема в TV; риск принят владельцем. Все индикаторы и прогрев — только по настоящим свечам.' if row.get('accepted_by') == 'candlepilot_owner_tv_policy' else 'Принятые пропуски: EMA/RSI/MACD учитывают средние соседних Close. ATR, объём, уровни и прогрев — по реальным свечам.')) + json.dumps(row, ensure_ascii=False, indent=2))
            details.insert('1.0', gap_estimate_text(row.get('price_estimate'))+'\n')
            return
        if mode.get() == 'Допуск CP':
            labels = {'UNKNOWN': tr('Ожидает очереди'), 'STALE': tr('Изменились входные данные'), 'EXPIRED': tr('Истёк срок проверки'), 'ERROR': tr('Ошибка оценки'), 'CURRENT': tr('Проверка актуальна')}
            status = dict(row,state=row.get('eligibility_state',row.get('state')))
            context_text = tr('CORE: полный проверенный закрытый ряд. Исполнительный допуск проверяется отдельно.') if row.get('context') == 'full_closed_reference_v1' else tr('Отдельный CORE: до 200 закрытых свечей. Полный архивный расчёт не заменяется.')
            context_text += '\n'+tr('Контекст графика: ')+chart_assessment_text(row)+tr('. Последняя оценка: ')+str(row.get('chart_assessed_at'))+tr('. Срок оценки не удаляет свечи из архива.')
            if any(c.get('owner_reviewed_analysis') for c in row.get('candidates',[])):
                context_text=tr('Отдельный CORE с явно принятыми владельцем историческими рисками и проверкой устойчивости индикаторов. Демоисполнение по 15m проверяется отдельно.')
            if row.get('risk_context') or row.get('result', {}).get('accepted_risks'):
                context_text += tr(' Пропуски приняты владельцем; исходный DQ сохранён. Модель расчёта и ограничения указаны в данных принятого риска.')
            if row.get('impact_summary'):
                context_text += '\n' + row['impact_summary']
            review=data.get('owner_controls',{}).get(row['isin'],{}).get('historical_review')
            if review:
                context_text+='\n'+tr('Пересмотр исторических исключений: ')+str(review['reviewed'])+'/'+str(review['total'])+' · '+str(review['state'])+' · '+str(review.get('summary'))
                if review.get('execution_suspended'):context_text+='\n'+tr('Новые демосделки с историческими исключениями приостановлены. Существующая позиция сохраняется.')
            scope = row.get('analysis_scope')
            if scope:
                context_text = tr('Область анализа: последние 5 лет, с ') + str(scope.get('start')) + tr('. Старые дефекты вне области; Max и архив сохранены.\n') + context_text
            details.delete('1.0', 'end')
            details.insert('1.0', tr('{p0} · {p1}\n{p2}\n{p3}\nНастоящих свечей в контексте: {p4}\nПричины: ', p0=row['name'], p1=row['tf'], p2=labels.get(status.get('state'), tr('Неизвестно')), p3=context_text, p4=row.get('count')) + '; '.join((tr(eligibility_reason(x)) for x in row.get('blockers', []))) + tr('\nТорговля разрешается только действующим исполнительным контуром; эта панель не снимает ограничения.\n\nТехнические подробности:\n') + json.dumps(row, ensure_ascii=False, indent=2))
            return
        stocks = snapshot.get('stocks', [])
        stock = next((s for s in stocks if s.get('isin') == row['isin']), None)
        known = {r['tf'] for r in data.get('rows', []) if all((r.get(k) == row.get(k) for k in ('isin', *FIELDS)))}
        diagnostics = next((d for d in snapshot.get('diagnostic_detail', []) if d.get('isin') == row['isin']), None)
        text = {'reasons': row.get('issues'), 'warnings': row.get('warnings'), 'available_saved_TF': sorted(known), 'TF_without_saved_DQ': [t for t in ('5m', '10m', '15m', '30m', '1H', '2H', '3H', '4H', '1D', '1W', '1M') if t not in known], 'CORE': data.get('core', {}).get(row['isin'], {'state': 'UNKNOWN'}), 'diagnostic_detail': diagnostics, 'saved_report': row, 'analysis_snapshot_ready': snapshot.get('ready', False), 'analytics_status': snapshot.get('analytics_status', 'UNKNOWN'), 'trading_engine': stock.get('trading_engine') if stock else None, 'analysis_note': 'Snapshot may use another TF/listing; do not infer permission for the selected row. Missing CORE/TF = NOT_AVAILABLE, not READY.'}
        details.delete('1.0', 'end')
        details.insert('1.0', json.dumps(text, ensure_ascii=False, indent=2))

    def apply(result):
        busy[0] = False
        if not window.winfo_exists():
            return
        result['activity'] = getattr(getattr(app, 'bridge', None), 'cp_activity', {'state': 'UNKNOWN'})
        active=result['activity']
        activity_text=active.get('state','UNKNOWN')
        if active.get('state')=='ACTIVE':activity_text+=' · '+' · '.join(str(active[k]) for k in ('name','isin','timeframe','phase') if active.get(k))
        elif active.get('state')=='ERROR':activity_text+=' · '+str(active.get('reason') or 'Проверьте диагностику')
        data.clear()
        data.update(result)
        filter_rows()
        review = result.get('problem_review') or {}
        if review:
            page[0] = review['page']
        page_label.configure(text=tr('{p0}/{p1} · проблем {p2}; по 150 на странице', p0=review.get('page', 0) + 1, p1=review.get('pages', 1), p2=review.get('count', 0)) if review else '')
        header.configure(text=tr('CP: {p0} | Инструменты: {p1} | Архивные ряды: {p2} | DQ-ряды: {p3} | {p4}\nИзменено уникальных свечей (watcher): {p5} | Восстановлено с начала учёта: {p6} (исторически UNKNOWN) | NO_TRADES 5m-интервалов: {p7} (не квантовано: {p8})\nПоследнее успешное действие: {p9} | Чтение: {p10} с | Изменения: {p11}\nСохранённые DQ ≠ актуальный торговый допуск. Поиск фильтрует все сохранённые TF; отсутствующие отчёты не пересчитываются.', p0=result['activity'], p1=result['instruments'], p2=result['archive_series'], p3=result['saved_dq_series'], p4=result['states'], p5=result['revision_changed_unique_candles'], p6=result['recovered_since_tracking'], p7=result['no_trades_confirmed_5m_intervals'], p8=result['no_trades_unquantified_ranges'], p9=result['last_success'], p10=result['read_seconds'], p11=result['delta']))
        header.configure(text=header.cget('text').replace(str(result['activity']),activity_text,1))
        header.configure(text=diagnostic_scope_text(result.get('diagnostic_scopes',{}))+'\n'+header.cget('text'))
        tv=[u['tv_risk'] for u in result.get('owner_controls',{}).values() if u.get('tv_risk')]
        if tv:
            header.configure(text=header.cget('text')+tr('\nПодобная проблема в TV: риск принят для {p0} диапазонов, актуальны {p1}. Остальные проверки анализа и демосчёта сохраняются.',p0=sum(x['reviewed_ranges'] for x in tv),p1=sum(x['active_ranges'] for x in tv)))
        s = result['eligibility_summary']
        header.configure(text=readiness_text(s) + header.cget('text'))
        if mode.get()=='Сверка TradingView':
            q=result.get('reconciliation',{})
            page[0]=q.get('page',0)
            page_label.configure(text=f"{page[0]+1}/{q.get('pages',1)} · задач {q.get('total',0)}; по 100")
            counts='; '.join(REVIEW_LABELS.get(k,k)+': '+str(v) for k,v in q.get('counts',{}).items())
            header.configure(text='Сверка TradingView: CP готовит пакеты автоматически; визуальную проверку подтверждает человек.\n'+counts+'\n'+q['awaiting_user_meaning']+'\n'+header.cget('text'))

    def failed(error):
        busy[0] = False
        if window.winfo_exists():
            header.configure(text=tr('Ошибка чтения: ') + str(error))

    def refresh():
        if not window.winfo_exists():
            return
        if busy[0]:
            return
        busy[0] = True
        snapshot = getattr(getattr(app, 'bridge', None), 'state', {})
        selected_isin = stock_names.get(stock.get()) if mode.get() in ('Проблемы свечей','Сверка TradingView') else None
        selected_page = page[0]
        app.job(lambda: collect(app.store.path, snapshot, selected_isin, selected_page), apply, failed, lane='misc', key=('cp-health', id(window)))

    def checkpoint():
        if not data:
            return
        frozen = json.dumps(data, ensure_ascii=False)

        def write():
            p = Path(app.store.path).parent / 'candlepilot-health-checkpoint.json'
            tmp = p.with_suffix('.tmp')
            tmp.write_text(frozen, encoding='utf-8')
            tmp.replace(p)
            return collect(app.store.path, getattr(getattr(app, 'bridge', None), 'state', {}))
        app.job(write, apply, failed, lane='misc', key=('cp-health-save', id(window)))
    ttk.Button(bar, text=tr('Обновить показатели'), command=refresh).pack(side='left', padx=5)

    def change_mode(*_):
        batch_panel.pack_forget()
        filter_rows()
        refresh()
    choice = CanonicalCombobox(bar, textvariable=mode, values=('Допуск CP', 'DQ', 'Проблемы свечей', 'Опыт CP','Сверка TradingView'), state='readonly', width=18)
    choice.pack(side='left')
    choice.bind('<<ComboboxSelected>>', change_mode)
    ttk.Button(bar, text=tr('Контрольная точка'), command=checkpoint).pack(side='left')
    actions = ttk.Frame(window)
    actions.pack(fill='x', padx=8, pady=4, before=tree)
    paging = ttk.Frame(window)
    paging.pack(fill='x', padx=8, before=tree)
    ttk.Label(paging, text=tr('Проблемы акции:')).pack(side='left')
    stocks = ttk.Combobox(paging, textvariable=stock, values=list(stock_names), state='readonly', width=25)
    stocks.pack(side='left')

    def stock_changed(*_):
        batch_panel.pack_forget()
        page[0] = 0
        if mode.get()!='Сверка TradingView':mode.set('Проблемы свечей')
        refresh()
    stocks.bind('<<ComboboxSelected>>', stock_changed)

    def turn(delta):
        page[0] = max(0, page[0] + delta)
        refresh()
    ttk.Button(paging, text=tr('← Страница'), command=lambda: turn(-1)).pack(side='left')
    ttk.Button(paging, text=tr('Страница →'), command=lambda: turn(1)).pack(side='left')
    page_label = ttk.Label(paging, text='')
    page_label.pack(side='left')
    action_busy = [False]
    control_buttons = []

    def action(kind):
        if mode.get() == 'Опыт CP':
            return
        picked = tree.selection()
        if not picked or action_busy[0]:
            return
        row = shown[picked[0]]
        isin = row['isin']
        from cp_controls import accept, revoke, request_repair
        reviewed = [p for p in data.get('problems', []) if p['isin'] == isin]
        if kind in ('one', 'all', 'repair') and mode.get() != 'Проблемы свечей':
            stock.set(row.get('name', isin))
            page[0] = 0
            mode.set('Проблемы свечей')
            search.set('')
            refresh()
            return
        if kind in ('one', 'all'):
            selected_problems = [p for p in reviewed if p['id'] == row.get('id')] if kind == 'one' else reviewed
            description = '\n'.join((f"{p['tf']} · {p['identity'].get('source')}/{p['identity'].get('venue')} · {p['range'] or p['reasons']}" for p in selected_problems[:12]))
            if not selected_problems:
                return
            review = data.get('problem_review') or {}
            total = review.get('count', len(selected_problems)) if kind == 'all' else len(selected_problems)
            if kind == 'all' and review.get('isin') != isin:
                stock.set(row.get('name', isin))
                stock_changed()
                return
            if not messagebox.askyesno(tr('Принятие риска — только демосчёт'), tr('{p0} · {p1}\nПроблем: {p2} (первые строки ниже; весь список доступен по страницам)\n{p3}\n\nИгнорировать эти исторические блокировки для ручных демоопераций? Свечи и исходный DQ не исправляются. Принятые пропуски разрешены в отдельном расчёте CORE по реальным свечам; повреждённые данные и недостаточный прогрев остаются блокирующими. Свежая валидная цена, EUR и защита от повторного исполнения обязательны. Новые проблемы требуют нового решения. Кнопка не создаёт сделку.', p0=row.get('name', isin), p1=isin, p2=total, p3=description), parent=window):
                return
            work = lambda: accept(app.store, isin, selected_problems, kind == 'all', review.get('digest') if kind == 'all' else None)
        elif kind == 'revoke':
            work = lambda: revoke(app.store, isin)
        else:
            work = lambda: request_repair(app.store, row)
        action_busy[0] = True

        def done(result):
            action_busy[0] = False
            if kind == 'repair':
                info = next((i for i in getattr(app, 'watchlist', []) if i['isin'] == isin), None)
                if info is not None:
                    app.start_feed(info)
                app.dispatcher.prioritize_feed('feed:' + isin)
            refresh()

        def error(value):
            action_busy[0] = False
            if window.winfo_exists():
                messagebox.showerror('CandlePilot', str(value), parent=window)
        app.job(work, done, error, lane='misc', key=('cp-control', isin))
    for label, kind in [(tr('Исправить проблему'), 'repair'), (tr('Игнорировать: принять риск'), 'one'), (tr('Принять риски акции'), 'all'), (tr('Отозвать допуск'), 'revoke')]:
        button = ttk.Button(actions, text=label, command=lambda k=kind: action(k))
        button.pack(side='left', padx=3)
        control_buttons.append(button)

    def record_tv():
        from tkinter import simpledialog,filedialog
        from cp_reconciliation import observe
        from cp_controls import digest
        if mode.get()!='Сверка TradingView':
            mode.set('Сверка TradingView');page[0]=0;refresh();return
        picked=tree.selection()
        if not picked or action_busy[0]:return
        row=shown[picked[0]]
        if not row.get('input_fingerprint'):
            messagebox.showinfo('CandlePilot','Дождитесь штатной проверки этой задачи CP.',parent=window);return
        symbol=simpledialog.askstring('Сверка TradingView','Точный символ TradingView, например GETTEX:APC:',initialvalue=(row.get('observation') or {}).get('tv_symbol','GETTEX:'),parent=window)
        if symbol is None:return
        labels={'нет свечи':'ABSENT','нулевой объём':'ZERO_VOLUME','есть свеча':'PRESENT','отличается':'DIFFERENT','неясно':'UNCONFIRMED'}
        answer=simpledialog.askstring('Сверка TradingView','Результат: нет свечи / нулевой объём / есть свеча / отличается / неясно',parent=window)
        if answer is None:return
        result=labels.get(answer.strip().lower())
        if result is None:
            messagebox.showerror('CandlePilot','Выберите один из перечисленных результатов.',parent=window);return
        if not messagebox.askyesno('Подтвердить наблюдение','Проверены именно '+row['isin']+' / GETTEX / EUR / '+row['tf']+' / '+row['timezone']+'\n'+row['first']+' — '+row['end']+'?\nНаблюдение не выдаёт торговый допуск и не заменяет биржевое доказательство.',parent=window):return
        files=filedialog.askopenfilenames(title='Скриншоты сверки (необязательно)',filetypes=[('Скриншот','*.png *.jpg *.jpeg')],parent=window)
        action_busy[0]=True
        def done(value):action_busy[0]=False;refresh()
        def error(value):
            action_busy[0]=False
            if window.winfo_exists():messagebox.showerror('CandlePilot',str(value),parent=window)
        app.job(lambda:observe(app.store,row['id'],row['isin'],row['review_digest'],result,symbol.strip(),True,evidence=list(files)),done,error,lane='misc',key=('cp-tv-observation',row['id']))
    ttk.Button(actions,text='Записать сверку TV',command=record_tv).pack(side='left',padx=3)

    batch_panel = ttk.LabelFrame(window, text='Пакет CP: проверка в TradingView')
    def open_batch():
        from cp_reconciliation import review_url, observe_batch
        import copy, uuid, webbrowser
        if action_busy[0]:return
        if mode.get()!='Сверка TradingView':
            mode.set('Сверка TradingView');refresh();return
        isin=stock_names.get(stock.get())
        packet=copy.deepcopy(data.get('reconciliation',{}).get('review_batches',{}).get(isin,{}))
        if not packet.get('items'):
            messagebox.showinfo('CandlePilot','CP ещё не подготовил пакет для этой акции. Проверенные интервалы повторять не нужно.',parent=window);return
        for child in batch_panel.winfo_children():child.destroy()
        batch_panel.pack(fill='x',padx=8,pady=4,before=tree)
        ttk.Label(batch_panel,text=f"{stock.get()} · {isin} · EUR · Europe/Berlin\nСверьте биржу GETTEX, символ, TF и точные интервалы. Конец интервала не включается. Alt+G — переход к дате.\nОтсутствие свечи в TV не доказывает NO_TRADES; новые допуски не выдаются.").pack(anchor='w')
        navigation=ttk.Frame(batch_panel);navigation.pack(fill='x')
        ttk.Label(navigation,text='Символ TV:').pack(side='left')
        symbol=tk.StringVar(value=packet['items'][0].get('tv_symbol') or 'GETTEX:')
        ttk.Entry(navigation,textvariable=symbol,width=20).pack(side='left')
        def open_tv():
            try:url=review_url(symbol.get().strip())
            except ValueError:
                messagebox.showerror('CandlePilot','Нужен подтверждённый символ вида GETTEX:APC.',parent=window);return
            # Public link only; Desktop routing depends on the owner's association.
            app.job(lambda:webbrowser.open(url),lambda _:None,lambda e:messagebox.showerror('TradingView',str(e),parent=window),lane='misc',key=('cp-tv-link',isin))
        ttk.Button(navigation,text='Открыть TV',command=open_tv).pack(side='left',padx=3)
        instructions='\n'.join(f"{i['tf']} | {i['start']} — {i['end']} {i['timezone']} | UTC {i['utc_start']} — {i['utc_end']} | SM {i['identity']['source']}/{i['identity']['symbol']}" for i in packet['items'])
        def copy_periods():window.clipboard_clear();window.clipboard_append(instructions)
        ttk.Button(navigation,text='Копировать интервалы',command=copy_periods).pack(side='left')
        ttk.Label(navigation,text='Если ссылка открылась в браузере: в Desktop → Open link from clipboard.').pack(side='left',padx=4)
        labels={'Не проверено':None,'Нет свечи':'ABSENT','Нулевой объём':'ZERO_VOLUME','Есть свеча':'PRESENT','Отличается':'DIFFERENT','Неясно':'UNCONFIRMED'}
        entries=[]
        for item in packet['items']:
            line=ttk.Frame(batch_panel);line.pack(fill='x')
            ttk.Label(line,text=f"{item['tf']} · {item['start']} — {item['end']} · {item['identity']['source']}",width=75).pack(side='left')
            answer=tk.StringVar(value='Не проверено')
            ttk.Combobox(line,textvariable=answer,values=list(labels),state='readonly',width=18).pack(side='left')
            entries.append((item,answer))
        footer=ttk.Frame(batch_panel);footer.pack(fill='x')
        note=tk.StringVar();files=[];confirmed=tk.BooleanVar(value=False)
        ttk.Label(footer,text='Примечание:').pack(side='left')
        ttk.Entry(footer,textvariable=note,width=50).pack(side='left')
        attachment=ttk.Label(footer,text='Скриншотов: 0');attachment.pack(side='left',padx=4)
        def attach():
            from tkinter import filedialog
            chosen=filedialog.askopenfilenames(title='До 4 скриншотов проверки',filetypes=[('Скриншот','*.png *.jpg *.jpeg')],parent=window)
            if chosen:files[:]=list(chosen);attachment.configure(text=f'Скриншотов: {len(files)}')
        ttk.Button(footer,text='Скриншоты',command=attach).pack(side='left')
        buttons=ttk.Frame(batch_panel);buttons.pack(fill='x')
        ttk.Checkbutton(buttons,text='Выбранные интервалы и GETTEX/EUR проверены мной',variable=confirmed).pack(side='left')
        def submit():
            if action_busy[0]:return
            answers=[dict(id=item['id'],result=labels[value.get()],tv_symbol=symbol.get().strip(),note=note.get(),evidence=list(files)) for item,value in entries if labels[value.get()]]
            if not answers or not confirmed.get():
                messagebox.showinfo('CandlePilot','Отметьте только проверенные интервалы и подтвердите их проверку.',parent=window);return
            action_busy[0]=True;submission=uuid.uuid4().hex
            def done(receipt):
                action_busy[0]=False;batch_panel.pack_forget()
                info=next((i for i in app.watchlist if i['isin']==isin),None)
                if info is not None:app.start_feed(info)
                app.dispatcher.prioritize_feed('feed:'+isin)
                refresh()
            def error(value):
                action_busy[0]=False
                if window.winfo_exists():messagebox.showerror('Пакет не записан',str(value)+'\nЕсли данные изменились, откройте свежий пакет CP.',parent=window)
            app.job(lambda:observe_batch(app.store,isin,packet,answers,submission,True),done,error,lane='misc',key=('cp-tv-batch',isin))
        ttk.Button(buttons,text='Подтвердить пакет',command=submit).pack(side='left',padx=5)
        ttk.Button(buttons,text='Закрыть без записи',command=batch_panel.pack_forget).pack(side='left')
    ttk.Button(actions,text='Пакет CP → TV',command=open_batch).pack(side='left',padx=3)

    def poll():
        if window.winfo_exists():
            refresh()
            window.after(5000, poll)
    search.trace_add('write', filter_rows)
    tree.bind('<<TreeviewSelect>>', selected)
    poll()
