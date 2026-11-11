"""Session-aware eligibility on the existing feed lane. No OHLCV/DQ writes."""
import hashlib,json
import pandas as pd
from timeframes import schedule,MINUTES

FIELDS=('source','venue','currency','symbol','timezone')

def clock(now=None):return pd.Timestamp(now if now is not None else pd.Timestamp.now(tz='UTC'))
def unit_key(isin,meta,base):
 payload=[isin,base]+[meta.get(k) for k in FIELDS]
 return 'cp_schedule:'+hashlib.sha256(json.dumps(payload).encode()).hexdigest()

def boundary(meta,base,now=None,delay_seconds=None):
 return _boundary(tuple((k,meta.get(k)) for k in ('source','venue','timezone')),base,clock(now).isoformat(),delay_seconds)

from functools import lru_cache
@lru_cache(maxsize=256)
def _boundary(items,base,now,delay_seconds):
 meta=dict(items)
 now=clock(now);zone=meta['timezone'];day=now.tz_convert(zone).tz_localize(None).normalize()
 # Safety buffers are policy, NOT measured publication latency.
 delay=delay_seconds if delay_seconds is not None else (900 if meta['source']=='gettex' else 60)
 first=day-pd.Timedelta(days=14)
 if meta.get('venue')=='GETTEX':first=max(first,pd.Timestamp('2026-01-01'))
 rows=schedule(meta,first,day)
 ends=[];active=False
 for _,r in rows.iterrows():
  if r.open<=now<r.close and not(pd.notna(r.break_start) and r.break_start<=now<r.break_end):active=True
  if base=='1D':ends.append(r.close);continue
  for a,b in ([(r.open,r.close)] if pd.isna(r.break_start) else [(r.open,r.break_start),(r.break_end,r.close)]):
   # Official GETTEX publishes quarter-hour file slices; five-minute polls
   # cannot acquire a slice which has not closed yet.
   step=max(MINUTES[base],15) if meta['source']=='gettex' else MINUTES[base]
   ends.extend(min(t+pd.Timedelta(minutes=step),b) for t in pd.date_range(a,b,freq=f'{step}min',inclusive='left'))
 eligible=[e for e in ends if e+pd.Timedelta(seconds=delay)<=now]
 last=max(eligible) if eligible else None
 return {'close_at':last.isoformat() if last is not None else None,'eligible_at':(last+pd.Timedelta(seconds=delay)).isoformat() if last is not None else None,'session_open':active,'delay_seconds':delay,'delay_basis':'conservative_polling_policy; provider publication latency UNKNOWN'}

def revision(store,isin,base):
 with store.connect() as db:
  scopes=['instrument:'+isin,'market:'+isin+':'+base]
  return {s:(db.execute('SELECT revision FROM input_revisions WHERE scope=?',(s,)).fetchone() or [0])[0] for s in scopes}

