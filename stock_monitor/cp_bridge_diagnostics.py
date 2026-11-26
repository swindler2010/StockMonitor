"""Bounded, coherent, read-only CandlePilot sessions. No repair or trading calls."""
import hashlib,json,sqlite3,time,threading,uuid
from pathlib import Path
from datetime import datetime,timezone
from collections import Counter,OrderedDict
from contextlib import contextmanager

TFS=('5m','10m','15m','30m','1H','2H','3H','4H','1D','1W','1M')
FIELDS=('source','symbol','venue','currency','timezone')
# One continuation for each independent TF proof and the three final phases
# (exact approval reconciliation, physical capture, comparison). Cold proofs
# can consume a whole request budget. Five total requests could exhaust before
# a later phase got any time, despite retained progress. No request timeout is
# increased and a non-progressing instrument still has a finite ceiling.
MAX_READ_ATTEMPTS=len(TFS)+3

def digest(v):return hashlib.sha256(json.dumps(v,sort_keys=True,ensure_ascii=False,default=str).encode()).hexdigest()
def read(db,key,default=None):
 row=db.execute('SELECT value FROM settings WHERE key=?',(key,)).fetchone()
 return json.loads(row[0]) if row else default

@contextmanager
def budget(db,seconds):
 if not hasattr(db,'deadlines'):db.deadlines=[]
 db.deadlines.append(time.monotonic()+seconds)
 db.set_progress_handler(lambda:int(time.monotonic()>=min(db.deadlines)),500)
 try:yield
 finally:
  db.deadlines.pop()
  db.set_progress_handler((lambda:int(time.monotonic()>=min(db.deadlines))) if db.deadlines else None,500)

class ReadConnection(sqlite3.Connection):
 pass