def decision(store,isin,meta,base,now=None):
 now=clock(now);key=unit_key(isin,meta,base);prior=store.setting(key,{})
 cooldown=store.setting('cp_source_retry:'+str(meta.get('source')),None)
 if cooldown and now<pd.Timestamp(cooldown):return {'due':False,'reason':'SOURCE_COOLDOWN','key':key}
 if prior.get('retry_after') and now<pd.Timestamp(prior['retry_after']):return {'due':False,'reason':'RETRY_COOLDOWN','key':key}
 try:b=boundary(meta,base,now)
 except (ValueError,KeyError,TypeError):
  # Bootstrap uses existing identity validation; no guessed calendar/READY.
  due=not prior.get('finished_at') or now>=pd.Timestamp(prior['finished_at'])+pd.Timedelta(hours=1)
  return {'due':due,'reason':'CALENDAR_UNCONFIRMED_BOOTSTRAP','key':key,'close_at':None,'eligible_at':None}
 rev=revision(store,isin,base)
 if not b['close_at']:return {**b,'due':False,'reason':'NO_PUBLISHED_CLOSED_INTERVAL','key':key}
 reason='RETRY_DUE' if prior.get('outcome')=='ERROR' else 'NEW_CLOSED_INTERVAL' if prior.get('close_at')!=b['close_at'] else 'INPUT_REVISION' if prior.get('revisions')!=rev else 'UNCHANGED'
 # The final session bar can arrive after the first post-close request.
 # A successful HTTP receipt does not prove that this candle was published.
 # Reuse the durable scheduler, with four bounded checks, never an overnight
 # loop or a claim that an absent response proves NO_TRADES.
 if not b['session_open'] and base in ('5m','15m') and meta.get('source')=='gettex_chart':
  saved=store.setting('gettex_chart:'+isin+':'+base,{})
  try:
   target=pd.Timestamp(b['close_at']);observed=pd.Timestamp(saved['last']).tz_localize(meta['timezone'])+pd.Timedelta(minutes=MINUTES[base])
   if saved.get('identity')==meta and saved.get('state') in ('SAVED','UNCHANGED','PARTIAL') and observed<target:
    count=prior.get('intraday_publication_rechecks',0) if prior.get('pending_intraday_close')==b['close_at'] else 0
    elapsed=pd.Timestamp(prior.get('finished_at') or b['eligible_at'])
    due_at=max(pd.Timestamp(b['eligible_at']),elapsed+pd.Timedelta(minutes=(0,5,15,30)[min(count,3)]))
    until=target+pd.Timedelta(hours=2);due=count<4 and now<until and now>=due_at
    return {**b,'due':due,'reason':'PENDING_INTRADAY_PUBLICATION' if due else 'INTRADAY_PUBLICATION_WAIT' if count<4 and now<until else 'INTRADAY_PUBLICATION_UNCONFIRMED',
      'key':key,'revisions':rev,'planned_at':now.isoformat(),'pending_intraday_close':b['close_at'],
      'intraday_publication_rechecks':count+1 if due else count,'next_publication_check':due_at.isoformat(),
      'publication_retry_until':until.isoformat(),'publication_delay_status':'UNKNOWN'}
  except (ValueError,TypeError,KeyError):pass
 # A checked HTTP response is not proof that the last daily candle arrived.
 # Recheck a delayed GETTEX daily tail after session close, with durable bounded
 # backoff. Never change DQ or invent a candle for an empty provider response.
 if reason=='UNCHANGED' and base=='1D' and meta.get('source')=='gettex_chart':
  saved=store.setting('gettex_chart:'+isin+':1D',{})
  try:
   target=pd.Timestamp(b['close_at']);observed=pd.Timestamp(saved['last'])
   pending=(saved.get('identity')==meta and saved.get('state') in ('SAVED','UNCHANGED','PARTIAL') and
            observed.date()<target.tz_convert(meta['timezone']).date())
   if pending:
    count=prior.get('publication_rechecks',0) if prior.get('pending_daily_close')==b['close_at'] else 0
    due_at=pd.Timestamp(prior['finished_at'])+pd.Timedelta(minutes=min(60,15*2**min(count,2)))
    due=count<12 and now<target+pd.Timedelta(days=1) and now>=due_at
    return {**b,'due':due,'reason':'PENDING_DAILY_PUBLICATION' if due else 'DAILY_PUBLICATION_WAIT' if count<12 and now<target+pd.Timedelta(days=1) else 'DAILY_PUBLICATION_UNCONFIRMED',
            'key':key,'revisions':rev,'planned_at':now.isoformat(),'pending_daily_close':b['close_at'],
            'publication_rechecks':count+1 if due else count,'next_publication_check':due_at.isoformat(),
            'observed_daily_label':str(observed),'publication_delay_status':'UNKNOWN'}
  except (ValueError,TypeError,KeyError):pass
 recheck=pd.Timedelta(days=1) if base=='1D' else pd.Timedelta(minutes=15)
 if reason=='UNCHANGED' and b['session_open'] and now>=pd.Timestamp(prior['finished_at'])+recheck:reason='PUBLICATION_RECHECK_EXPIRED'
 return {**b,'due':reason!='UNCHANGED','reason':reason,'key':key,'revisions':rev,'planned_at':now.isoformat()}

def finish(store,isin,meta,base,plan,error=None,now=None):
 now=clock(now)
 row={**plan,'isin':isin,'identity':meta,'base':base,'finished_at':now.isoformat(),'outcome':'ERROR' if error else 'CHECKED','revisions':revision(store,isin,base)}
 # An unrelated revision/error retry for the same closed day must not reset
 # the publication retry budget. A newly closed session gets its own budget.
 prior=store.setting(plan['key'],{})
 if prior.get('pending_daily_close') and prior['pending_daily_close']==plan.get('close_at'):
  row['pending_daily_close']=prior['pending_daily_close']
  row['publication_rechecks']=max(prior.get('publication_rechecks',0),row.get('publication_rechecks',0))
 if prior.get('pending_intraday_close') and prior['pending_intraday_close']==plan.get('close_at'):
  row['pending_intraday_close']=prior['pending_intraday_close']
  row['intraday_publication_rechecks']=max(prior.get('intraday_publication_rechecks',0),row.get('intraday_publication_rechecks',0))
 if error:
  row['error']=type(error).__name__;row['retry_after']=(now+pd.Timedelta(minutes=15)).isoformat()
  response=getattr(error,'response',None);status=getattr(error,'code',None) or getattr(response,'status_code',None)
  if status==429:
   from email.utils import parsedate_to_datetime
   headers=getattr(error,'headers',None) or getattr(response,'headers',{}) or {}
   value=headers.get('Retry-After');due=now+pd.Timedelta(hours=1)
   if value:
    try:due=max(due,now+pd.Timedelta(seconds=float(value)))
    except ValueError:
     try:due=max(due,pd.Timestamp(parsedate_to_datetime(value)))
     except (ValueError,TypeError):pass
   row['retry_after']=due.isoformat();store.set_setting('cp_source_retry:'+str(meta['source']),due.isoformat())
 store.set_setting(plan['key'],row)

def gettex_meta(isin):return dict(source='gettex',venue='GETTEX',currency='EUR',symbol=isin,timezone='Europe/Berlin')