class Session:
 def __init__(self,path,instance,previous=None,checkpoint=None):
  self.db=None;self.checkpoint=checkpoint;self.manifest_integrities={};self.reconciliation_memos={};self.readiness_memos={};self.readiness_values={};self.base_values=OrderedDict()
  try:self._initialize(path,instance,previous)
  except Exception:
   if self.db is not None:self.db.close()
   raise
 def _initialize(self,path,instance,previous=None):
  self.db=sqlite3.connect(Path(path).resolve().as_uri()+'?mode=ro',uri=True,timeout=.2,check_same_thread=False,factory=ReadConnection)
  self.closed=False
  self.db.execute('PRAGMA query_only=ON');self.db.execute('BEGIN')
  self.db.execute('SELECT count(*) FROM input_revisions').fetchone()
  self.generated_at=datetime.now(timezone.utc).isoformat();self.started=self.touched=time.monotonic()
  self.instance=instance;self.previous=previous;self.rows={};self.attempts={};self.lock=threading.Lock();self.complete_value=None;self.unverified=set();self.evidence_complete=True
  with budget(self.db,5):
   self.infos=[]
   for i,v in self.db.execute('SELECT isin,payload FROM instruments ORDER BY isin'):
    try:info=dict(json.loads(v),isin=i)
    except (ValueError,TypeError):info=dict(isin=i,name=i,catalog_error=True)
    self.infos.append(info)
   token=list(self.db.execute('SELECT scope,revision FROM input_revisions ORDER BY scope'))
   self.tokens=dict(token);self.input_revision=digest(token)
   from cp_bridge_comparison import revision_coverage
   self.revision_coverage=revision_coverage(self.db)
   self.tables={r[0] for r in self.db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
   self.journal_head=self.db.execute('SELECT coalesce(max(id),0) FROM candle_revision_history').fetchone()[0] if 'candle_revision_history' in self.tables else None
   self.journal_anchor=self.journal(self.journal_head) if self.journal_head else None
  self.revision=digest([instance,self.generated_at,self.input_revision,uuid.uuid4().hex])
  # Only this pinned diagnostic view may share final approval validations
  # across TF and reconciliation. Neither writer nor execution paths opt in.
  self.db._cp_approval_scope=(self.revision,self.generated_at)
  self.db._cp_approval_results={}
  def immutable(action,a,b,*unused):
   if action in (sqlite3.SQLITE_TRANSACTION,sqlite3.SQLITE_SAVEPOINT,sqlite3.SQLITE_ATTACH,sqlite3.SQLITE_DETACH):return sqlite3.SQLITE_DENY
   if action==sqlite3.SQLITE_PRAGMA and a.lower()=='query_only' and b is not None and b.upper() not in ('ON','1'):return sqlite3.SQLITE_DENY
   return sqlite3.SQLITE_OK
  self.db.set_authorizer(immutable)
 def journal(self,n):
  row=self.db.execute('SELECT * FROM candle_revision_history WHERE id=?',(n,)).fetchone()
  return digest(row) if row else None
 def close(self):
  with self.lock:
   self.closed=True;self.reconciliation_memos.clear();self.readiness_memos.clear();self.readiness_values.clear();self.base_values.clear()
   self.db._cp_approval_scope=None
   if isinstance(self.db._cp_approval_results,dict):self.db._cp_approval_results.clear()
   self.db.set_authorizer(None);self.db.close()
 @staticmethod
 def retryable(row):
  if row.get('data_status')=='INSTRUMENT_DIAGNOSTIC_UNAVAILABLE':return row.get('reason')=='INSTRUMENT_READ_BUDGET'
  return (row.get('ohlcv_integrity',{}).get('reason')=='MANIFEST_READ_BUDGET' or row.get('reconciliation',{}).get('evidence_status')=='NOT_REVALIDATED_BUDGET' or row.get('ohlcv_comparison',{}).get('reason') in ('COMPARISON_READ_BUDGET','MANIFEST_READ_BUDGET') or any(v.get('validity')=='NOT_REVALIDATED_BUDGET' or v.get('chart_validity')=='NOT_REVALIDATED_BUDGET' or v.get('trade_reason')=='NOT_REVALIDATED_BUDGET' for v in row.get('readiness',{}).values()))
 def row(self,n):
  if not 0<=n<len(self.infos):raise ValueError('PATH_NOT_FOUND')
  prior=self.rows.get(n)
  if prior is not None and (not self.retryable(prior) or self.attempts.get(n,0)>=MAX_READ_ATTEMPTS):return prior
  # One bounded attempt per request. A cold/read-budget refusal is not a
  # permanent observation; retry the same pinned SQL view, never a fresh DB.
  self.attempts[n]=self.attempts.get(n,0)+1
  self.complete_value=None
  self.rows.pop(n,None)
  try:
   with budget(self.db,4):
    # All phases observe this same immutable SQL transaction. If only the
    # physical manifest/comparison ran out of budget, resume that phase instead
    # of spending the retry on the already completed readiness/approval work.
    base_complete=(prior is not None and prior.get('data_status')=='AVAILABLE'
     and prior.get('reconciliation',{}).get('evidence_status')=='EXACT_SCOPES_RECONCILED'
     and all(v.get('validity')!='NOT_REVALIDATED_BUDGET' and v.get('chart_validity')!='NOT_REVALIDATED_BUDGET' and v.get('trade_reason')!='NOT_REVALIDATED_BUDGET' for v in prior.get('readiness',{}).values()))
    result=self._physical(dict(prior)) if base_complete else self._row(n)
  except (sqlite3.OperationalError,ValueError,TypeError,KeyError,AttributeError,IndexError) as e:
   if not self.db.in_transaction:raise RuntimeError('DIAGNOSTIC_VIEW_LOST')
   info=self.infos[n];self.evidence_complete=False
   result=dict(isin=info['isin'],name=info.get('name'),kind=info.get('kind','stock'),generated_at=self.generated_at,
    data_status='INSTRUMENT_DIAGNOSTIC_UNAVAILABLE',reason='INSTRUMENT_READ_BUDGET' if str(e)=='interrupted' else type(e).__name__,
    dq=[],raw_dq_states={},stored_series=[],readiness={tf:dict(validity='NOT_REVALIDATED_BUDGET',chart_ready=None,analysis_ready=None,signal_ready=None,trade_ready=None,execution_permission=False) for tf in TFS},
    reconciliation=dict(evidence_status='NOT_REVALIDATED',unresolved_errors=None),execution_permission=False)
  result.update(read_attempts=self.attempts[n],retryable=self.retryable(result) and self.attempts[n]<MAX_READ_ATTEMPTS,
   retry_remaining=MAX_READ_ATTEMPTS-self.attempts[n] if self.retryable(result) else 0)
  if not self.retryable(result) or self.attempts[n]>=MAX_READ_ATTEMPTS:
   self.reconciliation_memos.pop(self.infos[n]['isin'],None);self.readiness_memos.pop(self.infos[n]['isin'],None);self.readiness_values.pop(self.infos[n]['isin'],None)
   self.base_values.pop(n,None)
  self.rows[n]=result
  return result
 def _base(self,n):
  """Resume decoded metadata/counts in the same view; bound partial retention."""
  info=self.infos[n];isin=info['isin']
  base=self.base_values.setdefault(n,{});self.base_values.move_to_end(n)
  while len(self.base_values)>4:self.base_values.popitem(last=False)
  if 'reports' not in base:
   prefix='data_quality:'+isin+':'
   base['reports']={k:json.loads(v) for k,v in self.db.execute('SELECT key,value FROM settings WHERE key>=? AND key<?',(prefix,prefix+'\uffff'))}
  if 'saved' not in base:base['saved']=read(self.db,'cp_eligibility:'+isin,{})
  if 'archive' not in base:base['archive']=read(self.db,'history_archive_progress:'+isin,{})
  if 'dq' not in base:
   dq=[]
   for key,q in base['reports'].items():dq.append(dict(key=key,identity=q.get('identity',{}),tf=q.get('base_tf',key.rsplit(':',1)[-1]),state=q.get('state','UNKNOWN'),checked_at=q.get('checked_at'),issues=q.get('issues',[]),ranges_count=len(q.get('ranges',[])),origin='saved_dq'))
   for key,unit in base['archive'].get('series',{}).items():
    source,venue,currency,symbol,tf=json.loads(key);q=unit.get('quality',{})
    dq.append(dict(key='archive:'+key,identity=dict(source=source,venue=venue,currency=currency,symbol=symbol),tf=tf,state=q.get('state','UNKNOWN'),checked_at=q.get('checked_at'),issues=q.get('issues',[]),ranges_count=len(q.get('ranges',[])),origin='archive_quality'))
   base['dq']=dq
  for table in ('series','candles'):
   if table in self.tables and table not in base:
    series=[]
    if table=='series':
     sql='SELECT source,venue,currency,symbol,base_tf,count(*),min(ts),max(ts) FROM series WHERE isin=? GROUP BY source,venue,currency,symbol,base_tf'
     for s,v,c,y,tf,count,first,last in self.db.execute(sql,(isin,)):series.append(dict(storage_table=table,source=s,venue=v,currency=c,symbol=y,timeframe=tf,count=count,first=first,last=last))
    else:
     for s,tf,count,first,last in self.db.execute('SELECT source,base_tf,count(*),min(ts),max(ts) FROM candles WHERE isin=? GROUP BY source,base_tf',(isin,)):
      series.append(dict(storage_table=table,source=s,venue='GETTEX' if s=='gettex' else None,currency='EUR' if s=='gettex' else None,symbol=isin if s=='gettex' else None,timeframe=tf,count=count,first=first,last=last))
    base[table]=series
  return base
 def _row(self,n):
  info=self.infos[n];isin=info['isin'];stamp=time.monotonic()
  if info.get('catalog_error'):raise ValueError('MALFORMED_INSTRUMENT_METADATA')
  with budget(self.db,4):base=self._base(n)
  reports=base['reports'];saved=base['saved'];dq=base['dq'];series=base.get('series',[])+base.get('candles',[])
  from calculation_window import eligibility_state,candidate_state,execution_eligibility,chart_state
  import pandas as pd
  now=pd.Timestamp(self.generated_at);memo=self.readiness_memos.setdefault(isin,{});readiness={};errors=[]
  try:
   with budget(self.db,1):record_state=eligibility_state(self.db,saved,now)
  except sqlite3.OperationalError as e:
   if str(e)!='interrupted':raise
   record_state='NOT_REVALIDATED_BUDGET'
  deadline=min(self.db.deadlines)
  completed_tf=self.readiness_values.setdefault(isin,{})
  for tf in TFS:
   if tf in completed_tf:
    readiness[tf]=completed_tf[tf];continue
   unit=saved.get('timeframes',{}).get(tf,{})
   candidates=[c for c in saved.get('candidates',[]) if c.get('timeframe')==tf]
   current=[];states=[]
   for c in candidates:
    state='NOT_REVALIDATED_BUDGET'
    for attempt in (0.6,1.2,max(0.0,deadline-time.monotonic()-0.1)):
     if time.monotonic()>=deadline:break
     try:
      with budget(self.db,attempt):
       # An expired result cannot be admitted; do not hash its old input archive.
       try:expired=now>=pd.Timestamp(c.get('expires_at',saved.get('expires_at')))
       except (TypeError,ValueError):expired=False
       state='EXPIRED' if expired else candidate_state(self.db,saved,c,now,memo)
      break
     except sqlite3.OperationalError as e:
      if str(e)!='interrupted':raise
    states.append(state)
    if state=='CURRENT':current.append(c)
   known=record_state=='CURRENT' or bool(current)
   chart_token=unit.get('_input_revisions',{})
   chart_known=bool(chart_token) and 'instrument:'+isin in chart_token and all(self.tokens.get(k,0)==v for k,v in chart_token.items())
   chart_validity='CURRENT_INPUTS' if chart_known else record_state if record_state=='CURRENT' else 'NO_CURRENT_CHART_PROOF'
   if unit.get('chart_closed_inputs'):
    try:
     # An expired proof cannot grant chart readiness. Like expired candidates,
     # it needs no source hashing. A current proof may use the remainder of
     # this request's unchanged four-second budget; completed TFs survive the
     # next bounded continuation. Repeating a 0.6s sub-budget made a valid
     # slower proof restart forever without making progress.
     try:chart_expired=now>=pd.Timestamp(unit.get('valid_until'))
     except (TypeError,ValueError):chart_expired=False
     if chart_expired:chart_validity='EXPIRED'
     else:
      with budget(self.db,max(0.,deadline-time.monotonic()-0.05)):
       chart_validity=chart_state(self.db,saved,tf,now,memo)
     chart_known=chart_validity=='CURRENT'
    except sqlite3.OperationalError as e:
     if str(e)!='interrupted':raise
     chart_known=False;chart_validity='NOT_REVALIDATED_BUDGET'
   validity='CURRENT' if current else states[0] if states else record_state
   trade=False if candidates else None;trade_reason=None
   if any(c.get('trade_ready') for c in current):
    try:
     with budget(self.db,1):execution_eligibility(self.db,info,tf,next(c for c in current if c.get('trade_ready')).get('execution_identity'),now)
     trade=True
    except (ValueError,KeyError,TypeError) as e:trade=False;trade_reason=str(e)[:160]
    except sqlite3.OperationalError as e:
     if str(e)!='interrupted':raise
     trade=None;trade_reason='NOT_REVALIDATED_BUDGET'
   uncertain=validity in ('UNKNOWN','NOT_REVALIDATED_BUDGET')
   readiness[tf]=dict(validity=validity,checked_at=saved.get('checked_at'),expires_at=min((c.get('expires_at',saved.get('expires_at','')) for c in current),default=saved.get('expires_at')),
    chart_ready=bool(unit.get('chart_ready')) if chart_known or record_state=='CURRENT' and not unit.get('chart_closed_inputs') else None,
    chart_validity=chart_validity,
    analysis_ready=any(c.get('analysis_ready') for c in current) if not uncertain else None,
    signal_ready=any(c.get('signal_ready') for c in current) if not uncertain else None,
    trade_ready=trade if not uncertain else None,trade_reason=trade_reason,
    saved_flags={k:unit.get(k) for k in ('chart_ready','analysis_ready')},
    blockers=list(dict.fromkeys(b for c in candidates for b in c.get('blockers',[])))[:16],
    trade_blockers=list(dict.fromkeys(b for c in candidates for b in c.get('trade_blockers',[])))[:16],
    identity=unit.get('identity'),saved_dq_state=unit.get('archive_state'),
    reason='NO_SAVED_ASSESSMENT' if not saved else 'Observed saved proof revalidated in this SQL view; not a new calculation',execution_permission=False)
   value=readiness[tf]
   if value['validity']!='NOT_REVALIDATED_BUDGET' and value['chart_validity']!='NOT_REVALIDATED_BUDGET' and value.get('trade_reason')!='NOT_REVALIDATED_BUDGET':completed_tf[tf]=value
  from mcp_observability import exact_reconciliation
  reconciliation=None
  scopes={}
  for seconds in (3.,):
   if time.monotonic()>=deadline:break
   scopes={}
   try:
    with budget(self.db,seconds):reconciliation=exact_reconciliation(self.db,info,reports,now,min(deadline,time.monotonic()+seconds),observer=lambda key,status:scopes.__setitem__(bytes.fromhex(digest(key)),status),_read_memo=self.reconciliation_memos.setdefault(isin,{}))
    break
   except sqlite3.OperationalError as e:
    if str(e)!='interrupted':raise
  if reconciliation is None or reconciliation.get('evidence_status')!='EXACT_SCOPES_RECONCILED':
   reconciliation=dict(evidence_status='NOT_REVALIDATED_BUDGET',unresolved_errors=None,owner_accepted_risk=None,unverified_saved_scopes=None);self.evidence_complete=False
  else:
   self.unverified.update(k for k,v in scopes.items() if v=='unverified')
  if not self.db.in_transaction:raise RuntimeError('DIAGNOSTIC_VIEW_LOST')
  result=dict(isin=isin,name=info.get('name',isin),kind=info.get('kind','stock'),generated_at=self.generated_at,input_revision=self.input_revision,
   dq=dq,raw_dq_states=dict(Counter(x['state'] for x in dq)),readiness=readiness,stored_series=series,
   reconciliation=reconciliation,data_status='AVAILABLE',read_seconds=round(time.monotonic()-stamp,4),execution_permission=False)
  self._physical(result)
  result['read_seconds']=round(time.monotonic()-stamp,4)
  self.rows[n]=result;return result
 def _physical(self,result):
  """Resume bounded checkpoint work without restarting completed proof checks."""
  stamp=time.monotonic();isin=result['isin']
  if self.manifest_integrities.get(isin,{}).get('status')=='EXACT':result['ohlcv_integrity']=self.manifest_integrities[isin]
  if self.checkpoint is not None and result.get('ohlcv_integrity',{}).get('status')!='EXACT':
   from cp_diagnostic_baseline import capture
   try:
    blob,integrity=capture(self,isin)
    self.checkpoint.put(self.revision,isin,blob,integrity)
    self.manifest_integrities[isin]=integrity
    result['ohlcv_integrity']=integrity
   except (TimeoutError,ValueError,sqlite3.Error) as error:
    result['ohlcv_integrity']=dict(status='UNKNOWN',confirmed_structural_errors=None,reason='MANIFEST_READ_BUDGET' if str(error)=='interrupted' else str(error) if str(error) in ('MANIFEST_READ_BUDGET','MANIFEST_SIZE_LIMIT','CHECKPOINT_STORAGE_LIMIT') else 'DIAGNOSTIC_CHECKPOINT_UNAVAILABLE')
   result['diagnostic_checkpoint_required']=True
  from cp_bridge_comparison import compare_instrument
  result['ohlcv_comparison']=compare_instrument(self,self.previous,isin)
  result['read_seconds']=round(time.monotonic()-stamp,4)
  return result
 def detail(self,n,kind):
  info=self.infos[n];isin=info['isin']
  with budget(self.db,4):
   if kind=='assessment':return read(self.db,'cp_eligibility:'+isin,{})
   prefix='data_quality:'+isin+':'
   reports={k:json.loads(v) for k,v in self.db.execute('SELECT key,value FROM settings WHERE key>=? AND key<?',(prefix,prefix+'\uffff'))}
   if kind=='saved_dq':return reports
   if kind=='archive_quality':return read(self.db,'history_archive_progress:'+isin,{})
   if kind=='problems':
    # The row already reconciled every exact scope in this pinned SQL view.
    # Expand only its proven classification index; do not repeat thousands of
    # source/approval checks merely to page the same unresolved suspicions.
    if self.rows.get(n,{}).get('reconciliation',{}).get('evidence_status')!='EXACT_SCOPES_RECONCILED':raise ValueError('DIAGNOSTIC_DETAIL_BUDGET')
    import data_quality as dq
    values={}
    for report_key,q in reports.items():
     if q.get('state')=='READY':continue
     meta={k:q.get('identity',{}).get(k) for k in FIELDS}
     if not meta['timezone']:meta['timezone']=dq.TIMEZONES.get(dq.CALENDARS.get(meta['venue']))
     tf=q.get('base_tf',report_key.rsplit(':',1)[-1]);meta_key=json.dumps(meta,sort_keys=True)
     for span in q.get('ranges') or [None]:
      key=(isin,meta_key,tf,json.dumps(span or q.get('issues',[]),sort_keys=True))
      if not all(meta.values()):key=(*key,report_key)
      token=digest(key)
      if bytes.fromhex(token) in self.unverified:values[token]=dict(isin=isin,identity=meta,tf=tf,scope=json.loads(key[3]),report_key=key[4] if len(key)>4 else None,classification='UNVERIFIED_SAVED_SCOPE_NOT_CONFIRMED_ERROR')
    return list(values.values())
  raise ValueError('PATH_NOT_FOUND')
 def completion(self):
  if len(self.rows)!=len(self.infos):return dict(status='COLLECTING',completed=len(self.rows),total=len(self.infos))
  if self.complete_value:return self.complete_value
  rows=[self.rows[n] for n in range(len(self.infos))]
  self.evidence_complete=all(r.get('data_status')=='AVAILABLE' and r['reconciliation'].get('evidence_status')=='EXACT_SCOPES_RECONCILED' for r in rows)
  result=dict(status='COMPLETE',completed=len(rows),total=len(rows),generated_at=self.generated_at,revision=self.revision,
   stocks=sum(r['kind']!='benchmark' for r in rows),benchmarks=sum(r['kind']=='benchmark' for r in rows),
   raw_dq_states=dict(sum((Counter(r['raw_dq_states']) for r in rows),Counter())),
   validation_states=dict(Counter(v['validity'] for r in rows for v in r['readiness'].values())),
   unresolved_errors=None,readiness_totals={k:dict(true=sum(v[k] is True for r in rows for v in r['readiness'].values()),false=sum(v[k] is False for r in rows for v in r['readiness'].values()),unknown=sum(v[k] is None for r in rows for v in r['readiness'].values())) for k in ('chart_ready','analysis_ready','signal_ready','trade_ready')},execution_permission=False)
  result['input_revision']=self.input_revision
  result['budget_limited_instruments']=sum(r.get('data_status')=='INSTRUMENT_DIAGNOSTIC_UNAVAILABLE' or r['reconciliation'].get('evidence_status')!='EXACT_SCOPES_RECONCILED' or self.retryable(r) for r in rows)
  if self.checkpoint is not None:
   exact=all(r.get('ohlcv_integrity',{}).get('status')=='EXACT' for r in rows)
   result['confirmed_ohlcv_errors']=dict(status='EXACT' if exact else 'UNKNOWN',
    count=sum(r['ohlcv_integrity']['confirmed_structural_errors'] for r in rows) if exact else None,
    unit='Unique stored physical keys with mathematically impossible OHLCV; not all possible source errors.',
    source_accuracy='NOT_CERTIFIED',unverified_saved_scopes=sum(r['reconciliation']['unverified_saved_scopes'] for r in rows) if self.evidence_complete else None)
   result['budget_limited_instruments']=sum(r.get('data_status')=='INSTRUMENT_DIAGNOSTIC_UNAVAILABLE' or r['reconciliation'].get('evidence_status')!='EXACT_SCOPES_RECONCILED' or self.retryable(r) or r.get('ohlcv_integrity',{}).get('status')!='EXACT' for r in rows)
  if result['budget_limited_instruments']:result['status']='COMPLETE_WITH_LIMITS'
  result['retryable_instruments']=[dict(isin=r['isin'],path='/by_stock/'+str(n),retry_remaining=r.get('retry_remaining',0)) for n,r in enumerate(rows) if r.get('retryable')]
  result['retry_policy']=f'At most {MAX_READ_ATTEMPTS} bounded reads per instrument (one per supported TF plus three final phases) in this exact SQL view. Completed TF/proof/manifest phases are retained; each request keeps its original four-second SQL budget. No market load or revision substitution. Retry returned paths, then read /completion again.'
  try:result['comparison']=compare(self,self.previous)
  except sqlite3.OperationalError:result['comparison']=dict(comparable=False,reason='COMPARISON_BUDGET',new_unique_ohlcv=None,corrected_unique_ohlcv=None)
  from cp_bridge_comparison import totals
  result['comparison']['physical_ohlcv']=totals(rows,self.previous)
  if self.checkpoint is not None:
   physical=result['comparison']['physical_ohlcv']
   result['comparison'].update(new_unique_ohlcv=physical['new_unique_physical_ohlcv'],corrected_unique_ohlcv=physical['corrected_unique_physical_ohlcv'],
    ohlcv_count_unit='Physical ISIN/source/venue/currency/symbol/TF/timestamp keys; not economic deduplication across feeds/TF.')
  if result['status']=='COMPLETE':self.previous=None
  self.complete_value=result;return result
 def header(self):
  import mcp_observability,cp_controls
  runtime=dict(reader='cp-pages-20261125-r13',reconciliation=getattr(mcp_observability,'READ_DIAGNOSTIC_REVISION',None),validator_memo_supported='_read_memo' in (getattr(cp_controls.effective_accepted,'__kwdefaults__',None) or {}))
  return dict(schema_version=3,diagnostic_runtime=runtime,generated_at=self.generated_at,revision=self.revision,input_revision=self.input_revision,
   instruments=len(self.infos),stocks=sum(i.get('kind')!='benchmark' for i in self.infos),benchmarks=sum(i.get('kind')=='benchmark' for i in self.infos),
   by_stock=[dict(isin=i['isin'],path='/by_stock/'+str(n),data_status='EXPAND_PATH') for n,i in enumerate(self.infos)],
   completion=self.completion(),execution_permission=False,
   semantics='Fresh coherent SQL observation of saved diagnostics; checked_at/expiry preserved. UNKNOWN is not zero/false. DQ reports are not candle errors. Readiness is as-of generated_at, never execution permission.',
   details='Append /saved_dq, /archive_quality, /assessment or /problems to a /by_stock/{n} path. Problems excludes exact active approvals; saved_dq retains original evidence and is not a list of errors.',
   paging='Read /by_stock offset/limit (recommended limit=2), then /completion. Preserve expected_revision. Each instrument is isolated; no global analytical-snapshot validation.')
 def baseline(self):
  return dict(view=self,revision=self.revision,generated_at=self.generated_at,scope=[(r['isin'],r['kind']) for r in self.rows.values()],
   checkpoint=self.checkpoint,evidence_complete=self.evidence_complete,unverified=self.unverified,entities={digest([r['isin'],q['identity'],q['tf'],q['origin'],q['key']]):q['state'] for r in self.rows.values() for q in r['dq']},journal_head=self.journal_head,journal_anchor=self.journal_anchor,
   observations=dict(status='ARCHIVED_CHECKPOINT',generated_at=self.generated_at,revision=self.revision,input_revision=self.input_revision,execution_permission=False,
    semantics='Saved observations at the named revision, never current readiness or an execution grant. Compare exact listing/TF contexts before interpreting changes.',
    by_stock={r['isin']:dict(name=r['name'],kind=r['kind'],raw_dq_states=r['raw_dq_states'],readiness=r['readiness'],reconciliation=r['reconciliation']) for r in self.rows.values()}))

def compare(session,previous):
 if not previous:return dict(comparable=False,reason='NO_PREVIOUS_COMPLETE_SNAPSHOT',new_unique_ohlcv=None,corrected_unique_ohlcv=None)
 scope=sorted((r['isin'],r['kind']) for r in session.rows.values())
 if scope!=sorted(map(tuple,previous['scope'])):return dict(comparable=False,reason='CATALOG_SCOPE_CHANGED',new_unique_ohlcv=None,corrected_unique_ohlcv=None)
 old=previous['entities'];new=session.baseline()['entities'];shared=old.keys()&new.keys()
 result=dict(comparable=True,from_revision=previous['revision'],to_revision=session.revision,
  dq_transitions=dict(shared=len(shared),added=len(new.keys()-old.keys()),removed=len(old.keys()-new.keys()),changed=sum(old[k]!=new[k] for k in shared)),
  semantics='DQ transitions are saved opinions, not OHLCV repairs; identities/TF/origins matched exactly.',new_unique_ohlcv=None,corrected_unique_ohlcv=None)
 result['readiness_baseline']=previous.get('observations') or dict(status='PREVIOUS_OBSERVATIONS_NOT_RETAINED',revision=previous['revision'],generated_at=previous['generated_at'],execution_permission=False)
 if session.evidence_complete and previous.get('evidence_complete'):
  result['unverified_scope_changes']=dict(added=len(session.unverified-previous['unverified']),removed=len(previous['unverified']-session.unverified),unit='exact unverified scopes after active approvals, NOT proven errors')
 else:result['unverified_scope_changes']=None
 head=previous['journal_head']
 if head is None or session.journal_head is None:result['journal_reason']='NO_JOURNAL';return result
 if session.journal_head<head or (head and session.journal(head)!=previous['journal_anchor']):result.update(comparable=False,journal_reason='JOURNAL_RESET');return result
 with budget(session.db,3):
  events=session.db.execute('SELECT isin,identity,base_tf,ts,old_ohlcv,new_ohlcv FROM candle_revision_history WHERE id>? AND id<=? ORDER BY id',(head,session.journal_head)).fetchall()
 changes={};unknown=0
 for isin,identity,tf,ts,before,after in events:
  try:
   meta=json.loads(identity);a=json.loads(before);b=json.loads(after)
   if not isinstance(a,dict) or not isinstance(b,dict):raise ValueError()
   key=digest([isin,{k:meta.get(k) for k in FIELDS},tf,ts]);changes.setdefault(key,[a,b])[1]=b
  except (TypeError,ValueError):unknown+=1
 keys=('open','high','low','close','volume');added=corrected=0
 for a,b in changes.values():
  if not all(k in b for k in keys):unknown+=1;continue
  if not a:added+=1
  elif all(k in a for k in keys):corrected+=any(a[k]!=b[k] for k in keys)
  else:unknown+=1
 result.update(journal_events=len(events),journal_new_unique_physical_ohlcv=added,journal_corrected_unique_physical_ohlcv=corrected,journal_unclassified=unknown,
  journal_reason='Journal subset only; ordinary insert coverage not proven, total new/corrected bars remain unknown. Never sum across feeds/TF as economic unique candles.')
 return result

class Diagnostics:
 """At most two pinned views; delivery uses the existing request thread."""
 def __init__(self,cache_path=None):
  self.sessions=OrderedDict();self.previous={};self.lock=threading.Lock();self.checkpoint=None;self.checkpoint_status='DISABLED';self.loaded_for=None
  if cache_path is not None:
   from cp_diagnostic_baseline import Checkpoint
   try:self.checkpoint=Checkpoint(cache_path);self.checkpoint_status='AVAILABLE'
   except (sqlite3.Error,OSError,ValueError):self.checkpoint_status='UNAVAILABLE'

 @staticmethod
 def compact(row,n):
  """Inline all four TF flags; bulky original evidence remains expandable."""
  pointer='/by_stock/'+str(n)
  flags=('validity','chart_validity','checked_at','expires_at','chart_ready','analysis_ready','signal_ready','trade_ready','saved_flags')
  return {**{k:v for k,v in row.items() if k not in ('dq','stored_series','readiness')},
   'readiness':{tf:{k:v for k,v in value.items() if k in flags} for tf,value in row['readiness'].items()},
   'dq':dict(path=pointer+'/dq',reports=len(row['dq']),semantics='Original saved reports, not independent error counts'),
   'stored_series':dict(path=pointer+'/stored_series',series=len(row['stored_series'])),
   'details_path':pointer,'problems_path':pointer+'/problems','read_only':True,'execution_permission':False}
 def close(self):
  with self.lock:
   for s in self.sessions.values():s.close()
   self.sessions.clear();self.previous.clear()
   if self.checkpoint is not None:self.checkpoint.close()
 def read(self,path,instance,args):
  # A concurrent client gets a bounded, explicit retry, never a 15s lock wait.
  if not self.lock.acquire(timeout=.05):
   return dict(status='BUSY',reason_code='DIAGNOSTIC_READER_BUSY',retry_after_seconds=1,read_only=True,execution_permission=False)
  try:return self._read(path,instance,args)
  finally:self.lock.release()
 def _read(self,path,instance,args):
  from mcp_projection import page
  if self.checkpoint is not None and self.loaded_for!=(str(path),instance):
   self.loaded_for=(str(path),instance)
   try:
    previous=self.checkpoint.load(path)
    if previous:self.previous={instance:previous};self.checkpoint_status='RESTORED'
   except (sqlite3.Error,ValueError,KeyError,TypeError):self.checkpoint_status='INVALID_SAVED_CHECKPOINT'
  now=time.monotonic();expected=args.get('expected_revision')
  target=args.get('path','');offset=args.get('offset',0);limit=args.get('limit',2)
  if not isinstance(target,str) or (target and not target.startswith('/')):raise ValueError('INVALID_JSON_POINTER')
  if type(offset)!=int or offset<0 or type(limit)!=int or not 1<=limit<=50:raise ValueError('INVALID_PAGE')
  if not expected and (offset or target):raise ValueError('REVISION_REQUIRED')
  active=self.sessions.get(expected)
  if active and active.previous and active.previous.get('view'):
   active.previous['view'].touched=now
  for key,s in list(self.sessions.items()):
   if now-s.started>=900 or now-s.touched>=120 or s.instance!=instance:
    s.close();del self.sessions[key]
  if expected:
   s=self.sessions.get(expected)
   if s is None:raise ValueError('PAGE_SESSION_EXPIRED')
  else:
   while len(self.sessions)>=2:self.sessions.popitem(last=False)[1].close()
   s=Session(path,instance,self.previous.get(instance),checkpoint=self.checkpoint);self.sessions[s.revision]=s
   if self.checkpoint is not None:
    keep=set(self.sessions)|{p['revision'] for p in self.previous.values()}
    keep.update(v.previous['revision'] for v in self.sessions.values() if v.previous)
    try:self.checkpoint.prune(keep)
    except sqlite3.Error:self.checkpoint_status='CLEANUP_DEFERRED'
  s.touched=now
  with s.lock:
   data=s.header();scoped=dict(args,expected_revision=s.revision)
   if target=='/by_stock':
    limit=min(limit,2);selected=[s.row(n) for n in range(offset,min(len(s.infos),offset+limit))]
    data['by_stock']=[self.compact(row,offset+j) for j,row in enumerate(selected)];scoped.update(offset=0,limit=limit)
    result=page(data,scoped,s.revision)
    for item in result['items']:item['key']+=offset;item['path']='/by_stock/'+str(item['key'])
    result.update(offset=offset,total_count=len(s.infos),next_offset=offset+len(selected) if offset+len(selected)<len(s.infos) else None,complete=offset+len(selected)>=len(s.infos))
   elif target.startswith('/by_stock/'):
    try:n=int(target.split('/')[2]);row=s.row(n)
    except (ValueError,IndexError):raise ValueError('PATH_NOT_FOUND')
    parts=target.split('/')[3:];prefix='/by_stock/'+str(n)
    if parts and parts[0] in ('assessment','saved_dq','archive_quality','problems'):
     row=s.detail(n,parts[0]);prefix+='/'+parts.pop(0)
    result=page(row,dict(scoped,path='/'+'/'.join(parts) if parts else ''),s.revision)
    result['path']=prefix+result['path']
    for item in result.get('items',[]):item['path']=prefix+item['path']
   else:result=page(data,scoped,s.revision)
   complete=s.completion()
   if complete['status']=='COMPLETE' and (instance not in self.previous or self.previous[instance]['revision']!=s.revision and self.previous[instance]['generated_at']<=s.generated_at):
    baseline=s.baseline();self.previous[instance]=baseline
    if self.checkpoint is not None:
     try:self.checkpoint.publish(path,baseline);self.checkpoint_status='SAVED'
     except (sqlite3.Error,ValueError,TypeError):self.checkpoint_status='SAVE_FAILED'
   result['checkpoint_status']=self.checkpoint_status
   result.update(instance_id=instance,generated_at=s.generated_at,revision=s.revision,read_only=True,execution_permission=False,
    diagnostic_status=complete['status'],completed_instruments=len(s.rows),instrument_total=len(s.infos),
    page_lease_remaining_seconds=min(120,900-(time.monotonic()-s.started)),lease_semantics='Idle120s, absolute900s for diagnostic delivery only; no readiness extension')
   return result