def planned_feeds(store,infos,now=None,focus=None):
 from eur_listings import OWNER_HISTORY_POLICY,owner_history_info,venue_allowed
 policy=store.setting(OWNER_HISTORY_POLICY,{})
 infos=[owner_history_info(i,policy) for i in infos]
 now=clock(now);due=[];prevented=0
 for info in infos:
  if info.get('kind')=='benchmark':
   state=store.setting('benchmark_history:'+info['isin'],{})
   seconds=86400 if info.get('history_source')=='cboe' else 3600
   if not state.get('attempted_at') or now>=pd.Timestamp(state['attempted_at'])+pd.Timedelta(seconds=seconds):due.append((state.get('attempted_at',''),info,'EXISTING_BENCHMARK_POLICY'))
   else:prevented+=1
   continue
  isin=info['isin'];maintenance=store.setting('cp_maintenance:'+isin,{})
  if not maintenance.get('at') or now>=pd.Timestamp(maintenance['at'])+pd.Timedelta(minutes=30):
   due.append((maintenance.get('at',''),info,'HISTORY_MAINTENANCE'));continue
  metas=[gettex_meta(isin)]
  saved=store.setting('history_identity:'+isin,{})
  if saved.get('symbol'):metas.append({k:saved.get(k) for k in FIELDS})
  # The separate GETTEX chart provider shares this queue. Its 5m cadence must
  # not wait for the post-trade file feed's quarter-hour publication slices.
  chart=store.setting('gettex_chart:'+isin+':15m',{})
  if chart.get('identity'):metas.append(chart['identity'])
  ready=any(decision(store,isin,m,b,now)['due'] for m in metas if venue_allowed(info,m) for b in (('5m',) if m.get('source')=='gettex' else ('5m','15m','1D') if m.get('source')=='gettex_chart' else ('5m','15m','1H','1D')))
  if ready:due.append((maintenance.get('at',''),info,'FRESH_CANDLES'))
  else:prevented+=1
 # Oldest history turn first; each existing feed performs bounded history work
 # after live acquisition, so repeated short-TF updates cannot omit it.
 from calculation_window import eligibility_due
 from cp_controls import due_request,historical_review_due
 from cp_reconciliation import due_observation
 with store.connect() as db:
  # Automatic dependency repairs use ordinary per-stock history turns. They
  # must not fill the explicit owner-priority lane and starve the whole feed.
  urgent=due_request(db,now=now,include_automatic=False)
  observation=None if urgent else due_observation(db,infos,now)
 priority_isin=urgent['problem']['isin'] if urgent else observation['isin'] if observation else None
 priority=next((i for i in infos if i['isin']==priority_isin),None)
 fresh,freshness=fresh_candidate(store,infos,now,focus)
 return {'fresh':fresh,'freshness':freshness,'planned_at':now.isoformat(),'priority':priority,'observation_recheck':observation,'feeds':[(i,r) for _,i,r in sorted(due,key=lambda x:x[0])],'review':historical_review_due(store,infos),'eligibility':eligibility_due(store,infos,now),'prevented_feed_jobs':prevented}

FRESH_ROTATION='cp_fresh_rotation'

def trade_maintenance(store,infos,now):
 """Maintain confirmed automatic and owner-reviewed demo contexts, not permits.

 Expired quotes/changed market rows need fresh acquisition and reassessment.
 Keep that maintenance priority until the ordinary eligibility writer replaces
 the receipt. Changed instrument/owner policy, or a new BLOCKED receipt, removes
 it immediately. No indicator calculation or archive scan in the planner.
 """
 from trading_engine import VERSION,profile
 from cp_controls import DEMO_INITIALIZATION,DEMO_ALL_INITIALIZATION
 result={}
 with store.connect() as db:
  from paper_preparation import maintenance
  preparing=maintenance(db)
  for info in infos:
   if info.get('kind')=='benchmark':continue
   isin=info['isin']
   # The explicit hourly trial also needs fresh inputs while CORE is waiting.
   # It receives the existing bounded maintenance slot, never a trade permit.
   from paper_hourly import authority
   from eur_listings import stored_identity
   try:
    cfg=authority(db,isin);exact=cfg['identities'][isin]
    if stored_identity(db,info,exact,'15m'):
     result.setdefault(isin,[]).append(dict(identity=exact,timeframe='1H',required_tf=['15m','1H','4H','1D'],
       execution_tf=['5m','15m'],strategy='CORE:'+VERSION,proof_state='OWNER_HOURLY_MAINTENANCE',execution_permission=False))
   except ValueError:pass
   if preparing and preparing[0]==isin:
    result.setdefault(isin,[]).append(dict(identity=preparing[1],timeframe='1H',
      required_tf=['15m','1H','4H','1D'],execution_tf=['5m','15m'],
      strategy='CORE:'+VERSION,proof_state='SEQUENTIAL_PREPARATION',execution_permission=False))
   # Project only positive receipts; avoid decoding each full 11-TF payload.
   rows=db.execute("""SELECT json_extract(s.value,'$.isin','$.execution_version','$.context',
             '$.error','$.expires_at','$._input_revisions'),c.value
       FROM settings s,json_each(s.value,'$.candidates') c
       WHERE s.key=? AND (json_extract(c.value,'$.trade_ready')=1 OR
        (json_extract(c.value,'$.owner_reviewed_analysis')=1 AND
         json_extract(c.value,'$.analysis_ready')=1 AND
         json_extract(c.value,'$.timeframe')='15m'))""",('cp_eligibility:'+isin,)).fetchall()
   for header,payload in rows:
    owner,version,context,error,expires,token_fallback=json.loads(header)
    if error or owner!=isin or version!=1 or context!='bounded_200_exact_reference_v1':continue
    c=json.loads(payload)
    tf=c.get('timeframe');meta=c.get('identity') or {}
    required=list(dict.fromkeys((tf,*profile(tf))))
    candidate_required=list(dict.fromkeys(c.get('required_tf') or []))
    reviewed=(c.get('owner_reviewed_analysis') is True and
              c.get('context') in (DEMO_INITIALIZATION,DEMO_ALL_INITIALIZATION))
    if reviewed:
     # A valid manual demo calculation can be WAIT. Keeping its real inputs
     # fresh is useful even without an automatic BUY/SELL execution permit.
     proof=c.get('closed_inputs') or {};token=proof.get('revisions') or {}
     own=[v for v in proof.get('series',[]) if v.get('spec',{}).get('isin')==isin]
     if (tf!='15m' or c.get('analysis_ready') is not True or c.get('analytical_only')
         or c.get('owner_initialization_semantics')!=3 or c.get('input_binding_version')!=2
         or c.get('result',{}).get('status')!='READY'
         or c.get('result',{}).get('signal') not in ('WAIT','BUY','SELL')
         or c.get('strategy')!='CORE:'+VERSION or candidate_required!=required
         or meta.get('source') not in ('gettex','gettex_chart') or meta.get('venue')!='GETTEX'
         or meta.get('currency')!='EUR' or not all(meta.get(k) for k in FIELDS)
         or not set(c['required_tf'])<={v['spec'].get('timeframe') for v in own if v.get('count',0)>0}
         or any(v['spec'].get('identity')!=meta for v in own)
         or 'instrument:'+isin not in proof.get('strict',{})
         or 'setting:cp_manual_risk:'+isin not in token):continue
     changed=[k for k,v in token.items() if (db.execute('SELECT revision FROM input_revisions WHERE scope=?',(k,)).fetchone() or [0])[0]!=v]
     policy_scopes=set(proof['strict'])|{'instrument:'+isin,'setting:cp_manual_risk:'+isin,
       'setting:cp_reviewed_gap_policy:'+isin,'setting:cp_demo_initialization_policy',
       'setting:cp_auto_risk_policy','setting:cp_tv_risk_policy'}
     if 'instrument:'+isin not in token or policy_scopes.intersection(changed):continue
     try:
      expiry=pd.Timestamp(c.get('expires_at'))
      if pd.isna(expiry) or expiry.tzinfo is None:continue
     except (TypeError,ValueError):continue
     result.setdefault(isin,[]).append(dict(identity=meta,timeframe=tf,required_tf=required,
       strategy=c['strategy'],proof_state='RECHECK_REQUIRED' if changed or now>=expiry else 'OWNER_REVIEWED_DEMO_CONTEXT',
       execution_permission=False))
     continue
    if (tf not in ('5m','15m') or not all(c.get(k) for k in ('analysis_ready','signal_ready','trade_ready'))
        or c.get('analytical_only') or c.get('result',{}).get('status')!='READY'
        or c.get('result',{}).get('signal') not in ('BUY','SELL')
        or c.get('strategy')!='CORE:'+VERSION or candidate_required!=required
        or c.get('execution_identity')!=meta or meta.get('currency')!='EUR'
        or not all(meta.get(k) for k in FIELDS)):continue
    token=c.get('execution_revisions') or token_fallback or {}
    if 'instrument:'+isin not in token:continue
    changed=[k for k,v in token.items() if (db.execute('SELECT revision FROM input_revisions WHERE scope=?',(k,)).fetchone() or [0])[0]!=v]
    if any(not k.startswith('market:') for k in changed):continue
    try:
     expiry=pd.Timestamp(c.get('execution_expires_at',expires))
     if pd.isna(expiry) or expiry.tzinfo is None:continue
    except (TypeError,ValueError):continue
    result.setdefault(isin,[]).append(dict(identity=meta,timeframe=tf,required_tf=required,
      strategy=c['strategy'],proof_state='RECHECK_REQUIRED' if changed or now>=expiry else 'SAVED_EXECUTION_PROOF',
      execution_permission=False))
 return result

def fresh_window(plan,base,now):
 """Include the last publication just after close, not an overnight poll loop."""
 if plan.get('session_open') or base=='1D':return True
 if plan.get('reason')=='PENDING_INTRADAY_PUBLICATION':
  return pd.Timestamp(plan['next_publication_check'])<=now<pd.Timestamp(plan['publication_retry_until'])
 try:
  eligible=pd.Timestamp(plan['eligible_at'])
  return pd.notna(eligible) and eligible<=now<eligible+pd.Timedelta(minutes=15)
 except (KeyError,TypeError,ValueError):return False


def operational_chart_due(db,now):
 """Bounded configured/held instruments only; no all-stock audit or IO."""
 from contextlib import contextmanager
 from paper_hourly import read,authority,CONFIG
 from paper_account import ledger
 from gettex_chart import confirmed_state
 class ExistingSnapshot:
  @contextmanager
  def connect(self):yield db
  def setting(self,k,d=None):return read(db,k,d)
 store=ExistingSnapshot();identities={}
 for isin in read(db,CONFIG,{}).get('identities',{}):
  try:identities[isin]=authority(db,isin)['identities'][isin]
  except ValueError:pass
 for p in ledger(db).get('positions',[]):
  if float(p['quantity'])>0:identities[p['isin']]=p['identity']
 due=[]
 for isin,meta in identities.items():
  if meta.get('source')!='gettex_chart' or meta.get('venue')!='GETTEX' or meta.get('currency')!='EUR':continue
  row=db.execute('SELECT payload FROM instruments WHERE isin=?',(isin,)).fetchone()
  if not row:continue
  info=json.loads(row[0])
  for base in ('5m','15m','1D'):
   saved=store.setting('gettex_chart:'+isin+':'+base,{})
   if not confirmed_state(info,saved) or saved['identity']!=meta:continue
   if saved.get('retry_after') and now<pd.Timestamp(saved['retry_after']):continue
   request=decision(store,isin,meta,base,now)
   # The daily trend filter is an operational dependency too. Only reserve
   # a missing already-closed session, not archive work or a forming day.
   if base=='1D':
    try:
     observed=pd.Timestamp(saved['last']);target=pd.Timestamp(request['close_at'])
     if pd.isna(observed) or pd.isna(target) or observed.date()>=target.tz_convert(meta['timezone']).date():continue
    except (KeyError,ValueError,TypeError):continue
   if request['due'] and fresh_window(request,base,now):due.append((saved.get('received_at',''),isin,base))
 return sorted(due)

def fresh_candidate(store,infos,now,focus=None):
 """One short job on the existing serial feed lane, for the whole watchlist."""
 from gettex_chart import confirmed_state
 rotation=store.setting(FRESH_ROTATION,{})
 from paper_account import ledger
 with store.connect() as db:
  positions=[p for p in ledger(db).get('positions',[]) if float(p['quantity'])>0]
 trades=trade_maintenance(store,infos,now)
 live_slots=4 if trades else 3
 stats={'scope':'all registered stocks','stocks':sum(i.get('kind')!='benchmark' for i in infos),'due':0,
        'history_turn_due':rotation.get('consecutive',0)>=live_slots,'chart_rate_limited':False,
        'priority_policy':'positions_trade_context_focus_background_v1','trade_maintenance_stocks':len(trades),
        'live_slots_before_history':live_slots,'execution_permission':False}
 if stats['history_turn_due']:return None,stats
 reserved=store.setting('gettex_chart_next_request')
 chart_allowed=not reserved or now>=pd.Timestamp(reserved)
 stats['chart_rate_limited']=not chart_allowed
 candidates=[]
 for info in infos:
  if info.get('kind')=='benchmark':continue
  isin=info['isin'];choices=[]
  preferred='1m' if (focus or {}).get('isin')==isin else None
  for base in dict.fromkeys((preferred or '15m','5m','15m','1D','1m')):
   saved=store.setting('gettex_chart:'+isin+':'+base,{})
   if not confirmed_state(info,saved):
    if saved.get('retry_after') and now<pd.Timestamp(saved['retry_after']):continue
    if base!='1m' or preferred!='1m':continue
    # Bootstrap the visible stock's minute lane using an existing listing
    # proof; fetch validates the actual response again before any write.
    saved=store.setting('gettex_chart:'+isin+':15m',{})
    if not confirmed_state(info,saved):continue
    saved={**saved,'received_at':''}
   plan=decision(store,isin,saved['identity'],base,now)
   retry=saved.get('retry_after')
   # A missing already-closed daily tail is fresh data too. Previously it
   # waited for a complete archive-maintenance turn while intraday kept moving.
   # Only promote a proven missing tail, never an unclosed day or bootstrap.
   if base=='1D':
    try:
     observed=pd.Timestamp(saved['last']);target=pd.Timestamp(plan['close_at'])
     if pd.isna(observed) or pd.isna(target) or observed.date()>=target.tz_convert(saved['identity']['timezone']).date():continue
    except (KeyError,ValueError,TypeError):continue
   if plan['due'] and fresh_window(plan,base,now) and (not retry or now>=pd.Timestamp(retry)):
    stats['due']+=1
    if chart_allowed:choices.append((saved.get('received_at',''),'gettex_chart',base))
  # Official GETTEX files are shared on disk by all instruments. The existing
  # downloader verifies EUR trades and downloads only files not already cached.
  meta=gettex_meta(isin);plan=decision(store,isin,meta,'5m',now)
  if plan['due'] and fresh_window(plan,'5m',now):
   prior=store.setting(plan['key'],{});choices.append((prior.get('finished_at',''),'gettex','5m'));stats['due']+=1
  if not choices:continue
  last,source,base=min(choices)
  trade_choice=None;trade_proof=None
  for choice in sorted(choices):
   meta=(store.setting('gettex_chart:'+isin+':'+choice[2],{}).get('identity') if choice[1]=='gettex_chart' else gettex_meta(isin)) or {}
   for proof in trades.get(isin,[]):
    # Use the strategy's real base lanes, not optional 1m/5m chart detail.
    bases={'1D' if tf in ('1D','1W','1M') else '5m' if tf in ('5m','10m') or choice[1]=='gettex' else '15m' for tf in proof['required_tf']}
    # Execution may use either native closed quote lane. Keeping 5m fresh is
    # not a 5m analysis/warmup dependency of the hourly strategy.
    bases.update(proof.get('execution_tf',[]))
    if choice[2] in bases and all(meta.get(k)==proof['identity'].get(k) for k in FIELDS):
     trade_choice=choice;trade_proof=proof;break
   if trade_choice:break
  position=next((p for p in positions if p['isin']==isin),None)
  position_choice=None
  if position:
   for choice in sorted(choices,key=lambda c:(c[2]!=position.get('timeframe','15m'),c[0])):
    m=(store.setting('gettex_chart:'+isin+':'+choice[2],{}).get('identity') if choice[1]=='gettex_chart' else gettex_meta(isin)) or {}
    if all(m.get(k)==position['identity'].get(k) for k in FIELDS):position_choice=choice;break
  # Prefer the displayed provider only for bounded foreground turns.
  is_focus=isin==(focus or {}).get('isin')
  if is_focus and rotation.get('foreground_streak',0)<2:
   # The minute lane forms the live tail, but cannot replace closed strategy
   # inputs. Interleaved position jobs reset foreground_streak, so always
   # choosing 1m here could leave the displayed 15m archive at yesterday.
   tf=(focus or {}).get('timeframe')
   display_base='1D' if tf in ('1D','1W','1M') else '5m' if tf in ('5m','10m') else '15m' if tf in ('15m','30m','1H','2H','3H','4H') else preferred
   displayed=next((x for x in choices if x[1]=='gettex_chart' and x[2]==display_base),None)
   if displayed is None:displayed=next((x for x in choices if x[1]=='gettex_chart' and x[2]==preferred),None)
   if displayed:last,source,base=displayed
  if trade_choice:last,source,base=trade_choice
  if position_choice and rotation.get('position_streak',0)<2:last,source,base=position_choice
  candidates.append((last,isin,info,source,base,is_focus,bool(position_choice),trade_proof))
 if not candidates:return None,stats
 oldest=lambda items:min(items,key=lambda x:(x[0],x[1]),default=None)
 chosen=oldest(candidates)
 # Both independent streaks reset when the other priority runs. Alternating
 # positions and focus therefore used to starve every other live instrument.
 # Add one maintenance slot only when a saved trade context exists. Retain a
 # separate oldest-background slot and a history turn after at most four jobs.
 slot=rotation.get('consecutive',0)
 position=oldest(x for x in candidates if x[6])
 foreground=oldest(x for x in candidates if x[5])
 trading=oldest(x for x in candidates if x[7])
 if slot==0:chosen=position or trading or foreground or chosen
 elif trades and slot==1:chosen=trading or foreground or position or chosen
 elif slot==(2 if trades else 1):chosen=foreground or trading or position or chosen
 else:chosen=oldest(x for x in candidates if not x[5] and not x[6] and not x[7]) or chosen
 _,isin,info,source,base,is_focus,is_position,trade_proof=chosen
 details=({'priority_reason':'TRADE_CONTEXT_MAINTENANCE','required_tf':trade_proof['required_tf'],
           'strategy':trade_proof['strategy'],'proof_state':trade_proof['proof_state'],'execution_permission':False} if trade_proof else {})
 return {**info,'_gettex_poll_only':True,'_cp_foreground':is_focus,
         '_cp_position':is_position,
         '_cp_focus':{'isin':isin,'source':source,'timeframe':base,**details}},stats

def fresh_finished(store,info,source,outcome,now=None):
 # A failed reservation is not successful acquisition or a new data watermark.
 if outcome in ('RATE_WAIT','NOT_DUE','INSTRUMENT_REMOVED'):return
 with store.connect() as db:
  db.execute('BEGIN IMMEDIATE');row=db.execute('SELECT value FROM settings WHERE key=?',(FRESH_ROTATION,)).fetchone()
  prior=json.loads(row[0]) if row else {}
  foreground=bool(info.get('_cp_foreground'))
  value={'consecutive':prior.get('consecutive',0)+1,
         'foreground_streak':prior.get('foreground_streak',0)+1 if foreground and prior.get('isin')==info['isin'] else 1 if foreground else 0,
         'position_streak':prior.get('position_streak',0)+1 if info.get('_cp_position') else 0,
         'isin':info['isin'],'source':source,'outcome':outcome,'finished_at':clock(now).isoformat(),
         'priority_reason':info.get('_cp_focus',{}).get('priority_reason'),
         'execution_permission':False}
  db.execute('INSERT OR REPLACE INTO settings VALUES (?,?)',(FRESH_ROTATION,json.dumps(value)))

def history_finished(store,now=None):
 prior=store.setting(FRESH_ROTATION,{})
 store.set_setting(FRESH_ROTATION,{**prior,'consecutive':0,'history_finished_at':clock(now).isoformat()})
